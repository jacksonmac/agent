"""Tests for session resume: _load_resume_context over previous run artifacts,
plus the --resume flag's target resolution and goal recovery."""

import json

import pytest

from harness import history
from harness.cli import _load_resume_context, _previous_goal, _resolve_resume


def _seed_run(tmp_path, passed=True, feedback="fix the tests"):
    run = tmp_path / "run_prev"
    ws = run / "workspace"
    ws.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        json.dumps({"event": "run_start", "goal": "build a calculator"}) + "\n"
        + json.dumps({"event": "attempt", "n": 1, "passed": passed}) + "\n")
    (run / "attempt_history.json").write_text(json.dumps([
        {"attempt": 1, "passed": passed, "verdict": feedback}]))
    (run / ("final_output.txt" if passed else "final_output_UNVERIFIED.txt")) \
        .write_text("answer")
    return str(ws)


def test_summary_from_full_artifacts(tmp_path):
    ws = _seed_run(tmp_path, passed=True)
    out = _load_resume_context(ws, files=["calc.py", "test_calc.py"])
    assert "PREVIOUS SESSION" in out
    assert "build a calculator" in out
    assert "PASSED on attempt 1" in out
    assert "calc.py" in out
    assert "Build on this work" in out


def test_failed_run_includes_feedback(tmp_path):
    ws = _seed_run(tmp_path, passed=False, feedback="missing division support")
    out = _load_resume_context(ws, files=[])
    assert "FAILED after 1 attempt" in out
    assert "missing division support" in out


def test_no_artifacts_returns_empty(tmp_path):
    ws = tmp_path / "fresh" / "workspace"
    ws.mkdir(parents=True)
    assert _load_resume_context(str(ws), files=["a.py"]) == ""


def test_corrupt_artifacts_partial_summary(tmp_path):
    run = tmp_path / "run_prev"
    ws = run / "workspace"
    ws.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        "not json at all\n"
        + json.dumps({"event": "run_start", "goal": "the goal"}) + "\n")
    (run / "attempt_history.json").write_text("{broken json")
    out = _load_resume_context(str(ws), files=[])
    assert "the goal" in out  # events still parsed despite bad line + bad history


def test_summary_capped(tmp_path):
    ws = _seed_run(tmp_path, passed=False, feedback="x" * 10_000)
    out = _load_resume_context(ws, files=[])
    assert len(out) <= 3_000 + 100  # truncate_middle marker slack
    assert "truncated" in out


def test_file_list_capped_at_30(tmp_path):
    ws = _seed_run(tmp_path)
    out = _load_resume_context(ws, files=[f"f{i}.py" for i in range(50)])
    assert "30 shown of 50" in out


# ── --resume: goal recovery + target resolution ─────────────────────

def test_previous_goal_recovered(tmp_path):
    ws = _seed_run(tmp_path)
    assert _previous_goal(ws) == "build a calculator"
    fresh = tmp_path / "fresh" / "workspace"
    fresh.mkdir(parents=True)
    assert _previous_goal(str(fresh)) == ""


def _seed_history(tmp_path, monkeypatch, n=2):
    """n fake run dirs (each with a workspace/) recorded in a temp history db."""
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(history, "db_path", lambda: db)
    dirs = []
    for i in range(1, n + 1):
        run = tmp_path / f"run_{i}"
        (run / "workspace").mkdir(parents=True)
        history.record(db, ts=f"t{i}", goal=f"goal {i}", model="m",
                       executor_model=None, reviewer_model=None, passed=False,
                       attempts=1, duration_secs=1.0, run_dir=str(run))
        dirs.append(str(run))
    return dirs


def test_resolve_resume_latest_and_id(tmp_path, monkeypatch):
    run1, run2 = _seed_history(tmp_path, monkeypatch)
    assert _resolve_resume("latest") == f"{run2}/workspace"
    assert _resolve_resume("1") == f"{run1}/workspace"


def test_resolve_resume_path_forms(tmp_path):
    run = tmp_path / "run_x"
    ws = run / "workspace"
    ws.mkdir(parents=True)
    assert _resolve_resume(str(run)) == str(ws)   # run dir → its workspace
    assert _resolve_resume(str(ws)) == str(ws)    # workspace dir → itself


def test_resolve_resume_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "db_path",
                        lambda: str(tmp_path / "empty.db"))
    with pytest.raises(SystemExit, match="no previous runs"):
        _resolve_resume("latest")
    with pytest.raises(SystemExit, match="no run with id 9"):
        _resolve_resume("9")
    with pytest.raises(SystemExit, match="no workspace directory"):
        _resolve_resume(str(tmp_path / "nope"))


def test_history_listing_shows_ids_and_hint(tmp_path, monkeypatch, capsys):
    _seed_history(tmp_path, monkeypatch)
    history.print_history(history.db_path())
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("id")
    assert "--resume <id>" in out
