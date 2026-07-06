"""Tests for Session retry memory and the feedback digest."""

import pytest

from harness.config import settings
from harness.llm import Session
from harness.run import _feedback_digest


def _session_with_history(n_tool: int = 5, size: int = 2_000) -> Session:
    s = Session("m", "system prompt", tool_schemas=None)
    s.messages.append({"role": "user", "content": "the task"})
    for i in range(n_tool):
        s.messages.append({"role": "tool", "content": f"result {i} " + "x" * size})
    s.messages.append({"role": "assistant", "content": "final answer " + "y" * size})
    return s


def test_compact_completed_attempts_stubs_middle_keeps_ends():
    s = _session_with_history()
    s.compact_completed_attempts()
    assert s.messages[0]["content"] == "system prompt"
    assert s.messages[1]["content"] == "the task"
    # the final answer of the attempt is preserved in full
    assert s.messages[-1]["content"].startswith("final answer")
    assert len(s.messages[-1]["content"]) > 1_000
    # tool results in between are stubbed
    for m in s.messages[2:-1]:
        assert len(str(m["content"])) < 400
        assert "compacted" in m["content"]


def test_compact_respects_full_context(monkeypatch):
    monkeypatch.setattr(settings, "full_context", True)
    s = _session_with_history()
    before = [dict(m) for m in s.messages]
    s.compact_completed_attempts()
    assert s.messages == before


def test_over_budget(monkeypatch):
    s = _session_with_history(n_tool=20, size=5_000)
    monkeypatch.setattr(settings, "num_ctx", 1_000)
    assert s.over_budget()
    monkeypatch.setattr(settings, "num_ctx", 1_000_000)
    assert not s.over_budget()


def test_feedback_digest_short_history_verbatim():
    h = ["[attempt 1] fix A", "[attempt 2] fix B"]
    assert _feedback_digest(h) == "[attempt 1] fix A\n[attempt 2] fix B"


def test_feedback_digest_stubs_older_entries():
    h = [f"[attempt {i}] feedback line\nmore detail" for i in range(1, 6)]
    out = _feedback_digest(h)
    # last two entries are complete
    assert "more detail" in out
    assert out.count("more detail") == 2
    # older ones survive as one-line stubs — nothing silently dropped
    for i in range(1, 6):
        assert f"[attempt {i}]" in out
