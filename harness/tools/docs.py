"""Spreadsheet tools, jailed to the run's Workspace.

Named tools rather than "drive openpyxl through run_python", because a small
executor model calls `edit_cells(name, sheet, cells)` far more reliably than
it recalls an API. See the Roadmap in Readme.md.

Every write goes through `harness.office`: the file is edited on a copy, the
copy is fingerprinted against the original, and it only replaces the original
if nothing outside the requested change was destroyed. That guard exists
because openpyxl silently drops what it cannot model — sparklines vanish from
inside a sheet part that still exists, `customXml/` disappears, macros need
`keep_vba=True`, and `data_only=True` replaces every formula with its cached
value. Measurements are in `tests/test_office.py`.

openpyxl is optional, like ddgs/trafilatura: absent, these tools return an
actionable error instead of raising.
"""

import os
import shutil

from .. import office, ui
from ..workspace import Workspace


def _tmp_path(path: str) -> str:
    """A sibling temp path that keeps the original extension — openpyxl
    validates by suffix and refuses to open anything else."""
    base, ext = os.path.splitext(path)
    return f"{base}.editing{ext}"


MAX_CELLS = 20_000          # a read that would bury the model's context
MAX_COLS_SHOWN = 40


def _openpyxl():
    try:
        import openpyxl
        return openpyxl, ""
    except ImportError:
        return None, ("[ERROR] openpyxl is not installed, so spreadsheet tools "
                      "are unavailable. Run: pip install openpyxl")


def _resolve(ws: Workspace, name: str, must_exist: bool):
    """Jail the path and check existence, returning (path, error)."""
    try:
        path = ws.resolve(name)
    except ValueError:
        return None, f"[ERROR] refusing to touch a path outside the workspace: {name}"
    if must_exist and not os.path.isfile(path):
        return None, f"[ERROR] no such file: {name}"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return None, (f"[ERROR] {name} is not a .xlsx/.xlsm file. Use read_file "
                      f"and write_file for text formats such as CSV.")
    return path, ""


def _load(openpyxl, path: str, *, values: bool):
    """Open a workbook for reading (values=True) or for editing.

    `data_only` is never combined with a later save: it returns cached values
    and drops every formula, so a workbook opened that way and written back
    loses all of them. Reads use a throwaway handle for exactly that reason.
    """
    return openpyxl.load_workbook(path, data_only=values,
                                  keep_vba=path.lower().endswith(".xlsm"))


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value)


def read_sheet(ws: Workspace, name: str, sheet: str = "",
               max_rows: int = 200) -> str:
    """Read a worksheet as text. Returns both the computed value and the
    formula where they differ, since the model usually needs to know which
    it is looking at."""
    openpyxl, err = _openpyxl()
    if err:
        return err
    path, err = _resolve(ws, name, must_exist=True)
    if err:
        return err
    try:
        vals = _load(openpyxl, path, values=True)
        forms = _load(openpyxl, path, values=False)
    except Exception as e:  # a corrupt or password-protected file
        return f"[ERROR] could not open {name}: {type(e).__name__}: {e}"

    names = vals.sheetnames
    if sheet and sheet not in names:
        return (f"[ERROR] no sheet named {sheet!r} in {name}. "
                f"Sheets: {', '.join(names)}")
    target = sheet or names[0]
    vsheet, fsheet = vals[target], forms[target]

    lines = [f"{name} [{target}]  (sheets: {', '.join(names)})"]
    shown = 0
    for row in range(1, min(vsheet.max_row, max_rows) + 1):
        cells = []
        for col in range(1, min(vsheet.max_column, MAX_COLS_SHOWN) + 1):
            v = _cell_text(vsheet.cell(row=row, column=col).value)
            f = _cell_text(fsheet.cell(row=row, column=col).value)
            # a formula cell reads as "=B2+C2 -> 260": the model needs the
            # formula to edit it and the value to judge it
            cells.append(f"{f} -> {v}" if f.startswith("=") and f != v else v)
            shown += 1
            if shown > MAX_CELLS:
                lines.append("… truncated (too many cells)")
                return "\n".join(lines)
        if any(cells):
            lines.append(f"{row}: " + " | ".join(cells).rstrip(" |"))
    if vsheet.max_row > max_rows:
        lines.append(f"… {vsheet.max_row - max_rows} more rows "
                     f"(raise max_rows to see them)")
    return "\n".join(lines)


