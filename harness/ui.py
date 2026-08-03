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

import contextlib
import dataclasses
import json
import subprocess
import sys
import threading
import time
from collections import deque

from .keys import KeyReader

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.markup import escape as rich_escape
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    HAVE_RICH = True
except ImportError:  # rich is optional — the harness must still run without it
    HAVE_RICH = False


@dataclasses.dataclass
class ToolRow:
    """One entry in the dashboard's tool timeline."""
    name: str
    args_short: str
    status: str = "running"          # "running" | "done" | "error"
    started: float = dataclasses.field(default_factory=time.monotonic)
    duration: float | None = None    # set when the result arrives
    diff_stat: str | None = None     # e.g. "+14 −2" for file writes/edits
    depth: int = 0                   # 1 while running under a subagent
    is_subagent_header: bool = False  # the "▸ subagent kind: task" row


# which single argument best identifies a call in one timeline cell
_ARG_KEYS = {"write_file": "name", "read_file": "name", "edit_file": "name",
             "run_script": "name", "load_skill": "name", "run_shell": "command",
             "grep_files": "pattern", "list_files": "path",
             "spawn_subagent": "task", "web_search": "query", "fetch_page": "url"}


def _short_args(name: str, arguments) -> str:
    """The one argument a human wants to see for this tool (path, command,
    query, …), falling back to compact JSON."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    s = ""
    if isinstance(arguments, dict):
        if name == "run_python":
            code_lines = str(arguments.get("code", "")).strip().splitlines()
            s = code_lines[0] if code_lines else ""
        else:
            key = _ARG_KEYS.get(name)
            s = str(arguments.get(key, "")) if key else ""
    if not s:
        s = arguments if isinstance(arguments, str) else _fmt_args(arguments)
    return " ".join(s.split())[:80]


def _context_bar(tokens: int, num_ctx: int, width: int = 40):
    """Explicit block bar — rich's ProgressBar draws its empty track with the
    same ━ glyph as the filled part, so an empty bar looked full."""
    pct = (tokens / num_ctx) if num_ctx else 0
    pct = max(0.0, min(pct, 1.0))
    style = "red" if pct > 0.85 else ("yellow" if pct > 0.7 else "green")
    filled = round(pct * width)
    t = Text()
    t.append("█" * filled, style=style)
    t.append("░" * (width - filled), style="dim")
    return t


def _fmt_tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"


def _spark(values, width: int = 8) -> str:
    """Tiny block-glyph sparkline of the last `width` values; empty until
    there are at least 2 points, flat midline when they're all equal."""
    vals = list(values)[-width:]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return "▄" * len(vals)
    return "".join(_SPARK_GLYPHS[round((v - lo) / (hi - lo) * 7)]
                   for v in vals)


def _ledger_criteria(entry: dict) -> dict:
    """{criterion text: met?} for one ledger entry, ignoring bare-string
    (not-yet-reviewed) items."""
    return {str(c.get("criterion", "?")): bool(c.get("met"))
            for c in entry.get("criteria") or [] if isinstance(c, dict)}


def _regressions(ledger: list) -> list[str]:
    """Criteria that were met in some earlier attempt and are not met in the
    latest reviewed one — the retry loop going backwards."""
    reviewed = [e for e in ledger if _ledger_criteria(e)]
    if len(reviewed) < 2:
        return []
    latest = _ledger_criteria(reviewed[-1])
    ever_met = set()
    for e in reviewed[:-1]:
        ever_met |= {k for k, met in _ledger_criteria(e).items() if met}
    return [k for k, met in latest.items() if not met and k in ever_met]


