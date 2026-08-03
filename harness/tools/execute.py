"""Code-execution tools: python snippets, saved scripts, and an allowlisted shell.

All run with cwd inside the run's workspace, so scripts and generated
files land there — never in the repo. With --sandbox they run inside a
per-run Docker container instead (workspace mounted at /ws), which is why
the shell allowlist is waived there.

The allowlist itself comes from the active policy (policy.shell), not from
a constant here — see harness/policy.py.
"""

import atexit
import os
import shlex
import subprocess

from .. import policy
from ..config import settings
from ..workspace import Workspace

# ── --sandbox: one long-lived container per workspace ────────────────
# Long-lived (not one docker run per call) so pip installs persist across
# tool calls within a run. Keyed by workspace root: --best-of candidates
# each get their own mount.

_containers: dict[str, str] = {}


def _stop_containers() -> None:
    for name in _containers.values():
        try:
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    _containers.clear()


def _ensure_container(ws: Workspace) -> tuple[str | None, str]:
    """The sandbox container for this workspace, started on first use.
    Returns (name, "") or (None, "[ERROR] ...") — errors go back to the
    model like any tool failure."""
    if ws.root in _containers:
        return _containers[ws.root], ""
    name = f"agent-sandbox-{os.getpid()}-{len(_containers)}"
    try:
        proc = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name,
             "-v", f"{ws.root}:/ws", "-w", "/ws",
             settings.sandbox_image, "sleep", "infinity"],
            capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return None, "[ERROR] --sandbox: docker is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, ("[ERROR] --sandbox: docker took too long to start "
                      f"(is the image {settings.sandbox_image} still pulling?)")
    if proc.returncode != 0:
        return None, ("[ERROR] --sandbox: could not start the container: "
                      + proc.stderr.strip()[:400])
    if not _containers:
        atexit.register(_stop_containers)
    _containers[ws.root] = name
    return name, ""


def _run(ws: Workspace, argv: list[str], timeout: int, what: str) -> str:
    """Run argv in the workspace — directly, or via docker exec when
    --sandbox is on (the container's workdir is already /ws)."""
    if settings.sandbox:
        name, err = _ensure_container(ws)
        if err:
            return err
        argv = ["docker", "exec", name, *argv]
        cwd = None
    else:
        cwd = ws.root
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return f"[ERROR] {what} timed out after {timeout} seconds"
    except FileNotFoundError:
        return f"[ERROR] program not found: {argv[0]}"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


# ── the tools ────────────────────────────────────────────────────────

def run_python(ws: Workspace, code: str) -> str:
    """Execute python code in a subprocess."""
    return _run(ws, ["python3", "-c", code], timeout=120, what="code")


def run_script(ws: Workspace, name: str, args: list = None) -> str:
    """Run a saved python script from the workspace with optional arguments."""
    try:
        path = ws.resolve(name)
    except ValueError:
        return f"[ERROR] refusing to run outside the workspace: {name}"
    if not os.path.isfile(path):
        return f"[ERROR] no such file: {name} (use list_files to see the workspace)"
    # relative to the workspace, so the same path works at /ws in the sandbox
    rel = os.path.relpath(path, ws.root)
    argv = [str(a) for a in (args or [])]
    return _run(ws, ["python3", rel, *argv], timeout=120, what="script")


_SHELL_META = set(";|&<>`$\n")


def run_shell(ws: Workspace, command: str) -> str:
    """Run a shell command. Under shell.mode 'allowlist': one plain command
    from policy.shell.allowed, shell=False + shlex so the allowlist can't be
    bypassed with 'echo hi; curl ... | sh' style chaining. Under 'any' — or
    inside --sandbox, where the container is the guardrail — the model gets a
    real shell."""
    if settings.sandbox or policy.current.shell.mode == "any":
        return _run(ws, ["sh", "-c", command], timeout=360, what="command")
    if any(ch in _SHELL_META for ch in command):
        return ("[ERROR] shell metacharacters (; | & < > ` $) are not allowed. "
                "Run one plain command at a time.")
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"[ERROR] could not parse command: {e}"
    if not parts:
        return "[ERROR] empty command"
    allowed = policy.current.shell.allowed
    if parts[0] not in allowed:
        if not allowed:
            return (f"[ERROR] command '{parts[0]}' is not allowed: policy "
                    f"'{policy.current.name}' permits no shell commands at all.")
        return (f"[ERROR] command '{parts[0]}' is not allowed. "
                f"Allowed commands: {', '.join(allowed)}")
    return _run(ws, parts, timeout=360, what="command")
