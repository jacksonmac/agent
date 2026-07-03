"""Presentation layer for agent.py — a pluggable seam between the agent loop
and however its progress gets displayed.

Two feeds, one contract:
  * events.jsonl (via agent.log_event) is the machine-readable event stream.
  * The Renderer below is the presentation stream.
A GUI (or any other frontend) can consume either. To retarget output, just set
    ui.renderer = MyRenderer()
where MyRenderer subclasses Renderer and overrides whatever it cares about —
no change to the loop is required.

Renderer (the base class) IS the plain-text renderer: its output is byte-for-byte
what agent.py printed before this layer existed, so headless/piped runs never
regress. TerminalRenderer subclasses it to add colour/rules via `rich` when the
library is installed and stdout is a real terminal.
"""

import sys

try:
    from rich.console import Console
    from rich.rule import Rule
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:  # rich is optional — fall back to plain text
    _RICH_AVAILABLE = False

_BAR = "=" * 70


class Renderer:
    """Plain-text renderer. Reproduces agent.py's original stdout exactly.

    Every method is one display moment. Subclass and override the ones you want
    to restyle; anything you don't override keeps this plain behaviour.
    """

    # ── startup / per-run framing ──────────────────────────────────────
    def run_header(self, *, version, server, executor, reviewer, workspace,
                   num_ctx, stream, think, temp, seed):
        print(f"agent.py v{version}")
        print(f"  server:    {server}")
        print(f"  executor:  {executor}")
        print(f"  reviewer:  {reviewer}")
        print(f"  workspace: {workspace}")
        line = (f"  num_ctx:   {num_ctx} · stream={'on' if stream else 'off'} · "
                f"think={'on' if think else 'off'}")
        if temp is not None:
            line += f" · temp={temp}"
        if seed is not None:
            line += f" · seed={seed}"
        print(line)

    def attempt_banner(self, attempt, total):
        print(f"\n{_BAR}\nATTEMPT {attempt} of {total}\n{_BAR}")

    def success(self, attempt):
        print(f"\n{_BAR}\nWE DID IT — goal met on attempt {attempt}.\n{_BAR}")

    def not_verified(self, max_attempts):
        print(f"\n{_BAR}\nWARNING: goal NOT verified after {max_attempts} attempts.\n"
              f"Saving the last attempt anyway.\n{_BAR}")

    def interrupted(self):
        print("\n\nInterrupted — saving progress before exiting.")

    # ── chat loop ──────────────────────────────────────────────────────
    def round_marker(self, round_no, tokens):
        print(f"\n--- round {round_no} · ~{tokens} tokens in context ---")

    def llm_timing(self, label, secs, prompt_tokens, eval_tokens):
        print(f"  [{label}] {secs:.1f}s · {prompt_tokens}→{eval_tokens} tokens")

    def thinking(self, text):
        print(f"\n[thinking] {text}")

    def answer(self, text):
        print(f"\nAnswer: {text}")

    def tool_call(self, name, args_preview):
        print(f"\n  [tool call] {name}({args_preview})")

    def tool_result(self, result_preview):
        print(f"  [tool result] {result_preview}")

    def force_final(self, max_tool_rounds):
        print(f"\nWARNING: hit {max_tool_rounds} tool rounds — "
              "forcing a final answer without tools.")

    # ── streaming: raw live token writes ───────────────────────────────
    def stream_thinking(self, text, first):
        if first:
            sys.stdout.write("\n[thinking] ")
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_answer(self, text, first, after_thinking):
        if first:
            sys.stdout.write("\n[answer] " if after_thinking else "")
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_end(self):
        print()

    # ── review + goalsmith ─────────────────────────────────────────────
    def reviewer_verdict(self, verdict, unmet_count, feedback):
        print(f"\nReviewer verdict: {verdict}"
              + (f" — {unmet_count} unmet criteria" if unmet_count else ""))
        if feedback:
            print(f"Reviewer feedback: {feedback}")

    def reviewer_says(self, text, reasked=False):
        prefix = "Reviewer (re-asked) says: " if reasked else "\nReviewer says: "
        print(f"{prefix}{text}")

    def goalsmith_start(self):
        print("\nAsking the model to write a GOAL and TASK from your prompt...")

    def goal_task(self, goal, task, criteria=None):
        print(f"\nGOAL: {goal}\nTASK: {task}")
        if criteria:
            print("CRITERIA:")
            for i, c in enumerate(criteria, 1):
                print(f"  {i}. {c}")

    # ── stall detection ────────────────────────────────────────────────
    def stall(self, ratio):
        print(f"\nSTALL DETECTED (similarity {ratio:.2f}) — skipping review, "
              "demanding a different approach.")

    def stall_bump(self, temp):
        print(f"  [stall] bumping temperature to {temp:.1f}")

    # ── summaries ──────────────────────────────────────────────────────
    def timing_summary(self, total_time, token_totals, chars_per_token):
        print("\n─── run summary ───")
        for label, times in total_time.items():
            tin, tout = token_totals.get(label, [0, 0])
            print(f"  {label}: {len(times)} call(s), total {sum(times):.1f}s, "
                  f"avg {sum(times) / len(times):.1f}s, tokens {tin} in / {tout} out")
        grand_in = sum(v[0] for v in token_totals.values())
        grand_out = sum(v[1] for v in token_totals.values())
        print(f"  TOTAL: {sum(sum(t) for t in total_time.values()):.1f}s of LLM time, "
              f"{grand_in} in / {grand_out} out tokens "
              f"(calibrated ~{chars_per_token:.1f} chars/token)")

    def run_summary(self, status, attempts, workspace):
        print(f"\nRUN SUMMARY\n  status:    {status}\n  attempts:  {attempts}\n"
              f"  workspace: {workspace}")

    # ── generic styled passthroughs (text kept verbatim) ───────────────
    def info(self, text):
        print(text)

    def note(self, text):
        print(text)

    def warning(self, text):
        print(text)

    def error(self, text):
        print(text)


