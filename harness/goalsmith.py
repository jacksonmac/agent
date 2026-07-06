"""Smart goal (-sg): rewrite a rough user prompt into (goal, criteria, task)."""

import re

from .config import settings
from .llm import _role_options, chat_v2
from .prompts import GOALSMITH_SYSTEM

GOAL_CRIT_TASK_PAT = re.compile(
    r"GOAL:\s*(.+?)\s*CRITERIA:\s*(.+?)\s*TASK:\s*(.+)", re.DOTALL | re.IGNORECASE)
GOAL_TASK_PAT = re.compile(r"GOAL:\s*(.+?)\s*TASK:\s*(.+)", re.DOTALL | re.IGNORECASE)
_CRIT_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*(.+)$")


def _parse_criteria(block: str) -> list[str]:
    out = []
    for line in block.splitlines():
        m = _CRIT_LINE.match(line)
        if m:
            out.append(m.group(1).strip())
    return out


def make_goal_task(model: str, prompt: str) -> tuple[str, str, list[str]]:
    """Have the LM rewrite a rough user prompt into (goal, task, criteria)."""
    reply = chat_v2(
        model,
        GOALSMITH_SYSTEM,
        f"User request: {prompt}",
        tool_schemas=None,
        think=settings.goalsmith.think,
        label="goalsmith",
        options=_role_options(settings.goalsmith),
    )
    m = GOAL_CRIT_TASK_PAT.search(reply)
    if m:
        goal, crit_block, task = (m.group(1).strip(), m.group(2), m.group(3).strip())
        criteria = _parse_criteria(crit_block)
        print(f"\nGOAL: {goal}")
        for i, c in enumerate(criteria, 1):
            print(f"  {i}. {c}")
        print(f"TASK: {task}\n")
        return goal, task, criteria
    m = GOAL_TASK_PAT.search(reply)  # older format, no criteria — still usable
    if m:
        goal, task = m.group(1).strip(), m.group(2).strip()
        print(f"\nGOAL: {goal}\nTASK: {task}\n")
        return goal, task, []
    print("[WARNING] could not parse GOAL/TASK from model reply, "
          "using your prompt as both")
    return prompt, prompt, []
