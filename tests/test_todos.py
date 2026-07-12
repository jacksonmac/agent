"""Tests for the set_todos checklist tool."""

import json
import os

import pytest

from harness import review as review_mod
from harness import runlog, todos
from harness import tools as tools_mod
from harness.runlog import RunLog
from harness.tools import execute_tool_call
from harness.workspace import Workspace


@pytest.fixture(autouse=True)
def clean_todos():
    todos.reset()
    yield
    todos.reset()


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


def test_set_and_render():
    out = todos.set_todos([{"text": "write app.py", "status": "in_progress"},
                           {"text": "run tests"}])
    assert "[>] write app.py" in out
    assert "[ ] run tests" in out  # status defaults to pending
    assert todos.current[1]["status"] == "pending"


def test_update_replaces_list():
    todos.set_todos([{"text": "a"}, {"text": "b"}])
    todos.set_todos([{"text": "a", "status": "done"}])
    assert len(todos.current) == 1
    assert todos.render() == "[x] a"


def test_malformed_leaves_current_untouched():
    todos.set_todos([{"text": "keep me"}])
    assert todos.set_todos("not a list").startswith("[ERROR]")
    assert todos.set_todos([]).startswith("[ERROR]")
    assert todos.set_todos([{"status": "done"}]).startswith("[ERROR]")
    assert todos.set_todos([{"text": "x", "status": "doing"}]).startswith("[ERROR]")
    assert [t["text"] for t in todos.current] == ["keep me"]


def test_render_empty_state():
    assert todos.render() == "(no todos recorded)"


def test_via_execute_tool_call_and_logged(ws, monkeypatch):
    log = RunLog(ws.run_dir)
    monkeypatch.setattr(runlog, "current", log)
    # string-JSON args, the way some models send them
    out = execute_tool_call("set_todos",
                            json.dumps({"todos": [{"text": "step one"}]}))
    assert "step one" in out
    with open(os.path.join(ws.run_dir, "events.jsonl")) as f:
        events = [json.loads(line) for line in f]
    todo_events = [e for e in events if e["event"] == "todos"]
    assert todo_events and todo_events[0]["items"] == ["pending:step one"]


def test_review_prompt_includes_todos(scripted_llm, ws):
    todos.set_todos([{"text": "build the thing", "status": "done"}])
    scripted_llm.queue_text(json.dumps({"pass": True, "criteria": [], "feedback": ""}))
    review_mod.review("m", "goal", "answer", ws)
    user_msg = scripted_llm.payloads[0]["messages"][1]["content"]
    assert "EXECUTOR'S OWN CHECKLIST" in user_msg
    assert "[x] build the thing" in user_msg
