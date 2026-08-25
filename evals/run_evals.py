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
import math
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from goals import GOALS, GOALS_BY_NAME  # noqa: E402

RUN_DIR_PAT = re.compile(r"^\[run\] (.+)$", re.MULTILINE)


ARM_KEYS = {"flags", "prompts", "description"}


def _unknown_prompt_names(mapping: dict) -> set:
    """Validate override names against harness.prompts up front, so a typo
    fails when the experiment is loaded rather than 8 hours in."""
    if not mapping:
        return set()
    sys.path.insert(0, REPO)
    from harness import prompts as prompts_mod
    return set(mapping) - set(prompts_mod._overridable())


def load_experiment(path: str) -> dict:
    """Parse an experiment file into {name, arms: {arm: {flags: [...]}}}.

    Validation is strict and names the offending key, following policy.py:
    an experiment that silently ignores a typo'd arm would report a
    difference between two identical arms and waste a night proving it.
    """
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be an object")
    unknown = set(raw) - {"name", "arms"}
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {sorted(unknown)}")
    arms = raw.get("arms")
    if not isinstance(arms, dict) or len(arms) < 2:
        raise ValueError(f"{path}: 'arms' must be an object with at least two "
                         f"arms — one arm is not a comparison")
    out = {}
    for name, spec in arms.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{path}: arm '{name}' must be an object")
        bad = set(spec) - ARM_KEYS
        if bad:
            raise ValueError(f"{path}: arm '{name}' has unknown key(s) "
                             f"{sorted(bad)} (allowed: {sorted(ARM_KEYS)})")
        flags = spec.get("flags", [])
        if not isinstance(flags, list) or any(not isinstance(f, str) for f in flags):
            raise ValueError(f"{path}: arm '{name}': 'flags' must be a list of strings")
        prompts = spec.get("prompts") or {}
        if not isinstance(prompts, dict) or \
                any(not isinstance(v, str) for v in prompts.values()):
            raise ValueError(f"{path}: arm '{name}': 'prompts' must map "
                             f"template names to strings")
        unknown = _unknown_prompt_names(prompts)
        if unknown:
            # a typo'd name would leave this arm running the stock prompt,
            # and the experiment would report a difference between two
            # identical configurations
            raise ValueError(f"{path}: arm '{name}': unknown prompt "
                             f"template(s) {sorted(unknown)}")
        out[name] = {"flags": list(flags), "prompts": dict(prompts),
                     "description": spec.get("description", "")}
    return {"name": raw.get("name", os.path.basename(path)), "arms": out}