def _digest_checks(output: str) -> str:
    """One line out of a pytest run: the first failure if there is one, else
    the summary line."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        if ln.startswith(("FAILED", "ERROR", "E   ")):
            return ln[:160]
    for ln in reversed(lines):
        if "passed" in ln or "failed" in ln or "error" in ln:
            return ln[:160]
    return lines[-1][:160]


# which phase() labels collapse to which run-rail name
_RAIL_NAMES = {"planning": "plan", "executing": "exec", "reviewing": "review"}

# loop banner thresholds: N identical calls inside the last _LOOP_WINDOW, or
# this many seconds of tool calls that changed no file
_LOOP_REPEATS = 3
_LOOP_WINDOW = 8
_IDLE_EDIT_SECS = 120

# [/] one-key steering: the things you always end up typing by hand
STEER_PRESETS = [
    "You are repeating yourself — take a different approach.",
    "Stop exploring and make the change now.",
    "Run the tests and fix whatever fails.",
    "Summarize what you have done so far, then continue.",
]


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
        self.tokens = 0          # executor context (pinned bar)
        self.other_label = ""    # reviewer/subagent/… while it's making calls
        self.other_tokens = 0
        self.tool_rows: deque[ToolRow] = deque(maxlen=10)
        self.tool_history: list[ToolRow] = []  # full run, capped at 500
        self.last_diff: tuple[str, str] | None = None  # (path, diff_text)
        self.tool_count = 0
        # path -> {"add", "rm", "edits"}: cumulative diff totals per file
        self.files_touched: dict[str, dict] = {}
        self.llm_calls = 0
        self.llm_secs = 0.0
        self.criteria_items: list = []   # {criterion, met, note} or bare str
        self.criteria_source = ""
        self.todo_items: list = []
        self.stream_buf = ""
        self.stream_thinking = ""
        self.last_verdict: str | None = None
        self.verdict_passed: bool | None = None
        self.tint = "cyan"  # header accent: cyan running, red failed, green done
        self.rail: list[list] = []       # [label, status] phase trail
        self.burn: deque = deque(maxlen=6)  # (monotonic, tokens) samples
        self.subagent: tuple | None = None  # (kind, task, started) while active
        self.quiet = False               # [z]: collapse to a 2-line strip
        self.run_started = time.monotonic()
        self.llm_durs: deque = deque(maxlen=16)   # per-call secs → sparkline
        self.pulse: deque = deque(maxlen=12)      # (monotonic, chars) stream chunks
        self.trend: list = []                     # (met, total) per review
        self.ctx_hist: deque = deque(maxlen=24)   # executor tokens → sawtooth
        self.paused = False
        # [a] attempt ledger: one dict per attempt, so a criterion that goes
        # ✓→✗ between attempts is visible instead of being overwritten
        self.ledger: list[dict] = []
        self.last_checks = ""            # digest of the reviewer's pytest run
        # [b] budget: per-role token/time accumulators + per-attempt cost
        self.roles: dict[str, dict] = {}
        self.attempt_tokens: dict[int, int] = {}
        self.reclaimed: list[int] = []   # tokens each compaction dip gave back
        # loop banner: repeated identical calls, or a long stretch with no edits
        self.loop_warn: str | None = None
        self.loop_sig: tuple | None = None   # the (name, args) that raised it
        self.last_change = time.monotonic()
        self.tools_since_change = 0
        self._transcript: deque[str] = deque()
        self._transcript_len = 0
        self._transcript_dropped = 0
        # guards containers mutated on the main thread and iterated on the
        # reader / Live-refresh threads (deques raise RuntimeError if they
        # mutate mid-iteration, and neither rich's refresh thread nor our
        # reader thread would survive that)
        self._lock = threading.Lock()
        self.console = Console()
        # get_renderable (not a static renderable) so the 4 Hz background
        # refresh re-renders — that's what makes the phase clock tick
        self.live = Live(get_renderable=self._render, console=self.console,
                         refresh_per_second=4)
        self.live.start()

    # ── rendering ────────────────────────────────────────────────────
    @staticmethod
    def _pair(a, b):
        row = Table.grid(padding=(0, 1), expand=True)
        row.add_column(ratio=1)
        row.add_column(ratio=1)
        row.add_row(a, b)
        return row

    def _render(self):
        input_line = self._render_input_line()
        if self.quiet:  # [z]: just the status strip and the input line
            parts = [self._render_status_line()]
            if input_line is not None:
                parts.append(input_line)
            return Group(*parts)
        parts = [self._render_header()]  # context bars live inside the header
        banner = self._render_loop_banner()
        if banner is not None:
            parts.append(banner)
        wide = self.console.width >= 110
        timeline = self._render_timeline()
        stream = self._render_stream()
        if wide and timeline is not None and stream is not None:
            parts.append(self._pair(timeline, stream))
        else:
            parts.extend(p for p in (timeline, stream) if p is not None)
        lane = self._render_subagent()
        if lane is not None:
            parts.append(lane)
        files = self._render_files()
        plan = self._render_plan(wide and files is None)
        if wide and plan is not None and files is not None:
            parts.append(self._pair(plan, files))
        else:
            parts.extend(p for p in (plan, files) if p is not None)
        if input_line is not None:
            parts.append(input_line)
        return Group(*parts)

    def _render_loop_banner(self):
        """Live version of the stall detection run.py only does after the
        fact: the same call repeating, or a long stretch of tool calls with
        nothing written. One line, above the timeline."""
        text = self.loop_warn
        idle = time.monotonic() - self.last_change
        # tools_since_change, not tool_count: the signal is "it has done
        # several things and produced nothing", which a cumulative counter
        # would report forever after the first four calls of the run
        if text is None and self.tools_since_change >= 4 and idle > _IDLE_EDIT_SECS:
            text = (f"no file changes for {int(idle) // 60}:{int(idle) % 60:02d}"
                    f" · {self.tool_count} tool calls so far")
        if text is None:
            return None
        body = Text(no_wrap=True, overflow="ellipsis")
        body.append("⚠ ", style="bold yellow")
        body.append(text, style="yellow")
        body.append("  [i] interrupt · [/] presets", style="dim")
        return Panel(body, border_style="yellow")

    def _render_subagent(self):
        """Mini-panel for a running subagent: task, clock, and its context."""
        if self.subagent is None:
            return None
        kind, task, started = self.subagent
        el = int(time.monotonic() - started)
        frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
        body = Text(no_wrap=True, overflow="ellipsis")
        body.append(f"{frame} ", style="cyan")
        body.append(" ".join(task.split())[:100])
        body.append(f"  {el // 60}:{el % 60:02d}", style="dim")
        if self.other_label:  # its session is mid-call: show its context
            body.append(" · ctx ", style="dim")
            body.append_text(_context_bar(self.other_tokens, self.num_ctx,
                                          width=6))
            pct = round(100 * self.other_tokens / self.num_ctx) \
                if self.num_ctx else 0
            body.append(f" {pct}%", style="dim")
        return Panel(body, title=f"subagent · {kind}", title_align="left",
                     border_style="cyan")

    def _render_files(self):
        """Cumulative per-file diff totals, fed by ui.diff()."""
        with self._lock:  # diff()/tool() mutate this on the main thread
            items = list(self.files_touched.items())
        if not items:
            return None
        grid = Table.grid(padding=(0, 1))
        grid.add_column(ratio=1, no_wrap=True)  # path
        grid.add_column(justify="right")        # +n −m
        grid.add_column(justify="right")        # per-edit churn sparkline
        grid.add_column(justify="right")        # edit count
        for path, st in items[-8:]:
            stat = Text()
            stat.append(f"+{st['add']}", style="green")
            stat.append(f" −{st['rm']}", style="red")
            n = st["edits"]
            grid.add_row(Text(path, overflow="ellipsis"), stat,
                         Text(_spark(st.get("hist", [])), style="cyan"),
                         Text(f"{n} edit{'s' if n != 1 else ''}", style="dim"))
        return Panel(grid, title="files", title_align="left",
                     border_style="dim")

    def _render_queued(self):
        """Chip for messages waiting to be delivered. Without it a queued
        message vanishes into pending_msgs with no way to see or undo it."""
        st = _state
        if st is None:
            return None
        msgs = list(st.pending_msgs)  # snapshot; the reader thread appends
        if not msgs:
            return None
        body = Text(no_wrap=True, overflow="ellipsis")
        body.append("✉ queued ", style="cyan")
        body.append(" ".join(msgs[-1].split())[:70])
        if len(msgs) > 1:
            body.append(f" (+{len(msgs) - 1} more)", style="dim")
        body.append(" → lands before the next model call · [e]dit [c]ancel",
                    style="dim")
        return body

    def _render_menu(self):
        body = Text()
        for i, preset in enumerate(STEER_PRESETS, 1):
            body.append(f"[{i}] ", style="bold cyan")
            body.append(preset)
            if i < len(STEER_PRESETS):
                body.append("\n")
        return Panel(body, title="steer", title_align="left",
                     subtitle=Text("number sends · Esc cancel", style="dim"),
                     subtitle_align="right", border_style="cyan")

    def _render_input_line(self):
        st = _state
        if st is None:
            return None
        if st.menu:
            return self._render_menu()
        if not st.focused:
            queued = self._render_queued()
            if self.quiet:
                hint = Text("› message the agent · [m] type · [z] expand",
                            style="blue")  # blue ≠ the cyan accents
            else:
                # no_wrap: with this many keys the line would otherwise wrap
                # to two rows on a narrow terminal and make the whole
                # dashboard jump every refresh
                hint = Text(
                    "› [m]essage [i]nterrupt [/]presets · [p]ause [o]transcript "
                    "[t]ools [d]iff [a]ttempts [b]udget [q]uit [z]quiet",
                    style="blue", no_wrap=True, overflow="ellipsis")
            return Group(queued, hint) if queued is not None else hint
        # focused: a bordered composer box — the buffer wraps instead of
        # truncating, so longer instructions stay readable while typing
        body = Text(st.buffer)  # single reference read: safe without the lock
        body.append("█", style="cyan")
        title = "interrupt" if st.compose_interrupt else "message"
        sub = ("Enter send + skip the rest of this tool round · Esc cancel"
               if st.compose_interrupt else "Enter send · Esc cancel")
        return Panel(body, title=title, title_align="left",
                     subtitle=Text(sub, style="dim"),
                     subtitle_align="right", border_style="cyan")

    def _render_header(self):
        grid = Table.grid(padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_row(Text(self.goal[:200], overflow="ellipsis"))
        grid.add_row(self._render_status_line())
        title = Text(f"agent · {self.model}", style="bold")
        if self.reviewer_model != self.model:
            title.append(f"  (reviewer: {self.reviewer_model})",
                         style="dim not bold")
        return Panel(grid, title=title, title_align="left",
                     border_style="yellow" if self.paused else self.tint)

    def _render_status_line(self):
        """One dense line: run rail (the actual plan→exec→review trail) ·
        context · run stats. Per-call llm timing lives behind [t]."""
        line = Text(no_wrap=True, overflow="ellipsis")
        elapsed = int(time.monotonic() - self.phase_started)
        if self.paused:
            line.append("PAUSED", style="bold yellow")
            line.append(" — any key resumes · [m] message · [q] quit",
                        style="yellow")
            return line
        frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
        line.append(f"{frame} ", style="cyan")
        if self.rail:
            shown = self.rail[-5:]
            if len(self.rail) > 5:
                line.append("… ", style="dim")
            for i, (label, status) in enumerate(shown):
                if i:
                    line.append(" · ", style="dim")
                if status == "running":
                    line.append(f"{label} ")
                    line.append(f"{elapsed // 60}:{elapsed % 60:02d}",
                                style="cyan")
                else:
                    line.append(f"{label} ", style="dim")
                    if status == "fail":
                        line.append("✗", style="red")
                    else:  # done / pass
                        line.append("✓", style="green")
        else:  # before the first phase() lands
            line.append(f"{self.phase_text} {elapsed // 60}:{elapsed % 60:02d}")
        if self.attempt_n:
            line.append(f"  attempt {self.attempt_n}/{self.max_attempts}",
                        style="dim")
        line.append(" · ", style="dim")
        line.append_text(self._render_context_bar())
        if len(self.llm_durs) >= 2:
            line.append(" · llm ", style="dim")
            line.append(_spark(self.llm_durs), style="cyan")
        if self.tool_count or self.llm_calls:
            line.append(
                f" · tools {self.tool_count} · files {len(self.files_touched)}",
                style="dim")
        return line

    def _render_context_bar(self):
        """Inline context fragment for the status line: pinned executor bar
        as a percentage, plus a transient reviewer/subagent bar. Exact token
        counts moved behind [t]."""
        def pct(tokens):
            return round(100 * tokens / self.num_ctx) if self.num_ctx else 0
        bar = Text(no_wrap=True, overflow="ellipsis")
        label = "executor" if self.other_label else "ctx"
        bar.append(f"{label} ", style="dim")
        bar.append_text(_context_bar(self.tokens, self.num_ctx, width=10))
        bar.append(f" {pct(self.tokens)}%", style="dim")
        sawtooth = _spark(self.ctx_hist, width=6)
        if sawtooth:  # whole-run shape: compaction dips become visible
            bar.append(f" {sawtooth}", style="cyan")
        if len(self.burn) >= 2:  # trend + time-to-full from recent samples
            (t0, k0), (t1, k1) = self.burn[0], self.burn[-1]
            if t1 - t0 > 1 and k1 > k0:
                rate = (k1 - k0) / (t1 - t0) * 60  # tokens/min
                bar.append(f" ↗ {_fmt_tok(round(rate))}/min", style="yellow")
                if self.num_ctx > k1:
                    eta = (self.num_ctx - k1) / rate
                    bar.append(f" · full ~{max(1, round(eta))}m", style="dim")
        if self.other_label:  # while reviewer/subagent/… is making calls
            bar.append(f" · {self.other_label} ", style="cyan")
            bar.append_text(_context_bar(self.other_tokens, self.num_ctx,
                                         width=6))
            bar.append(f" {pct(self.other_tokens)}%", style="dim")
        return bar

    def _render_timeline(self):
        with self._lock:  # snapshot: ui.tool() appends on the main thread
            rows = list(self.tool_rows)
            earlier = len(self.tool_history) - len(rows)
            recent = self.tool_history[-24:]
        if not rows:
            return None
        # cadence ticker: one tick per recent finished call — error clusters
        # and bursts read as texture without taking a row
        title = Text("tools")
        ticks = [r.status for r in recent if r.status != "running"]
        if len(ticks) >= 2:
            title.append(" · ", style="dim")
            for s in ticks:
                if s == "error":
                    title.append("✗", style="red")
                else:
                    title.append("·", style="dim")
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)                # status glyph
        grid.add_column(ratio=1, no_wrap=True)  # name + args
        grid.add_column(justify="right")        # duration
        grid.add_column()                       # diff stat
        # the 4 Hz get_renderable refresh animates this frame for free
        frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
        for row in rows:
            if row.status == "done":
                glyph = Text("✓", style="green")
            elif row.status == "error":
                glyph = Text("✗", style="bold red")
            else:
                glyph = Text(frame, style="yellow")
            body = Text(no_wrap=True, overflow="ellipsis")
            if row.is_subagent_header:
                glyph = glyph if row.status != "running" else Text("▸", style="magenta")
                body.append(f"subagent {row.args_short}", style="bold")
            else:
                if row.depth:
                    body.append("└ ", style="dim")
                body.append(row.name)
                if row.args_short:
                    body.append(f" {row.args_short}", style="dim")
            if row.duration is not None:
                dur = Text(f"{row.duration:.1f}s", style="dim")
            else:
                dur = Text(f"{time.monotonic() - row.started:.0f}s",
                           style="dim yellow")
            diff_cell = Text()
            if row.diff_stat:
                added, _, removed = row.diff_stat.partition(" ")
                diff_cell.append(added, style="green")
                if removed:
                    diff_cell.append(f" {removed}", style="red")
            grid.add_row(glyph, body, dur, diff_cell)
        if earlier > 0:
            grid.add_row(Text(""),
                         Text(f"{earlier} earlier calls · [t] expand",
                              style="blue"), Text(""), Text(""))
        return Panel(grid, title=title, title_align="left", border_style="dim")

    def _render_stream(self):
        """Fixed-height tail: one collapsed thinking line, a rule, and the
        last few wrapped lines of output — the panel stops changing height
        as chunks arrive."""
        if not (self.stream_buf or self.stream_thinking):
            return None
        # in the wide layout this panel shares a row with the timeline, so
        # wrap to the column it actually gets, not the full console
        half = self.console.width >= 110 and bool(self.tool_rows)
        width = max(20, (self.console.width // 2 if half
                         else self.console.width) - 8)
        stream_text = Text(no_wrap=True, overflow="ellipsis")
        if self.stream_thinking:
            squashed = " ".join(self.stream_thinking.split())
            tail = squashed[-(width - 12):]
            if len(squashed) > width - 12:
                tail = "…" + tail[1:]
            stream_text.append(f"⋯ thinking: {tail}\n", style="dim italic")
            stream_text.append("─" * width + "\n", style="dim")
        rows: list[str] = []
        for ln in self.stream_buf.splitlines():
            rows.extend([ln[i:i + width] for i in range(0, len(ln), width)]
                        or [""])
        stream_text.append("\n".join(rows[-4:]))
        # pulse: chunk arrival rate in the title; a flatlining generation
        # (the LAN box about to drop the connection) reads as "stalled"
        title = Text("streaming")
        with self._lock:
            samples = list(self.pulse)
        if len(samples) >= 2:
            age = time.monotonic() - samples[-1][0]
            shape = _spark([c for _, c in samples], width=6)
            if age > 2:
                title.append(f" · {shape} · stalled {int(age)}s",
                             style="yellow")
            else:
                dt = samples[-1][0] - samples[0][0]
                title.append(" · ", style="dim")
                title.append(shape, style="cyan")
                if dt > 1:  # need a real window or the rate is noise
                    rate = sum(c for _, c in samples) / dt / 4  # chars→tok
                    title.append(f" · ~{rate:.0f} tok/s", style="dim")
        # a Text, not a str — "[o]" would otherwise be parsed as rich markup
        subtitle = Text("[o] full transcript", style="not dim blue") \
            if _state is not None else None
        return Panel(stream_text, title=title, title_align="left",
                     subtitle=subtitle, subtitle_align="right",
                     border_style="dim")

    def _render_criteria(self):
        if not self.criteria_items:
            return None
        text = Text()
        for i, c in enumerate(self.criteria_items):
            if isinstance(c, dict):
                if c.get("met"):
                    text.append("✓ ", style="green")
                    text.append(str(c.get("criterion", "?")))
                else:
                    text.append("✗ ", style="red")
                    text.append(str(c.get("criterion", "?")))
                    note = c.get("note", "")
                    src = self.criteria_source
                    annot = " · ".join(s for s in (note, src) if s)
                    if annot:
                        text.append(f"  ← {annot[:80]}", style="dim")
            else:  # bare string: not yet reviewed
                text.append("○ ", style="dim")
                text.append(str(c), style="dim")
            if i < len(self.criteria_items) - 1:
                text.append("\n")
        return text

    def _render_todos(self):
        if not self.todo_items:
            return None
        todo_text = Text()
        for i, t in enumerate(self.todo_items):
            mark = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}[t["status"]]
            style = "dim" if t["status"] == "done" else \
                ("bold" if t["status"] == "in_progress" else "")
            todo_text.append(f"{mark} {t['text']}", style=style)
            if i < len(self.todo_items) - 1:
                todo_text.append("\n")
        return todo_text

    def _render_plan(self, wide: bool = False):
        """One panel for criteria + todos (side by side when wide) with the
        last verdict folded in as a single line — it already prints to
        scrollback when it happens, so it doesn't need its own panel."""
        crit = self._render_criteria()
        todo = self._render_todos()
        if crit is None and todo is None and not self.last_verdict:
            return None
        if wide and crit is not None and todo is not None:
            body = self._pair(crit, todo)
        else:
            body = Group(*(p for p in (crit, todo) if p is not None))
        parts = [body]
        if self.last_verdict:
            tag = "PASSED" if self.verdict_passed else "FAILED"
            summary = " ".join(self.last_verdict[:200].split())
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"review: {tag} — {summary}",
                        style="green" if self.verdict_passed else "red")
            parts.append(line)
        # a criterion that was met and then wasn't is the signal worth
        # noticing without opening the ledger. Snapshot under the lock:
        # attempt_result() sorts this list, and CPython empties a list for
        # the duration of a sort — an unlocked read can see nothing there.
        with self._lock:
            ledger = list(self.ledger)
        regressed = _regressions(ledger)
        if regressed:
            hint = Text(no_wrap=True, overflow="ellipsis")
            hint.append(f"{len(regressed)} regressed", style="bold yellow")
            hint.append(f" · {regressed[0][:60]}", style="yellow")
            hint.append(" · [a] ledger", style="dim")
            parts.append(hint)
        title = Text("plan")
        if len(self.trend) >= 2:  # is the retry loop converging or stuck?
            stuck = self.trend[-1][0] == self.trend[-2][0]
            title.append(" · ", style="dim")
            title.append(_spark([m for m, _ in self.trend]),
                         style="yellow" if stuck else "cyan")
            title.append(" " + " → ".join(f"{m}/{t}"
                                          for m, t in self.trend[-3:]),
                         style="dim")
        return Panel(Group(*parts), title=title, title_align="left",
                     border_style="dim")

    def stream_add(self, text: str, thinking: bool) -> None:
        # a live tail, not a transcript — keep only the newest chunk
        if thinking:
            self.stream_thinking = (self.stream_thinking + text)[-600:]
        else:
            self.stream_buf = (self.stream_buf + text)[-1200:]
        # ...but ALSO keep the full stream for [o], capped so a runaway
        # generation can't eat memory (thinking included — it's often the
        # part the user wants to scroll back through)
        with self._lock:
            self.pulse.append((time.monotonic(), len(text)))
            self._transcript.append(text)
            self._transcript_len += len(text)
            while self._transcript_len > 200_000 and self._transcript:
                dropped = self._transcript.popleft()
                self._transcript_len -= len(dropped)
                self._transcript_dropped += len(dropped)
        self.refresh()

    def transcript_text(self) -> str:
        with self._lock:  # [o] runs on the reader thread mid-stream
            body = "".join(self._transcript)
            dropped = self._transcript_dropped
        if dropped:
            return f"[… {dropped} chars dropped …]\n{body}"
        return body

    def transcript_reset(self) -> None:
        with self._lock:
            self._transcript.clear()
            self._transcript_len = 0
            self._transcript_dropped = 0

    def stream_clear(self) -> None:
        self.stream_buf = ""
        self.stream_thinking = ""
        with self._lock:  # stale samples would read as "stalled" next stream
            self.pulse.clear()
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
_subagent_depth = 0  # dashboard timeline nesting for the same situation
_streamed_recently = False  # plain mode: answer()/thinking() skip the re-print

