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
import subprocess
import sys
import time
from collections import deque

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.spinner import Spinner
    from rich.syntax import Syntax
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
        self.phase_started = time.monotonic()
        self.last_llm = ""
        self.tokens = 0
        self.tools: deque = deque(maxlen=8)
        self.todo_items: list = []
        self.stream_buf = ""
        self.stream_thinking = ""
        self.last_verdict: str | None = None
        self.console = Console()
        # get_renderable (not a static renderable) so the 4 Hz background
        # refresh re-renders — that's what makes the phase clock tick
        self.live = Live(get_renderable=self._render, console=self.console,
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
        elapsed = int(time.monotonic() - self.phase_started)
        header.add_row("phase", Spinner("dots", text=Text(
            f" {self.phase_text} · {elapsed // 60}:{elapsed % 60:02d}")))
        if self.last_llm:
            header.add_row("last call", Text(self.last_llm, style="dim"))

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
        if self.stream_buf or self.stream_thinking:
            stream_text = Text()
            if self.stream_thinking:
                stream_text.append(self.stream_thinking, style="dim italic")
                if self.stream_buf:
                    stream_text.append("\n")
            stream_text.append(self.stream_buf)
            parts.append(Panel(stream_text, title="streaming", title_align="left",
                               border_style="dim"))
        if self.todo_items:
            todo_text = Text()
            for i, t in enumerate(self.todo_items):
                mark = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}[t["status"]]
                style = "dim" if t["status"] == "done" else \
                    ("bold" if t["status"] == "in_progress" else "")
                todo_text.append(f"{mark} {t['text']}", style=style)
                if i < len(self.todo_items) - 1:
                    todo_text.append("\n")
            parts.append(Panel(todo_text, title="todos", title_align="left",
                               border_style="dim"))
        if self.last_verdict:
            parts.append(Panel(Text(self.last_verdict[:500]), title="last verdict",
                               title_align="left", border_style="dim"))
        return Group(*parts)

    def stream_add(self, text: str, thinking: bool) -> None:
        # a live tail, not a transcript — keep only the newest chunk
        if thinking:
            self.stream_thinking = (self.stream_thinking + text)[-600:]
        else:
            self.stream_buf = (self.stream_buf + text)[-1200:]
        self.refresh()

    def stream_clear(self) -> None:
        self.stream_buf = ""
        self.stream_thinking = ""
        self.refresh()

    def refresh(self):
        self.live.refresh()

    def print(self, *args, **kwargs):
        self.live.console.print(*args, **kwargs)

    def stop(self):
        try:
            self.live.stop()
        except Exception:
            pass


_dash: _Dashboard | None = None
tool_prefix = ""  # set to "  └ " while a subagent runs, so its tools read nested
_streamed_recently = False  # plain mode: answer()/thinking() skip the re-print


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
        _dash.phase_started = time.monotonic()
        _dash.refresh()
    else:
        print(f"--- {label} ---")


def llm_stats(label: str, secs: float, prompt_tokens, eval_tokens) -> None:
    """Per-call timing/token stats, straight from the Ollama response meta."""
    tok = f"{prompt_tokens or '?'}→{eval_tokens or '?'} tok"
    line = f"[{label}] {secs:.1f}s · {tok}"
    if _dash:
        _dash.last_llm = line
        _dash.refresh()
    else:
        print(f"  {line}")


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
        _dash.tools.append(f"{tool_prefix}{name}({args_s[:120]})")
        _dash.refresh()
    else:
        print(f"  {tool_prefix}[tool call] {name}({args_s[:200]})")


def tool_result(text: str) -> None:
    if _dash:
        if _dash.tools:
            _dash.tools[-1] += f" -> {' '.join(text[:80].split())}"
        _dash.refresh()
    else:
        print(f"  [tool result] {text[:300]}")


def stream_delta(text: str, thinking: bool = False) -> None:
    """A chunk of model output as it arrives. Dashboard: rolling tail panel.
    Plain mode: progressive print (thinking stays quiet — too noisy raw)."""
    global _streamed_recently
    if _dash:
        _dash.stream_add(text, thinking)
    elif not thinking:
        _streamed_recently = True
        sys.stdout.write(text)
        sys.stdout.flush()


def stream_end() -> None:
    if _dash:
        _dash.stream_clear()
    elif _streamed_recently:
        sys.stdout.write("\n")
        sys.stdout.flush()


def todos(items: list) -> None:
    if _dash:
        _dash.todo_items = list(items)
        _dash.refresh()
    else:
        marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
        for t in items:
            print(f"  {marks[t['status']]} {t['text']}")


def confirm(prompt: str) -> str:
    """y/n/a prompt that plays nice with the Live dashboard. Returns 'y', 'n'
    or 'a'; anything else (including EOF/Ctrl-C) is a deny."""
    full = f"{prompt} [y]es / [n]o / [a]lways this run: "
    try:
        if _dash:
            # pause the live region: input during its background refresh
            # garbles the prompt line
            _dash.live.stop()
            try:
                ans = _dash.console.input(f"[bold yellow]{full}[/bold yellow]")
            finally:
                _dash.live.start()
                _dash.refresh()
        else:
            ans = input(full)
    except (EOFError, KeyboardInterrupt):
        return "n"
    ans = ans.strip().lower()[:1]
    return ans if ans in ("y", "n", "a") else "n"


def subagent_start(kind: str, task: str) -> None:
    global tool_prefix
    tool_prefix = "  └ "
    text = f"[subagent:{kind}] {' '.join(task[:120].split())}"
    if _dash:
        _dash.print(Text(text, style="dim"))
    else:
        print(f"  {text}")


def subagent_end() -> None:
    global tool_prefix
    tool_prefix = ""
    if _dash:
        _dash.print(Text("[subagent done]", style="dim"))
    else:
        print("  [subagent done]")


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
    global _streamed_recently
    label = "Answer (forced)" if forced else "Answer"
    if _dash:
        try:
            body = Markdown(text[:4000])
        except Exception:  # a malformed answer must still print
            body = Text(text[:2000])
        _dash.print(Panel(body, title=label, title_align="left",
                          border_style="cyan"))
    elif _streamed_recently:
        # the body already printed progressively — don't double the output
        _streamed_recently = False
        print(f"{label}: (streamed above)")
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


def diff(path: str, diff_text: str, max_lines: int = 80) -> None:
    """Display-only unified diff of a file the agent wrote/edited. Never sent
    back to the model — that would cost context for no benefit."""
    lines = diff_text.splitlines()
    shown = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        shown += f"\n… {len(lines) - max_lines} more lines"
    if _dash:
        _dash.print(Panel(Syntax(shown, "diff", background_color="default"),
                          title=path, title_align="left", border_style="dim"))
    else:
        print(shown)


def notify(title: str, message: str, success: bool = True) -> None:
    """End-of-run notification: terminal bell, plus a macOS banner. Best
    effort — must never break a run."""
    if sys.stdout.isatty():
        sys.stdout.write("\a")
        sys.stdout.flush()
    if sys.platform == "darwin":
        script = ('display notification "{}" with title "{}"'
                  .format(message.replace('"', "'")[:120],
                          title.replace('"', "'")[:60]))
        try:
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=5)
        except Exception:
            pass
