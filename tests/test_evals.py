"""Sanity tests for the eval suite plumbing (checkers + stats), no LLM."""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "evals"))

from goals import GOALS, GOALS_BY_NAME  # noqa: E402


def _seed(goal, ws):
    for name, content in goal.seed_files.items():
        path = os.path.join(ws, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def test_goal_registry_is_complete():
    assert len(GOALS) == 8
    assert {g.category for g in GOALS} == {"multi_file", "data_processing"}
    for g in GOALS:
        assert g.prompt and callable(g.check)


def test_checkers_fail_untouched_workspace(tmp_path):
    """Seeds alone must never pass — otherwise the benchmark is free points."""
    for goal in GOALS:
        ws = tmp_path / goal.name
        ws.mkdir()
        _seed(goal, str(ws))
        ok, detail = goal.check(str(ws))
        assert not ok, f"{goal.name} passed an untouched workspace: {detail}"


def test_log_parse_checker_accepts_correct_solution(tmp_path):
    goal = GOALS_BY_NAME["log_parse"]
    ws = str(tmp_path)
    _seed(goal, ws)
    with open(os.path.join(ws, "parse_log.py"), "w") as f:
        f.write(
            "import json\n"
            "from collections import Counter\n"
            "errors = warns = 0\n"
            "ips = Counter()\n"
            "for line in open('server.log'):\n"
            "    parts = line.split()\n"
            "    if len(parts) < 4: continue\n"
            "    if parts[2] == 'ERROR': errors += 1\n"
            "    elif parts[2] == 'WARN': warns += 1\n"
            "    ips[parts[3]] += 1\n"
            "json.dump({'error_count': errors, 'warn_count': warns,\n"
            "           'top_ip': ips.most_common(1)[0][0]}, open('summary.json', 'w'))\n")
    ok, detail = goal.check(ws)
    assert ok, detail


def test_stats_from_events(tmp_path):
    import run_evals
    path = tmp_path / "events.jsonl"
    rows = [
        {"event": "llm", "secs": 2.5, "prompt_tokens": 100, "eval_tokens": 10},
        {"event": "llm", "secs": 1.5, "prompt_tokens": 200, "eval_tokens": 20},
        {"event": "attempt", "n": 1, "passed": False},
        {"event": "attempt", "n": 2, "passed": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    stats = run_evals._stats_from_events(str(path))
    assert stats["attempts_used"] == 2
    assert stats["harness_passed"] is True
    assert stats["llm_secs"] == 4.0
    assert stats["prompt_tokens"] == 300
