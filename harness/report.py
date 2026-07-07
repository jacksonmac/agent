"""Self-contained HTML report for a run.

Renders report.html into the run dir from events.jsonl + attempt_history.json
(+ final_output*.txt). Pure stdlib, inline CSS, <details> collapsibles — one
file you can open or send anywhere, no server, no JS dependencies.

    python3 -m harness.report runs/latest        # regenerate by hand
"""

from __future__ import annotations

import html
import json
import os
import sys

_CSS = """
body { font-family: -apple-system, 'Segoe UI', sans-serif; margin: 2rem auto;
       max-width: 60rem; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e4e4e4;
         vertical-align: top; }
th { background: #f6f6f4; }
pre { background: #f6f6f4; padding: .8rem; border-radius: 6px; overflow-x: auto;
      white-space: pre-wrap; font-size: .85rem; }
details { border: 1px solid #ddd; border-radius: 6px; margin: .6rem 0;
          padding: .4rem .8rem; }
summary { cursor: pointer; font-weight: 600; padding: .2rem 0; }
.pass { color: #0a7a43; font-weight: 600; } .fail { color: #b3261e; font-weight: 600; }
.meta { color: #666; font-size: .9rem; }
.pill { display: inline-block; background: #eee; border-radius: 999px;
        padding: 0 .6rem; font-size: .8rem; margin-right: .4rem; }
.crit-met { color: #0a7a43; } .crit-unmet { color: #b3261e; }
"""


def _load_events(run_dir: str) -> list[dict]:
    events = []
    try:
        with open(os.path.join(run_dir, "events.jsonl")) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def _load_attempts(run_dir: str) -> list[dict]:
    try:
        with open(os.path.join(run_dir, "attempt_history.json")) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _stats(events: list[dict]) -> dict:
    s = {"llm_calls": 0, "llm_secs": 0.0, "prompt_tokens": 0, "eval_tokens": 0,
         "tool_calls": 0, "tools_failed": 0, "by_label": {}, "by_tool": {}}
    for ev in events:
        if ev.get("event") == "llm":
            s["llm_calls"] += 1
            s["llm_secs"] += ev.get("secs") or 0
            s["prompt_tokens"] += ev.get("prompt_tokens") or 0
            s["eval_tokens"] += ev.get("eval_tokens") or 0
            label = ev.get("label", "llm")
            lab = s["by_label"].setdefault(label, {"calls": 0, "secs": 0.0})
            lab["calls"] += 1
            lab["secs"] += ev.get("secs") or 0
        elif ev.get("event") == "tool":
            s["tool_calls"] += 1
            if not ev.get("ok", True):
                s["tools_failed"] += 1
            s["by_tool"][ev.get("name", "?")] = s["by_tool"].get(ev.get("name", "?"), 0) + 1
    s["llm_secs"] = round(s["llm_secs"], 1)
    return s


def _criteria_rows(criteria: list) -> str:
    rows = []
    for c in criteria:
        if not isinstance(c, dict):
            continue
        met = bool(c.get("met"))
        mark = "&#10003;" if met else "&#10007;"
        cls = "crit-met" if met else "crit-unmet"
        rows.append(f"<tr><td class='{cls}'>{mark}</td>"
                    f"<td>{_esc(c.get('criterion', '?'))}</td>"
                    f"<td class='meta'>{_esc(c.get('note', ''))}</td></tr>")
    if not rows:
        return ""
    return ("<table><tr><th></th><th>criterion</th><th>note</th></tr>"
            + "".join(rows) + "</table>")