_keys: KeyReader | None = None  # live only alongside _dash, and only when stdin is a TTY


class QuitRequested(Exception):
    """User pressed [q] — run.py catches this and finalizes the run."""


class ControlState:
    """All cross-thread interactive state, guarded by one Condition. The
    reader thread mutates it; safe points (poll_controls/drain_messages)
    and the 4 Hz render read it."""

    def __init__(self):
        self.cond = threading.Condition()
        self.focused = False        # [m] pressed: keys go to the input line
        self.buffer = ""            # the docked input line's contents
        self.paused = False
        self.quit_requested = False
        self.pending_msgs: list[str] = []
        self.compose_interrupt = False  # this message came from [i]
        self.interrupt = False      # skip the rest of the current tool round
        self.menu = False           # [/] preset picker is open


_state: ControlState | None = None
_input_thread: "_InputThread | None" = None


def _queue(state: ControlState, text: str, dash=None) -> None:
    """Append a message for the executor. Caller holds state.cond."""
    state.pending_msgs.append(text)
    if state.paused:
        # delivery happens before the next model call, and pause blocks
        # exactly there — a queued message that stayed paused would never
        # arrive
        state.paused = False
        if dash is not None:
            dash.paused = False


def _feed_key(state: ControlState, ch: str, dash=None) -> None:
    """Per-key state machine — the reader thread calls this in production;
    tests call it directly. Never raises, never blocks; o/t/d printing
    happens outside the lock."""
    action = None
    with state.cond:
        if state.menu:
            if ch.isdigit() and 1 <= int(ch) <= len(STEER_PRESETS):
                _queue(state, STEER_PRESETS[int(ch) - 1], dash)
                action = "queued"
            state.menu = False  # any other key, including Esc, just closes it
        elif state.focused:
            if ch in ("\r", "\n"):
                text = state.buffer.strip()
                if text:
                    _queue(state, text, dash)
                    if state.compose_interrupt:
                        state.interrupt = True
                        action = "interrupted"
                    else:
                        action = "queued"
                state.focused = False
                state.compose_interrupt = False
                state.buffer = ""
            elif ch in ("\x7f", "\x08"):
                state.buffer = state.buffer[:-1]
            elif ch == "\x1b":  # lone Esc: cancel
                state.focused = False
                state.compose_interrupt = False
                state.buffer = ""
            elif ch.isprintable():
                state.buffer += ch  # case preserved
        else:
            c = ch.lower()
            if c == "m":
                state.focused = True
                state.buffer = ""
            elif c == "i":
                # same composer, but sending also abandons whatever tool calls
                # the model queued for this round
                state.focused = True
                state.compose_interrupt = True
                state.buffer = ""
            elif c == "/":
                state.menu = True
            elif c == "c":  # not [x]: any unbound printable is the resume key
                if state.pending_msgs:
                    state.pending_msgs.clear()
                    action = "cleared"
            elif c == "e":
                if state.pending_msgs:  # reopen the last one for editing
                    state.buffer = state.pending_msgs.pop()
                    state.focused = True
            elif c == "a":
                action = "ledger"
            elif c == "b":
                action = "budget"
            elif c == "q":
                state.quit_requested = True
            elif c == "p":
                state.paused = not state.paused
                if dash is not None:
                    dash.paused = state.paused
            elif c == "o":
                action = "transcript"
            elif c == "t":
                action = "tools"
            elif c == "d":
                action = "diff"
            elif c == "z":
                if dash is not None:
                    dash.quiet = not dash.quiet
            elif state.paused and ch.isprintable():
                state.paused = False  # any other key resumes
                if dash is not None:
                    dash.paused = False
        state.cond.notify_all()
    if dash is not None:
        if action == "queued":
            dash.print(Text("message queued — delivered before the next "
                            "model call", style="dim"))
        elif action == "interrupted":
            dash.print(Text("interrupting — the rest of this tool round is "
                            "skipped and your message goes next", style="yellow"))
        elif action == "cleared":
            dash.print(Text("queued messages discarded", style="dim"))
        elif action == "transcript":
            _show_transcript()
        elif action == "tools":
            _show_tool_history()
        elif action == "diff":
            _show_last_diff()
        elif action == "ledger":
            _show_ledger()
        elif action == "budget":
            _show_budget()
        dash.refresh()


