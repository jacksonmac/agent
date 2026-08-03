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


# ─── --repeat: goals become pass rates, not bits ────────────────────

def _row(goal, repeat, passed, wall=10.0, attempts=1, ptok=100, etok=10):
    return {"goal": goal, "category": "data", "repeat": repeat,
            "wall_secs": wall, "timed_out": False, "checker_passed": passed,
            "checker_detail": "" if passed else "assert failed",
            "harness_passed": passed, "attempts_used": attempts,
            "llm_secs": 1.0, "prompt_tokens": ptok, "eval_tokens": etok}


def test_per_goal_aggregates_repeats():
    import run_evals
    rows = [_row("csv_cleanup", 1, True), _row("csv_cleanup", 2, False),
            _row("csv_cleanup", 3, True), _row("log_parse", 1, True)]
    g = run_evals.per_goal(rows)
    assert g["csv_cleanup"]["runs"] == 3
    assert g["csv_cleanup"]["passed"] == 2
    assert g["csv_cleanup"]["pass_rate"] == round(2 / 3, 3)
    assert g["log_parse"]["pass_rate"] == 1.0


def test_summary_separates_goals_from_runs():
    import run_evals
    rows = [_row("a", 1, True), _row("a", 2, False), _row("b", 1, True)]
    s = run_evals.summarize(rows)
    assert s["goals"] == 2 and s["runs"] == 3      # not the same number anymore
    assert s["checker_passed"] == 2
    assert s["per_goal"]["a"]["pass_rate"] == 0.5


def test_summary_shape_unchanged_at_one_repeat():
    """Old results files must stay comparable: the keys print_comparison
    reads are still present and still mean the same thing."""
    import run_evals
    rows = [_row("a", 1, True), _row("b", 1, False)]
    s = run_evals.summarize(rows)
    assert s["goals"] == s["runs"] == 2
    assert s["checker_pass_rate"] == 0.5
    for key in ("mean_attempts", "total_wall_secs", "total_llm_secs"):
        assert key in s


def test_flaky_goals_are_those_that_both_pass_and_fail():
    import run_evals
    g = run_evals.per_goal([_row("steady", 1, True), _row("steady", 2, True),
                            _row("flip", 1, True), _row("flip", 2, False)])
    assert run_evals._flaky(g) == ["flip"]


def test_comparison_warns_when_one_run_per_goal(capsys):
    import run_evals
    before = {"label": "baseline", "results": [_row("a", 1, False)],
              "summary": run_evals.summarize([_row("a", 1, False)])}
    after = [_row("a", 1, True)]
    run_evals.print_comparison(before, after, run_evals.summarize(after))
    out = capsys.readouterr().out
    assert "0/1 -> 1/1" in out
    assert "cannot separate a real change from noise" in out
    assert "REGRESSED" not in out and "improved" not in out


def test_comparison_reports_rate_delta_with_repeats(capsys):
    import run_evals
    b = [_row("a", i, i <= 1) for i in range(1, 5)]     # 1/4
    a = [_row("a", i, i <= 3) for i in range(1, 5)]     # 3/4
    run_evals.print_comparison(
        {"label": "base", "results": b, "summary": run_evals.summarize(b)},
        a, run_evals.summarize(a))
    out = capsys.readouterr().out
    assert "1/4 -> 3/4" in out and "+0.50" in out
    assert "cannot separate" not in out                 # enough runs to bother


def test_repeat_tables_render(capsys):
    import run_evals
    run_evals.print_table([_row("a", 1, True), _row("a", 2, False)])
    out = capsys.readouterr().out
    assert "1/2" in out and "assert failed" in out
    run_evals.print_table([_row("a", 1, True)])         # single-run view intact
    assert "PASS" in capsys.readouterr().out


def test_repeats_get_separate_workspaces(tmp_path, monkeypatch):
    """Sharing one workspace would let repeat 2 start from repeat 1's files."""
    import run_evals
    seen = []

    class FakeProc:
        stdout = ""
        returncode = 0

    def fake_run(cmd, **kw):
        seen.append(cmd[cmd.index("--workspace") + 1])
        return FakeProc()

    monkeypatch.setattr(run_evals.subprocess, "run", fake_run)
    goal = GOALS[0]
    for rep in (1, 2):
        run_evals.run_goal(goal, 1, 60, None, None, str(tmp_path), repeat=rep)
    assert len(set(seen)) == 2
    assert "rep_1" in seen[0] and "rep_2" in seen[1]


def _nested_same_quote_fstrings(source: str) -> list:
    """f-strings that reuse their own quote character inside a replacement
    field. Legal from 3.12 (PEP 701), a SyntaxError on 3.10/3.11 — and
    `ast.parse(feature_version=(3, 10))` does NOT reject it, because what
    changed was the tokenizer, not the grammar."""
    import ast
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        seg = ast.get_source_segment(source, node) or ""
        body = seg.lstrip("fFrRbB")
        if body[:1] not in ("'", '"'):
            continue
        quote = body[:1]
        # Only the replacement fields matter. Checking the whole literal
        # would flag implicit concatenation (`f"…" f"…"`), whose boundary
        # quotes are legal everywhere. Nested f-strings are separate
        # JoinedStr nodes, so this walk reaches them on their own terms.
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            field = ast.get_source_segment(source, part.value) or ""
            if quote in field:
                hits.append(seg)
                break
    return hits


def test_no_312_only_fstrings_in_the_eval_runner():
    """The repo advertises Python 3.10+, so the runner must not use f-string
    syntax that only parses from 3.12."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "evals" / "run_evals.py").read_text()
    assert _nested_same_quote_fstrings(src) == []


def test_the_fstring_check_actually_detects_the_pattern():
    """Guard the guard. The first version of this test used
    ast.parse(feature_version=(3, 10)), which accepts the bad syntax happily
    and therefore tested nothing at all."""
    bad = 'g = {"passed": 1, "runs": 2}\n' + \
          'print(f"{f' + chr(39) + '{g[' + chr(39) + 'passed' + chr(39) + \
          ']}' + chr(39) + ':<8}")\n'
    assert _nested_same_quote_fstrings(bad), "the check missed a known-bad f-string"
    good = 'g = {"passed": 1}\nratio = "{}".format(g["passed"])\nprint(f"{ratio:<8}")\n'
    assert _nested_same_quote_fstrings(good) == []