def _publish(path: str, tmp: str, allow: tuple = ()) -> str:
    """Swap an edited copy in for the original, but only if the fidelity
    check passes. Returns "" on success, or the refusal to hand the model."""
    problems = office.check_edit(path, tmp, allow=allow)
    if problems:
        os.remove(tmp)
        return office.describe(problems)
    shutil.move(tmp, path)
    return ""


def edit_cells(ws: Workspace, name: str, cells: dict, sheet: str = "") -> str:
    """Set cells in an existing workbook. `cells` maps A1-style references to
    values; a value starting with '=' is written as a formula."""
    openpyxl, err = _openpyxl()
    if err:
        return err
    path, err = _resolve(ws, name, must_exist=True)
    if err:
        return err
    if not isinstance(cells, dict) or not cells:
        return ("[ERROR] cells must be a non-empty object mapping references "
                'to values, e.g. {"B2": 42, "D2": "=B2+C2"}')

    tmp = _tmp_path(path)
    shutil.copy2(path, tmp)
    try:
        wb = _load(openpyxl, tmp, values=False)   # never data_only on a write
        target = sheet or wb.sheetnames[0]
        if target not in wb.sheetnames:
            os.remove(tmp)
            return (f"[ERROR] no sheet named {target!r} in {name}. "
                    f"Sheets: {', '.join(wb.sheetnames)}")
        sh = wb[target]
        for ref, value in cells.items():
            try:
                sh[ref] = value
            except Exception:
                os.remove(tmp)
                return f"[ERROR] {ref!r} is not a valid cell reference"
        wb.save(tmp)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return f"[ERROR] could not edit {name}: {type(e).__name__}: {e}"

    refusal = _publish(path, tmp)
    if refusal:
        return refusal
    ui.info(f"{name}[{target}]: set {len(cells)} cell(s)")
    return (f"set {len(cells)} cell(s) in {name}[{target}]: "
            + ", ".join(sorted(cells)[:20]))


def write_sheet(ws: Workspace, name: str, rows: list, sheet: str = "") -> str:
    """Create a workbook, or replace one sheet of an existing one, with
    `rows` (a list of row lists). Existing workbooks keep every other sheet
    and are subject to the same fidelity check as edit_cells."""
    openpyxl, err = _openpyxl()
    if err:
        return err
    path, err = _resolve(ws, name, must_exist=False)
    if err:
        return err
    if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
        return '[ERROR] rows must be a list of lists, e.g. [["a", 1], ["b", 2]]'

    exists = os.path.isfile(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if not exists:
        wb = openpyxl.Workbook()
        sh = wb.active
        if sheet:
            sh.title = sheet
        for row in rows:
            sh.append(row)
        wb.save(path)
        cells = sum(len(r) for r in rows)
        ui.info(f"new spreadsheet {name} ({len(rows)} rows)")
        ui.file_created(name, len(rows))
        return f"wrote {name}[{sh.title}]: {len(rows)} rows, {cells} cells"

    tmp = _tmp_path(path)
    shutil.copy2(path, tmp)
    try:
        wb = _load(openpyxl, tmp, values=False)
        target = sheet or wb.sheetnames[0]
        # replacing a sheet is a deletion the caller asked for, so its own
        # constructs are allowed to go; everything else still has to survive
        if target in wb.sheetnames:
            del wb[target]
        sh = wb.create_sheet(target)
        for row in rows:
            sh.append(row)
        wb.save(tmp)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return f"[ERROR] could not write {name}: {type(e).__name__}: {e}"

    refusal = _publish(path, tmp)
    if refusal:
        return refusal
    ui.info(f"{name}[{target}]: replaced with {len(rows)} rows")
    return f"replaced {name}[{target}]: {len(rows)} rows"
