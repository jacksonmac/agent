"""Tests for streamed /api/chat consumption — no server, FakeResp only."""

import json

import pytest

from harness import llm, ui
from harness.config import settings


class FakeResp:
    def __init__(self, lines=None, status_code=200, text="", data=None):
        self._lines = [json.dumps(l).encode() for l in (lines or [])]
        self.status_code = status_code
        self.text = text
        self._data = data

    def iter_lines(self):
        yield from self._lines

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def stream_on(monkeypatch):
    monkeypatch.setattr(settings, "stream", True)
    llm._no_think_models.clear()
    yield
    llm._no_think_models.clear()


def _deltas(*parts, thinking=None, tool_calls=None):
    lines = []
    if thinking:
        lines.append({"message": {"thinking": thinking}})
    for p in parts:
        lines.append({"message": {"content": p}})
    if tool_calls:
        lines.append({"message": {"tool_calls": tool_calls}})
    lines.append({"done": True, "prompt_eval_count": 10, "eval_count": 5})
    return lines


def test_stream_aggregates_message(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "_request",
                        lambda payload, stream: sent.append((dict(payload), stream))
                        or FakeResp(_deltas("a", "b", "c", thinking="hmm",
                                            tool_calls=[{"function": {"name": "t"}}])))
    msg = llm._post_chat({"model": "m", "messages": []})
    assert msg["content"] == "abc"
    assert msg["thinking"] == "hmm"
    assert msg["tool_calls"] == [{"function": {"name": "t"}}]
    assert sent[0][1] is True  # requested as a stream
    assert sent[0][0]["stream"] is True


def test_no_think_retry_under_streaming(monkeypatch):
    responses = [FakeResp(status_code=400, text='"m" does not support thinking'),
                 FakeResp(_deltas("ok"))]
    payloads = []
    monkeypatch.setattr(llm, "_request",
                        lambda payload, stream: payloads.append(dict(payload))
                        or responses.pop(0))
    msg = llm._post_chat({"model": "m", "messages": [], "think": True})
    assert msg["content"] == "ok"
    assert "m" in llm._no_think_models
    assert "think" not in payloads[1]


def test_stream_off_uses_single_json(monkeypatch):
    monkeypatch.setattr(settings, "stream", False)
    monkeypatch.setattr(llm, "_request",
                        lambda payload, stream: FakeResp(
                            data={"message": {"role": "assistant", "content": "hi"},
                                  "prompt_eval_count": 3, "eval_count": 1}))
    msg = llm._post_chat({"model": "m", "messages": []})
    assert msg == {"role": "assistant", "content": "hi"}


def test_tool_calls_only_message(monkeypatch):
    lines = [{"message": {"tool_calls": [{"function": {"name": "x", "arguments": {}}}]}},
             {"done": True}]
    monkeypatch.setattr(llm, "_request", lambda payload, stream: FakeResp(lines))
    msg = llm._post_chat({"model": "m", "messages": []})
    assert msg["content"] == ""
    assert msg["tool_calls"]


def test_plain_mode_progressive_print_no_duplicate(monkeypatch, capsys):
    monkeypatch.setattr(llm, "_request",
                        lambda payload, stream: FakeResp(_deltas("hel", "lo")))
    msg = llm._post_chat({"model": "m", "messages": []})
    ui.answer(msg["content"])
    out = capsys.readouterr().out
    assert out.startswith("hello\n")           # streamed progressively
    assert "(streamed above)" in out           # answer() didn't re-print the body
    assert out.count("hello") == 1


def test_stream_end_cleans_state_on_midstream_error(monkeypatch):
    class Boom(FakeResp):
        def iter_lines(self):
            yield json.dumps({"message": {"content": "par"}}).encode()
            raise ConnectionError("dropped")

    monkeypatch.setattr(llm, "_request", lambda payload, stream: Boom())
    with pytest.raises(ConnectionError):
        llm._post_chat({"model": "m", "messages": []})
    # stream_end ran via finally: next answer() in plain mode is normal again
    ui.answer("later")  # must not raise; flag was consumed by stream_end path