def render(run_dir: str) -> str:
    events = _load_events(run_dir)
    attempts = _load_attempts(run_dir)
    start = next((e for e in events if e.get("event") == "run_start"), {})
    stats = _stats(events)
    passed_overall = any(a.get("passed") for a in attempts)

    parts = [f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
             f"<title>agent run — {_esc(os.path.basename(os.path.abspath(run_dir)))}</title>"
             f"<style>{_CSS}</style></head><body>"]

    verdict_cls = "pass" if passed_overall else "fail"
    verdict_word = "PASSED" if passed_overall else "did not pass"
    parts.append(f"<h1>Agent run <span class='{verdict_cls}'>{verdict_word}</span></h1>")
    parts.append(f"<p class='meta'>{_esc(start.get('ts', ''))} — "
                 f"{_esc(os.path.abspath(run_dir))}</p>")
    parts.append(f"<p><b>Goal:</b> {_esc(start.get('goal', '(unknown)'))}</p>")
    if start.get("task") and start.get("task") != start.get("goal"):
        parts.append(f"<p><b>Task:</b> {_esc(start.get('task'))}</p>")
    if start.get("criteria"):
        items = "".join(f"<li>{_esc(c)}</li>" for c in start["criteria"])
        parts.append(f"<p><b>Criteria:</b></p><ul>{items}</ul>")

    pills = [f"model {_esc(start.get('model', '?'))}"]
    if start.get("reviewer") and start.get("reviewer") != start.get("model"):
        pills.append(f"reviewer {_esc(start.get('reviewer'))}")
    if start.get("best_of", 1) and start.get("best_of", 1) > 1:
        pills.append(f"best-of {start['best_of']}")
    pills += [f"{len(attempts)} attempt(s)",
              f"{stats['llm_calls']} llm calls / {stats['llm_secs']}s",
              f"{stats['prompt_tokens']} prompt tok",
              f"{stats['eval_tokens']} output tok",
              f"{stats['tool_calls']} tool calls"
              + (f" ({stats['tools_failed']} failed)" if stats["tools_failed"] else "")]
    parts.append("<p>" + "".join(f"<span class='pill'>{p}</span>" for p in pills) + "</p>")

    # ── attempts ────────────────────────────────────────────────────
    parts.append("<h2>Attempts</h2>")
    ev_by_attempt = {e.get("n"): e for e in events if e.get("event") == "attempt"}
    for a in attempts:
        n = a.get("attempt")
        ok = a.get("passed")
        flags = []
        ev = ev_by_attempt.get(n, {})
        if ev.get("no_tools"):
            flags.append("no tools used")
        if ev.get("stalled"):
            flags.append("stalled")
        flag_s = f" <span class='meta'>({', '.join(flags)})</span>" if flags else ""
        parts.append(f"<details {'open' if ok else ''}><summary>attempt {n} — "
                     f"<span class='{'pass' if ok else 'fail'}'>"
                     f"{'PASSED' if ok else 'failed'}</span>{flag_s}</summary>")
        if a.get("files"):
            parts.append("<p><b>Files changed:</b> "
                         + ", ".join(f"<code>{_esc(f)}</code>" for f in a["files"]) + "</p>")
        parts.append(_criteria_rows(a.get("criteria") or []))
        if a.get("verdict") and not ok:
            parts.append(f"<p><b>Reviewer:</b> {_esc(a['verdict'])}</p>")
        parts.append(f"<pre>{_esc(a.get('output', ''))}</pre></details>")

    # ── candidates (best-of runs) ───────────────────────────────────
    cands = [e for e in events if e.get("event") == "candidate"]
    if cands:
        parts.append("<h2>Best-of candidates</h2><table>"
                     "<tr><th>candidate</th><th>passed</th><th>criteria met</th>"
                     "<th>files</th></tr>")
        winner = next((e.get("winner") for e in events
                       if e.get("event") == "candidate_selected"), "")
        for c in cands:
            mark = " &#8592; promoted" if f"candidate_{c.get('i')}" == winner else ""
            parts.append(f"<tr><td>candidate {_esc(c.get('i'))}{mark}</td>"
                         f"<td>{_esc(c.get('passed'))}</td>"
                         f"<td>{_esc(c.get('criteria_met'))}</td>"
                         f"<td>{_esc(', '.join(c.get('files') or []))}</td></tr>")
        parts.append("</table>")

    # ── timeline ────────────────────────────────────────────────────
    parts.append("<h2>Timeline</h2><details><summary>"
                 f"{len(events)} events</summary><table>"
                 "<tr><th>time</th><th>event</th><th>details</th></tr>")
    for ev in events:
        detail = {k: v for k, v in ev.items() if k not in ("ts", "event")}
        parts.append(f"<tr><td>{_esc(ev.get('ts'))}</td><td>{_esc(ev.get('event'))}</td>"
                     f"<td class='meta'>{_esc(json.dumps(detail)[:300])}</td></tr>")
    parts.append("</table></details>")

    # ── llm/tool breakdowns ─────────────────────────────────────────
    parts.append("<h2>Where the time went</h2><table>"
                 "<tr><th>role</th><th>calls</th><th>seconds</th></tr>")
    for label, lab in sorted(stats["by_label"].items()):
        parts.append(f"<tr><td>{_esc(label)}</td><td>{lab['calls']}</td>"
                     f"<td>{round(lab['secs'], 1)}</td></tr>")
    parts.append("</table>")
    if stats["by_tool"]:
        parts.append("<h2>Tool usage</h2><table><tr><th>tool</th><th>calls</th></tr>")
        for name, count in sorted(stats["by_tool"].items(), key=lambda kv: -kv[1]):
            parts.append(f"<tr><td>{_esc(name)}</td><td>{count}</td></tr>")
        parts.append("</table>")

    # ── final output ────────────────────────────────────────────────
    for fname, label in (("final_output.txt", "Final output"),
                         ("final_output_UNVERIFIED.txt", "Final output (UNVERIFIED)")):
        path = os.path.join(run_dir, fname)
        if os.path.exists(path):
            with open(path) as f:
                parts.append(f"<h2>{label}</h2><pre>{_esc(f.read())}</pre>")
            break

    parts.append("</body></html>")
    return "".join(parts)


def write_report(run_dir: str) -> str | None:
    """Render report.html into run_dir. Never raises — a broken report must
    not kill a finished run."""
    try:
        out = os.path.join(run_dir, "report.html")
        html_text = render(run_dir)
        with open(out, "w") as f:
            f.write(html_text)
        return out
    except Exception as e:
        print(f"  [report] failed to render report.html: {type(e).__name__}: {e}")
        return None


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 -m harness.report <run_dir>")
    run_dir = os.path.realpath(sys.argv[1])
    if not os.path.isdir(run_dir):
        raise SystemExit(f"not a directory: {run_dir}")
    out = write_report(run_dir)
    if out:
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
