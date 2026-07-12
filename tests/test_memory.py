"""Tests for persistent memory (AGENT.md lessons notes)."""

import os

import pytest
import requests

from harness import llm, memory
from harness import tools as tools_mod
from harness.config import settings
from harness.memory import _trim_sections, update_agent_md
from harness.prompts import MEMORY_SYSTEM
from harness.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


@pytest.fixture
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "memory", True)


def _agent_md(ws):
    with open(os.path.join(ws.root, "AGENT.md")) as f:
        return f.read()


def test_note_appended_with_dated_header(scripted_llm, ws, memory_on):
    scripted_llm.queue_text("- lesson one\n- lesson two")
    update_agent_md(ws, "build a calc", True, files=["calc.py"],
                    feedback_history=["[attempt 1] looked good"])
    content = _agent_md(ws)
    assert "## 20" in content and "build a calc (passed)" in content
    assert "- lesson one" in content and "- lesson two" in content
    # the note call uses the goalsmith fallback model, no tools
    payload = scripted_llm.payloads[0]
    assert payload["messages"][0]["content"] == MEMORY_SYSTEM
    assert "tools" not in payload


def test_uses_goalsmith_model(scripted_llm, ws, memory_on, monkeypatch):
    monkeypatch.setattr(settings, "goalsmith_model", "tiny")
    scripted_llm.queue_text("- x")
    update_agent_md(ws, "g", True, files=[], feedback_history=[])
    assert scripted_llm.payloads[0]["model"] == "tiny"


def test_preamble_preserved(scripted_llm, ws, memory_on):
    path = os.path.join(ws.root, "AGENT.md")
    with open(path, "w") as f:
        f.write("Use tabs, not spaces.\n\n## 2026-01-01 — old run (passed)\n- old lesson\n")
    scripted_llm.queue_text("- new lesson")
    update_agent_md(ws, "g", False, files=[], feedback_history=[])
    content = _agent_md(ws)
    assert content.startswith("Use tabs, not spaces.")
    assert "- old lesson" in content and "- new lesson" in content


def test_prose_stripped_to_bullets(scripted_llm, ws, memory_on):
    scripted_llm.queue_text("Sure! Here are the lessons:\n- a\n- b\nHope that helps!")
    update_agent_md(ws, "g", True, files=[], feedback_history=[])
    content = _agent_md(ws)
    assert "- a" in content and "- b" in content
    assert "Sure!" not in content and "Hope" not in content


def test_llm_failure_warns_and_skips(ws, memory_on, monkeypatch):
    monkeypatch.setattr(llm, "_post_chat",
                        lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError()))
    update_agent_md(ws, "g", True, files=[], feedback_history=[])  # must not raise
    assert not os.path.exists(os.path.join(ws.root, "AGENT.md"))


def test_memory_off_writes_nothing(scripted_llm, ws):
    update_agent_md(ws, "g", True, files=[], feedback_history=[])
    assert not scripted_llm.payloads
    assert not os.path.exists(os.path.join(ws.root, "AGENT.md"))


def test_trim_drops_oldest_sections():
    preamble = "user instructions\n"
    old = "\n## 2026-01-01 — old (passed)\n" + "- x\n" * 200
    new = "\n## 2026-02-02 — new (passed)\n- keep me\n"
    out = _trim_sections(preamble + old + new, max_chars=len(new) + 10)
    assert out.startswith("user instructions")
    assert "keep me" in out
    assert "2026-01-01" not in out


def test_trim_always_keeps_newest_even_if_over():
    huge = "\n## 2026-02-02 — new (passed)\n" + "- y\n" * 500
    out = _trim_sections(huge, max_chars=10)
    assert "2026-02-02" in out  # newest survives regardless