class _InputThread(threading.Thread):
    """Owns all stdin consumption while the dashboard is live. park() hands
    stdin to a cooked-mode input() (permission prompts) and blocks until the
    thread is provably idle; unpark() resumes reading."""

    def __init__(self, source, state: ControlState):
        super().__init__(daemon=True, name="ui-input")
        self._source = source  # anything with read_token(timeout) -> str|None
        self._state = state
        self._running = True
        self._park_req = threading.Event()
        self._parked = threading.Event()

    def run(self):
        warned = False
        while self._running:
            if self._park_req.is_set():
                self._parked.set()
                time.sleep(0.05)
                continue
            try:
                tok = self._source.read_token(timeout=0.1)
                if tok is not None:
                    _feed_key(self._state, tok, _dash)
            except Exception as e:
                # the thread must outlive any single bad keystroke — a dead
                # reader means [q]/[p] silently stop working for the run
                if not warned and _dash is not None:
                    warned = True
                    with contextlib.suppress(Exception):
                        _dash.print(Text(f"[WARNING] input thread error: {e}",
                                         style="bold yellow"))
                time.sleep(0.1)

    def park(self):
        self._park_req.set()
        self._parked.wait(timeout=2.0)

    def unpark(self):
        self._park_req.clear()
        self._parked.clear()

    def stop(self):
        self._running = False
        self._park_req.clear()
        if self.is_alive():
            self.join(timeout=1.0)


