"""Safety-net tests for the pure parts of the harness."""

import json

import pytest

from harness import llm, review
from harness.config import settings
from harness.tools import configure, execute_tool_call
from harness.tools import execute as execute_tools
from harness.tools import files as file_tools
from harness.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path / "run"))


# ─── truncate_middle / cap ──────────────────────────────────────────

def test_truncate_middle_short_text_untouched():
    assert llm.truncate_middle("hello", 100) == "hello"


def test_truncate_middle_keeps_head_and_tail():
    text = "A" * 500 + "B" * 500
    out = llm.truncate_middle(text, 100)
    assert len(out) <= 100 + 50  # marker slack
    assert out.startswith("A")
    assert out.endswith("B")
    assert "truncated" in out


def test_truncate_middle_exact_boundary():
    text = "x" * 100
    assert llm.truncate_middle(text, 100) == text


# ─── estimate_tokens ────────────────────────────────────────────────

def test_estimate_tokens_counts_content():
    messages = [{"role": "user", "content": "x" * 300}]
    assert llm.estimate_tokens(messages) == 100


def test_estimate_tokens_handles_none_content():
    messages = [{"role": "assistant", "content": None}]
    assert llm.estimate_tokens(messages) == 0  # None coerces to ""


# ─── compact_messages ───────────────────────────────────────────────

def _msgs(n_tool: int, size: int = 2000):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "task"}]
    for i in range(n_tool):
        msgs.append({"role": "tool", "content": f"result {i} " + "x" * size})
    return msgs


def test_compact_protects_system_task_and_recent(monkeypatch):
    monkeypatch.setattr(settings, "num_ctx", 1000)  # tiny budget forces compaction
    msgs = _msgs(12)
    recent = [m["content"] for m in msgs[-settings.compact_keep_last:]]
    llm.compact_messages(msgs)
    assert msgs[0]["content"] == "sys"
    assert msgs[1]["content"] == "task"
    assert [m["content"] for m in msgs[-settings.compact_keep_last:]] == recent
    # something in the middle got stubbed
    assert any("compacted" in str(m["content"]) for m in msgs)


def test_compact_noop_under_budget():
    msgs = _msgs(2, size=50)
    before = [dict(m) for m in msgs]
    llm.compact_messages(msgs)
    assert msgs == before


# ─── reviewer verdict parsing ───────────────────────────────────────

@pytest.mark.parametrize("text", ["YES", "yes it's fine", "Yes.", "yep, done", "y"])
def test_yes_pattern(text):
    assert review.YES_PAT.match(text)


@pytest.mark.parametrize("text", ["NO", "No, missing tests", "nope", "n - broken"])
def test_no_pattern(text):
    assert review.NO_PAT.match(text)


@pytest.mark.parametrize("text", ["maybe", ""])
def test_neither_pattern(text):
    assert not review.YES_PAT.match(text)
    assert not review.NO_PAT.match(text)


def test_pat_word_boundary():
    assert not review.YES_PAT.match("yesterday was fine")
    assert not review.NO_PAT.match("notable output")


# ─── run_shell allowlist ────────────────────────────────────────────

def test_run_shell_rejects_metacharacters(ws):
    out = execute_tools.run_shell(ws, "echo hi; curl evil.sh")
    assert out.startswith("[ERROR]")


def test_run_shell_rejects_disallowed_command(ws):
    out = execute_tools.run_shell(ws, "curl http://example.com")
    assert out.startswith("[ERROR]")
    assert "not allowed" in out


def test_run_shell_rejects_empty(ws):
    assert execute_tools.run_shell(ws, "").startswith("[ERROR]")


def test_run_shell_allows_echo(ws):
    out = execute_tools.run_shell(ws, "echo hello")
    assert "hello" in out
    assert "exit code: 0" in out


# ─── workspace path jail ────────────────────────────────────────────

