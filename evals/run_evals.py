"""Run the benchmark goals through the harness and score them.

Usage (from the repo root, with the venv python so the harness deps resolve):
    venv/bin/python evals/run_evals.py --label baseline
    venv/bin/python evals/run_evals.py --label after --compare evals/results_baseline.json
    venv/bin/python evals/run_evals.py --goals csv_cleanup,log_parse --attempts 2

Each goal gets a fresh seeded workspace; the harness runs as a subprocess
(python3 agent.py -g ... --workspace ...); the goal's checker then judges the
workspace independently of the harness's own reviewer. Per-goal stats come
from the run's events.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from goals import GOALS, GOALS_BY_NAME  # noqa: E402

RUN_DIR_PAT = re.compile(r"^\[run\] (.+)$", re.MULTILINE)


def run_goal(goal, attempts: int, timeout: int, model: str | None,
             url: str | None, out_root: str,
             reviewer_model: str | None = None, extra_args: list | None = None,
             repeat: int = 1) -> dict:
    # each repeat gets its own workspace: sharing one would let repeat 2 start
    # from the files repeat 1 produced, which measures nothing
    ws_dir = os.path.join(out_root, goal.name, f"rep_{repeat}", "workspace")
    os.makedirs(ws_dir, exist_ok=True)
    for name, content in goal.seed_files.items():
        path = os.path.join(ws_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    # --yolo is mandatory here: eval subprocesses have non-TTY stdin, so the
    # permission gate would auto-deny every run_shell/run_python call and the
    # benchmark would measure "agent forbidden from running code" instead of
    # agent quality
    cmd = [sys.executable, os.path.join(REPO, "agent.py"),
           "-g", goal.prompt, "--workspace", ws_dir, "--attempts", str(attempts),
           "--yolo"]
    if model:
        cmd += ["--model", model]
    if url:
        cmd += ["--url", url]
    if reviewer_model:
        cmd += ["--reviewer-model", reviewer_model]
    if extra_args:
        cmd += extra_args

    # the agent's run_shell/run_python tools resolve python3/pytest from PATH;
    # prepend the venv bin so those calls see the same interpreter the
    # checkers use (system python3 has no pytest on this machine)
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=REPO, env=env)
        timed_out = False
        stdout = proc.stdout
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    wall = round(time.time() - t0, 1)

    result = {"goal": goal.name, "category": goal.category, "repeat": repeat,
              "wall_secs": wall,
              "timed_out": timed_out, "checker_passed": False, "checker_detail": "",
              "harness_passed": None, "attempts_used": None,
              "llm_secs": None, "prompt_tokens": None, "eval_tokens": None}

    try:
        ok, detail = goal.check(ws_dir)
    except Exception as e:  # a crashing checker is a fail, not an abort
        ok, detail = False, f"checker raised {type(e).__name__}: {e}"
    result["checker_passed"], result["checker_detail"] = ok, detail

    m = RUN_DIR_PAT.search(stdout or "")
    if m:
        result.update(_stats_from_events(os.path.join(m.group(1), "events.jsonl")))
        result["run_dir"] = m.group(1)
    return result


def _stats_from_events(path: str) -> dict:
    stats = {"harness_passed": False, "attempts_used": 0,
             "llm_secs": 0.0, "prompt_tokens": 0, "eval_tokens": 0}
    try:
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "attempt":
                    stats["attempts_used"] += 1
                    if ev.get("passed"):
                        stats["harness_passed"] = True
                elif ev.get("event") == "llm":
                    stats["llm_secs"] += ev.get("secs") or 0
                    stats["prompt_tokens"] += ev.get("prompt_tokens") or 0
                    stats["eval_tokens"] += ev.get("eval_tokens") or 0
    except OSError:
        return {}
    stats["llm_secs"] = round(stats["llm_secs"], 1)
    return stats


def per_goal(results: list[dict]) -> dict:
    """goal name -> {runs, passed, pass_rate, …}. With one repeat this is the
    old per-goal row; with several it is the rate that makes a comparison
    mean anything."""
    out: dict[str, dict] = {}
    for r in results:
        g = out.setdefault(r["goal"], {"runs": 0, "passed": 0, "wall_secs": 0.0,
                                       "attempts": 0, "tokens": 0})
        g["runs"] += 1
        g["passed"] += bool(r["checker_passed"])
        g["wall_secs"] += r["wall_secs"]
        g["attempts"] += r["attempts_used"] or 0
        g["tokens"] += (r["prompt_tokens"] or 0) + (r["eval_tokens"] or 0)
    for g in out.values():
        n = g["runs"]
        g["pass_rate"] = round(g["passed"] / n, 3)
        g["mean_wall_secs"] = round(g["wall_secs"] / n, 1)
        g["mean_attempts"] = round(g["attempts"] / n, 2)
        g["mean_tokens"] = round(g["tokens"] / n)
    return out


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(r["checker_passed"] for r in results)
    goals = per_goal(results)
    return {
        # "goals" counts distinct goals; "runs" counts executions. They are
        # equal only at --repeat 1, which is why the old files can still be
        # compared against.
        "goals": len(goals),
        "runs": n,
        "checker_pass_rate": round(passed / n, 3) if n else 0,
        "checker_passed": passed,
        "mean_attempts": round(sum(r["attempts_used"] or 0 for r in results) / n, 2) if n else 0,
        "total_wall_secs": round(sum(r["wall_secs"] for r in results), 1),
        "total_llm_secs": round(sum(r["llm_secs"] or 0 for r in results), 1),
        "per_goal": goals,
    }


def print_table(results: list[dict]) -> None:
    goals = per_goal(results)
    repeats = max((g["runs"] for g in goals.values()), default=1)
    if repeats == 1:  # unchanged single-run view
        hdr = f"{'goal':<16} {'check':<6} {'harness':<8} {'att':<4} {'wall s':<8} detail"
        print("\n" + hdr + "\n" + "-" * len(hdr))
        for r in results:
            print(f"{r['goal']:<16} {'PASS' if r['checker_passed'] else 'fail':<6} "
                  f"{str(r['harness_passed']):<8} {str(r['attempts_used'] or '-'):<4} "
                  f"{r['wall_secs']:<8} {r['checker_detail'][:60]}")
        return
    hdr = (f"{'goal':<16} {'passed':<8} {'rate':<6} {'att':<5} {'wall s':<8} "
           f"{'tokens':<9} first failure")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for name, g in goals.items():
        fail = next((r["checker_detail"] for r in results
                     if r["goal"] == name and not r["checker_passed"]), "")
        # built outside the f-string: nesting the same quote type is 3.12+
        # syntax and this repo targets 3.10
        ratio = "{}/{}".format(g["passed"], g["runs"])
        print(f"{name:<16} {ratio:<8} {g['pass_rate']:<6} "
              f"{g['mean_attempts']:<5} {g['mean_wall_secs']:<8} "
              f"{g['mean_tokens']:<9} {fail[:40]}")


def _flaky(goals: dict) -> list[str]:
    """Goals that both passed and failed across their repeats. These are the
    reason one run per goal cannot be compared: they change answer on their
    own, with nothing about the harness changing at all."""
    return [n for n, g in goals.items() if 0 < g["passed"] < g["runs"]]


def print_comparison(before: dict, after_results: list[dict], after_summary: dict) -> None:
    prev = per_goal(before.get("results", []))
    now = per_goal(after_results)
    print(f"\n─── comparison vs {before.get('label', '?')} ───")
    for name, g in now.items():
        p = prev.get(name)
        if not p:
            continue
        delta = g["pass_rate"] - p["pass_rate"]
        arrow = "=" if abs(delta) < 1e-9 else ("+" if delta > 0 else "-")
        print(f"  {name:<16} {p['passed']}/{p['runs']} -> {g['passed']}/{g['runs']}"
              f"  {arrow}{abs(delta):.2f}")
    b = before.get("summary", {})
    print(f"  pass rate  {b.get('checker_pass_rate')} -> {after_summary['checker_pass_rate']}")
    print(f"  mean attempts  {b.get('mean_attempts')} -> {after_summary['mean_attempts']}")
    print(f"  total wall s  {b.get('total_wall_secs')} -> {after_summary['total_wall_secs']}")

    # The honesty line. Single-run-per-goal comparisons used to print
    # "improved"/"REGRESSED" for what may be one coin toss; say so instead.
    runs = min(min((g["runs"] for g in prev.values()), default=1),
               min((g["runs"] for g in now.values()), default=1))
    flaky = sorted(set(_flaky(prev)) | set(_flaky(now)))
    if flaky:
        print(f"\n  flaky under repetition (passed AND failed with no harness "
              f"change): {', '.join(flaky)}")
    if runs < 2:
        print("\n  NOTE: one run per goal. A goal that flipped may simply have "
              "resampled — this comparison cannot separate a real change from "
              "noise. Re-run both sides with --repeat 5 or more.")


def main():
    p = argparse.ArgumentParser(description="harness eval suite")
    p.add_argument("--goals", default=None,
                   help="comma-separated goal names (default: all)")
    p.add_argument("--attempts", type=int, default=3,
                   help="max attempts per goal (default 3; keep it constant across runs)")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="run every goal N times (default 1). The executor samples "
                        "at temperature 0.7, so a single run per goal cannot tell a "
                        "real change from resampling; N>=5 is where a comparison "
                        "starts to mean something")
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds allowed per goal (default 1800)")
    p.add_argument("--model", default=None, help="passed through to agent.py")
    p.add_argument("--url", default=None, help="passed through to agent.py")
    p.add_argument("--reviewer-model", default=None,
                   help="passed through to agent.py (for reviewer A/B runs)")
    p.add_argument("--agent-args", default=None,
                   help="extra flags for agent.py, e.g. --agent-args='--best-of 2'")
    p.add_argument("--label", default=time.strftime("%Y%m%d_%H%M%S"),
                   help="name for this results file")
    p.add_argument("--compare", default=None,
                   help="previous results_*.json to diff against")
    args = p.parse_args()

    if args.goals:
        missing = [n for n in args.goals.split(",") if n not in GOALS_BY_NAME]
        if missing:
            raise SystemExit(f"unknown goal(s): {', '.join(missing)} "
                             f"(known: {', '.join(GOALS_BY_NAME)})")
        goals = [GOALS_BY_NAME[n] for n in args.goals.split(",")]
    else:
        goals = GOALS

    out_root = os.path.join(HERE, "eval_runs", args.label)
    os.makedirs(out_root, exist_ok=True)

    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")

    total = len(goals) * args.repeat
    if args.repeat > 1:
        print(f"{len(goals)} goals × {args.repeat} repeats = {total} runs", flush=True)

    results = []
    n = 0
    # repeat-major, not goal-major: an interrupted run then still holds one
    # complete pass over every goal rather than all repeats of the first few
    for rep in range(1, args.repeat + 1):
        for goal in goals:
            n += 1
            tag = f"[eval {n}/{total}]"
            rep_s = f" rep {rep}/{args.repeat}" if args.repeat > 1 else ""
            print(f"{tag} {goal.name}{rep_s} ...", flush=True)
            r = run_goal(goal, args.attempts, args.timeout, args.model, args.url,
                         out_root, reviewer_model=args.reviewer_model,
                         extra_args=args.agent_args.split() if args.agent_args else None,
                         repeat=rep)
            status = "PASS" if r["checker_passed"] else f"fail ({r['checker_detail'][:80]})"
            print(f"{tag} {goal.name}{rep_s}: {status}  "
                  f"[{r['wall_secs']}s, attempts={r['attempts_used']}]", flush=True)
            results.append(r)

    summary = summarize(results)
    out_path = os.path.join(HERE, f"results_{args.label}.json")
    with open(out_path, "w") as f:
        json.dump({"label": args.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "attempts": args.attempts, "repeat": args.repeat,
                   "model": args.model,
                   "summary": summary, "results": results}, f, indent=2)

    print_table(results)
    print(f"\nsummary: {json.dumps(summary)}")
    print(f"results written to {out_path}")

    if args.compare:
        with open(args.compare) as f:
            print_comparison(json.load(f), results, summary)


if __name__ == "__main__":
    main()
