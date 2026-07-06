"""Tests for structured verdict parsing and its fallbacks."""

import json

import pytest

from harness.review import Verdict, _extract_json, parse_verdict
from harness.tools import files as file_tools
from harness.review import automated_checks
from harness.workspace import Workspace


GOOD = json.dumps({
    "pass": False,
    "criteria": [
        {"criterion": "fizzbuzz.py exists", "met": True, "note": ""},
        {"criterion": "prints correct output", "met": False, "note": "line 15 wrong"},
    ],
    "feedback": "fix line 15",
})


def test_parse_clean_json():
    v = parse_verdict(GOOD)
    assert v is not None
    assert v.passed is False
    assert len(v.criteria) == 2
    assert v.feedback == "fix line 15"


def test_parse_fenced_json():
    v = parse_verdict(f"Here is my verdict:\n```json\n{GOOD}\n```\nDone.")
    assert v is not None and v.passed is False


def test_parse_json_with_surrounding_prose():
    v = parse_verdict(f"Sure! {GOOD} hope that helps")
    assert v is not None and v.feedback == "fix line 15"


def test_parse_pass_true():
    v = parse_verdict('{"pass": true, "criteria": [], "feedback": ""}')
    assert v is not None and v.passed is True


@pytest.mark.parametrize("garbage", [
    "",
    "YES",
    "NO, it is broken",
    "{not json at all",
    '{"unrelated": 1}',          # json but no "pass" key
    '{"pass": true',              # unbalanced
    "the criteria were met { } but",  # empty object without pass
])
def test_parse_garbage_returns_none(garbage):
    assert parse_verdict(garbage) is None


def test_extract_json_nested_braces():
    text = 'x {"pass": false, "criteria": [{"criterion": "a {b} c", "met": true}], "feedback": ""} y'
    data = _extract_json(text)
    assert data is not None and data["pass"] is False


def test_unmet_and_summary():
    v = parse_verdict(GOOD)
    unmet = v.unmet()
    assert unmet == ["prints correct output (line 15 wrong)"]
    s = v.summary()
    assert "fix line 15" in s and "Unmet:" in s


def test_verdict_criteria_non_dict_entries_dropped():
    v = parse_verdict('{"pass": false, "criteria": ["just a string", {"criterion": "x", "met": false}], "feedback": "f"}')
    assert v is not None
    assert len(v.criteria) == 1


# ─── automated checks ───────────────────────────────────────────────

@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path / "run"))


def test_automated_checks_none_without_tests(ws):
    file_tools.write_file(ws, "print('hi')", "app.py")
    assert automated_checks(ws) == "(none)"


def test_automated_checks_runs_pytest(ws):
    file_tools.write_file(ws, "def test_ok():\n    assert True\n", "test_thing.py")
    out = automated_checks(ws)
    assert "pytest" in out
    assert "exit code:" in out
