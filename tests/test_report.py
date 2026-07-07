"""Tests for the HTML report renderer."""

import json
import os

import pytest

from harness.report import render, write_report


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run_x"
    d.mkdir()
    events = [
        {"ts": "2026-07-05T10:00:00", "event": "run_start",
         "goal": "make <script>alert(1)</script> safe", "task": "same",
         "model": "gemma4:26b", "reviewer": "big:32b", "max_attempts": 3,
         "criteria": ["file exists"], "best_of": 2},
        {"ts": "2026-07-05T10:00:05", "event": "llm", "label": "executor",
         "secs": 4.2, "prompt_tokens": 900, "eval_tokens": 120},
        {"ts": "2026-07-05T10:00:06", "event": "tool", "name": "write_file",
         "args": "{}", "ok": True, "result_chars": 30},
        {"ts": "2026-07-05T10:00:08", "event": "candidate", "i": 1,
         "passed": False, "criteria_met": 1, "files": ["a.txt"]},
        {"ts": "2026-07-05T10:00:09", "event": "candidate", "i": 2,
         "passed": True, "criteria_met": 2, "files": ["a.txt"]},
        {"ts": "2026-07-05T10:00:10", "event": "candidate_selected",
         "winner": "candidate_2", "passed": True},
        {"ts": "2026-07-05T10:00:12", "event": "llm", "label": "reviewer",
         "secs": 2.0, "prompt_tokens": 500, "eval_tokens": 60},
        {"ts": "2026-07-05T10:00:15", "event": "attempt", "n": 1, "passed": True,
         "stalled": False, "no_tools": False, "files": ["a.txt"], "feedback": ""},
    ]
    with open(d / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    with open(d / "attempt_history.json", "w") as f:
        json.dump([{"attempt": 1, "output": "did it <b>boldly</b>",
                    "files": ["a.txt"], "passed": True, "verdict": "passed",
                    "criteria": [{"criterion": "file exists", "met": True,
                                  "note": "saw a.txt"}]}], f)
    with open(d / "final_output.txt", "w") as f:
        f.write("the final answer")
    return str(d)


def test_report_renders_and_escapes(run_dir):
    html = render(run_dir)
    assert "<script>alert(1)</script>" not in html      # goal is escaped
    assert "&lt;script&gt;" in html
    assert "did it &lt;b&gt;boldly&lt;/b&gt;" in html   # outputs escaped too
    assert "PASSED" in html
    assert "the final answer" in html


def test_report_shows_candidates_and_stats(run_dir):
    html = render(run_dir)
    assert "Best-of candidates" in html
    assert "promoted" in html
    assert "executor" in html and "reviewer" in html    # time breakdown rows
    assert "write_file" in html                          # tool usage table


def test_write_report_creates_file(run_dir):
    out = write_report(run_dir)
    assert out and os.path.exists(out)
    assert open(out).read().startswith("<!DOCTYPE html>")


def test_write_report_never_raises_on_garbage(tmp_path):
    d = tmp_path / "empty_run"
    d.mkdir()
    (d / "events.jsonl").write_text("not json at all\n")
    out = write_report(str(d))          # renders a minimal page, or None —
    assert out is None or os.path.exists(out)  # either way: no exception