@contextlib.contextmanager
def _stdin_handoff():
    """Exclusive cooked-mode stdin for console.input: park the reader
    thread, then cook the tty; reverse on exit. Keys pressed before the
    prompt were already consumed live, so nothing leaks into the answer."""
    if _input_thread is None or _keys is None:
        yield
        return
    _input_thread.park()
    try:
        with _keys.suspend():
            yield
    finally:
        _input_thread.unpark()


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
    _set_title(f"agent · {model}")
    global _keys, _state, _input_thread
    if sys.stdin.isatty():  # stdout being a TTY doesn't guarantee stdin is
        _keys = KeyReader()
        _keys.start()
        _state = ControlState()
        _input_thread = _InputThread(_keys, _state)
        _input_thread.start()


def stop() -> None:
    global _dash, _keys, _state, _input_thread, _subagent_depth
    _subagent_depth = 0
    # order matters: reader thread first, so nothing touches stdin while
    # termios is being restored
    if _input_thread is not None:
        _input_thread.stop()
        _input_thread = None
    if _keys is not None:
        _keys.stop()
        _keys = None
    _state = None
    if _dash is not None:
        _set_title("")  # hand the tab title back to the shell
        _dash.stop()
        _dash = None


# ── interactive controls ([m] focus / [p]ause / [o][t][d] / [q]uit) ──
#
# The reader thread consumes keys continuously (live echo in the docked
# input line); quit/pause/messages still take effect only at safe points
# (between tool rounds, between phases). In plain mode _state is None and
# everything here is a no-op, so pytest/evals/pipes behave as before.

def poll_controls() -> None:
    """Safe-point check — no stdin I/O. May raise QuitRequested; blocks
    while paused (the PAUSED banner keeps rendering meanwhile)."""
    if _state is None:
        return
    with _state.cond:
        while True:
            if _state.quit_requested:
                _state.quit_requested = False
                if _dash:
                    _dash.paused = False
                raise QuitRequested()
            if not _state.paused:
                break
            if _dash:
                _dash.paused = True
            _state.cond.wait(timeout=0.25)
    if _dash:
        _dash.paused = False


def drain_messages() -> list[str]:
    """Queued messages, cleared on read. Empty in plain mode."""
    if _state is None:
        return []
    with _state.cond:
        msgs, _state.pending_msgs = _state.pending_msgs, []
        return msgs


def interrupt_requested() -> bool:
    """True once after [i] sent a message: the caller should abandon the tool
    calls it hasn't run yet and get back to the model. False in plain mode."""
    if _state is None:
        return False
    with _state.cond:
        flag, _state.interrupt = _state.interrupt, False
        return flag


def steer() -> str | None:
    """--interactive: pause after a failed verdict. Enter = plain retry
    (returns None), typed text = guidance for the next attempt, q = stop
    (raises QuitRequested). Non-TTY stdin never blocks — evals and pipes
    behave as if the flag were off."""
    if not sys.stdin.isatty():
        return None
    prompt = "steer the retry — Enter: continue · q: stop · or type guidance: "
    try:
        if _dash:
            _dash.live.stop()
            try:
                with _stdin_handoff():
                    ans = _dash.console.input(
                        f"[bold yellow]{rich_escape(prompt)}[/bold yellow]")
            finally:
                _dash.live.start()
                _dash.refresh()
        else:
            ans = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    ans = ans.strip()
    if ans.lower() == "q":
        raise QuitRequested()
    return ans or None


