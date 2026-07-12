"""spawn_subagent: run a scoped child Session and return only its summary.

The child gets a fresh conversation (it cannot see the parent's history), the
executor model, and the normal tool belt minus spawn_subagent (no recursion)
and set_todos (the checklist belongs to the parent's run narrative). Only the
child's final text comes back to the parent, capped like any tool result.
"""

from .. import runlog, ui
from ..config import settings

_depth = 0  # recursion guard: subagents may not spawn subagents


def spawn_subagent(task: str, kind: str = "general") -> str:
    global _depth
    if _depth >= 1:
        return "[ERROR] subagents cannot spawn subagents — do this subtask yourself"
    task = str(task or "").strip()
    if not task:
        return "[ERROR] spawn_subagent needs a self-contained task description"

    # late imports: tools/__init__ imports this module before TOOL_SCHEMAS
    # exists, and llm imports tools lazily — same convention as Session.send
    from ..llm import Session, _role_options
    from ..prompts import SUBAGENT_SYSTEM
    from ..skills import system_prompt_block
    from . import TOOL_SCHEMAS

    child_schemas = [s for s in TOOL_SCHEMAS
                     if s["function"]["name"] not in ("spawn_subagent", "set_todos")]
    model = settings.executor_model or settings.model
    child = Session(model, SUBAGENT_SYSTEM + system_prompt_block(), child_schemas,
                    think=settings.executor.think,
                    max_tool_rounds=settings.subagent_max_rounds,
                    label="subagent", options=_role_options(settings.executor))

    ui.subagent_start(kind, task)
    runlog.log_event("subagent_start", kind=kind, task=task[:300])
    _depth += 1
    try:
        summary = child.send(task)
    finally:
        _depth -= 1
        ui.subagent_end()
    runlog.log_event("subagent_end", kind=kind, rounds=child.last_tool_calls,
                     chars=len(summary))
    return summary
