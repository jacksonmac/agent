"""Tests for --interactive steering: ui.steer() itself and its wiring into
the retry loop."""

import builtins
import json
import os

import pytest

from harness import run as run_mod
from harness import tools as tools_mod
from harness import ui
from harness.config import settings
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


# ── ui.steer() ───────────────────────────────────────────────────────

def _tty_stdin(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)


def test_steer_returns_typed_guidance(monkeypatch):
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: "  use argparse instead ")
    assert ui.steer() == "use argparse instead"


def test_steer_enter_means_plain_retry(monkeypatch):
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: "")
    assert ui.steer() is None


def test_steer_q_raises_quit(monkeypatch):
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: "Q")
    with pytest.raises(ui.QuitRequested):
        ui.steer()


def test_steer_never_blocks_without_a_tty(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("input() must not be called"))
    assert ui.steer() is None


def test_steer_eof_means_plain_retry(monkeypatch):
    _tty_stdin(monkeypatch)

    def boom(*a):
        raise EOFError
    monkeypatch.setattr(builtins, "input", boom)
    assert ui.steer() is None


# ── retry-loop wiring ────────────────────────────────────────────────

def _fail_then_pass(monkeypatch):
    verdicts = [Verdict(passed=False, feedback="not good"),
                Verdict(passed=True, feedback="")]
    monkeypatch.setattr(run_mod, "review", lambda *a, **kw: verdicts.pop(0))


def _two_attempts(scripted_llm):
    scripted_llm.queue_tool_call("write_file", {"text": "v1", "name": "a.txt"})
    scripted_llm.queue_text("first try: wrote a.txt with the initial version")
    scripted_llm.queue_tool_call("write_file", {"text": "v2", "name": "b.txt"})
    scripted_llm.queue_text("completely different second approach using b.txt")


def _retry_user_msg(scripted_llm) -> str:
    # the retry message is the last user message of the second attempt's payload
    users = [m for m in scripted_llm.payloads[-1]["messages"] if m["role"] == "user"]
    return users[-1]["content"]


def test_guidance_lands_in_the_retry_message(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "interactive", True)
    monkeypatch.setattr(ui, "steer", lambda: "look at the README first")
    _fail_then_pass(monkeypatch)
    _two_attempts(scripted_llm)

    assert run_mod.main("m", "goal", "goal", ws, log, max_attempts=2) is True
    msg = _retry_user_msg(scripted_llm)
    assert "USER GUIDANCE" in msg
    assert "look at the README first" in msg


def test_plain_enter_leaves_retry_untouched(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "interactive", True)
    monkeypatch.setattr(ui, "steer", lambda: None)
    _fail_then_pass(monkeypatch)
    _two_attempts(scripted_llm)

    run_mod.main("m", "goal", "goal", ws, log, max_attempts=2)
    assert "USER GUIDANCE" not in _retry_user_msg(scripted_llm)


def test_steer_not_called_when_flag_off(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "interactive", False)
    monkeypatch.setattr(ui, "steer",
                        lambda: pytest.fail("steer() must not be called"))
    _fail_then_pass(monkeypatch)
    _two_attempts(scripted_llm)
    run_mod.main("m", "goal", "goal", ws, log, max_attempts=2)


def test_steer_not_called_after_the_last_attempt(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "interactive", True)
    monkeypatch.setattr(ui, "steer",
                        lambda: pytest.fail("no retry follows the last attempt"))
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=False, feedback="no"))
    scripted_llm.queue_tool_call("write_file", {"text": "v1", "name": "a.txt"})
    scripted_llm.queue_text("only attempt")
    run_mod.main("m", "goal", "goal", ws, log, max_attempts=1)


def test_steer_quit_finalizes_the_run(scripted_llm, ws, log, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(settings, "interactive", True)

    def quit_now():
        raise ui.QuitRequested()
    monkeypatch.setattr(ui, "steer", quit_now)
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=False, feedback="no"))
    scripted_llm.queue_tool_call("write_file", {"text": "v1", "name": "a.txt"})
    scripted_llm.queue_text("first try")

    assert run_mod.main("m", "goal", "goal", ws, log, max_attempts=3) is False
    history = json.load(open(os.path.join(ws.run_dir, "attempt_history.json")))
    assert history[-1]["verdict"] == "aborted by user (q)"
    assert os.path.exists(os.path.join(ws.run_dir, "report.html"))
