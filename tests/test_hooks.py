"""Tests for observe-only hooks (hooks.json)."""

import json

import pytest

from harness import hooks, ui
from harness.tools import execute_tool_call


@pytest.fixture(autouse=True)
def clean_hooks():
    yield
    hooks.configure(None)


def _write_hooks(tmp_path, data):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_fire_substitutes_placeholders(tmp_path):
    out = tmp_path / "log.txt"
    path = _write_hooks(tmp_path, {"pre_tool": [
        {"match": "write_file", "run": f"echo {{tool}} {{file}} >> {out}"}]})
    hooks.configure(path, run_dir=str(tmp_path), workspace=str(tmp_path))
    hooks.fire("pre_tool", tool="write_file", file="a.py")
    assert out.read_text().strip() == "write_file a.py"


def test_match_filters_by_tool(tmp_path):
    out = tmp_path / "log.txt"
    path = _write_hooks(tmp_path, {"post_tool": [
        {"match": "run_*", "run": f"echo {{tool}} >> {out}"}]})
    hooks.configure(path)
    hooks.fire("post_tool", tool="read_file")
    hooks.fire("post_tool", tool="run_shell")
    assert out.read_text().strip() == "run_shell"


def test_failing_hook_warns_not_raises(tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    path = _write_hooks(tmp_path, {"run_end": [{"run": "exit 3"}]})
    hooks.configure(path)
    hooks.fire("run_end", passed=True)
    assert warned and "exited 3" in warned[0]


def test_timeout_warns_not_raises(tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT", 0.1)
    path = _write_hooks(tmp_path, {"run_end": [{"run": "sleep 5"}]})
    hooks.configure(path)
    hooks.fire("run_end", passed=True)
    assert warned and "hook failed" in warned[0]


def test_missing_and_malformed_config(tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    hooks.configure(str(tmp_path / "nope.json"))
    assert hooks._hooks == {} and not warned  # missing file is silent
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    hooks.configure(str(bad))
    assert hooks._hooks == {} and warned


def test_unknown_placeholder_becomes_empty(tmp_path):
    out = tmp_path / "log.txt"
    path = _write_hooks(tmp_path, {"run_end": [
        {"run": f"echo start{{bogus}}end >> {out}"}]})
    hooks.configure(path)
    hooks.fire("run_end", passed=True)
    assert out.read_text().strip() == "startend"


def test_fires_through_execute_tool_call(tmp_path):
    out = tmp_path / "log.txt"
    path = _write_hooks(tmp_path, {"post_tool": [
        {"match": "set_todos", "run": f"echo fired >> {out}"}]})
    hooks.configure(path)
    execute_tool_call("set_todos", {"todos": [{"text": "x"}]})
    assert out.read_text().strip() == "fired"


def test_unconfigured_fire_is_noop():
    hooks.configure(None)
    hooks.fire("pre_tool", tool="anything")  # must not raise or spawn anything
