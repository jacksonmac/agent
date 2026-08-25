"""Spreadsheet tools: the jail, the fidelity guard, and the degradation path
when openpyxl is absent.

The guard tests are the point of the file: an edit that would destroy part of
a workbook must leave the original untouched and tell the model why.
"""

import os
import zipfile

import pytest

from harness import tools as tools_mod
from harness.tools import docs
from harness.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


def _book(ws, name="sales.xlsx", formulas=True):
    """A workbook with the mainstream features: formulas, a second sheet,
    a merged range and a chart."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sh = wb.active
    sh.title = "Data"
    sh.append(["region", "q1", "q2", "total"])
    for i, row in enumerate([("north", 120, 140), ("south", 95, 88)], start=2):
        sh.append(list(row))
        if formulas:
            sh[f"D{i}"] = f"=B{i}+C{i}"
    sh.merge_cells("A5:B5")
    chart = openpyxl.chart.BarChart()
    chart.add_data(openpyxl.chart.Reference(sh, min_col=2, min_row=1,
                                            max_col=3, max_row=3),
                   titles_from_data=True)
    sh.add_chart(chart, "F2")
    wb.create_sheet("Notes")["A1"] = "seeded by the user"
    path = ws.resolve(name)
    wb.save(path)
    return path


# ─── reading ────────────────────────────────────────────────────────

def test_read_sheet_shows_formula_and_value(ws):
    pytest.importorskip("openpyxl")
    _book(ws)
    out = docs.read_sheet(ws, "sales.xlsx")
    assert "sales.xlsx [Data]" in out
    assert "sheets: Data, Notes" in out
    assert "region" in out and "north" in out
    assert "=B2+C2" in out          # the formula, so the model can edit it


def test_read_sheet_selects_a_named_sheet(ws):
    pytest.importorskip("openpyxl")
    _book(ws)
    assert "seeded by the user" in docs.read_sheet(ws, "sales.xlsx", sheet="Notes")
    err = docs.read_sheet(ws, "sales.xlsx", sheet="Nope")
    assert err.startswith("[ERROR]") and "Data, Notes" in err


def test_read_sheet_rejects_non_spreadsheets_and_escapes(ws):
    with open(ws.resolve("notes.txt"), "w") as f:
        f.write("hi")
    assert "not a .xlsx" in docs.read_sheet(ws, "notes.txt")
    assert "outside the workspace" in docs.read_sheet(ws, "../escape.xlsx")
    assert "no such file" in docs.read_sheet(ws, "missing.xlsx")


# ─── editing, and the guard ─────────────────────────────────────────

def test_edit_cells_writes_values_and_formulas(ws):
    openpyxl = pytest.importorskip("openpyxl")
    path = _book(ws)
    out = docs.edit_cells(ws, "sales.xlsx", {"B2": 999, "D4": "=B2*2"})
    assert not out.startswith("[ERROR]"), out
    wb = openpyxl.load_workbook(path)
    assert wb["Data"]["B2"].value == 999
    assert wb["Data"]["D4"].value == "=B2*2"
    assert wb["Notes"]["A1"].value == "seeded by the user"   # untouched


def test_edit_cells_preserves_formulas_elsewhere(ws):
    """The data_only footgun: an edit must not flatten the other formulas."""
    openpyxl = pytest.importorskip("openpyxl")
    path = _book(ws)
    docs.edit_cells(ws, "sales.xlsx", {"B2": 1})
    wb = openpyxl.load_workbook(path)
    assert wb["Data"]["D3"].value == "=B3+C3"


def test_edit_is_refused_when_it_would_destroy_something(ws, monkeypatch):
    """The reason the guard exists. Simulate a library that drops a part —
    the original must survive and the model must be told why."""
    pytest.importorskip("openpyxl")
    path = _book(ws)
    before = open(path, "rb").read()

    real_check = docs.office.check_edit
    monkeypatch.setattr(docs.office, "check_edit",
                        lambda a, b, allow=(): ["lost sparklines"])
    out = docs.edit_cells(ws, "sales.xlsx", {"B2": 5})
    assert out.startswith("[ERROR]")
    assert "lost sparklines" in out
    assert "original file is untouched" in out
    assert "Do not retry" in out
    assert open(path, "rb").read() == before        # genuinely untouched
    assert not os.path.exists(docs._tmp_path(path))  # no debris left behind
    del real_check


def test_edit_cells_validates_its_arguments(ws):
    pytest.importorskip("openpyxl")
    _book(ws)
    assert "non-empty object" in docs.edit_cells(ws, "sales.xlsx", {})
    assert "non-empty object" in docs.edit_cells(ws, "sales.xlsx", "B2=1")
    bad = docs.edit_cells(ws, "sales.xlsx", {"not a ref": 1})
    assert bad.startswith("[ERROR]")
    assert not os.path.exists(docs._tmp_path(ws.resolve("sales.xlsx")))
    miss = docs.edit_cells(ws, "sales.xlsx", {"B2": 1}, sheet="Ghost")
    assert "no sheet named" in miss


def test_a_real_edit_keeps_the_chart(ws):
    """openpyxl 3.1 does round-trip charts; if a future version stops, the
    guard should be what tells us — not a silently damaged file."""
    pytest.importorskip("openpyxl")
    path = _book(ws)
    assert not docs.edit_cells(ws, "sales.xlsx", {"B2": 7}).startswith("[ERROR]")
    with zipfile.ZipFile(path) as z:
        assert any(n.startswith("xl/charts/") for n in z.namelist())


# ─── writing ────────────────────────────────────────────────────────

def test_write_sheet_creates_a_workbook(ws):
    openpyxl = pytest.importorskip("openpyxl")
    out = docs.write_sheet(ws, "new.xlsx", [["a", 1], ["b", 2]], sheet="Rows")
    assert "2 rows" in out and not out.startswith("[ERROR]")
    wb = openpyxl.load_workbook(ws.resolve("new.xlsx"))
    assert wb.sheetnames == ["Rows"]
    assert wb["Rows"]["B2"].value == 2


def test_write_sheet_replaces_one_sheet_and_keeps_the_others(ws):
    openpyxl = pytest.importorskip("openpyxl")
    _book(ws)
    out = docs.write_sheet(ws, "sales.xlsx", [["x"], ["y"]], sheet="Notes")
    assert not out.startswith("[ERROR]"), out
    wb = openpyxl.load_workbook(ws.resolve("sales.xlsx"))
    assert set(wb.sheetnames) == {"Data", "Notes"}
    assert wb["Notes"]["A1"].value == "x"
    assert wb["Data"]["D2"].value == "=B2+C2"      # the other sheet survived


def test_write_sheet_validates_rows(ws):
    pytest.importorskip("openpyxl")
    assert "list of lists" in docs.write_sheet(ws, "n.xlsx", "not rows")
    assert "list of lists" in docs.write_sheet(ws, "n.xlsx", ["flat"])
    assert "outside the workspace" in docs.write_sheet(ws, "../x.xlsx", [[1]])


# ─── optional dependency ────────────────────────────────────────────

def test_tools_degrade_without_openpyxl(ws, monkeypatch):
    """Same contract as the web tools: an actionable error, not a crash."""
    monkeypatch.setattr(docs, "_openpyxl",
                        lambda: (None, "[ERROR] openpyxl is not installed, so "
                                       "spreadsheet tools are unavailable. "
                                       "Run: pip install openpyxl"))
    for out in (docs.read_sheet(ws, "a.xlsx"),
                docs.edit_cells(ws, "a.xlsx", {"A1": 1}),
                docs.write_sheet(ws, "a.xlsx", [[1]])):
        assert out.startswith("[ERROR]") and "pip install openpyxl" in out


# ─── registration ───────────────────────────────────────────────────

def test_sheet_tools_are_registered_and_jailed(ws):
    from harness.tools import TOOL_SCHEMAS, tools
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert {"read_sheet", "edit_cells", "write_sheet"} <= names
    # configure(ws) binds the workspace, so the model never passes a path
    for name in ("read_sheet", "edit_cells", "write_sheet"):
        assert name in tools
    assert "outside the workspace" in tools["read_sheet"](name="/etc/passwd.xlsx")


def test_sheet_tools_are_not_permission_gated():
    """They are jailed and reversible, like the other file tools."""
    from harness import permissions
    assert not ({"read_sheet", "edit_cells", "write_sheet"}
                & set(permissions.gated()))