def run_goal(goal, attempts: int, timeout: int, model: str | None,
             url: str | None, out_root: str,
             reviewer_model: str | None = None, extra_args: list | None = None,
             repeat: int = 1, arm: str = "",
             prompt_overrides: dict | None = None) -> dict:
    # each repeat gets its own workspace: sharing one would let repeat 2 start
    # from the files repeat 1 produced, which measures nothing
    parts = [out_root, goal.name] + ([arm] if arm else []) + [f"rep_{repeat}",
                                                             "workspace"]
    ws_dir = os.path.join(*parts)
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
    if prompt_overrides:
        # one file per (goal, repeat, arm), beside that run's workspace, so a
        # results directory records exactly which prompts its arm ran with
        ov_path = os.path.join(os.path.dirname(ws_dir), "prompt_overrides.json")
        with open(ov_path, "w") as f:
            json.dump(prompt_overrides, f, indent=2)
        env["AGENT_PROMPT_OVERRIDES"] = ov_path
    else:
        env.pop("AGENT_PROMPT_OVERRIDES", None)

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
              "llm_secs": None, "prompt_tokens": None, "eval_tokens": None,
              "tool_calls": None, "tool_errors": None, "tool_repeats": None,
              "arm": arm}

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
             "llm_secs": 0.0, "prompt_tokens": 0, "eval_tokens": 0,
             # tool efficiency: how much of the work was wasted motion.
             # A change can leave the pass rate alone and still halve the
             # flailing, which is the thing the loop banner shows live and
             # nothing has ever recorded.
             "tool_calls": 0, "tool_errors": 0, "tool_repeats": 0}
    seen: set = set()
    try:
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event")
                if kind == "attempt":
                    stats["attempts_used"] += 1
                    if ev.get("passed"):
                        stats["harness_passed"] = True
                elif kind == "llm":
                    stats["llm_secs"] += ev.get("secs") or 0
                    stats["prompt_tokens"] += ev.get("prompt_tokens") or 0
                    stats["eval_tokens"] += ev.get("eval_tokens") or 0
                elif kind == "tool":
                    stats["tool_calls"] += 1
                    if ev.get("ok") is False:
                        stats["tool_errors"] += 1
                    # the args are already capped to 500 chars in the event,
                    # so identical calls compare equal without re-reading
                    sig = (ev.get("name"), ev.get("args"))
                    if sig in seen:
                        stats["tool_repeats"] += 1
                    seen.add(sig)
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
                                       "attempts": 0, "tokens": 0,
                                       "tool_calls": 0, "tool_errors": 0,
                                       "tool_repeats": 0})
        g["runs"] += 1
        g["passed"] += bool(r["checker_passed"])
        g["wall_secs"] += r["wall_secs"]
        g["attempts"] += r["attempts_used"] or 0
        g["tokens"] += (r["prompt_tokens"] or 0) + (r["eval_tokens"] or 0)
        for k in ("tool_calls", "tool_errors", "tool_repeats"):
            g[k] += r.get(k) or 0
    for g in out.values():
        n = g["runs"]
        calls = g["tool_calls"]
        g["pass_rate"] = round(g["passed"] / n, 3)
        g["mean_wall_secs"] = round(g["wall_secs"] / n, 1)
        g["mean_attempts"] = round(g["attempts"] / n, 2)
        g["mean_tokens"] = round(g["tokens"] / n)
        g["mean_tool_calls"] = round(calls / n, 1)
        # shares of calls, not of runs: "what fraction of the work was
        # wasted" is the comparable number across arms of different length
        g["tool_error_rate"] = round(g["tool_errors"] / calls, 3) if calls else 0
        g["tool_repeat_rate"] = round(g["tool_repeats"] / calls, 3) if calls else 0
    return out


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(r["checker_passed"] for r in results)
    goals = per_goal(results)
    tool_calls = sum(r.get("tool_calls") or 0 for r in results)
    tool_errors = sum(r.get("tool_errors") or 0 for r in results)
    tool_repeats = sum(r.get("tool_repeats") or 0 for r in results)
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
        "tool_calls": tool_calls,
        "tool_error_rate": round(tool_errors / tool_calls, 3) if tool_calls else 0,
        "tool_repeat_rate": round(tool_repeats / tool_calls, 3) if tool_calls else 0,
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
           f"{'tokens':<9} {'tools':<7} {'err':<6} {'rpt':<6} first failure")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for name, g in goals.items():
        fail = next((r["checker_detail"] for r in results
                     if r["goal"] == name and not r["checker_passed"]), "")
        # built outside the f-string: nesting the same quote type is 3.12+
        # syntax and this repo targets 3.10
        ratio = "{}/{}".format(g["passed"], g["runs"])
        print(f"{name:<16} {ratio:<8} {g['pass_rate']:<6} "
              f"{g['mean_attempts']:<5} {g['mean_wall_secs']:<8} "
              f"{g['mean_tokens']:<9} {g['mean_tool_calls']:<7} "
              f"{g['tool_error_rate']:<6} {g['tool_repeat_rate']:<6} {fail[:30]}")


def per_arm(results: list[dict]) -> dict:
    """arm name -> its results, in the order the arms were declared."""
    out: dict[str, list] = {}
    for r in results:
        out.setdefault(r.get("arm") or "", []).append(r)
    return out


def paired_deltas(control: list[dict], variant: list[dict]) -> dict:
    """Pair the two arms by (goal, repeat) and report the mean difference.

    Pairing is the whole point of interleaving: the two runs of a pair
    happened seconds apart on the same server, so whatever drifted between
    the start and the end of the night drifted for both of them.
    """
    def key(r):
        return (r["goal"], r["repeat"])

    a = {key(r): r for r in control}
    b = {key(r): r for r in variant}
    pairs = [(a[k], b[k]) for k in sorted(a.keys() & b.keys())]
    if not pairs:
        return {"pairs": 0}

    def mean(f):
        return sum(f(y) - f(x) for x, y in pairs) / len(pairs)

    def num(r, k):
        return r.get(k) or 0

    def series(f):
        return [f(y) - f(x) for x, y in pairs]

    wins = sum(1 for x, y in pairs if y["checker_passed"] and not x["checker_passed"])
    losses = sum(1 for x, y in pairs if x["checker_passed"] and not y["checker_passed"])
    return {
        "pairs": len(pairs),
        "pass_delta": round(mean(lambda r: float(bool(r["checker_passed"]))), 3),
        "wins": wins, "losses": losses, "discordant": wins + losses,
        "wall_delta": round(mean(lambda r: r["wall_secs"]), 1),
        "token_delta": round(mean(lambda r: num(r, "prompt_tokens") + num(r, "eval_tokens"))),
        "tool_call_delta": round(mean(lambda r: num(r, "tool_calls")), 1),
        "tool_repeat_delta": round(mean(lambda r: num(r, "tool_repeats")), 1),
        # raw per-pair differences, so the caller can put an interval on
        # each without re-deriving the pairing
        "series": {
            "pass": series(lambda r: float(bool(r["checker_passed"]))),
            "wall": series(lambda r: r["wall_secs"]),
            "tokens": series(lambda r: float(num(r, "prompt_tokens")
                                             + num(r, "eval_tokens"))),
            "tool_calls": series(lambda r: float(num(r, "tool_calls"))),
            "tool_repeats": series(lambda r: float(num(r, "tool_repeats"))),
        },
    }


