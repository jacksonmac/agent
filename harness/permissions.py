"""Permission gate for code-executing tools.

run_shell / run_python / run_script spawn subprocesses — that's the risk
boundary (file tools are jailed to the workspace and reversible; web tools
are read-only). By default a gated call pauses for interactive y/n/a
approval; --yolo disables the gate; non-interactive sessions (evals, pipes,
cron) auto-deny with an error the model can react to.

The gate sits in tools.execute_tool_call, so the executor, reviewer, and
subagents all share it — nothing executes code without approval, and an
"always" grant covers the whole run.
"""

from __future__ import annotations

import sys

from . import ui
from .runlog import log_event

GATED = frozenset({"run_shell", "run_python", "run_script"})

_yolo = False
_always: set = set()        # tools granted "always" this run
_warned_non_tty = False


def _interactive() -> bool:  # wrapped for testability
    return sys.stdin.isatty()


def configure(yolo: bool) -> None:
    """Set the gate for this run. Resets per-run grants."""
    global _yolo, _always, _warned_non_tty
    _yolo = yolo
    _always = set()
    _warned_non_tty = False


def check(name: str, arguments) -> str | None:
    """None = allowed; otherwise an '[ERROR] permission denied ...' string
    returned to the model instead of running the tool."""
    global _warned_non_tty
    if _yolo or name not in GATED or name in _always:
        return None

    if not _interactive():
        if not _warned_non_tty:
            _warned_non_tty = True
            ui.warn("permission prompts need a terminal — code-executing tools "
                    "will be denied (use --yolo for non-interactive runs)")
        log_event("permission", tool=name, decision="auto-deny")
        return (f"[ERROR] permission denied: '{name}' requires interactive "
                f"approval and this session is non-interactive. Re-run with "
                f"--yolo to allow execution tools, or accomplish the task "
                f"without running code.")

    args_s = str(arguments)[:120]
    ans = ui.confirm(f"allow {name}? {args_s}")
    if ans == "a":
        _always.add(name)
        log_event("permission", tool=name, decision="always")
        return None
    if ans == "y":
        log_event("permission", tool=name, decision="allow")
        return None
    log_event("permission", tool=name, decision="deny")
    return (f"[ERROR] permission denied by user for '{name}'. Do not retry "
            f"this exact call; explain what you wanted to run and continue "
            f"another way.")
