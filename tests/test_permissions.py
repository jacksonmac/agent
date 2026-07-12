"""Tests for the permission gate on code-executing tools."""

import pytest

from harness import permissions, ui
from harness import tools as tools_mod
from harness.tools import execute_tool_call
from harness.workspace import Workspace


@pytest.fixture
def gated(monkeypatch):
    """Gate ON (overriding the suite-wide yolo fixture), non-interactive."""
    permissions.configure(yolo=False)
    monkeypatch.setattr(permissions, "_interactive", lambda: False)
    yield
    permissions.configure(yolo=True)


@pytest.fixture
def interactive(monkeypatch):
    permissions.configure(yolo=False)
    monkeypatch.setattr(permissions, "_interactive", lambda: True)
    yield
    permissions.configure(yolo=True)


def test_non_tty_auto_denies_with_single_warn(gated, monkeypatch):
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    out1 = permissions.check("run_shell", {"command": "ls"})
    out2 = permissions.check("run_python", {"code": "1"})
    assert out1.startswith("[ERROR] permission denied")
    assert "--yolo" in out1
    assert out2.startswith("[ERROR]")
    assert len(warned) == 1  # warn once, not per call


def test_ungated_tools_pass(gated):
    assert permissions.check("read_file", {"name": "a"}) is None
    assert permissions.check("set_todos", {"todos": []}) is None


def test_yolo_bypasses_everything(monkeypatch):
    permissions.configure(yolo=True)
    monkeypatch.setattr(permissions, "_interactive", lambda: False)
    assert permissions.check("run_shell", {"command": "ls"}) is None


def test_interactive_yes_no(interactive, monkeypatch):
    answers = iter(["y", "n"])
    monkeypatch.setattr(ui, "confirm_tool", lambda *a, **kw: next(answers))
    assert permissions.check("run_shell", {"command": "ls"}) is None
    denial = permissions.check("run_shell", {"command": "rm x"})
    assert denial.startswith("[ERROR] permission denied by user")


def test_always_caches_for_the_run(interactive, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "confirm_tool",
                        lambda *a, **kw: calls.append(a) or "a")
    assert permissions.check("run_python", {"code": "1"}) is None
    assert permissions.check("run_python", {"code": "2"}) is None
    assert len(calls) == 1  # second call rode the "always" grant
    # but a different gated tool prompts again
    assert permissions.check("run_shell", {"command": "ls"}) is None
    assert len(calls) == 2


def test_execute_tool_call_denial_skips_tool(gated, tmp_path, monkeypatch):
    ws = Workspace(str(tmp_path / "run"))
    tools_mod.configure(ws)
    monkeypatch.setitem(tools_mod.tools, "run_shell",
                        lambda **kw: pytest.fail("tool ran despite denial"))
    out = execute_tool_call("run_shell", {"command": "ls"})
    assert out.startswith("[ERROR] permission denied")
