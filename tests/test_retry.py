"""Tests for transient connection-drop retries in _post_chat — no server."""

import json

import pytest
import requests

from harness import llm
from harness.config import settings


class FakeResp:
    def __init__(self, lines):
        self._lines = [json.dumps(l).encode() for l in lines]
        self.status_code = 200

    def iter_lines(self):
        yield from self._lines

    def raise_for_status(self):
        pass


def _ok_lines(text="ok"):
    return [{"message": {"content": text}},
            {"done": True, "prompt_eval_count": 3, "eval_count": 1}]


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "stream", True)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "had_successful_call", False)


def test_retries_connection_error_then_succeeds(monkeypatch):
    calls = []

    def flaky(payload, stream):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("reset by peer")
        return FakeResp(_ok_lines())

    monkeypatch.setattr(llm, "_request", flaky)
    msg = llm._post_chat({"model": "m", "messages": []})
    assert msg["content"] == "ok"
    assert len(calls) == 2
    assert llm.had_successful_call


def test_reraises_after_all_attempts(monkeypatch):
    calls = []

    def dead(payload, stream):
        calls.append(1)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(llm, "_request", dead)
    with pytest.raises(requests.ConnectionError):
        llm._post_chat({"model": "m", "messages": []})
    assert len(calls) == llm.RETRY_ATTEMPTS
    assert not llm.had_successful_call


def test_retries_midstream_chunked_encoding_error(monkeypatch):
    class Dies(FakeResp):
        def iter_lines(self):
            yield json.dumps({"message": {"content": "par"}}).encode()
            raise requests.exceptions.ChunkedEncodingError("stream died")

    responses = [Dies([]), FakeResp(_ok_lines("whole"))]
    monkeypatch.setattr(llm, "_request", lambda p, s: responses.pop(0))
    msg = llm._post_chat({"model": "m", "messages": []})
    assert msg["content"] == "whole"
