"""Benchmark goals for the harness eval suite.

Each Goal carries a prompt, files seeded into the workspace before the run,
and a programmatic checker that inspects the workspace afterwards. Checkers
are ground truth, independent of the reviewer — reviewer changes cannot
inflate the score.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

CHECK_TIMEOUT = 60  # seconds per checker subprocess

# the interpreter running the evals (the venv python) — the system python3
# on this machine has no pytest, so checkers must not rely on bare "python3"
PY = sys.executable


@dataclass
class Goal:
    name: str
    category: str  # "multi_file" | "data_processing"
    prompt: str
    seed_files: dict = field(default_factory=dict)
    check: Callable[[str], tuple[bool, str]] = None  # ws_dir -> (ok, detail)


def _run(ws: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=CHECK_TIMEOUT, cwd=ws)


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ─── multi-file project goals ───────────────────────────────────────

def _check_pkg_stats(ws: str) -> tuple[bool, str]:
    for name in ("stats/__init__.py", "stats/core.py", "test_stats.py"):
        if not os.path.exists(os.path.join(ws, name)):
            return False, f"missing {name}"
    p = _run(ws, PY, "-c",
             "from stats import mean, median; "
             "assert mean([1, 2, 3]) == 2; "
             "assert median([1, 2, 3, 4]) == 2.5; "
             "assert median([5, 1, 3]) == 3; print('ok')")
    if p.returncode != 0:
        return False, f"import/behavior check failed: {p.stderr.strip()[:200]}"
    p = _run(ws, PY, "-m", "pytest", "-q", "test_stats.py")
    if p.returncode != 0:
        return False, f"pytest failed: {(p.stdout + p.stderr).strip()[:200]}"
    return True, "package imports, behaves, and tests pass"


PKG_STATS = Goal(
    name="pkg_stats",
    category="multi_file",
    prompt=(
        "Create a python package: a folder 'stats' containing __init__.py and core.py. "
        "core.py implements mean(values) and median(values) (median must handle even-length "
        "lists by averaging the middle two, and must not mutate its input). __init__.py "
        "re-exports both so 'from stats import mean, median' works. Also write test_stats.py "
        "in the workspace root with pytest tests for both functions, including an even-length "
        "median case. Run the tests and make sure they pass."
    ),
    check=_check_pkg_stats,
)


_WC_SAMPLE = "the quick brown fox\njumps over the lazy dog\nhello world\nfinal line here\n"


def _check_cli_wordcount(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "wc.py")):
        return False, "missing wc.py"
    p = _run(ws, PY, "wc.py", "sample.txt", "--lines")
    if p.returncode != 0 or p.stdout.strip() != "4":
        return False, f"--lines gave {p.stdout.strip()!r} (want '4'); stderr: {p.stderr.strip()[:120]}"
    p = _run(ws, PY, "wc.py", "sample.txt", "--words")
    if p.returncode != 0 or p.stdout.strip() != "14":
        return False, f"--words gave {p.stdout.strip()!r} (want '14')"
    if not os.path.exists(os.path.join(ws, "test_wc.py")):
        return False, "missing test_wc.py"
    p = _run(ws, PY, "-m", "pytest", "-q", "test_wc.py")
    if p.returncode != 0:
        return False, f"pytest failed: {(p.stdout + p.stderr).strip()[:200]}"
    return True, "wc.py output correct, tests pass"


CLI_WORDCOUNT = Goal(
    name="cli_wordcount",
    category="multi_file",
    prompt=(
        "Write wc.py, a command-line tool using argparse. 'python3 wc.py <file> --lines' "
        "prints ONLY the number of lines in the file; 'python3 wc.py <file> --words' prints "
        "ONLY the number of whitespace-separated words. No extra output. The workspace "
        "contains sample.txt to try it on. Also write test_wc.py with pytest tests that "
        "exercise both flags (creating their own temp input files), and make the tests pass."
    ),
    seed_files={"sample.txt": _WC_SAMPLE},
    check=_check_cli_wordcount,
)


def _check_todo_app(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "todo.py")):
        return False, "missing todo.py"
    store = os.path.join(ws, "todos.json")
    if os.path.exists(store):
        os.unlink(store)  # start from a clean state, the CLI must create it
    for argv in (("python3", "todo.py", "add", "buy milk"),
                 ("python3", "todo.py", "add", "write code"),
                 ("python3", "todo.py", "done", "1")):
        p = _run(ws, *argv)
        if p.returncode != 0:
            return False, f"{' '.join(argv)} failed: {p.stderr.strip()[:150]}"
    try:
        with open(store) as f:
            items = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"todos.json unreadable after CLI use: {e}"
    if not (isinstance(items, list) and len(items) == 2):
        return False, f"todos.json should hold 2 items, got: {items!r}"
    if items[0].get("text") != "buy milk" or items[0].get("done") is not True:
        return False, f"item 1 should be 'buy milk' and done, got: {items[0]!r}"
    if items[1].get("done") is not False:
        return False, f"item 2 should not be done, got: {items[1]!r}"
    p = _run(ws, PY, "todo.py", "list")
    if p.returncode != 0 or "buy milk" not in p.stdout:
        return False, "todo.py list should print the items"
    return True, "CLI works and todos.json has the right shape"


TODO_APP = Goal(
    name="todo_app",
    category="multi_file",
    prompt=(
        "Write todo.py, a command-line todo app storing state in todos.json (a JSON list of "
        "objects shaped {\"text\": str, \"done\": bool}) in the same directory. Commands: "
        "'python3 todo.py add \"some text\"' appends an item; 'python3 todo.py done <n>' marks "
        "the n-th item (1-based) done; 'python3 todo.py list' prints each item with its number "
        "and status. The file must be created on first use if absent. Also write test_todo.py "
        "with pytest tests covering add and done (isolate state per test), and make them pass."
    ),
    check=_check_todo_app,
)


_MD_HOME = "# Home\n\nWelcome to the demo site.\n\nThis page is generated.\n"
_MD_ABOUT = "# About\n\nWe convert markdown to html.\n"


def _check_site_gen(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "build.py")):
        return False, "missing build.py"
    # regenerate to prove build.py actually works (not hand-written output)
    import shutil
    dist = os.path.join(ws, "dist")
    if os.path.isdir(dist):
        shutil.rmtree(dist)
    p = _run(ws, PY, "build.py")
    if p.returncode != 0:
        return False, f"build.py failed: {p.stderr.strip()[:200]}"
    for page, h1, body in (("home.html", "<h1>Home</h1>", "Welcome to the demo site."),
                           ("about.html", "<h1>About</h1>", "We convert markdown to html.")):
        path = os.path.join(dist, page)
        if not os.path.exists(path):
            return False, f"missing dist/{page}"
        html = open(path).read()
        if h1 not in html:
            return False, f"dist/{page} lacks {h1}"
        if f"<p>{body}</p>" not in html:
            return False, f"dist/{page} lacks <p>{body}</p>"
    return True, "build.py regenerates correct html"


SITE_GEN = Goal(
    name="site_gen",
    category="multi_file",
    prompt=(
        "The workspace has a content/ folder with markdown files. Write build.py that, when "
        "run with 'python3 build.py', creates a dist/ folder and converts every content/*.md "
        "file into dist/<same-name>.html. Conversion rules: a line starting with '# ' becomes "
        "<h1>...</h1>; every other non-empty line becomes <p>...</p>; the result is wrapped in "
        "<html><body> ... </body></html>. Run it and verify the dist/ files look right."
    ),
    seed_files={"content/home.md": _MD_HOME, "content/about.md": _MD_ABOUT},
    check=_check_site_gen,
)


# ─── data / file processing goals ───────────────────────────────────

_MESSY_CSV = """ Name , EMAIL ,Age
Alice , ALICE@example.com ,30

