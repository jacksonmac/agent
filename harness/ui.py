"""Live console dashboard (rich), with a plain-print fallback.

One module-level singleton, mirrors how runlog.current works. All harness
code calls the module functions (ui.phase(...), ui.tool(...), ...); they
render into a rich Live dashboard when stdout is a real terminal and rich is
importable, and degrade to the old print() style otherwise (pytest, eval
subprocesses, redirected output).

Persistent lines (answers, verdicts, warnings) are printed ABOVE the live
region so they survive; the dashboard itself only shows current state.
"""

from __future__ import annotations

import json
from collections import deque

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text
    HAVE_RICH = True
except ImportError:  # rich is optional — the harness must still run without it
    HAVE_RICH = False


class _Dashboard:
    def __init__(self, goal: str, model: str, reviewer_model: str,
                 max_attempts: int, num_ctx: int):
        self.goal = goal
        self.model = model
        self.reviewer_model = reviewer_model
        self.max_attempts = max_attempts
        self.num_ctx = num_ctx
        self.attempt_n = 0
        self.phase_text = "starting"
        self.tokens = 0
        self.tools: deque = deque(maxlen=8)
        self.last_verdict: str | None = None
        self.console = Console()
        self.live = Live(self._render(), console=self.console,
                         refresh_per_second=4)
        self.live.start()

    # ── rendering ────────────────────────────────────────────────────
    def _render(self):
        header = Table.grid(padding=(0, 1))
        header.add_column(style="bold", width=9)
        header.add_column()
        header.add_row("goal", Text(self.goal[:200], overflow="ellipsis"))
        models = self.model if self.reviewer_model == self.model \
            else f"{self.model}  (reviewer: {self.reviewer_model})"
        header.add_row("model", models)
        attempt = f"{self.attempt_n}/{self.max_attempts}" if self.attempt_n else "-"
        header.add_row("attempt", attempt)
        header.add_row("phase", Spinner("dots", text=Text(f" {self.phase_text}")))

        bar = Table.grid(padding=(0, 1))
        bar.add_column(width=9)
        bar.add_column(ratio=1)
        bar.add_column(justify="right")
        pct = self.tokens / self.num_ctx if self.num_ctx else 0
        style = "red" if pct > 0.85 else ("yellow" if pct > 0.7 else "green")
        bar.add_row("context",
                    ProgressBar(total=self.num_ctx, completed=self.tokens,
                                complete_style=style),
                    f"~{self.tokens} / {self.num_ctx} tok")

        parts = [Panel(header, border_style="dim"), bar]
        if self.tools:
            tool_text = Text("\n".join(self.tools), no_wrap=True, overflow="ellipsis")
            parts.append(Panel(tool_text, title="recent tools",
                               title_align="left", border_style="dim"))
        if self.last_verdict:
            parts.append(Panel(Text(self.last_verdict[:500]), title="last verdict",
                               title_align="left", border_style="dim"))
        return Group(*parts)

    def refresh(self):
        self.live.update(self._render())

    def print(self, *args, **kwargs):
        self.live.console.print(*args, **kwargs)

    def stop(self):
        try:
            self.live.stop()
        except Exception:
            pass


_dash: _Dashboard | None = None


def _fmt_args(arguments) -> str:
    try:
        s = json.dumps(arguments)
    except (TypeError, ValueError):
        s = str(arguments)
    return s


# ── lifecycle ───────────────────────────────────────────────────────

def start(goal: str, model: str, reviewer_model: str | None,
          max_attempts: int, num_ctx: int) -> None:
    """Begin the live dashboard — only when rich exists and stdout is a TTY."""
    global _dash
    if not HAVE_RICH or _dash is not None:
        return
    if not Console().is_terminal:
        return  # piped/captured output (evals, pytest): stay line-oriented
    _dash = _Dashboard(goal, model, reviewer_model or model, max_attempts, num_ctx)


def stop() -> None:
    global _dash
    if _dash is not None:
        _dash.stop()
        _dash = None


# ── state updates (dashboard region) ────────────────────────────────

def attempt(n: int, total: int) -> None:
    if _dash:
        _dash.attempt_n = n
        _dash.max_attempts = total
        _dash.refresh()
    else:
        print(f"\n=== EXECUTING (attempt {n}/{total}) ===")


def phase(label: str) -> None:
    if _dash:
        _dash.phase_text = label
        _dash.refresh()
    else:
        print(f"--- {label} ---")


def context_tokens(estimate: int, num_ctx: int) -> None:
    if _dash:
        _dash.tokens = estimate
        _dash.num_ctx = num_ctx
        _dash.refresh()
    else:
        print(f"  [context ~{estimate} tokens / {num_ctx}]")


def tool(name: str, arguments) -> None:
    args_s = _fmt_args(arguments)
    if _dash:
        _dash.tools.append(f"{name}({args_s[:120]})")
        _dash.refresh()
    else:
        print(f"  [tool call] {name}({args_s[:200]})")


def tool_result(text: str) -> None:
    if _dash:
        if _dash.tools:
            _dash.tools[-1] += f" -> {' '.join(text[:80].split())}"
        _dash.refresh()
    else:
        print(f"  [tool result] {text[:300]}")


# ── persistent lines (survive above the live region) ────────────────

def info(text: str) -> None:
    if _dash:
        _dash.print(Text(text, style="dim"))
    else:
        print(text)


def warn(text: str) -> None:
    if _dash:
        _dash.print(Text(f"[WARNING] {text}", style="bold yellow"))
    else:
        print(f"  [WARNING] {text}")


def thinking(text: str) -> None:
    if _dash:
        _dash.print(Text(f"[thinking] {' '.join(text[:300].split())}", style="dim italic"))
    else:
        print(f"  [thinking]:\n{text[:500]}\n")


def answer(text: str, forced: bool = False) -> None:
    label = "Answer (forced)" if forced else "Answer"
    if _dash:
        _dash.print(Panel(Text(text[:2000]), title=label, title_align="left",
                          border_style="cyan"))
    else:
        print(f"{label}:\n{text}\n")


def verdict(passed: bool, summary: str) -> None:
    if _dash:
        _dash.last_verdict = summary
        style = "bold green" if passed else "bold red"
        _dash.print(Text(f"review: {'PASSED' if passed else 'FAILED'} — "
                         f"{' '.join(summary[:300].split())}", style=style))
        _dash.refresh()
    else:
        print(f"reviewer said: passed={passed} {summary!r}")


def success(text: str) -> None:
    if _dash:
        _dash.print(Text(text, style="bold green"))
    else:
        print(text)
