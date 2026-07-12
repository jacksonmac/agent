"""Cross-run history: one sqlite row per run, queried via `agent.py history`.

events.jsonl stays the detailed per-run record; this is the index over all
runs for "what did I run last week and how often does model X pass".
"""

from __future__ import annotations

import os
import sqlite3

from .config import HERE

DB_NAME = "history.db"

_SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    goal TEXT,
    model TEXT,
    executor_model TEXT,
    reviewer_model TEXT,
    passed INTEGER,
    attempts INTEGER,
    duration_secs REAL,
    run_dir TEXT
)"""


def db_path() -> str:
    return os.path.join(HERE, "runs", DB_NAME)


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


def record(path: str, *, ts: str, goal: str, model: str,
           executor_model: str | None, reviewer_model: str | None,
           passed: bool, attempts: int, duration_secs: float,
           run_dir: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO runs (ts, goal, model, executor_model, reviewer_model,"
            " passed, attempts, duration_secs, run_dir)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, goal, model, executor_model, reviewer_model,
             int(passed), attempts, duration_secs, run_dir))


def latest_run_dir(path: str) -> str | None:
    """run_dir of the most recent recorded run, or None."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT run_dir FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def run_dir_for(path: str, run_id: int) -> str | None:
    """run_dir for a history id (the `id` column in the listing), or None."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT run_dir FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row[0] if row else None


def print_history(path: str, limit: int = 20) -> None:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, ts, goal, COALESCE(executor_model, model), passed,"
            " attempts, duration_secs, run_dir FROM runs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("(no runs recorded yet)")
        return
    print(f"{'id':<4} {'when':<20} {'pass':<5} {'att':<4} {'secs':>7}  "
          f"{'executor':<24} goal")
    for rid, ts, goal, model, passed, attempts, secs, _run_dir in rows:
        goal_s = " ".join(goal.split())[:60]
        print(f"{rid:<4} {ts:<20} {'yes' if passed else 'NO':<5} {attempts:<4} "
              f"{secs:>7.1f}  {model[:24]:<24} {goal_s}")
    print("\ncontinue one with: agent.py --resume <id>   (or just --resume "
          "for the latest)")


def print_stats(path: str) -> None:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT COALESCE(executor_model, model) AS m, COUNT(*), SUM(passed),"
            " AVG(attempts), AVG(duration_secs) FROM runs GROUP BY m"
            " ORDER BY COUNT(*) DESC").fetchall()
    if not rows:
        print("(no runs recorded yet)")
        return
    print(f"{'executor':<28} {'runs':>5} {'passed':>7} {'rate':>6} {'avg att':>8} {'avg secs':>9}")
    for model, n, passed, avg_att, avg_secs in rows:
        rate = (passed or 0) / n * 100
        print(f"{model[:28]:<28} {n:>5} {passed or 0:>7} {rate:>5.0f}% "
              f"{avg_att:>8.1f} {avg_secs:>9.1f}")
