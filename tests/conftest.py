"""Shared fixtures — most importantly a scripted fake LLM, so loop-level
behavior (plan turn, self-check, best-of) is testable without the Ollama
server."""

import pytest

from harness import llm
from harness.config import settings


@pytest.fixture(autouse=True)
def _no_memory_notes(monkeypatch):
    """Keep the 100+ scripted full-run tests free of the end-of-run memory
    LLM call; memory tests flip it back on explicitly."""
    monkeypatch.setattr(settings, "memory", False)


@pytest.fixture(autouse=True)
def _isolated_history(monkeypatch, tmp_path):
    """Full-run loop tests record history — keep it out of the real runs/history.db."""
    from harness import history
    monkeypatch.setattr(history, "db_path", lambda: str(tmp_path / "history.db"))


@pytest.fixture(autouse=True)
def _yolo_permissions():
    """pytest stdin is non-TTY, so the permission gate would auto-deny every
    run_python/run_shell in the tool tests; permission tests opt out locally."""
    from harness import permissions
    permissions.configure(yolo=True)
    yield
    permissions.configure(yolo=True)


class ScriptedLLM:
    """Queue of canned Ollama 'message' dicts returned by _post_chat in
    order. Records every payload sent, for asserting on what the model saw."""

    def __init__(self):
        self.replies: list[dict] = []
        self.payloads: list[dict] = []

    def queue_text(self, content: str) -> None:
        self.replies.append({"role": "assistant", "content": content})

    def queue_tool_call(self, name: str, arguments: dict) -> None:
        self.replies.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": name,
                                                          "arguments": arguments}}]})

    def _post_chat(self, payload: dict, label: str = "llm") -> dict:
        self.payloads.append(payload)
        if not self.replies:
            raise AssertionError(
                f"scripted LLM ran out of replies (label={label}); "
                f"last user msg: {str(payload['messages'][-1])[:200]}")
        return self.replies.pop(0)


@pytest.fixture
def scripted_llm(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(llm, "_post_chat", fake._post_chat)
    return fake
