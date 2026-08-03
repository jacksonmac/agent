"""Fidelity checking for office documents (.xlsx/.xlsm/.docx/.pptx).

Editing a file the user seeded into the workspace is only safe if we can
prove the edit didn't destroy anything else. Measured against openpyxl
3.1.5 and python-docx 1.2.0:

* python-docx keeps the underlying XML tree and repackages parts it has no
  model for, so docx round-trips losslessly — content controls, tracked
  changes, fields, headers and images all survive an edit.
* openpyxl preserves far more than its reputation suggests (charts, images,
  conditional formatting, data validation, defined names, comments, merges)
  but silently drops whatever it has no model for. Crucially, some of those
  losses are INSIDE a part that still exists: an <extLst> sparkline group
  disappears from sheet1.xml while the part count stays identical. A
  package-level diff alone does not catch it.

Hence three signals, not one: which parts are present, which known
unmodelled constructs appear anywhere in the XML, and how many formulas
the workbook has (`data_only=True` swaps every formula for a cached value,
which no part or construct check would notice).

The module deliberately has no third-party dependencies — it reads the OPC
zip directly, so it works whether or not openpyxl/python-docx are
installed, and it can judge a file written by any of them.
"""

from __future__ import annotations

import dataclasses
import re
import zipfile

OFFICE_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm",
                   ".docx", ".docm", ".dotx",
                   ".pptx", ".pptm", ".potx")

# Constructs no python office library round-trips reliably, as
# label → substring searched across the package's XML. The matching is
# deliberately crude: a false positive costs one refused edit, a false
# negative ships a damaged file, and only one of those is recoverable.
MARKERS = {
    "sparklines": "sparklineGroup",
    "slicers": "slicer",
    "pivot tables": "pivotTableDefinition",
    "pivot caches": "pivotCache",
    "threaded comments": "ThreadedComment",
    "x14 conditional formatting": "x14:conditionalFormatting",
    "x14 data validation": "x14:dataValidation",
    "form controls": "<control ",
    "macros (vbaProject)": "vbaProject",
    "custom XML": "customXml",
    "tracked changes": "<w:ins ",
    "content controls": "<w:sdt>",
    "footnotes": "footnoteReference",
    "fields (TOC etc.)": "fldChar",
}

# <f> and "<f " only: <formula1> from a data validation must not count
_FORMULA_RE = re.compile(r"<f[ >]")

_TEXTUAL_SUFFIXES = (".xml", ".rels", ".vml")


@dataclasses.dataclass(frozen=True)
class Fingerprint:
    """What a package contained, at the three granularities that matter."""
    parts: frozenset
    constructs: frozenset
    formulas: int

    def as_dict(self) -> dict:
        """Log-friendly form — goes into the runlog so a refused edit is
        answerable afterwards by someone who wasn't watching."""
        return {"parts": len(self.parts),
                "constructs": sorted(self.constructs),
                "formulas": self.formulas}


def is_office_package(path: str) -> bool:
    """True for a file we can fingerprint: an OPC zip with the right
    suffix. Cheap enough to call before every write."""
    if not path.lower().endswith(OFFICE_SUFFIXES):
        return False
    return zipfile.is_zipfile(path)


def fingerprint(path: str) -> Fingerprint:
    """Inventory one office package. Raises zipfile.BadZipFile if the file
    isn't an OPC container — callers should check is_office_package first
    when the input is untrusted."""
    constructs: set[str] = set()
    formulas = 0
    with zipfile.ZipFile(path) as z:
        parts = frozenset(z.namelist())
        for name in parts:
            if not name.endswith(_TEXTUAL_SUFFIXES):
                continue
            # replace, not strict: a mis-declared encoding in one part must
            # not make the whole file unjudgeable
            xml = z.read(name).decode("utf-8", "replace")
            for label, needle in MARKERS.items():
                if needle in xml:
                    constructs.add(label)
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                formulas += len(_FORMULA_RE.findall(xml))
    # some constructs are whole binary parts (vbaProject.bin, customXml/),
    # which the text scan above skips
    for label, needle in MARKERS.items():
        if any(needle in p for p in parts):
            constructs.add(label)
    return Fingerprint(parts, frozenset(constructs), formulas)


def compare(before: Fingerprint, after: Fingerprint,
            allow: tuple = ()) -> list[str]:
    """Everything `after` lost relative to `before`, as readable lines.
    Empty means the edit took nothing with it.

    `allow` names losses the caller asked for — deleting the sheet that
    held the sparklines is a legitimate edit, and the tool that performed
    it is the only thing that knows so.
    """
    problems = []
    for part in sorted(before.parts - after.parts):
        if part not in allow:
            problems.append(f"dropped part: {part}")
    for construct in sorted(before.constructs - after.constructs):
        if construct not in allow:
            problems.append(f"lost {construct}")
    if after.formulas < before.formulas and "formulas" not in allow:
        problems.append(
            f"formulas: {before.formulas} → {after.formulas} "
            f"(a data_only load replaces formulas with cached values)")
    return problems


