"""Loop-level tests for the plan turn, self-check turn, and --best-of,
driven by the scripted fake LLM (no server needed)."""

import json
import os

import pytest

from harness import run as run_mod
from harness import tools as tools_mod
from harness.config import settings
from harness.llm import Session
from harness.review import Verdict
from harness.runlog import RunLog
from harness.workspace import Workspace

SYS = "you are a test executor"


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


@pytest.fixture
def log(ws):
    return RunLog(ws.run_dir)


def _events(ws):
    with open(os.path.join(ws.run_dir, "events.jsonl")) as f:
        return [json.loads(line) for line in f]


# ─── Session.send(with_tools=False) ─────────────────────────────────

def test_send_without_tools_omits_schemas(scripted_llm):
    session = Session("m", SYS, [{"function": {"name": "write_file"}}])
    scripted_llm.queue_text("a plan")
    out = session.send("plan please", with_tools=False)
    assert out == "a plan"
    assert "tools" not in scripted_llm.payloads[-1]


def test_send_with_tools_includes_schemas(scripted_llm):
    session = Session("m", SYS, [{"function": {"name": "write_file"}}])
    scripted_llm.queue_text("done")
    session.send("go")
    assert "tools" in scripted_llm.payloads[-1]


# ─── _run_attempt: plan + self-check ────────────────────────────────

