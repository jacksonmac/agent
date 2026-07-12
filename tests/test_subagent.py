"""Tests for spawn_subagent: scoped child session, summary-only return."""

import json
import os

import pytest

from harness import runlog
from harness import tools as tools_mod
from harness.config import settings
from harness.llm import Session
from harness.prompts import SUBAGENT_SYSTEM
from harness.review import reviewer_tool_schemas
from harness.runlog import RunLog
from harness.tools import TOOL_SCHEMAS, subagent
from harness.workspace import Workspace

SYS = "you are a test executor"


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


def _tool_names(payload) -> set:
    return {s["function"]["name"] for s in payload.get("tools", [])}


def test_parent_gets_only_summary(scripted_llm, ws):
    parent = Session("m", SYS, TOOL_SCHEMAS)
    scripted_llm.queue_tool_call("spawn_subagent",
                                 {"task": "survey the workspace", "kind": "research"})
    scripted_llm.queue_text("SUMMARY: workspace is empty")   # child's only turn
    scripted_llm.queue_text("done, used the summary")        # parent final answer

    out = parent.send("go")
    assert out == "done, used the summary"
    # the child got a fresh conversation with the subagent system prompt
    # (plus, when skills/ exists, the appended skills index)
    child_payload = scripted_llm.payloads[1]
    assert child_payload["messages"][0]["role"] == "system"
    assert child_payload["messages"][0]["content"].startswith(SUBAGENT_SYSTEM)
    assert child_payload["messages"][1]["content"] == "survey the workspace"
    # the parent's tool message is exactly the child's summary
    tool_msgs = [m for m in scripted_llm.payloads[2]["messages"] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "SUMMARY: workspace is empty"


def test_child_schemas_exclude_spawn_and_todos(scripted_llm, ws):
    parent = Session("m", SYS, TOOL_SCHEMAS)
    scripted_llm.queue_tool_call("spawn_subagent", {"task": "look around"})
    scripted_llm.queue_text("nothing there")
    scripted_llm.queue_text("ok")
    parent.send("go")
    child_tools = _tool_names(scripted_llm.payloads[1])
    assert "spawn_subagent" not in child_tools
    assert "set_todos" not in child_tools
    assert "read_file" in child_tools  # still has the real tool belt


def test_child_uses_executor_model(scripted_llm, ws, monkeypatch):
    monkeypatch.setattr(settings, "executor_model", "big-coder")
    scripted_llm.queue_text("child summary")
    subagent.spawn_subagent("do a thing")
    assert scripted_llm.payloads[0]["model"] == "big-coder"


def test_summary_capped_by_parent_loop(scripted_llm, ws, monkeypatch):
    monkeypatch.setattr(settings, "tool_result_max", 200)
    parent = Session("m", SYS, TOOL_SCHEMAS)
    scripted_llm.queue_tool_call("spawn_subagent", {"task": "long-winded task"})
    scripted_llm.queue_text("X" * 2000)
    scripted_llm.queue_text("ok")
    parent.send("go")
    tool_msgs = [m for m in scripted_llm.payloads[2]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs[0]["content"]) < 2000
    assert "truncated" in tool_msgs[0]["content"]


def test_depth_guard_blocks_recursion(scripted_llm, ws, monkeypatch):
    monkeypatch.setattr(subagent, "_depth", 1)
    out = subagent.spawn_subagent("nested task")
    assert out.startswith("[ERROR]")
    assert not scripted_llm.payloads  # no LLM call was made


def test_empty_task_rejected(scripted_llm, ws):
    assert subagent.spawn_subagent("  ").startswith("[ERROR]")
    assert not scripted_llm.payloads


def test_subagent_events_logged(scripted_llm, ws, monkeypatch):
    log = RunLog(ws.run_dir)
    monkeypatch.setattr(runlog, "current", log)
    scripted_llm.queue_text("found nothing")
    subagent.spawn_subagent("look", kind="research")
    with open(os.path.join(ws.run_dir, "events.jsonl")) as f:
        events = [json.loads(line) for line in f]
    kinds = [e["event"] for e in events]
    assert "subagent_start" in kinds and "subagent_end" in kinds
    end = next(e for e in events if e["event"] == "subagent_end")
    assert end["kind"] == "research"
    assert end["chars"] == len("found nothing")


def test_reviewer_schemas_exclude_new_tools():
    names = {s["function"]["name"] for s in reviewer_tool_schemas()}
    assert "spawn_subagent" not in names
    assert "set_todos" not in names
