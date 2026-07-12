"""Tests for session resume: _load_resume_context over previous run artifacts."""

import json

from harness.cli import _load_resume_context


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
