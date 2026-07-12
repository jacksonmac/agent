"""The executor's self-maintained checklist (set_todos tool).

One module-level list, mirroring runlog.current: run.py resets it per run,
the tool replaces it wholesale on every call, the UI and the reviewer read it.
"""

from __future__ import annotations

import re

from . import runlog, ui

STATUSES = ("pending", "in_progress", "done")
_MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}

current: list[dict] = []  # [{"text": str, "status": str}]


def reset() -> None:
    current.clear()


def render() -> str:
    if not current:
        return "(no todos recorded)"
    return "\n".join(f"{_MARKS[t['status']]} {t['text']}" for t in current)


_STEP_PAT = re.compile(r"^\s*\d+[.)]\s+(.+)")


def seed_from_plan(plan: str) -> int:
    """Parse the plan turn's numbered steps into a fresh pending checklist,
    so the executor starts from concrete phases instead of prose. Returns
    the number of steps seeded; 0 (nothing parseable) leaves todos alone
    and the model is told to call set_todos itself."""
    steps = []
    for line in plan.splitlines():
        m = _STEP_PAT.match(line)
        if m:
            text = m.group(1).strip().strip("*_").strip()
            if text:
                steps.append(text[:160])
    steps = steps[:12]
    if len(steps) < 2:
        return 0
    current[:] = [{"text": s, "status": "pending"} for s in steps]
    ui.todos(current)
    runlog.log_event("todos_seeded", items=[s[:80] for s in steps])
    return len(steps)


def set_todos(todos) -> str:
    """Replace the checklist. Validates everything before mutating, so a
    malformed call never destroys the existing list."""
    if not isinstance(todos, list) or not todos:
        return "[ERROR] todos must be a non-empty list of {text, status} objects"
    cleaned = []
    for i, item in enumerate(todos, 1):
        if not isinstance(item, dict):
            return f"[ERROR] todo {i} is not an object: {item!r}"
        text = str(item.get("text") or "").strip()
        if not text:
            return f"[ERROR] todo {i} has no text"
        status = item.get("status") or "pending"
        if status not in STATUSES:
            return (f"[ERROR] todo {i} has invalid status {status!r} "
                    f"(use one of: {', '.join(STATUSES)})")
        cleaned.append({"text": text, "status": status})
    current[:] = cleaned
    ui.todos(current)
    runlog.log_event("todos", items=[f"{t['status']}:{t['text'][:80]}" for t in current])
    return "Todo list updated:\n" + render()
