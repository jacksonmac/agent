"""Tests for the sqlite run history."""

import sqlite3
import sys

import pytest

from harness import cli, history


def _db(tmp_path):
    return str(tmp_path / "history.db")


def test_record_roundtrip(tmp_path):
    path = _db(tmp_path)
    history.record(path, ts="2026-07-07T10:00:00", goal="build x", model="m",
                   executor_model="coder", reviewer_model=None, passed=True,
                   attempts=2, duration_secs=12.5, run_dir="runs/run_1")
    rows = sqlite3.connect(path).execute(
        "SELECT goal, model, executor_model, reviewer_model, passed, attempts,"
        " duration_secs, run_dir FROM runs").fetchall()
    assert rows == [("build x", "m", "coder", None, 1, 2, 12.5, "runs/run_1")]


def test_print_history_newest_first(tmp_path, capsys):
    path = _db(tmp_path)
    for i in (1, 2):
        history.record(path, ts=f"2026-07-07T10:00:0{i}", goal=f"goal {i}",
                       model="m", executor_model=None, reviewer_model=None,
                       passed=(i == 2), attempts=i, duration_secs=1.0,
                       run_dir=f"runs/run_{i}")
    history.print_history(path)
    out = capsys.readouterr().out
    assert out.index("goal 2") < out.index("goal 1")
    assert "yes" in out and "NO" in out


def test_print_stats_groups_by_executor_fallback(tmp_path, capsys):
    path = _db(tmp_path)
    for passed in (True, True, False):
        history.record(path, ts="t", goal="g", model="base", executor_model="coder",
                       reviewer_model=None, passed=passed, attempts=1,
                       duration_secs=1.0, run_dir="r")
    history.record(path, ts="t", goal="g", model="base", executor_model=None,
                   reviewer_model=None, passed=True, attempts=1,
                   duration_secs=1.0, run_dir="r")
    history.print_stats(path)
    out = capsys.readouterr().out
    coder_line = next(line for line in out.splitlines() if line.startswith("coder"))
    assert "67%" in coder_line
    base_line = next(line for line in out.splitlines() if line.startswith("base"))
    assert "100%" in base_line


def test_empty_db(tmp_path, capsys):
    history.print_history(_db(tmp_path))
    history.print_stats(_db(tmp_path))
    out = capsys.readouterr().out
    assert out.count("(no runs recorded yet)") == 2


def test_cli_history_peek(tmp_path, capsys, monkeypatch):
    path = _db(tmp_path)
    history.record(path, ts="2026-07-07T10:00:00", goal="peeked goal", model="m",
                   executor_model=None, reviewer_model=None, passed=True,
                   attempts=1, duration_secs=1.0, run_dir="r")
    monkeypatch.setattr(history, "db_path", lambda: path)
    monkeypatch.setattr(sys, "argv", ["agent.py", "history"])
    # must dispatch without touching Workspace/RunLog
    from harness.workspace import Workspace
    monkeypatch.setattr(Workspace, "create",
                        classmethod(lambda *a, **kw: pytest.fail("workspace created")))
    cli.main()
    assert "peeked goal" in capsys.readouterr().out
