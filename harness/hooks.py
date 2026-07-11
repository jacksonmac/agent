"""Observe-only hooks: user shell commands fired on harness events.

Configured from a repo-root hooks.json:

    {"pre_tool":  [{"match": "write_file", "run": "echo about to write {file}"}],
     "post_tool": [{"match": "write_file", "run": "black {workspace}/{file}"}],
     "attempt_end": [{"run": "notify-send 'attempt {attempt}: {passed}'"}],
     "run_end":  [{"run": "echo done >> {run_dir}/hooks.log"}]}

Placeholders per event: pre/post_tool -> {tool} {file} {run_dir} {workspace};
attempt_end -> {attempt} {passed} {run_dir} {workspace}; run_end -> {passed}
{run_dir} {workspace}. Unknown placeholders become "".

Hooks are strictly observers: their exit codes and output never block or
modify anything. Failures (bad command, nonzero exit, timeout) warn and the
run continues. Commands run through the shell — hooks.json is user-authored
repo config, trusted like the code itself.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess

from . import runlog, ui

HOOK_TIMEOUT = 10  # seconds per hook command

_hooks: dict = {}     # event -> [{"match": ..., "run": ...}]
_context: dict = {}   # {"run_dir": ..., "workspace": ...}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def configure(path: str | None, run_dir: str = "", workspace: str = "") -> None:
    """Load hooks.json (missing file = no hooks). Resets all state."""
    global _hooks, _context
    _hooks = {}
    _context = {"run_dir": run_dir, "workspace": workspace}
    if not path:
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as e:
        ui.warn(f"could not load hooks from {path}: {e}")
        return
    if isinstance(data, dict):
        _hooks = {k: v for k, v in data.items() if isinstance(v, list)}


def fire(event: str, **fields) -> None:
    """Run every hook registered for this event. Never raises."""
    entries = _hooks.get(event)
    if not entries:
        return
    tool = fields.get("tool", "")
    ctx = _SafeDict({**_context, **fields})
    for entry in entries:
        if not isinstance(entry, dict) or "run" not in entry:
            continue
        if event in ("pre_tool", "post_tool") and \
                not fnmatch.fnmatch(tool, entry.get("match", "*")):
            continue
        cmd = str(entry["run"]).format_map(ctx)
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, timeout=HOOK_TIMEOUT)
        except Exception as e:
            ui.warn(f"hook failed ({event}): {e}")
            continue
        output = (proc.stdout + proc.stderr).strip()[:1000]
        runlog.log_event("hook", hook=event, cmd=cmd[:200],
                         exit=proc.returncode, output=output)
        if proc.returncode != 0:
            ui.warn(f"hook exited {proc.returncode} ({event}): {cmd[:120]}")
