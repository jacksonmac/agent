"""Per-run workspace isolation.

Each run gets runs/run_TIMESTAMP/ with a workspace/ subdirectory. Tools are
jailed to workspace/ — that's ALL the agent can see or touch. Harness
artifacts (attempt outputs, history, logs) live in the run dir next to it,
so leftovers from one run can never confuse the next.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import office
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
        self._git = False  # init_git() flips this when git is usable

    @classmethod
    def create(cls, runs_root: str, workspace_dir: str | None = None) -> "Workspace":
        """Make runs/run_TIMESTAMP/ (+ a 'latest' symlink) and return the
        Workspace. workspace_dir overrides where the agent-visible files live
        (--workspace, for continuing earlier work)."""
        os.makedirs(runs_root, exist_ok=True)
        # timestamps are second-granular: two runs started in the same second
        # must not share (and clobber) one run dir, so reserve the name
        # atomically and suffix -2, -3, ... on collision
        base = os.path.join(runs_root, time.strftime("run_%Y%m%d_%H%M%S"))
        run_dir, n = base, 1
        while True:
            try:
                os.makedirs(run_dir)
                break
            except FileExistsError:
                n += 1
                run_dir = f"{base}-{n}"
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

    # ─── git evidence (per-attempt commits → reviewer diffs) ────────

    def _git_run(self, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "-C", self.root, *args],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def init_git(self) -> bool:
        """git-init the workspace (idempotent — a reused workspace keeps its
        history) and commit whatever is already there, so attempt 1 has a
        base to diff against. False = git unusable, evidence stays
        snapshot-only."""
        if self._git:
            return True
        proc = self._git_run("init", "-q")
        if proc is None or proc.returncode != 0:
            return False
        # commits need an identity; scope it to this repo only
        self._git_run("config", "user.email", "agent@harness.local")
        self._git_run("config", "user.name", "agent harness")
        # keep bytecode/venv noise out of the diffs without an agent-visible
        # .gitignore in the workspace
        try:
            with open(os.path.join(self.root, ".git", "info", "exclude"), "a") as f:
                f.write("__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\nvenv/\n.venv/\n")
        except OSError:
            pass
        self._git = self._commit("workspace before the run")
        return self._git

    def _commit(self, message: str) -> bool:
        add = self._git_run("add", "-A")
        if add is None or add.returncode != 0:
            return False
        proc = self._git_run("commit", "-q", "--allow-empty", "-m", message)
        return proc is not None and proc.returncode == 0

    def commit_attempt(self, n: int) -> bool:
        """Snapshot the workspace as one commit per attempt (no-op without
        init_git). --allow-empty keeps HEAD~1 meaningful even for
        do-nothing attempts."""
        return self._git and self._commit(f"attempt {n}")

    def attempt_diff(self, max_chars: int = 8_000) -> str:
        """Unified diff of the last attempt commit — denser reviewer evidence
        than file snapshots. Empty string when git is off, the diff fails,
        or nothing changed (callers fall back to snapshot_files)."""
        if not self._git:
            return ""
        proc = self._git_run("diff", "HEAD~1", "HEAD")
        if proc is None or proc.returncode != 0:
            return ""
        return truncate_middle(proc.stdout.strip(), max_chars)

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
                path = self.resolve(name)
                # office formats are zips: read as text and the reviewer gets
                # binary noise and judges nothing, which quietly turns every
                # verdict on a document deliverable into "the file exists"
                if office.is_office_package(path):
                    content = (office.extract_text(path)
                               or "(no extractable text)")
                else:
                    with open(path) as f:
                        content = f.read()
            except (OSError, ValueError, UnicodeDecodeError) as e:
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
