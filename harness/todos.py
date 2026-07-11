"""The executor's self-maintained checklist (set_todos tool).

One module-level list, mirroring runlog.current: run.py resets it per run,
the tool replaces it wholesale on every call, the UI and the reviewer read it.
"""

from __future__ import annotations

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