def _show_tool_history() -> None:
    if _dash is None:
        return
    with _dash._lock:  # runs on the reader thread while tools execute
        rows = list(_dash.tool_history)
    if not rows:
        _dash.print(Text("no tool calls yet", style="dim"))
        return
    text = Text()
    for i, r in enumerate(rows):
        glyph = {"done": "✓", "error": "✗"}.get(r.status, "…")
        prefix = "▸ subagent " if r.is_subagent_header else \
            ("└ " if r.depth else "")
        dur = f" {r.duration:.1f}s" if r.duration is not None else ""
        stat = f" {r.diff_stat}" if r.diff_stat else ""
        text.append(f"{glyph} {prefix}{r.name} {r.args_short}{dur}{stat}")
        if i < len(rows) - 1:
            text.append("\n")
    # llm stats live here now instead of a pinned header row
    text.append(f"\ncontext {_fmt_tok(_dash.tokens)}/{_fmt_tok(_dash.num_ctx)}"
                f" · llm {_dash.llm_calls} calls · {_dash.llm_secs:.0f}s",
                style="dim")
    if len(_dash.llm_durs) >= 2:
        text.append(" · ", style="dim")
        text.append(_spark(_dash.llm_durs), style="cyan")
    if _dash.last_llm:
        text.append(f" · last {_dash.last_llm}", style="dim")
    wall = time.monotonic() - _dash.run_started
    if wall > 1:  # where the run's life went: generating, tools, or other
        tool_secs = sum(r.duration for r in rows if r.duration)
        lp = min(100, round(100 * _dash.llm_secs / wall))
        tp = min(100 - lp, round(100 * tool_secs / wall))
        text.append(f"\nspent llm {lp}% · tools {tp}% · other {100 - lp - tp}%",
                    style="dim")
    _dash.print(Panel(text, title=f"tool history — {len(rows)} calls",
                      title_align="left", border_style="dim"))


def _show_ledger() -> None:
    """[a] — every attempt's criteria side by side, so you can see which one
    flipped rather than only how the latest review scored."""
    if _dash is None:
        return
    with _dash._lock:  # runs on the reader thread; attempt_result sorts
        snapshot = list(_dash.ledger)
    ledger = [e for e in snapshot if _ledger_criteria(e)]
    if not ledger:
        _dash.print(Text("no reviewed attempts yet", style="dim"))
        return
    shown = ledger[-6:]  # a wider grid than this stops fitting the terminal
    regressed = set(_regressions(snapshot))
    # union of criteria, in first-seen order — reviewers reword and reorder
    names: list[str] = []
    for e in shown:
        for name in _ledger_criteria(e):
            if name not in names:
                names.append(name)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(ratio=1, no_wrap=True)          # criterion
    for _ in shown:
        grid.add_column(justify="center", width=3)  # one column per attempt
    grid.add_column(no_wrap=True)                   # regression marker
    header = [Text("criterion", style="dim")]
    header += [Text(f"a{e['n']}", style="dim") for e in shown]
    header.append(Text(""))
    grid.add_row(*header)
    for name in names:
        row = [Text(name, overflow="ellipsis")]
        for e in shown:
            met = _ledger_criteria(e).get(name)
            if met is None:
                row.append(Text("·", style="dim"))
            elif met:
                row.append(Text("✓", style="green"))
            else:
                row.append(Text("✗", style="red"))
        row.append(Text("← regressed", style="yellow")
                   if name in regressed else Text(""))
        grid.add_row(*row)

    body = [grid]
    last = shown[-1]
    tail = Text()
    tag = "passed" if last.get("passed") else "fail"
    tail.append(f"a{last['n']} {tag}", style="green" if last.get("passed") else "red")
    if last.get("summary"):
        tail.append(" · " + " ".join(last["summary"].split())[:160],
                    style="dim")
    body.append(tail)
    if last.get("evidence"):
        body.append(Text(f"checks: {last['evidence']}", style="dim"))
    if last.get("focus"):
        # the retry instruction the executor actually received — otherwise
        # invisible, and the usual reason a "wrong" retry went wrong
        body.append(Text(f"retry focus → {' '.join(last['focus'].split())[:200]}",
                         style="cyan"))
    _dash.print(Panel(Group(*body), title=f"attempt ledger — {len(ledger)} reviewed",
                      title_align="left", border_style="dim"))


def _show_budget() -> None:
    """[b] — where the tokens and the wall clock actually went, split by the
    role that spent them."""
    if _dash is None:
        return
    with _dash._lock:
        roles = {k: dict(v) for k, v in _dash.roles.items()}
        per_attempt = dict(_dash.attempt_tokens)
        reclaimed = list(_dash.reclaimed)
    if not roles:
        _dash.print(Text("no model calls yet", style="dim"))
        return
    total = sum(r["tin"] + r["tout"] for r in roles.values()) or 1
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)                  # role
    for _ in range(4):
        grid.add_column(justify="right")           # calls / in / out / secs
    grid.add_column(no_wrap=True)                  # share
    grid.add_row(*[Text(h, style="dim") for h in
                   ("role", "calls", "in", "out", "secs", "share")])
    for label, r in sorted(roles.items(),
                           key=lambda kv: -(kv[1]["tin"] + kv[1]["tout"])):
        share = (r["tin"] + r["tout"]) / total
        bar = Text()
        filled = round(share * 12)
        bar.append("█" * filled, style="cyan")
        bar.append("░" * (12 - filled), style="dim")
        bar.append(f" {round(share * 100)}%", style="dim")
        grid.add_row(Text(label), Text(str(r["calls"])),
                     Text(_fmt_tok(r["tin"])), Text(_fmt_tok(r["tout"])),
                     Text(f"{r['secs']:.0f}"), bar)

    body = [grid]
    # bucket 0 is everything spent before attempt 1 was announced: the
    # goalsmith turn and, on --best-of, the whole candidate round. Filtering
    # it out hid two thirds of a best-of run from the panel meant to show
    # where the tokens went.
    setup = per_attempt.get(0, 0)
    costs = [(n, t) for n, t in sorted(per_attempt.items()) if n]
    if costs or setup:
        line = Text("per attempt  ", style="dim")
        if setup:
            line.append("setup ", style="dim")
            line.append(f"{_fmt_tok(setup)}  ", style="dim")
        for i, (n, tok) in enumerate(costs):
            # a retry that costs more than the attempt before it is the loop
            # degenerating rather than converging
            rising = i and tok > costs[i - 1][1] * 1.25
            line.append(f"a{n} ")
            line.append(_fmt_tok(tok) + (" ↗" if rising else ""),
                        style="yellow" if rising else "dim")
            line.append("  ")
        body.append(line)
    if reclaimed:
        line = Text("compaction  ", style="dim")
        line.append(_spark(reclaimed), style="cyan")
        line.append(f"  reclaimed {_fmt_tok(sum(reclaimed))} over "
                    f"{len(reclaimed)} dips", style="dim")
        body.append(line)
    _dash.print(Panel(Group(*body), title="budget", title_align="left",
                      border_style="dim"))


