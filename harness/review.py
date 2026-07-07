"""Structured reviewer: JSON verdicts with a criteria checklist.

The reviewer judges artifacts, not claims: it gets the workspace listing,
the changed files' content, automated check output, and (by default)
read-only tools to inspect or run the code itself. Malformed replies fall
back through: strict re-ask → YES/NO regex → default NO. Never aborts.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

from . import ui
from .config import settings
from .llm import _role_options, cap, chat_v2, truncate_middle
from .prompts import REVIEW_USER, REVIEWER_SYSTEM
from .workspace import Workspace

YES_PAT = re.compile(r"^(yes|y|yeah|yep|yup)\b", re.IGNORECASE)
NO_PAT = re.compile(r"^(no|n|nah|nope)\b", re.IGNORECASE)


@dataclass
class Verdict:
    passed: bool
    criteria: list = field(default_factory=list)  # {criterion, met, note}
    feedback: str = ""
    raw: str = ""

    def unmet(self) -> list[str]:
        out = []
        for c in self.criteria:
            if not c.get("met"):
                note = c.get("note", "")
                out.append(f"{c.get('criterion', '?')}" + (f" ({note})" if note else ""))
        return out

    def summary(self) -> str:
        """One block of text for logs, transcripts and retry messages."""
        parts = [self.feedback] if self.feedback else []
        if self.unmet():
            parts.append("Unmet: " + "; ".join(self.unmet()))
        return "\n".join(parts) or ("passed" if self.passed else self.raw)


_FENCE_PAT = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Find and parse the first balanced {...} block, tolerating code fences
    and prose around it."""
    m = _FENCE_PAT.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_verdict(text: str) -> Verdict | None:
    """JSON verdict → Verdict, or None if the reply isn't usable."""
    data = _extract_json(text)
    if not isinstance(data, dict) or "pass" not in data:
        return None
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        criteria = []
    criteria = [c for c in criteria if isinstance(c, dict)]
    return Verdict(passed=bool(data["pass"]),
                   criteria=criteria,
                   feedback=str(data.get("feedback") or ""),
                   raw=text)


# read-only-ish subset the reviewer may use to gather evidence
_REVIEWER_TOOL_NAMES = ("read_file", "list_files", "run_script", "run_shell")


def reviewer_tool_schemas() -> list:
    from .tools import TOOL_SCHEMAS
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] in _REVIEWER_TOOL_NAMES]


def automated_checks(ws: Workspace) -> str:
    """Deterministic evidence for the reviewer: if the workspace contains
    pytest-style tests, run them and return the capped output."""
    tests = [f for f in ws.list_all_files()
             if f.rsplit("/", 1)[-1].startswith("test_") and f.endswith(".py")]
    if not tests:
        return "(none)"
    try:
        proc = subprocess.run(
            ["python3", "-m", "pytest", "-q", *tests],
            capture_output=True, text=True, timeout=120, cwd=ws.root,
        )
    except subprocess.TimeoutExpired:
        return "pytest timed out after 120 seconds"
    except FileNotFoundError:
        return "(pytest not available)"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return f"$ pytest -q {' '.join(tests)}\n{truncate_middle(out, 2_000)}\nexit code: {proc.returncode}"


def review(model: str, goal: str, output: str, ws: Workspace,
           criteria: list[str] | None = None,
           changed_files: list[str] | None = None) -> Verdict:
    """Ask the reviewer whether the goal was met. Always returns a Verdict."""
    criteria_text = ("\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
                     if criteria else
                     "(none provided — derive 3-6 binary-checkable criteria from the goal)")
    listing = "\n".join(ws.list_all_files()) or "(the workspace is empty)"
    files_text = ws.snapshot_files(changed_files or [])
    user = REVIEW_USER.format(
        goal=goal,
        criteria=criteria_text,
        output=cap(output, settings.retry_prev_max),
        listing=listing,
        files=files_text,
        checks=automated_checks(ws),
    )
    schemas = reviewer_tool_schemas() if settings.reviewer_tools else None

    raw = ""
    for strict in (False, True):
        raw = chat_v2(
            model,
            REVIEWER_SYSTEM,
            user + ("\n\nREMINDER: reply with ONLY the JSON object described in "
                    "your instructions — no prose, no code fences." if strict else ""),
            tool_schemas=schemas,
            think=settings.reviewer.think,
            max_tool_rounds=5,
            label="reviewer",
            options=_role_options(settings.reviewer),
        ).strip()
        verdict = parse_verdict(raw)
        if verdict is not None:
            return verdict
        ui.warn("review reply wasn't valid verdict JSON, re-asking once")

    # last-ditch: the old YES/NO reading, then a default NO — never abort the run
    ui.warn("review reply still malformed — falling back to YES/NO parsing")
    if YES_PAT.match(raw):
        return Verdict(passed=True, raw=raw)
    return Verdict(passed=False,
                   feedback=raw if NO_PAT.match(raw)
                   else "reviewer verdict was malformed: " + raw[:500],
                   raw=raw)
