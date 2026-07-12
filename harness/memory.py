"""Persistent memory: after each run, distill durable lessons into the
workspace AGENT.md so future runs on that workspace start smarter.

The note is written by the goalsmith model (cheap, no tools) and appended as
a dated section; cli._load_agent_md injects the file back into the executor
system prompt at startup. A user-authored preamble (anything before the
first '## ' section) is never touched or counted against the budget."""

from __future__ import annotations

import os
import time

from . import ui
from .config import settings
from .llm import _role_options, cap, chat_v2, truncate_middle
from .prompts import MEMORY_SYSTEM
from .runlog import log_event
from .workspace import Workspace

AGENT_MD_MAX = 6_000  # combined budget for dated sections after trimming
NOTE_MAX = 1_500


def update_agent_md(ws: Workspace, goal: str, passed: bool,
                    files: list[str], feedback_history: list[str]) -> None:
    """Append a lessons note to <workspace>/AGENT.md. Never raises — a run
    that already finished must not crash on its memory bookkeeping."""
    if not settings.memory:
        return
    try:
        note = _make_note(goal, passed, files, feedback_history)
        if not note:
            ui.warn("memory note skipped: model returned no usable bullets")
            return
        path = os.path.join(ws.root, "AGENT.md")
        existing = ""
        if os.path.isfile(path):
            with open(path) as f:
                existing = f.read()
        header = (f"\n\n## {time.strftime('%Y-%m-%d')} — {goal[:80]} "
                  f"({'passed' if passed else 'failed'})\n")
        with open(path, "w") as f:
            f.write(_trim_sections(existing + header + note + "\n", AGENT_MD_MAX))
        log_event("memory", chars=len(note), passed=passed)
    except Exception as e:
        ui.warn(f"memory note skipped: {e}")


def _make_note(goal: str, passed: bool, files: list[str],
               feedback_history: list[str]) -> str:
    user = (f"GOAL: {goal}\n"
            f"OUTCOME: {'passed' if passed else 'failed'}\n"
            f"FILES: {', '.join(files[:30]) or '(none)'}\n"
            f"REVIEWER FEEDBACK:\n"
            + (cap("\n".join(feedback_history), 2_000) or "(none)"))
    reply = chat_v2(settings.goalsmith_model or settings.model,
                    MEMORY_SYSTEM, user, tool_schemas=None, think=False,
                    label="memory", options=_role_options(settings.goalsmith))
    # keep only bullet lines — strips any "Here are the lessons:" preamble
    bullets = [line.strip() for line in reply.splitlines()
               if line.strip().startswith("-")]
    return truncate_middle("\n".join(bullets), NOTE_MAX)


def _trim_sections(text: str, max_chars: int) -> str:
    """Keep the user preamble untouched; keep the newest '## ' sections that
    fit in max_chars, dropping the oldest."""
    idx = text.find("\n## ")
    if idx == -1:
        return text
    preamble, rest = text[:idx], text[idx:]
    sections = ["\n## " + s for s in rest.split("\n## ") if s]
    kept: list[str] = []
    used = 0
    for section in reversed(sections):  # newest last → keep from the end
        if used + len(section) > max_chars and kept:
            break
        kept.insert(0, section)
        used += len(section)
    return preamble + "".join(kept)
