"""Code-execution tools: python snippets, saved scripts, and an allowlisted shell.

All run with cwd inside the run's workspace, so scripts and generated
files land there — never in the repo.
"""

import os
import shlex
import subprocess

from ..workspace import Workspace


def run_python(ws: Workspace, code: str) -> str:
    """Execute python code in a subprocess."""
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=120,
            cwd=ws.root,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] code timed out after 120 seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


def run_script(ws: Workspace, name: str, args: list = None) -> str:
    """Run a saved python script from the workspace with optional arguments."""
    try:
        path = ws.resolve(name)
    except ValueError:
        return f"[ERROR] refusing to run outside the workspace: {name}"
    if not os.path.isfile(path):
        return f"[ERROR] no such file: {name} (use list_files to see the workspace)"
    argv = [str(a) for a in (args or [])]
    try:
        proc = subprocess.run(
            ["python3", path, *argv],
            capture_output=True, text=True, timeout=120,
            cwd=ws.root,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] script timed out after 120 seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


ALLOWED_COMMANDS = ["pip", "pip3", "python3", "pytest", "ls", "mkdir", "cat", "echo"]
_SHELL_META = set(";|&<>`$\n")


def run_shell(ws: Workspace, command: str) -> str:
    """Run an allowlisted shell command. shell=False + shlex so the allowlist
    can't be bypassed with 'echo hi; curl ... | sh' style chaining."""
    if any(ch in _SHELL_META for ch in command):
        return ("[ERROR] shell metacharacters (; | & < > ` $) are not allowed. "
                "Run one plain command at a time.")
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"[ERROR] could not parse command: {e}"
    if not parts:
        return "[ERROR] empty command"
    if parts[0] not in ALLOWED_COMMANDS:
        return (f"[ERROR] command '{parts[0]}' is not allowed. "
                f"Allowed commands: {', '.join(ALLOWED_COMMANDS)}")
    try:
        print("RUN_SHELL", command)
        proc = subprocess.run(
            parts,
            capture_output=True, text=True, timeout=360,
            cwd=ws.root,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out after 360 seconds"
    except FileNotFoundError:
        return f"[ERROR] program not found: {parts[0]}"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"
