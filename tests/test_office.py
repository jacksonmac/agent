"""Office-document fidelity guard.

Two tiers. The first builds OPC packages by hand with zipfile, so the guard's
logic is tested wherever pytest runs. The second drives real openpyxl /
python-docx round-trips and skips when those aren't installed — they're
optional dependencies, and the measurements they pin down (sparklines lost
from inside a surviving part, data_only destroying formulas, docx surviving
intact) are the reason the module exists.
"""

import zipfile

import pytest

from harness import office

# ─── hand-built packages: no third-party dependency ─────────────────

CT = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
      '/package/2006/content-types"><Default Extension="xml" '
      'ContentType="application/xml"/></Types>')

SHEET = ('<worksheet><sheetData><row r="1">'
         '<c r="A1"><v>1</v></c>'
         '<c r="B1"><f>A1*2</f><v>2</v></c>'
         '<c r="C1"><f>SUM(A1:B1)</f><v>3</v></c>'
         '</sheetData>{extra}</worksheet>')

SPARKLINE = ('<extLst><ext uri="{05C60535-1F16-4fd2-B633-F4F36F0B64E0}">'
             '<x14:sparklineGroups><x14:sparklineGroup type="column"/>'
             '</x14:sparklineGroups></ext></extLst>')


def _pkg(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        for name, data in parts.items():
            z.writestr(name, data)
    return str(path)


def _book(path, extra="", extra_parts=None):
    parts = {"xl/worksheets/sheet1.xml": SHEET.format(extra=extra)}
    parts.update(extra_parts or {})
    return _pkg(path, parts)


def test_fingerprint_counts_formulas_not_validation_elements(tmp_path):
    sheet = ('<worksheet><sheetData><c r="A1"><f>A1*2</f></c></sheetData>'
             '<dataValidation><formula1>"a,b"</formula1></dataValidation>'
             '</worksheet>')
    path = _pkg(tmp_path / "b.xlsx", {"xl/worksheets/sheet1.xml": sheet})
    assert office.fingerprint(path).formulas == 1  # <formula1> must not count


def test_fingerprint_finds_constructs_and_parts(tmp_path):
    path = _book(tmp_path / "b.xlsm", extra=SPARKLINE,
                 extra_parts={"xl/vbaProject.bin": b"stub",
                              "customXml/item1.xml": "<ds/>"})
    fp = office.fingerprint(path)
    assert "sparklines" in fp.constructs
    assert "macros (vbaProject)" in fp.constructs   # binary part, not XML
    assert "custom XML" in fp.constructs
    assert fp.formulas == 2
    assert "xl/worksheets/sheet1.xml" in fp.parts


def test_identical_files_compare_clean(tmp_path):
    a = _book(tmp_path / "a.xlsx", extra=SPARKLINE)
    b = _book(tmp_path / "b.xlsx", extra=SPARKLINE)
    assert office.check_edit(a, b) == []


def test_dropped_part_is_reported(tmp_path):
    a = _book(tmp_path / "a.xlsm", extra_parts={"xl/vbaProject.bin": b"stub"})
    b = _book(tmp_path / "b.xlsm")
    problems = office.check_edit(a, b)
    assert any("dropped part: xl/vbaProject.bin" in p for p in problems)
    assert any("lost macros (vbaProject)" in p for p in problems)


def test_loss_inside_a_surviving_part_is_reported(tmp_path):
    """The case a package-level diff misses: same parts, same count, but the
    sparkline group is gone from inside sheet1.xml."""
    a = _book(tmp_path / "a.xlsx", extra=SPARKLINE)
    b = _book(tmp_path / "b.xlsx")
    assert office.fingerprint(a).parts == office.fingerprint(b).parts
    assert office.check_edit(a, b) == ["lost sparklines"]


def test_formula_loss_is_reported(tmp_path):
    a = _book(tmp_path / "a.xlsx")
    b = _pkg(tmp_path / "b.xlsx", {
        "xl/worksheets/sheet1.xml":
            '<worksheet><sheetData><c r="B1"><v>2</v></c></sheetData></worksheet>'})
    problems = office.check_edit(a, b)
    assert len(problems) == 1 and "formulas: 2 → 0" in problems[0]


def test_added_content_is_not_a_loss(tmp_path):
    """An edit that adds a sheet or a formula must pass — the guard exists to
    catch destruction, not change."""
    a = _book(tmp_path / "a.xlsx")
    b = _book(tmp_path / "b.xlsx",
              extra_parts={"xl/worksheets/sheet2.xml":
                           '<worksheet><sheetData><c r="A1"><f>1+1</f></c>'
                           '</sheetData></worksheet>'})
    assert office.check_edit(a, b) == []


def test_allow_list_permits_a_requested_deletion(tmp_path):
    a = _book(tmp_path / "a.xlsx", extra=SPARKLINE)
    b = _book(tmp_path / "b.xlsx")
    assert office.check_edit(a, b, allow=("sparklines",)) == []


def test_is_office_package_rejects_non_packages(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("hello")
    assert not office.is_office_package(str(text))
    fake = tmp_path / "fake.xlsx"          # right suffix, not a zip
    fake.write_text("hello")
    assert not office.is_office_package(str(fake))
    assert office.is_office_package(_book(tmp_path / "real.xlsx"))


def test_describe_tells_the_model_not_to_retry(tmp_path):
    msg = office.describe(["lost sparklines"])
    assert msg.startswith("[ERROR]")
    assert "lost sparklines" in msg
    assert "original file is untouched" in msg
    assert "Do not retry" in msg
    assert office.describe([]) == ""


def test_fingerprint_as_dict_is_loggable(tmp_path):
    fp = office.fingerprint(_book(tmp_path / "a.xlsm", extra=SPARKLINE))
    d = fp.as_dict()
    assert d["formulas"] == 2 and "sparklines" in d["constructs"]
    assert isinstance(d["parts"], int)      # a count, not the whole listing


# ─── real library round-trips ───────────────────────────────────────

def _write_xlsx(path, *, with_formulas=True):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["region", "q1", "q2", "total"])
    for i, row in enumerate([("north", 120, 140), ("south", 95, 88)], start=2):
        ws.append(row)
        if with_formulas:
            ws[f"D{i}"] = f"=B{i}+C{i}"
    chart = openpyxl.chart.BarChart()
    chart.add_data(openpyxl.chart.Reference(ws, min_col=2, min_row=1,
                                            max_col=3, max_row=3),
                   titles_from_data=True)
    ws.add_chart(chart, "F2")
    wb.save(path)
    return str(path)


def test_openpyxl_ordinary_edit_passes_the_guard(tmp_path):
    """The guard must not be so strict that it blocks the feature."""
    openpyxl = pytest.importorskip("openpyxl")
    src = _write_xlsx(tmp_path / "src.xlsx")
    dst = str(tmp_path / "edited.xlsx")
    wb = openpyxl.load_workbook(src)        # correct flags: no data_only
    wb["Data"]["B2"] = 999
    wb.save(dst)
    assert office.check_edit(src, dst) == []
    assert openpyxl.load_workbook(dst)["Data"]["B2"].value == 999


def test_openpyxl_data_only_is_caught(tmp_path):
    """The footgun: data_only is the natural flag for reading computed
    values, and it destroys every formula if the workbook is then saved."""
    openpyxl = pytest.importorskip("openpyxl")
    src = _write_xlsx(tmp_path / "src.xlsx")
    dst = str(tmp_path / "flat.xlsx")
    wb = openpyxl.load_workbook(src, data_only=True)
    wb.save(dst)
    problems = office.check_edit(src, dst)
    assert problems and "formulas" in problems[0]


def test_openpyxl_drops_vba_without_keep_vba(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    src = _write_xlsx(tmp_path / "src.xlsm")
    # inject the macro project the way Excel would
    items = {}
    with zipfile.ZipFile(src) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["xl/vbaProject.bin"] = b"stub"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in items.items():
            z.writestr(n, d)

    loose = str(tmp_path / "loose.xlsm")
    openpyxl.load_workbook(src).save(loose)
    assert any("vbaProject" in p for p in office.check_edit(src, loose))

    kept = str(tmp_path / "kept.xlsm")
    openpyxl.load_workbook(src, keep_vba=True).save(kept)
    assert not any("vbaProject" in p for p in office.check_edit(src, kept))


def test_python_docx_round_trips_losslessly(tmp_path):
    """docx needs no guard — python-docx repackages parts it has no model
    for. If this ever fails, the docx tools need the same treatment as xlsx."""
    docx = pytest.importorskip("docx")
    lxml_etree = pytest.importorskip("lxml.etree")
    src = str(tmp_path / "src.docx")
    d = docx.Document()
    d.add_heading("Report", level=1)
    d.add_paragraph("Body text.")
    d.add_table(rows=1, cols=2)
    d.sections[0].header.paragraphs[0].text = "internal"
    d.save(src)

    # constructs python-docx has no API for, injected as Word would write them
    d = docx.Document(src)
    body = d.element.body
    for xml in (
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:ins w:id="9" w:author="J" w:date="2026-08-01T10:00:00Z">'
        '<w:r><w:t>tracked</w:t></w:r></w:ins></w:p>',
        '<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:sdtPr><w:alias w:val="Client"/></w:sdtPr>'
        '<w:sdtContent><w:p><w:r><w:t>ACME</w:t></w:r></w:p></w:sdtContent>'
        '</w:sdt>',
    ):
        body.insert(len(body) - 1, lxml_etree.fromstring(xml))
    d.save(src)
    fp = office.fingerprint(src)
    assert {"tracked changes", "content controls"} <= fp.constructs

    edited = str(tmp_path / "edited.docx")
    d = docx.Document(src)
    for p in d.paragraphs:
        if p.text == "Body text.":
            p.runs[0].text = "Rewritten by the agent."
    d.add_paragraph("Appended.")
    d.save(edited)

    assert office.check_edit(src, edited) == []
    assert any(p.text == "Rewritten by the agent."
               for p in docx.Document(edited).paragraphs)