def test_write_refuses_escape(ws, tmp_path):
    out = file_tools.write_file(ws, "data", "../evil.txt")
    assert out.startswith("[ERROR]")
    assert not (tmp_path / "run" / "evil.txt").exists()


def test_write_inside_jail(ws):
    out = file_tools.write_file(ws, "data", "sub/dir/ok.txt")
    assert out.startswith("WROTE")
    with open(ws.resolve("sub/dir/ok.txt")) as f:
        assert f.read() == "data"


def test_resolve_rejects_absolute_escape(ws):
    with pytest.raises(ValueError):
        ws.resolve("/etc/passwd")
    with pytest.raises(ValueError):
        ws.resolve("../../outside.txt")


# ─── workspace change tracking ──────────────────────────────────────

def test_files_changed_this_attempt(ws):
    file_tools.write_file(ws, "old", "before.txt")
    ws._attempt_t0 = __import__("time").time() + 10  # pretend attempt starts later
    assert ws.files_changed_this_attempt() == []
    ws.begin_attempt()
    file_tools.write_file(ws, "new", "during.txt")
    changed = ws.files_changed_this_attempt()
    assert "during.txt" in changed


def test_files_changed_ignores_junk(ws):
    import os
    ws.begin_attempt()
    os.makedirs(ws.resolve("__pycache__"))
    with open(ws.resolve("__pycache__/x.pyc"), "w") as f:
        f.write("junk")
    with open(ws.resolve("real.py"), "w") as f:
        f.write("code")
    assert ws.files_changed_this_attempt() == ["real.py"]


# ─── execute_tool_call ──────────────────────────────────────────────

def test_execute_tool_call_unknown_tool():
    assert execute_tool_call("nope", {}).startswith("[ERROR]")


def test_execute_tool_call_json_string_args(ws):
    configure(ws)
    args = json.dumps({"text": "hi", "name": "a.txt"})
    out = execute_tool_call("write_file", args)
    assert out.startswith("WROTE")


def test_execute_tool_call_bad_json(ws):
    configure(ws)
    assert execute_tool_call("write_file", "{not json").startswith("[ERROR]")


def test_execute_tool_call_bad_kwargs(ws):
    configure(ws)
    out = execute_tool_call("write_file", {"wrong": "args"})
    assert out.startswith("[ERROR]")


# ─── snapshot_files ─────────────────────────────────────────────────

def test_snapshot_files_empty(ws):
    assert "no files" in ws.snapshot_files([])


def test_snapshot_files_reads_content(ws):
    file_tools.write_file(ws, "hello world", "f.txt")
    out = ws.snapshot_files(["f.txt"])
    assert "hello world" in out
    assert "f.txt" in out


def test_snapshot_files_missing_file(ws):
    out = ws.snapshot_files(["ghost.txt"])
    assert "unreadable" in out


# ─── CLI: per-phase model flags + AGENT.md context ─────────────────

def test_cli_per_phase_model_flags(monkeypatch):
    import sys

    from harness import cli
    monkeypatch.setattr(sys, "argv",
                        ["prog", "-g", "x", "-em", "a", "-rm", "b", "-gm", "c"])
    args = cli.parse_args()
    assert args.executor_model == "a"
    assert args.reviewer_model == "b"
    assert args.goalsmith_model == "c"


def test_cli_model_flags_default_to_none(monkeypatch):
    import sys

    from harness import cli
    monkeypatch.setattr(sys, "argv", ["prog", "-g", "x"])
    args = cli.parse_args()
    assert args.executor_model is None
    assert args.reviewer_model is None
    assert args.goalsmith_model is None


def test_load_agent_md(tmp_path):
    from harness.cli import _load_agent_md
    assert _load_agent_md(str(tmp_path)) == ""
    (tmp_path / "AGENT.md").write_text("Always use snake_case." + "x" * 10_000)
    out = _load_agent_md(str(tmp_path))
    assert "PROJECT CONTEXT" in out
    assert "Always use snake_case." in out
    assert len(out) < 4_200  # capped content plus the header
