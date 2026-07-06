"""Per-run workspace isolation.

Each run gets runs/run_TIMESTAMP/ with a workspace/ subdirectory. Tools are
jailed to workspace/ — that's ALL the agent can see or touch. Harness
artifacts (attempt outputs, history, logs) live in the run dir next to it,
so leftovers from one run can never confuse the next.
"""

from __future__ import annotations

import os
import time

from .llm import truncate_middle

_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git", "venv", ".venv",
                 "node_modules"}
_IGNORED_SUFFIXES = (".pyc", ".pyo")


def _ignored(name: str) -> bool:
    return name in _IGNORED_DIRS or name.startswith(".")


class Workspace:
    def __init__(self, run_dir: str, workspace_dir: str | None = None):
        self.run_dir = os.path.abspath(run_dir)
        self.root = os.path.abspath(workspace_dir) if workspace_dir \
            else os.path.join(self.run_dir, "workspace")
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(self.root, exist_ok=True)
        self._attempt_t0 = 0.0

    @classmethod
    def create(cls, runs_root: str, workspace_dir: str | None = None) -> "Workspace":
        """Make runs/run_TIMESTAMP/ (+ a 'latest' symlink) and return the
        Workspace. workspace_dir overrides where the agent-visible files live
        (--workspace, for continuing earlier work)."""
        run_dir = os.path.join(runs_root, time.strftime("run_%Y%m%d_%H%M%S"))
        ws = cls(run_dir, workspace_dir)
        latest = os.path.join(runs_root, "latest")
        try:
            if os.path.islink(latest):
                os.unlink(latest)
            os.symlink(os.path.basename(run_dir), latest)
        except OSError:
            pass  # symlinks are a convenience, not a requirement
        return ws

    # ─── path jail ──────────────────────────────────────────────────

    def resolve(self, name: str) -> str:
        """Absolute path for a workspace-relative name, or ValueError if the
        name escapes the workspace."""
        path = os.path.abspath(os.path.join(self.root, name))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise ValueError(f"path escapes the workspace: {name}")
        return path

    # ─── per-attempt change tracking ────────────────────────────────

    def begin_attempt(self) -> None:
        # -1s slack for filesystem mtime granularity
        self._attempt_t0 = time.time() - 1.0

    def files_changed_this_attempt(self) -> list[str]:
        """Workspace-relative paths of every file created or modified since
        begin_attempt(), however it was written (write_file, run_python,
        run_shell, ...). The reviewer judges these, not the agent's claims."""
        changed = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not _ignored(d)]
            for fn in filenames:
                if _ignored(fn) or fn.endswith(_IGNORED_SUFFIXES):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getmtime(full) >= self._attempt_t0:
                        changed.append(os.path.relpath(full, self.root))
                except OSError:
                    continue
        return sorted(changed)

    def list_all_files(self) -> list[str]:
        """Every (non-ignored) file currently in the workspace."""
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not _ignored(d)]
            for fn in filenames:
                if _ignored(fn) or fn.endswith(_IGNORED_SUFFIXES):
                    continue
                found.append(os.path.relpath(os.path.join(dirpath, fn), self.root))
        return sorted(found)

    # ─── reviewer snapshot ──────────────────────────────────────────

    def snapshot_files(self, names: list[str], per_file: int = 2_000,
                       total_max: int = 8_000) -> str:
        """Read the given workspace files, capped per-file and in total, so
        the reviewer verifies real on-disk content without the snapshot
        itself blowing the context window."""
        if not names:
            return "(no files were created or modified this attempt)"
        chunks, used = [], 0
        for name in dict.fromkeys(names):  # dedupe, keep order
            try:
                with open(self.resolve(name)) as f:
                    content = f.read()
            except (OSError, ValueError) as e:
                chunks.append(f"--- {name} --- [unreadable: {e}]")
                continue
            snippet = truncate_middle(content, per_file)
            entry = f"--- {name} ({len(content)} chars) ---\n{snippet}"
            if used + len(entry) > total_max:
                chunks.append(f"--- {name} ({len(content)} chars) --- [omitted, snapshot budget hit]")
                continue
            chunks.append(entry)
            used += len(entry)
        return "\n".join(chunks)

    # ─── harness artifacts (run dir, NOT agent-visible) ─────────────

    def save_artifact(self, name: str, text: str) -> str:
        path = os.path.join(self.run_dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path