def _show_last_diff() -> None:
    if _dash is None:
        return
    if _dash.last_diff is None:
        _dash.print(Text("no diff yet", style="dim"))
        return
    path, diff_text = _dash.last_diff
    lines = diff_text.splitlines()
    shown = "\n".join(lines[:80])
    if len(lines) > 80:
        shown += f"\n… {len(lines) - 80} more lines"
    _dash.print(Panel(Syntax(shown, "diff", background_color="default"),
                      title=path, title_align="left", border_style="dim"))


def _show_transcript() -> None:
    if _dash is None:
        return
    body = _dash.transcript_text()
    if not body:
        _dash.print(Text("transcript is empty (nothing streamed yet)", style="dim"))
        return
    # printed above the Live region, so the terminal's own scrollback keeps it
    _dash.print(Panel(Text(body), title=f"transcript — attempt {_dash.attempt_n}",
                      title_align="left", border_style="dim"))


# ── state updates (dashboard region) ────────────────────────────────

def attempt(n: int, total: int) -> None:
    if _dash:
        if n != _dash.attempt_n:
            _dash.transcript_reset()
        _dash.attempt_n = n
        _dash.max_attempts = total
        _dash.refresh()
    else:
        print(f"\n=== EXECUTING (attempt {n}/{total}) ===")


def _set_title(text: str) -> None:
    """Mirror run state into the terminal tab title. Best effort."""
    if _dash is not None:
        with contextlib.suppress(Exception):
            _dash.console.set_window_title(text)


def phase(label: str) -> None:
    if _dash:
        _dash.phase_text = label
        _dash.phase_started = time.monotonic()
        # a new phase means the run is actively working again — the red/green
        # verdict tint is scoped to the window between verdict and next phase
        _dash.tint = "cyan"
        # extend the run rail: previous phase is finished, this one runs
        word = label.split()[0].rstrip(":").lower() if label.split() else label
        if _dash.rail and _dash.rail[-1][1] == "running":
            _dash.rail[-1][1] = "done"
        _dash.rail.append([_RAIL_NAMES.get(word, word), "running"])
        del _dash.rail[:-30]  # bound memory on very long runs
        title = f"{label} · agent"
        if _dash.attempt_n:
            title = f"{label} {_dash.attempt_n}/{_dash.max_attempts} · agent"
        _set_title(title)
        _dash.refresh()
    else:
        print(f"--- {label} ---")


def llm_stats(label: str, secs: float, prompt_tokens, eval_tokens) -> None:
    """Per-call timing/token stats, straight from the Ollama response meta."""
    tok = f"{prompt_tokens or '?'}→{eval_tokens or '?'} tok"
    line = f"[{label}] {secs:.1f}s · {tok}"
    if _dash:
        _dash.last_llm = line
        _dash.llm_calls += 1
        _dash.llm_secs += secs
        _dash.llm_durs.append(secs)
        tin, tout = int(prompt_tokens or 0), int(eval_tokens or 0)
        with _dash._lock:  # read by [b] from the reader thread
            role = _dash.roles.setdefault(
                label, {"calls": 0, "tin": 0, "tout": 0, "secs": 0.0})
            role["calls"] += 1
            role["tin"] += tin
            role["tout"] += tout
            role["secs"] += secs
            n = _dash.attempt_n
            _dash.attempt_tokens[n] = _dash.attempt_tokens.get(n, 0) + tin + tout
        _dash.refresh()
    else:
        print(f"  {line}")


def context_tokens(estimate: int, num_ctx: int, label: str = "executor") -> None:
    if _dash:
        _dash.num_ctx = num_ctx
        if label == "executor":
            # the pinned bar; an executor call also means any reviewer/
            # subagent session finished, so its transient row goes away
            # a real drop is compaction (or a context reset) handing tokens
            # back — [b] reports how much each one bought
            if _dash.ctx_hist and estimate < _dash.ctx_hist[-1] - 1000:
                _dash.reclaimed.append(_dash.ctx_hist[-1] - estimate)
            _dash.tokens = estimate
            _dash.burn.append((time.monotonic(), estimate))
            _dash.ctx_hist.append(estimate)
            _dash.other_label = ""
        else:
            _dash.other_label = label
            _dash.other_tokens = estimate
        _dash.refresh()
    else:
        print(f"  [context ~{estimate} tokens / {num_ctx}]")


def tool(name: str, arguments) -> None:
    if _dash:
        row = ToolRow(name, _short_args(name, arguments),
                      depth=_subagent_depth)
        with _dash._lock:
            _dash.tool_rows.append(row)
            _dash.tool_history.append(row)
            if len(_dash.tool_history) > 500:
                del _dash.tool_history[0]
            _dash.tool_count += 1
            _dash.tools_since_change += 1
            # spinning on the same call is the failure mode run.py only
            # catches once the whole attempt is over
            window = _dash.tool_history[-_LOOP_WINDOW:]
            sig = (row.name, row.args_short)
            same = sum(1 for r in window
                       if r.name == row.name and r.args_short == row.args_short)
            if same >= _LOOP_REPEATS:
                _dash.loop_sig = sig
                _dash.loop_warn = (f"{row.name} {row.args_short}".strip()
                                   + f" ×{same} in the last {len(window)} calls")
            elif _dash.loop_sig is not None and sig != _dash.loop_sig:
                # the agent moved on — a banner naming a call it is no longer
                # making is worse than no banner
                _dash.loop_sig = None
                _dash.loop_warn = None
        # files_touched is populated by diff()/file_created() using the real
        # path — not seeded here, which would key it by the truncated
        # args_short and leave a phantom "+0 −0" row when no diff follows
        # (new-file writes, no-op edits)
        _dash.refresh()
    else:
        args_s = _fmt_args(arguments)
        print(f"  {tool_prefix}[tool call] {name}({args_s[:200]})")


def _last_running_row() -> ToolRow | None:
    """Rightmost still-running row. Caller must hold _dash._lock. Searches
    the full history, not just the visible deque — a subagent header that
    scrolled off the 10-row window must still be completable."""
    for row in reversed(_dash.tool_history):
        if row.status == "running":
            return row
    return None


def tool_result(text: str) -> None:
    if _dash:
        with _dash._lock:
            row = _last_running_row()
            if row is not None:
                row.duration = time.monotonic() - row.started
                row.status = "error" if text.startswith("[ERROR]") else "done"
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


def criteria(items: list, source: str = "") -> None:
    """Success-criteria checklist. Items are reviewer dicts
    {criterion, met, note} or bare strings (not yet reviewed → pending)."""
    if _dash:
        _dash.criteria_items = list(items)
        _dash.criteria_source = source
        if any(isinstance(c, dict) for c in items):  # a review happened
            met = sum(1 for c in items if isinstance(c, dict) and c.get("met"))
            _dash.trend.append((met, len(items)))
            del _dash.trend[:-8]
        _dash.refresh()
    else:
        for c in items:
            if isinstance(c, dict):
                mark = "met" if c.get("met") else "unmet"
                print(f"  [criterion] {c.get('criterion', '?')}: {mark}")
            else:
                print(f"  [criterion] {c}: pending")


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
            # garbles the prompt line. _stdin_handoff parks the reader
            # thread so console.input owns stdin exclusively.
            _dash.live.stop()
            try:
                with _stdin_handoff():
                    # escape: "[y]es" would otherwise be eaten as markup tags
                    ans = _dash.console.input(
                        f"[bold yellow]{rich_escape(full)}[/bold yellow]")
            finally:
                _dash.live.start()
                _dash.refresh()
        else:
            ans = input(full)
    except (EOFError, KeyboardInterrupt):
        return "n"
    ans = ans.strip().lower()[:1]
    return ans if ans in ("y", "n", "a") else "n"


