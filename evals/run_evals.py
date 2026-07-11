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
             reviewer_model: str | None = None, extra_args: list | None = None) -> dict:
    ws_dir = os.path.join(out_root, goal.name, "workspace")
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

    result = {"goal": goal.name, "category": goal.category, "wall_secs": wall,
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


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(r["checker_passed"] for r in results)
    return {
        "goals": n,
        "checker_pass_rate": round(passed / n, 3) if n else 0,
        "checker_passed": passed,
        "mean_attempts": round(sum(r["attempts_used"] or 0 for r in results) / n, 2) if n else 0,
        "total_wall_secs": round(sum(r["wall_secs"] for r in results), 1),
        "total_llm_secs": round(sum(r["llm_secs"] or 0 for r in results), 1),
    }


def print_table(results: list[dict]) -> None:
    hdr = f"{'goal':<16} {'check':<6} {'harness':<8} {'att':<4} {'wall s':<8} detail"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for r in results:
        print(f"{r['goal']:<16} {'PASS' if r['checker_passed'] else 'fail':<6} "
              f"{str(r['harness_passed']):<8} {str(r['attempts_used'] or '-'):<4} "
              f"{r['wall_secs']:<8} {r['checker_detail'][:60]}")


def print_comparison(before: dict, after_results: list[dict], after_summary: dict) -> None:
    prev = {r["goal"]: r for r in before.get("results", [])}
    print(f"\n─── comparison vs {before.get('label', '?')} ───")
    for r in after_results:
        p = prev.get(r["goal"])
        if not p:
            continue
        def mark(now, then):
            return "=" if now == then else ("improved" if now and not then else "REGRESSED")
        print(f"  {r['goal']:<16} check {str(p['checker_passed']):<5} -> "
              f"{str(r['checker_passed']):<5} {mark(r['checker_passed'], p['checker_passed'])}")
    b = before.get("summary", {})
    print(f"  pass rate  {b.get('checker_pass_rate')} -> {after_summary['checker_pass_rate']}")
    print(f"  mean attempts  {b.get('mean_attempts')} -> {after_summary['mean_attempts']}")
    print(f"  total wall s  {b.get('total_wall_secs')} -> {after_summary['total_wall_secs']}")


def main():
    p = argparse.ArgumentParser(description="harness eval suite")
    p.add_argument("--goals", default=None,
                   help="comma-separated goal names (default: all)")
    p.add_argument("--attempts", type=int, default=3,
                   help="max attempts per goal (default 3; keep it constant across runs)")
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

    results = []
    for i, goal in enumerate(goals, 1):
        print(f"[eval {i}/{len(goals)}] {goal.name} ...", flush=True)
        r = run_goal(goal, args.attempts, args.timeout, args.model, args.url,
                     out_root, reviewer_model=args.reviewer_model,
                     extra_args=args.agent_args.split() if args.agent_args else None)
        status = "PASS" if r["checker_passed"] else f"fail ({r['checker_detail'][:80]})"
        print(f"[eval {i}/{len(goals)}] {goal.name}: {status}  "
              f"[{r['wall_secs']}s, attempts={r['attempts_used']}]", flush=True)
        results.append(r)

    summary = summarize(results)
    out_path = os.path.join(HERE, f"results_{args.label}.json")
    with open(out_path, "w") as f:
        json.dump({"label": args.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "attempts": args.attempts, "model": args.model,
                   "summary": summary, "results": results}, f, indent=2)

    print_table(results)
    print(f"\nsummary: {json.dumps(summary)}")
    print(f"results written to {out_path}")

    if args.compare:
        with open(args.compare) as f:
            print_comparison(json.load(f), results, summary)


if __name__ == "__main__":
    main()
