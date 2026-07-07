"""Shared fixtures — most importantly a scripted fake LLM, so loop-level
behavior (plan turn, self-check, best-of) is testable without the Ollama
server."""

import pytest

from harness import llm


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