def confirm_tool(tool_name: str, detail: str, context: str = "",
                 command_scope: str | None = None) -> str:
    """Permission card for a gated tool call. Shows the full detail (command/
    code) untruncated in a panel, plus a dim context line. Returns 'y', 'n',
    'a' or — only when command_scope is given — 'c' (always allow this base
    command for the rest of the run). Anything else is a deny."""
    opts = "[y]es · [n]o · [a]lways this run"
    if command_scope:
        opts += f" · [c] always for '{command_scope}'"
    try:
        if _dash:
            _dash.live.stop()
            try:
                body = Text(detail[:2000])
                if command_scope:  # the part that earned the prompt, in red
                    body.highlight_words([command_scope], style="bold red")
                if context:
                    body.append(f"\n{context}", style="dim")
                body.append("\n\n  ")
                body.append("[y]", style="bold green")
                body.append(" allow once        ")
                body.append("[a]", style="bold cyan")
                body.append(" always this run\n  ")
                body.append("[n]", style="bold red")
                body.append(" deny")
                if command_scope:
                    body.append("              ")
                    body.append("[c]", style="bold cyan")
                    body.append(f" always for '{command_scope}'")
                _dash.print(Panel(body, title=f"permission · {tool_name}",
                                  title_align="left", border_style="yellow"))
                with _stdin_handoff():
                    ans = _dash.console.input(
                        "[bold yellow]› choose: [/bold yellow]")
            finally:
                _dash.live.start()
                _dash.refresh()
        else:
            print(f"permission · {tool_name}\n{detail[:2000]}")
            if context:
                print(context)
            ans = input(f"{opts}: ")
    except (EOFError, KeyboardInterrupt):
        return "n"
    ans = ans.strip().lower()[:1]
    valid = ("y", "n", "a", "c") if command_scope else ("y", "n", "a")
    return ans if ans in valid else "n"


def subagent_start(kind: str, task: str) -> None:
    global tool_prefix, _subagent_depth
    tool_prefix = "  └ "
    if _dash:
        # the subagent lives IN the timeline: its spawn row becomes a header
        # and its own tool calls render nested under it
        _dash.subagent = (kind, task, time.monotonic())  # + its own lane panel
        _subagent_depth = 1
        label = f"{kind}: {' '.join(task[:60].split())}"
        with _dash._lock:
            row = _last_running_row()
            if row is not None and row.name == "spawn_subagent":
                row.is_subagent_header = True
                row.args_short = label
            else:  # defensive: never lose the event even if the row is missing
                row = ToolRow("subagent", label, is_subagent_header=True)
                _dash.tool_rows.append(row)
                _dash.tool_history.append(row)
        _dash.refresh()
    else:
        print(f"  [subagent:{kind}] {' '.join(task[:120].split())}")


def subagent_end() -> None:
    global tool_prefix, _subagent_depth
    tool_prefix = ""
    _subagent_depth = 0
    if _dash:
        # the header row stays in the timeline; the parent's tool_result
        # completes it with a duration — nothing to print
        _dash.subagent = None
        _dash.refresh()
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
        _dash.verdict_passed = passed
        _dash.tint = "green" if passed else "red"
        for entry in reversed(_dash.rail):  # mark the review that just ended
            if entry[0] == "review":
                entry[1] = "pass" if passed else "fail"
                break
        _set_title(("✓ review passed" if passed else "✗ review failed")
                   + " · agent")
        style = "bold green" if passed else "bold red"
        _dash.print(Text(f"review: {'PASSED' if passed else 'FAILED'} — "
                         f"{' '.join(summary[:300].split())}", style=style))
        _dash.refresh()
    else:
        print(f"reviewer said: passed={passed} {summary!r}")


def checks(output: str) -> None:
    """The reviewer's deterministic evidence (its pytest run). Stashed as one
    line so the ledger can show WHY an attempt was judged the way it was."""
    if _dash:
        _dash.last_checks = _digest_checks(output)


def attempt_result(n: int, passed: bool, summary: str,
                   criteria: list | None = None) -> None:
    """Close out one attempt in the ledger. Called once per attempt, after
    the verdict — the per-attempt history the dashboard used to overwrite."""
    if not _dash:
        return
    entry = {"n": n, "passed": passed, "summary": summary,
             "criteria": list(criteria or []), "evidence": _dash.last_checks,
             "focus": ""}
    with _dash._lock:
        # a re-reported attempt (best-of promotion) replaces its own row
        _dash.ledger = [e for e in _dash.ledger if e["n"] != n] + [entry]
        _dash.ledger.sort(key=lambda e: e["n"])
        del _dash.ledger[:-20]
    _dash.last_checks = ""
    _dash.refresh()


def retry_focus(text: str) -> None:
    """What the executor was actually told to do next. Attaches to the most
    recent ledger entry."""
    if not _dash:
        return
    with _dash._lock:
        if _dash.ledger:
            _dash.ledger[-1]["focus"] = text
    _dash.refresh()


def success(text: str) -> None:
    if _dash:
        _dash.tint = "green"
        _set_title("✓ done · agent")
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
        # this fires DURING write_file/edit_file, so the matching timeline
        # row is still running — hang the +n −m stat on it
        added = sum(1 for l in lines
                    if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines
                      if l.startswith("-") and not l.startswith("---"))
        with _dash._lock:
            row = _last_running_row()
            if row is not None:
                row.diff_stat = f"+{added} −{removed}"
            _dash.last_diff = (path, diff_text)  # [d] reprints it on demand
        _record_file_stats(path, added, removed)
        _dash.print(Panel(Syntax(shown, "diff", background_color="default"),
                          title=path, title_align="left", border_style="dim"))
    else:
        print(shown)


def _record_file_stats(path: str, added: int, removed: int) -> None:
    """Accumulate one file mutation into the files panel. Takes the lock
    itself, so callers must not already hold it."""
    with _dash._lock:
        stats = _dash.files_touched.setdefault(
            path, {"add": 0, "rm": 0, "edits": 0, "hist": []})
        stats["add"] += added
        stats["rm"] += removed
        stats["edits"] += 1
        stats["hist"].append(added + removed)  # per-edit churn → sparkline
        del stats["hist"][:-8]
        # real progress: whatever the loop banner was complaining about, the
        # agent just wrote something
        _dash.last_change = time.monotonic()
        _dash.tools_since_change = 0
        _dash.loop_warn = None
        _dash.loop_sig = None


def file_created(name: str, added: int) -> None:
    """Record a brand-new file for the files panel. write_file routes new
    files to a concise info line instead of a full diff, so the per-file
    accounting has to be told about the additions separately — without this
    the panel would show a new file as '+0 −0 · 0 edits'."""
    if _dash and added:
        _record_file_stats(name, added, 0)
        _dash.refresh()


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