class TerminalRenderer(Renderer):
    """Colourful renderer for a real terminal, powered by `rich`.

    Overrides only the moments that benefit from colour or rules; everything
    else inherits the plain implementation above.
    """

    def __init__(self):
        # markup=False: bracketed text like "[tool call]" or "[stall]" is data,
        # not rich markup, and must print literally. highlight=False: don't
        # auto-recolour numbers/paths in otherwise-styled lines.
        self.console = Console(markup=False, highlight=False)
        self._err = Console(stderr=True, markup=False, highlight=False)

    def _rule(self, label, style):
        self.console.print()
        self.console.print(Rule(label, style=style, characters="="))

    def run_header(self, *, version, server, executor, reviewer, workspace,
                   num_ctx, stream, think, temp, seed):
        c = self.console
        c.print(f"agent.py v{version}", style="bold cyan")
        for field, value in (("server", server), ("executor", executor),
                             ("reviewer", reviewer), ("workspace", workspace)):
            c.print(f"  {field}:{' ' * (9 - len(field))}", style="dim", end="")
            c.print(str(value), style="white")
        opts = (f"num_ctx={num_ctx} · stream={'on' if stream else 'off'} · "
                f"think={'on' if think else 'off'}")
        if temp is not None:
            opts += f" · temp={temp}"
        if seed is not None:
            opts += f" · seed={seed}"
        c.print(f"  {opts}", style="dim")

    def attempt_banner(self, attempt, total):
        self._rule(f"ATTEMPT {attempt} of {total}", "bold blue")

    def success(self, attempt):
        self._rule(f"WE DID IT — goal met on attempt {attempt}", "bold green")

    def not_verified(self, max_attempts):
        self._rule(f"goal NOT verified after {max_attempts} attempts — "
                   "saving last attempt", "bold red")

    def interrupted(self):
        self.console.print("\n\nInterrupted — saving progress before exiting.",
                           style="yellow")

    def round_marker(self, round_no, tokens):
        self.console.print(
            f"\n--- round {round_no} · ~{tokens} tokens in context ---", style="dim")

    def llm_timing(self, label, secs, prompt_tokens, eval_tokens):
        self.console.print(
            f"  [{label}] {secs:.1f}s · {prompt_tokens}→{eval_tokens} tokens",
            style="dim")

    def thinking(self, text):
        self.console.print(f"\n[thinking] {text}", style="dim italic")

    def answer(self, text):
        self.console.print("\nAnswer:", style="bold green", end=" ")
        self.console.print(text)

    def tool_call(self, name, args_preview):
        self.console.print("\n  [tool call] ", style="bold magenta", end="")
        self.console.print(f"{name}({args_preview})", style="magenta")

    def tool_result(self, result_preview):
        self.console.print("  [tool result] ", style="bold cyan", end="")
        self.console.print(result_preview, style="cyan")

    def force_final(self, max_tool_rounds):
        self.console.print(
            f"\nWARNING: hit {max_tool_rounds} tool rounds — "
            "forcing a final answer without tools.", style="yellow")

    def stream_thinking(self, text, first):
        if first:
            self.console.print("\n[thinking] ", style="dim italic", end="")
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_answer(self, text, first, after_thinking):
        if first:
            if after_thinking:
                self.console.print("\n[answer] ", style="bold green", end="")
        sys.stdout.write(text)
        sys.stdout.flush()

    def reviewer_verdict(self, verdict, unmet_count, feedback):
        style = "bold green" if verdict.upper() == "PASS" else "bold red"
        self.console.print(f"\nReviewer verdict: ", end="")
        self.console.print(verdict, style=style, end="")
        self.console.print(f" — {unmet_count} unmet criteria" if unmet_count else "",
                           style="red")
        if feedback:
            self.console.print(f"Reviewer feedback: {feedback}", style="yellow")

    def reviewer_says(self, text, reasked=False):
        prefix = "Reviewer (re-asked) says: " if reasked else "\nReviewer says: "
        self.console.print(f"{prefix}{text}", style="yellow")

    def goalsmith_start(self):
        self.console.print(
            "\nAsking the model to write a GOAL and TASK from your prompt...",
            style="cyan")

    def goal_task(self, goal, task, criteria=None):
        c = self.console
        c.print("\nGOAL: ", style="bold cyan", end="")
        c.print(goal)
        c.print("TASK: ", style="bold cyan", end="")
        c.print(task)
        if criteria:
            c.print("CRITERIA:", style="bold cyan")
            for i, crit in enumerate(criteria, 1):
                c.print(f"  {i}. {crit}")

    def stall(self, ratio):
        self.console.print(
            f"\nSTALL DETECTED (similarity {ratio:.2f}) — skipping review, "
            "demanding a different approach.", style="yellow")

    def stall_bump(self, temp):
        self.console.print(f"  [stall] bumping temperature to {temp:.1f}",
                           style="yellow")

    def timing_summary(self, total_time, token_totals, chars_per_token):
        c = self.console
        c.print()
        c.print(Rule("run summary", style="dim"))
        for label, times in total_time.items():
            tin, tout = token_totals.get(label, [0, 0])
            c.print(f"  {label}: {len(times)} call(s), total {sum(times):.1f}s, "
                    f"avg {sum(times) / len(times):.1f}s, tokens {tin} in / {tout} out",
                    style="dim")
        grand_in = sum(v[0] for v in token_totals.values())
        grand_out = sum(v[1] for v in token_totals.values())
        c.print(f"  TOTAL: {sum(sum(t) for t in total_time.values()):.1f}s of LLM time, "
                f"{grand_in} in / {grand_out} out tokens "
                f"(calibrated ~{chars_per_token:.1f} chars/token)", style="bold dim")

    def run_summary(self, status, attempts, workspace):
        c = self.console
        colour = {"passed": "green", "interrupted": "yellow"}.get(status, "red")
        c.print("\nRUN SUMMARY", style="bold")
        c.print("  status:    ", style="dim", end="")
        c.print(status, style=colour)
        c.print(f"  attempts:  {attempts}", style="dim")
        c.print(f"  workspace: {workspace}", style="dim")

    def info(self, text):
        self.console.print(text)

    def note(self, text):
        self.console.print(text, style="dim")

    def warning(self, text):
        self.console.print(text, style="yellow")

    def error(self, text):
        self._err.print(text, style="bold red")


class _UI:
    """Thin facade the loop talks to. Delegates every call to the active
    renderer, so swapping `ui.renderer` retargets all output at once."""

    def __init__(self, renderer):
        self.renderer = renderer

    def __getattr__(self, name):
        # Any method not defined here is forwarded to the current renderer.
        return getattr(self.renderer, name)


def default_renderer():
    """Colour terminal when rich is present and stdout is a real TTY;
    plain text otherwise (pipes, files, no-rich installs)."""
    if _RICH_AVAILABLE and sys.stdout.isatty():
        return TerminalRenderer()
    return Renderer()


# The singleton the rest of the program imports and uses.
ui = _UI(default_renderer())