def bootstrap_ci(values: list[float], resamples: int = 5000,
                 alpha: float = 0.05, seed: int = 0) -> tuple:
    """Percentile bootstrap interval for the mean of `values`.

    Non-parametric on purpose: paired pass differences are -1/0/+1 and
    nothing about them is normal, so resampling the pairs we actually have
    beats assuming a distribution we do not.
    """
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo = means[int(alpha / 2 * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)


def mde_pass_rate(pairs: int, discordance: float = 0.25, z: float = 1.96) -> float:
    """Smallest pass-rate difference `pairs` paired runs could resolve.

    Paired differences are -1/0/+1 with standard deviation ≈ sqrt(d) where d
    is the share of pairs that change outcome, so the half-width of the
    interval is z·sqrt(d)/sqrt(n). d has to be assumed up front — that is
    the honest part: the number is quoted with its assumption attached,
    because more churn than assumed widens the interval.
    """
    if pairs <= 0:
        return 1.0
    return z * math.sqrt(discordance) / math.sqrt(pairs)


def print_budget(goals: int, repeats: int, arms: int) -> None:
    """Say what this run can and cannot answer, before it starts.

    An experiment that could never resolve the effect it is looking for
    should say so in the first second, not after a night.
    """
    pairs = goals * repeats
    if arms < 2:
        return
    mde = mde_pass_rate(pairs)
    print(f"\n{pairs} paired observations per comparison.")
    print(f"  Smallest pass-rate difference this can resolve: ~{mde:.2f} "
          f"({mde * 100:.0f} points), assuming a quarter of pairs change "
          f"outcome. A smaller true effect will come back inconclusive, and "
          f"that is the correct answer rather than a failure.")
    if mde > 0.2:
        need = math.ceil((1.96 ** 2 * 0.25) / (0.15 ** 2) / max(goals, 1))
        print(f"  To resolve 15 points you would need about {need} repeats "
              f"({goals * need} pairs). Cost and tool-efficiency deltas are "
              f"continuous and will be far better resolved than this.")


def print_arms(experiment: dict, results: list[dict]) -> None:
    """Per-arm tables, then each variant paired against the first arm."""
    arms = per_arm(results)
    order = [a for a in experiment["arms"] if a in arms]
    for name in order:
        desc = experiment["arms"][name].get("description") or \
            " ".join(experiment["arms"][name]["flags"]) or "(no flags)"
        print(f"\n=== arm: {name} — {desc} ===")
        print_table(arms[name])
        print(f"summary: {json.dumps(summarize(arms[name]))[:200]}")

    if len(order) < 2:
        return
    control = order[0]
    print(f"\n─── paired against '{control}' ───")
    for name in order[1:]:
        d = paired_deltas(arms[control], arms[name])
        if not d["pairs"]:
            print(f"  {name}: no comparable pairs")
            continue
        print(f"  {name}: {d['pairs']} pairs")

        def row(label, key, value, fmt):
            lo, hi = bootstrap_ci(d["series"][key])
            straddles = lo <= 0 <= hi
            mark = "  (interval includes 0)" if straddles else ""
            print(f"    {label:<11} {value:{fmt}}   95% CI ["
                  f"{lo:{fmt}}, {hi:{fmt}}]{mark}")
            return straddles

        row("pass rate", "pass", d["pass_delta"], "+.3f")
        print(f"                ({d['wins']} won, {d['losses']} lost, "
              f"{d['pairs'] - d['discordant']} unchanged)")
        row("wall secs", "wall", d["wall_delta"], "+.1f")
        row("tokens", "tokens", d["token_delta"], "+.0f")
        row("tool calls", "tool_calls", d["tool_call_delta"], "+.1f")
        row("repeats", "tool_repeats", d["tool_repeat_delta"], "+.1f")
        # No verdict. An interval that includes zero is the finding, not a
        # failure to find one, and a threshold applied to it would only
        # manufacture confidence this many samples cannot support.
        if d["discordant"] < 6:
            print(f"    NOTE: only {d['discordant']} pairs changed outcome. "
                  f"The pass-rate interval above is correspondingly wide — "
                  f"the cost and tool rows are what this run can speak to.")


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
    if after_summary.get("tool_calls"):
        print(f"  tool calls  {b.get('tool_calls', '?')} -> {after_summary['tool_calls']}"
              f"   error rate {b.get('tool_error_rate', '?')} -> "
              f"{after_summary['tool_error_rate']}"
              f"   repeat rate {b.get('tool_repeat_rate', '?')} -> "
              f"{after_summary['tool_repeat_rate']}")

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
    p.add_argument("--experiment", default=None, metavar="FILE",
                   help="JSON file defining two or more arms to run "
                        "interleaved and compare pairwise; see "
                        "evals/experiments/ for examples")
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

    experiment = None
    if args.experiment:
        try:
            experiment = load_experiment(args.experiment)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            raise SystemExit(f"could not load experiment: {e}")
    # no experiment == one unnamed arm, so the loop below has one shape
    arms = experiment["arms"] if experiment else {"": {"flags": [], "prompts": {}}}
    base_args = args.agent_args.split() if args.agent_args else []

    total = len(goals) * args.repeat * len(arms)
    if total != len(goals):
        parts = [f"{len(goals)} goals", f"{args.repeat} repeats"]
        if experiment:
            parts.append(f"{len(arms)} arms")
        print(" × ".join(parts) + f" = {total} runs", flush=True)
    if experiment:
        print(f"experiment: {experiment['name']}", flush=True)
        for name, spec in arms.items():
            bits = " ".join(spec["flags"])
            if spec.get("prompts"):
                over = "prompts: " + ", ".join(sorted(spec["prompts"]))
                bits = f"{bits} {over}".strip()
            print(f"  arm {name}: {bits or '(no changes)'}", flush=True)

    print_budget(len(goals), args.repeat, len(arms))

    results = []
    n = 0
    # Repeat-major so an interrupted run still holds complete passes, and
    # arm-innermost so the arms of one (goal, repeat) pair run seconds apart.
    # Whatever drifts over a long night — server load, model residency —
    # then drifts for both arms of every pair rather than for one of them.
    for rep in range(1, args.repeat + 1):
        for goal in goals:
            for arm_name, spec in arms.items():
                n += 1
                tag = f"[eval {n}/{total}]"
                rep_s = f" rep {rep}/{args.repeat}" if args.repeat > 1 else ""
                arm_s = f" [{arm_name}]" if arm_name else ""
                print(f"{tag} {goal.name}{rep_s}{arm_s} ...", flush=True)
                r = run_goal(goal, args.attempts, args.timeout, args.model,
                             args.url, out_root,
                             reviewer_model=args.reviewer_model,
                             extra_args=(base_args + spec["flags"]) or None,
                             repeat=rep, arm=arm_name,
                             prompt_overrides=spec.get("prompts"))
                status = ("PASS" if r["checker_passed"]
                          else f"fail ({r['checker_detail'][:80]})")
                print(f"{tag} {goal.name}{rep_s}{arm_s}: {status}  "
                      f"[{r['wall_secs']}s, attempts={r['attempts_used']}]",
                      flush=True)
                results.append(r)

    summary = summarize(results)
    out_path = os.path.join(HERE, f"results_{args.label}.json")
    with open(out_path, "w") as f:
        json.dump({"label": args.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "attempts": args.attempts, "repeat": args.repeat,
                   "model": args.model,
                   # the arms are recorded verbatim so a results file still
                   # says what it tested months later
                   "experiment": experiment,
                   "summary": summary, "results": results}, f, indent=2)

    if experiment:
        print_arms(experiment, results)
    else:
        print_table(results)
        print(f"\nsummary: {json.dumps(summary)}")
    print(f"\nresults written to {out_path}")

    if args.compare:
        with open(args.compare) as f:
            print_comparison(json.load(f), results, summary)


if __name__ == "__main__":
    main()
