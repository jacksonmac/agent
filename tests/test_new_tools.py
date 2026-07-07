"""Tests for the Phase 3 executor tools: read_file, list_files, edit_file, run_script."""

import pytest

from harness.tools import execute as execute_tools
from harness.tools import files as file_tools
from harness.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path / "run"))


# ─── read_file ──────────────────────────────────────────────────────

def test_read_file_basic(ws):
    file_tools.write_file(ws, "line1\nline2\n", "f.txt")
    out = file_tools.read_file(ws, "f.txt")
    assert "line1\nline2" in out
    assert "chars 0-12 of 12" in out


def test_read_file_paging(ws):
    file_tools.write_file(ws, "A" * 100, "big.txt")
    out = file_tools.read_file(ws, "big.txt", offset=0, max_chars=40)
    assert "chars 0-40 of 100" in out
    assert "offset=40" in out  # tells the model how to continue
    out2 = file_tools.read_file(ws, "big.txt", offset=90, max_chars=40)
    assert "chars 90-100 of 100" in out2


def test_read_file_missing(ws):
    assert file_tools.read_file(ws, "nope.txt").startswith("[ERROR]")


def test_read_file_escape(ws):
    assert file_tools.read_file(ws, "../secret").startswith("[ERROR]")


# ─── list_files ─────────────────────────────────────────────────────

def test_list_files_empty(ws):
    assert "empty" in file_tools.list_files(ws)


def test_list_files_recursive_with_sizes(ws):
    file_tools.write_file(ws, "hello", "a.txt")
    file_tools.write_file(ws, "world!", "sub/b.txt")
    out = file_tools.list_files(ws)
    assert "a.txt  (5 bytes)" in out
    assert "sub/b.txt  (6 bytes)" in out


def test_list_files_subdir(ws):
    file_tools.write_file(ws, "x", "a.txt")
    file_tools.write_file(ws, "y", "sub/b.txt")
    out = file_tools.list_files(ws, "sub")
    assert "b.txt" in out
    assert "a.txt  " not in out


# ─── edit_file ──────────────────────────────────────────────────────

def test_edit_file_replaces_once(ws):
    file_tools.write_file(ws, "print('helo')\n", "g.py")
    out = file_tools.edit_file(ws, "g.py", "helo", "hello")
    assert out.startswith("REPLACED")
    with open(ws.resolve("g.py")) as f:
        assert f.read() == "print('hello')\n"


def test_edit_file_not_found_gives_hint(ws):
    file_tools.write_file(ws, "print('hello world')\n", "g.py")
    out = file_tools.edit_file(ws, "g.py", "print('helo world')", "x")
    assert out.startswith("[ERROR]")
    assert "Closest line" in out


def test_edit_file_ambiguous(ws):
    file_tools.write_file(ws, "x = 1\nx = 1\n", "g.py")
    out = file_tools.edit_file(ws, "g.py", "x = 1", "x = 2")
    assert out.startswith("[ERROR]")
    assert "2 times" in out


def test_edit_file_missing_file(ws):
    assert file_tools.edit_file(ws, "nope.py", "a", "b").startswith("[ERROR]")


# ─── run_script ─────────────────────────────────────────────────────

def test_run_script_with_args(ws):
    file_tools.write_file(ws, "import sys\nprint('got', sys.argv[1])\n", "s.py")
    out = execute_tools.run_script(ws, "s.py", ["abc"])
    assert "got abc" in out
    assert "exit code: 0" in out


def test_run_script_missing(ws):
    out = execute_tools.run_script(ws, "ghost.py")
    assert out.startswith("[ERROR]")
    assert "list_files" in out


def test_run_shell_allows_pytest(ws):
    # pytest isn't necessarily on PATH in the workspace env; just confirm the
    # allowlist accepts it (a FileNotFoundError message is fine, a rejection is not)
    out = execute_tools.run_shell(ws, "pytest --version")
    assert "not allowed" not in out


# ─── grep_files ─────────────────────────────────────────────────────

def test_grep_files_matches_across_files(ws):
    file_tools.write_file(ws, "def alpha():\n    pass\n", "a.py")
    file_tools.write_file(ws, "from a import alpha\nalpha()\n", "sub/b.py")
    out = file_tools.grep_files(ws, r"alpha")
    assert "a.py:1: def alpha():" in out
    assert "sub/b.py:1: from a import alpha" in out
    assert "sub/b.py:2: alpha()" in out


def test_grep_files_no_match(ws):
    file_tools.write_file(ws, "nothing here", "a.txt")
    assert file_tools.grep_files(ws, "zzz").startswith("(no matches")


def test_grep_files_invalid_regex_falls_back_to_literal(ws):
    file_tools.write_file(ws, "cost is $(price)\n", "a.txt")
    out = file_tools.grep_files(ws, "$(price")  # invalid regex, valid literal
    assert "a.txt:1:" in out


def test_grep_files_escape(ws):
    assert file_tools.grep_files(ws, "x", path="../..").startswith("[ERROR]")


def test_grep_files_max_results(ws):
    file_tools.write_file(ws, "hit\n" * 10, "a.txt")
    out = file_tools.grep_files(ws, "hit", max_results=3)
    assert out.count("a.txt:") == 3
    assert "stopped at 3 matches" in out


def test_grep_files_subdir_only(ws):
    file_tools.write_file(ws, "needle", "a.txt")
    file_tools.write_file(ws, "needle", "sub/b.txt")
    out = file_tools.grep_files(ws, "needle", path="sub")
    assert "sub/b.txt:1:" in out
    assert "a.txt:1:" not in out.replace("sub/b.txt:1:", "")


def test_grep_files_registered_as_tool(ws):
    from harness.tools import TOOL_SCHEMAS, configure, execute_tool_call
    assert any(s["function"]["name"] == "grep_files" for s in TOOL_SCHEMAS)
    configure(ws)
    file_tools.write_file(ws, "needle here", "a.txt")
    out = execute_tool_call("grep_files", {"pattern": "needle"})
    assert "a.txt:1:" in out