def test_plan_and_self_check_flow(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", True)
    session = run_mod._new_session("m", SYS)

    scripted_llm.queue_text("1. write hello.txt  2. verify")          # plan turn
    scripted_llm.queue_tool_call("write_file", {"text": "hi", "name": "hello.txt"})
    scripted_llm.queue_text("wrote the file")                          # execute answer
    scripted_llm.queue_text("## What was built\nverified final")       # self-check answer

    answer, changed, tool_calls = run_mod._run_attempt(
        session, ws, log, "do the thing", "the goal", ["hello.txt exists"],
        attempt=1, plan_first=True)

    assert answer == "## What was built\nverified final"   # self-check wins
    assert changed == ["hello.txt"]
    assert tool_calls == 1
    assert not scripted_llm.replies                        # exactly 4 calls made
    # the plan turn must not offer tools; the execute turn must
    assert "tools" not in scripted_llm.payloads[0]
    assert "tools" in scripted_llm.payloads[1]
    kinds = [e["event"] for e in _events(ws)]
    assert "plan" in kinds and "self_check" in kinds


def test_self_check_skipped_when_nothing_done(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", True)
    session = run_mod._new_session("m", SYS)
    scripted_llm.queue_text("I would do X")  # no tool calls, no files
    answer, changed, tool_calls = run_mod._run_attempt(
        session, ws, log, "task", "goal", None, attempt=1, plan_first=False)
    assert answer == "I would do X"
    assert tool_calls == 0 and changed == []
    assert not scripted_llm.replies  # only ONE llm call — no self-check


def test_self_check_disabled_by_setting(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    session = run_mod._new_session("m", SYS)
    scripted_llm.queue_tool_call("write_file", {"text": "x", "name": "a.txt"})
    scripted_llm.queue_text("did it")
    answer, changed, _ = run_mod._run_attempt(
        session, ws, log, "task", "goal", None, attempt=1, plan_first=False)
    assert answer == "did it"
    assert changed == ["a.txt"]
    assert not scripted_llm.replies


# ─── best-of candidates ─────────────────────────────────────────────

def test_best_of_promotes_highest_scoring_candidate(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)

    # candidate 1 writes out.txt="one" (1 criterion met); candidate 2 writes
    # out.txt="two" (2 met) — candidate 2 must win and land in the main ws
    scripted_llm.queue_tool_call("write_file", {"text": "one", "name": "out.txt"})
    scripted_llm.queue_text("candidate one answer")
    scripted_llm.queue_tool_call("write_file", {"text": "two", "name": "out.txt"})
    scripted_llm.queue_text("candidate two answer")

    verdicts = [
        Verdict(passed=False, criteria=[{"criterion": "a", "met": True},
                                        {"criterion": "b", "met": False}]),
        Verdict(passed=False, criteria=[{"criterion": "a", "met": True},
                                        {"criterion": "b", "met": True}]),
    ]
    monkeypatch.setattr(run_mod, "review", lambda *a, **kw: verdicts.pop(0))

    session, answer, changed, verdict = run_mod._run_candidates(
        "m", "goal", "task", ws, log, SYS, ["a", "b"], best_of=2)

    assert answer == "candidate two answer"
    assert sum(1 for c in verdict.criteria if c["met"]) == 2
    with open(ws.resolve("out.txt")) as f:
        assert f.read() == "two"
    assert os.path.isdir(os.path.join(ws.run_dir, "candidate_1"))
    assert os.path.isdir(os.path.join(ws.run_dir, "candidate_2"))
    winners = [e for e in _events(ws) if e["event"] == "candidate_selected"]
    assert winners and winners[0]["winner"] == "candidate_2"


def test_best_of_tie_goes_to_first_candidate(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    scripted_llm.queue_tool_call("write_file", {"text": "one", "name": "out.txt"})
    scripted_llm.queue_text("first")
    scripted_llm.queue_tool_call("write_file", {"text": "two", "name": "out.txt"})
    scripted_llm.queue_text("second")
    same = [Verdict(passed=False, criteria=[{"criterion": "a", "met": True}]),
            Verdict(passed=False, criteria=[{"criterion": "a", "met": True}])]
    monkeypatch.setattr(run_mod, "review", lambda *a, **kw: same.pop(0))

    _, answer, _, _ = run_mod._run_candidates(
        "m", "goal", "task", ws, log, SYS, ["a"], best_of=2)
    assert answer == "first"
    with open(ws.resolve("out.txt")) as f:
        assert f.read() == "one"


# ─── the full loop end-to-end (scripted) ────────────────────────────

def test_main_loop_passes_first_attempt(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", True)
    monkeypatch.setattr(settings, "plan_first", True)
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=True, feedback=""))

    scripted_llm.queue_text("plan: write then verify")
    scripted_llm.queue_tool_call("write_file", {"text": "hello", "name": "hello.txt"})
    scripted_llm.queue_text("wrote it")
    scripted_llm.queue_text("## What was built\nfinal answer")

    run_mod.main("m", "make hello.txt", "make hello.txt", ws, log, max_attempts=3)

    assert os.path.exists(os.path.join(ws.run_dir, "final_output.txt"))
    assert os.path.exists(os.path.join(ws.run_dir, "report.html"))
    history = json.load(open(os.path.join(ws.run_dir, "attempt_history.json")))
    assert len(history) == 1 and history[0]["passed"] is True


def test_main_loop_retries_then_gives_unverified(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=False, feedback="not good"))

    # two attempts, both write files (dodging the no-tools gate) with
    # clearly different answers (dodging the stall gate)
    scripted_llm.queue_tool_call("write_file", {"text": "v1", "name": "a.txt"})
    scripted_llm.queue_text("first try: wrote a.txt with the initial version")
    scripted_llm.queue_tool_call("write_file", {"text": "v2", "name": "b.txt"})
    scripted_llm.queue_text("completely different second approach using b.txt instead")

    run_mod.main("m", "goal", "goal", ws, log, max_attempts=2)

    assert os.path.exists(os.path.join(ws.run_dir, "final_output_UNVERIFIED.txt"))
    history = json.load(open(os.path.join(ws.run_dir, "attempt_history.json")))
    assert [a["passed"] for a in history] == [False, False]


# ─── per-phase model selection ──────────────────────────────────────

def test_executor_model_overrides_default(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "executor_model", "big-coder")
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=True, feedback=""))

    scripted_llm.queue_tool_call("write_file", {"text": "x", "name": "a.txt"})
    scripted_llm.queue_text("done")

    run_mod.main("m", "goal", "goal", ws, log, max_attempts=1)

    assert all(p["model"] == "big-coder" for p in scripted_llm.payloads)
    start = [e for e in _events(ws) if e["event"] == "run_start"][0]
    assert start["executor"] == "big-coder"


def test_executor_model_defaults_to_model(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "executor_model", None)
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=True, feedback=""))

    scripted_llm.queue_tool_call("write_file", {"text": "x", "name": "a.txt"})
    scripted_llm.queue_text("done")

    run_mod.main("m", "goal", "goal", ws, log, max_attempts=1)

    assert all(p["model"] == "m" for p in scripted_llm.payloads)
    start = [e for e in _events(ws) if e["event"] == "run_start"][0]
    assert start["executor"] == "m"


def test_reviewer_falls_back_to_settings_model(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "executor_model", "big-coder")
    monkeypatch.setattr(settings, "reviewer_model", None)
    monkeypatch.setattr(settings, "model", "m")

    seen = {}

    def fake_review(model, *a, **kw):
        seen["model"] = model
        return Verdict(passed=True, feedback="")

    monkeypatch.setattr(run_mod, "review", fake_review)
    scripted_llm.queue_tool_call("write_file", {"text": "x", "name": "a.txt"})
    scripted_llm.queue_text("done")

    run_mod.main("m", "goal", "goal", ws, log, max_attempts=1)

    assert seen["model"] == "m"  # reviewer falls back to --model, not -em