def check_edit(original: str, edited: str, allow: tuple = ()) -> list[str]:
    """Convenience for the edit path: fingerprint both files and report
    what the edit destroyed. The caller refuses to publish `edited` when
    this returns anything."""
    return compare(fingerprint(original), fingerprint(edited), allow)


def describe(problems: list[str]) -> str:
    """The refusal message a tool hands back to the model. Says what to do
    next, because the model's default reaction to an error is to retry the
    identical call."""
    if not problems:
        return ""
    body = "\n".join(f"  - {p}" for p in problems)
    return ("[ERROR] the edit was discarded: saving it would have destroyed "
            "parts of the original file that you were not asked to change:\n"
            f"{body}\n"
            "The original file is untouched. Do not retry the same edit — "
            "either make the change without re-saving the whole workbook, or "
            "tell the user which feature blocks it.")


# ── text extraction for the reviewer ─────────────────────────────────
#
# A reviewer handed a .xlsx sees a zip full of XML, judges nothing, and the
# verdict quietly degrades to "the file exists" — the exact failure the
# review loop exists to catch. Extraction is stdlib-only for the same reason
# the fingerprint is: the reviewer must work whether or not openpyxl happens
# to be installed.

_NS_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _xlsx_text(z: zipfile.ZipFile, max_rows: int) -> str:
    import xml.etree.ElementTree as ET
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter(_NS_SHEET + "t")))

    names = {}   # sheet part -> display name, so the reviewer sees "Data" not "sheet1"
    if "xl/workbook.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/workbook.xml"))
        for i, sh in enumerate(root.iter(_NS_SHEET + "sheet"), start=1):
            names[f"xl/worksheets/sheet{i}.xml"] = sh.get("name", f"sheet{i}")

    out = []
    parts = sorted(n for n in z.namelist()
                   if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    for part in parts:
        root = ET.fromstring(z.read(part))
        out.append(f"[{names.get(part, part)}]")
        rows = 0
        for row in root.iter(_NS_SHEET + "row"):
            cells = []
            for c in row.iter(_NS_SHEET + "c"):
                formula = c.find(_NS_SHEET + "f")
                v = c.find(_NS_SHEET + "v")
                kind = c.get("t")
                text = ""
                if kind == "inlineStr":
                    # openpyxl writes inline strings; Excel writes shared ones.
                    # An extractor that handles only one silently loses every
                    # label in files produced by the other.
                    inline = c.find(_NS_SHEET + "is")
                    if inline is not None:
                        text = "".join(t.text or ""
                                       for t in inline.iter(_NS_SHEET + "t"))
                elif kind == "s" and v is not None and v.text:
                    idx = int(v.text)
                    text = shared[idx] if idx < len(shared) else ""
                elif v is not None:
                    text = v.text or ""
                if formula is not None:
                    # the formula is what the reviewer must judge; the cached
                    # value alone hides whether the sheet computes anything
                    text = f"={formula.text or ''}" + (f" -> {text}" if text else "")
                if text:
                    cells.append(f"{c.get('r', '')}={text}")
            if cells:
                out.append("  " + "  ".join(cells))
                rows += 1
            if rows >= max_rows:
                out.append(f"  … more rows omitted")
                break
    return "\n".join(out)


def _para_text(z: zipfile.ZipFile, part: str, para_tag: str, run_tag: str) -> str:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(z.read(part))
    lines = []
    for para in root.iter(para_tag):
        text = "".join(t.text or "" for t in para.iter(run_tag))
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def extract_text(path: str, max_rows: int = 200) -> str:
    """Readable text from an office package, or "" if this is not one.

    Never raises: a deliverable the reviewer cannot parse should degrade to
    a note saying so, not abort the run.
    """
    lower = path.lower()
    if not lower.endswith(OFFICE_SUFFIXES):
        return ""          # not ours; a text file is the caller's to read
    try:
        with zipfile.ZipFile(path) as z:
            if lower.endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
                return _xlsx_text(z, max_rows)
            if lower.endswith((".docx", ".docm", ".dotx")):
                return _para_text(z, "word/document.xml",
                                  _NS_WORD + "p", _NS_WORD + "t")
            if lower.endswith((".pptx", ".pptm", ".potx")):
                slides = sorted(n for n in z.namelist()
                                if n.startswith("ppt/slides/slide")
                                and n.endswith(".xml"))
                out = []
                for i, part in enumerate(slides, start=1):
                    out.append(f"[slide {i}]")
                    out.append(_para_text(z, part, _NS_DRAW + "p", _NS_DRAW + "t"))
                return "\n".join(out)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
        return f"[could not extract text: {type(e).__name__}: {e}]"
    return ""