Bob,bob@example.com, 25
alice, alice@example.com ,30
Carol , carol@example.com ,41

Bob,BOB@example.com, 25
"""


def _check_csv_cleanup(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "clean_data.py")):
        return False, "missing clean_data.py (the cleaning must be scripted)"
    path = os.path.join(ws, "clean.csv")
    if os.path.exists(path):
        os.unlink(path)  # must be reproducible from the script
    p = _run(ws, PY, "clean_data.py")
    if p.returncode != 0:
        return False, f"clean_data.py failed: {p.stderr.strip()[:200]}"
    if not os.path.exists(path):
        return False, "clean_data.py did not produce clean.csv"
    rows = _read_csv(path)
    header = list(rows[0].keys()) if rows else []
    if header != ["name", "email", "age"]:
        return False, f"header should be name,email,age — got {header}"
    if len(rows) != 3:
        return False, f"expected 3 unique rows, got {len(rows)}"
    by_email = {r["email"].lower(): r for r in rows}
    if set(by_email) != {"alice@example.com", "bob@example.com", "carol@example.com"}:
        return False, f"wrong emails: {sorted(by_email)}"
    for r in rows:
        for v in r.values():
            if v != v.strip():
                return False, f"untrimmed value: {v!r}"
    if by_email["alice@example.com"]["age"] != "30":
        return False, "alice's age wrong"
    return True, "clean.csv correct and reproducible"


CSV_CLEANUP = Goal(
    name="csv_cleanup",
    category="data_processing",
    prompt=(
        "The workspace contains messy.csv with inconsistent formatting. Write clean_data.py "
        "that reads messy.csv and writes clean.csv with: header exactly 'name,email,age' "
        "(lowercase, no spaces); every value stripped of surrounding whitespace; blank rows "
        "removed; duplicate people removed, where two rows are the same person if their "
        "emails match case-insensitively (keep the first occurrence, store the email as "
        "lowercase). Run it and check clean.csv yourself."
    ),
    seed_files={"messy.csv": _MESSY_CSV},
    check=_check_csv_cleanup,
)


_SERVER_LOG = """2026-07-01 12:00:01 INFO 10.0.0.3 GET /
2026-07-01 12:00:02 ERROR 10.0.0.7 GET /admin
2026-07-01 12:00:03 INFO 10.0.0.7 GET /index
2026-07-01 12:00:04 WARN 10.0.0.5 GET /old
2026-07-01 12:00:05 ERROR 10.0.0.7 POST /login
2026-07-01 12:00:06 INFO 10.0.0.3 GET /about
2026-07-01 12:00:07 WARN 10.0.0.3 GET /old
2026-07-01 12:00:08 INFO 10.0.0.7 GET /contact
2026-07-01 12:00:09 ERROR 10.0.0.5 GET /admin
"""
# ground truth: errors=3, warns=2, ip counts: 10.0.0.7 x4, 10.0.0.3 x3, 10.0.0.5 x2


def _check_log_parse(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "parse_log.py")):
        return False, "missing parse_log.py"
    out = os.path.join(ws, "summary.json")
    if os.path.exists(out):
        os.unlink(out)  # must be reproducible from the script
    p = _run(ws, PY, "parse_log.py")
    if p.returncode != 0:
        return False, f"parse_log.py failed: {p.stderr.strip()[:200]}"
    try:
        with open(out) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"summary.json unreadable: {e}"
    want = {"error_count": 3, "warn_count": 2, "top_ip": "10.0.0.7"}
    for k, v in want.items():
        if data.get(k) != v:
            return False, f"summary.json[{k!r}] = {data.get(k)!r}, want {v!r}"
    return True, "summary.json matches ground truth"


LOG_PARSE = Goal(
    name="log_parse",
    category="data_processing",
    prompt=(
        "The workspace contains server.log where each line is 'DATE TIME LEVEL IP METHOD "
        "PATH'. Write parse_log.py that reads it and writes summary.json with exactly these "
        "keys: error_count (number of ERROR lines, as an integer), warn_count (number of WARN "
        "lines, integer), and top_ip (the IP address appearing on the most lines, as a "
        "string). Run it and verify summary.json against the log yourself."
    ),
    seed_files={"server.log": _SERVER_LOG},
    check=_check_log_parse,
)


_RECORDS_JSON = json.dumps([
    {"id": 3, "user": {"name": "Cara", "city": "Oslo"}, "amount": 12.5},
    {"id": 1, "user": {"name": "Ann", "city": "Rome"}, "amount": 99.0},
    {"id": 4, "user": {"name": "Dev", "city": "Pune"}, "amount": 50.0},
    {"id": 2, "user": {"name": "Bo", "city": "Kyiv"}, "amount": 75.25},
], indent=2)


def _check_json_to_csv(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "flatten.py")):
        return False, "missing flatten.py"
    out = os.path.join(ws, "report.csv")
    if os.path.exists(out):
        os.unlink(out)
    p = _run(ws, PY, "flatten.py")
    if p.returncode != 0:
        return False, f"flatten.py failed: {p.stderr.strip()[:200]}"
    rows = _read_csv(out)
    if not rows:
        return False, "report.csv is empty"
    if list(rows[0].keys()) != ["id", "name", "city", "amount"]:
        return False, f"header wrong: {list(rows[0].keys())}"
    got = [(r["id"], r["name"]) for r in rows]
    want = [("1", "Ann"), ("2", "Bo"), ("4", "Dev"), ("3", "Cara")]
    if got != want:
        return False, f"rows/order wrong (want amount descending): {got}"
    return True, "report.csv flattened and sorted correctly"


JSON_TO_CSV = Goal(
    name="json_to_csv",
    category="data_processing",
    prompt=(
        "The workspace contains records.json, a JSON list where each record looks like "
        "{\"id\": int, \"user\": {\"name\": str, \"city\": str}, \"amount\": float}. Write "
        "flatten.py that reads it and writes report.csv with header 'id,name,city,amount' "
        "(name and city pulled out of the nested user object), rows sorted by amount in "
        "descending order. Run it and verify report.csv yourself."
    ),
    seed_files={"records.json": _RECORDS_JSON},
    check=_check_json_to_csv,
)


_CONTACTS_A = """name,email,phone
Alice Smith,alice@example.com,555-0101
Bob Jones,bob@example.com,
Carol Lee,carol@example.com,555-0103
"""

_CONTACTS_B = """name,email,phone
,ALICE@example.com,555-0199
Bob Jones,bob@example.com,555-0102
Dan Wu,dan@example.com,555-0104
"""


def _check_dedup_merge(ws: str) -> tuple[bool, str]:
    if not os.path.exists(os.path.join(ws, "merge.py")):
        return False, "missing merge.py"
    out = os.path.join(ws, "merged.csv")
    if os.path.exists(out):
        os.unlink(out)
    p = _run(ws, PY, "merge.py")
    if p.returncode != 0:
        return False, f"merge.py failed: {p.stderr.strip()[:200]}"
    rows = _read_csv(out)
    if not rows or list(rows[0].keys()) != ["name", "email", "phone"]:
        return False, f"header wrong or file empty: {rows[:1]}"
    if [r["email"] for r in rows] != ["alice@example.com", "bob@example.com",
                                      "carol@example.com", "dan@example.com"]:
        return False, f"emails/order wrong: {[r['email'] for r in rows]}"
    by_email = {r["email"]: r for r in rows}
    if by_email["alice@example.com"]["name"] != "Alice Smith":
        return False, "alice should keep her name from file A"
    if by_email["alice@example.com"]["phone"] != "555-0101":
        return False, "alice's phone should come from the first source that has one"
    if by_email["bob@example.com"]["phone"] != "555-0102":
        return False, "bob's phone should be filled from file B"
    return True, "merged.csv deduped and field-merged correctly"


DEDUP_MERGE = Goal(
    name="dedup_merge",
    category="data_processing",
    prompt=(
        "The workspace contains contacts_a.csv and contacts_b.csv, both with header "
        "'name,email,phone' and some blank fields. Write merge.py that merges them into "
        "merged.csv: one row per person, where rows refer to the same person if their emails "
        "match case-insensitively. Store emails lowercase. For name and phone, use the value "
        "from contacts_a.csv when it is non-empty, otherwise the value from contacts_b.csv. "
        "Sort rows by email ascending, header 'name,email,phone'. Run it and verify "
        "merged.csv yourself."
    ),
    seed_files={"contacts_a.csv": _CONTACTS_A, "contacts_b.csv": _CONTACTS_B},
    check=_check_dedup_merge,
)


GOALS: list[Goal] = [
    PKG_STATS, CLI_WORDCOUNT, TODO_APP, SITE_GEN,
    CSV_CLEANUP, LOG_PARSE, JSON_TO_CSV, DEDUP_MERGE,
]

GOALS_BY_NAME = {g.name: g for g in GOALS}
