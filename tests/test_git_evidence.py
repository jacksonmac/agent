"""Tests for git-aware reviewer evidence: per-attempt commits in Workspace
and the diff-first evidence path in review()."""

import shutil

import pytest

from harness.review import review
from harness.workspace import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not installed")


def _ws(tmp_path) -> Workspace:
    return Workspace(str(tmp_path / "run"))


# ── Workspace git methods ────────────────────────────────────────────

def test_init_git_commits_seeded_files(tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "run" / "workspace" / "seed.txt").write_text("seeded")
    assert ws.init_git() is True
    assert ws.init_git() is True  # idempotent
    # seeded files are the base commit: attempt 1's diff must not include them
    ws.commit_attempt(1)
    assert "seed.txt" not in ws.attempt_diff()


def test_attempt_diff_shows_changes(tmp_path):
    ws = _ws(tmp_path)
    ws.init_git()
    (tmp_path / "run" / "workspace" / "foo.py").write_text("print('hello')\n")
    ws.commit_attempt(1)
    diff = ws.attempt_diff()
    assert "diff --git" in diff
    assert "foo.py" in diff
    assert "+print('hello')" in diff


def test_attempt_diff_covers_only_the_last_attempt(tmp_path):
    ws = _ws(tmp_path)
    ws.init_git()
    (tmp_path / "run" / "workspace" / "a.py").write_text("a = 1\n")
    ws.commit_attempt(1)
    (tmp_path / "run" / "workspace" / "b.py").write_text("b = 2\n")
    ws.commit_attempt(2)
    diff = ws.attempt_diff()
    assert "b.py" in diff
    assert "a.py" not in diff


def test_empty_attempt_gives_empty_diff(tmp_path):
    ws = _ws(tmp_path)
    ws.init_git()
    ws.commit_attempt(1)  # --allow-empty: commit succeeds, diff is empty
    assert ws.attempt_diff() == ""


def test_without_init_git_everything_degrades(tmp_path):
    ws = _ws(tmp_path)
    assert ws.commit_attempt(1) is False
    assert ws.attempt_diff() == ""


def test_git_dir_hidden_from_listings(tmp_path):
    ws = _ws(tmp_path)
    ws.init_git()
    (tmp_path / "run" / "workspace" / "f.txt").write_text("x")
    assert ws.list_all_files() == ["f.txt"]


# ── review() evidence selection ──────────────────────────────────────

PASS_JSON = '{"pass": true, "criteria": [], "feedback": ""}'


def _review_user_msg(scripted_llm) -> str:
    return scripted_llm.payloads[0]["messages"][1]["content"]


def test_review_prefers_the_diff(scripted_llm, tmp_path):
    ws = _ws(tmp_path)
    ws.init_git()
    (tmp_path / "run" / "workspace" / "foo.py").write_text("print(42)\n")
    ws.commit_attempt(1)
    scripted_llm.queue_text(PASS_JSON)
    review("m", "goal", "output", ws, changed_files=["foo.py"])
    user = _review_user_msg(scripted_llm)
    assert "diff --git" in user
    assert "+print(42)" in user


def test_review_falls_back_to_snapshots_without_git(scripted_llm, tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "run" / "workspace" / "foo.py").write_text("print(42)\n")
    scripted_llm.queue_text(PASS_JSON)
    review("m", "goal", "output", ws, changed_files=["foo.py"])
    user = _review_user_msg(scripted_llm)
    assert "diff --git" not in user
    assert "--- foo.py" in user  # snapshot header
    assert "print(42)" in user
