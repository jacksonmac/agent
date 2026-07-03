"""agent.py v2 — execute -> review -> retry agent harness for a local Ollama server.

Usage:
    python3 agent.py -g "A script fizzbuzz.py that prints FizzBuzz for 1-30"
    python3 agent.py -sg "I need a folder called output with a readme in it"
    python3 agent.py -g "..." --model qwen3.5:9b --attempts 3 --num-ctx 32768
    python3 agent.py -g "..." --reviewer-model llama3.1:8b --temperature 0.4 --seed 7
    python3 agent.py -g "..." --full-context --num-ctx 65536
    python3 agent.py -g "..." --mcp                    # + Docker MCP Toolkit tools
    python3 agent.py -g "..." --mcp --mcp-profile dev  # a specific Toolkit profile

What's new in v2:
  * Per-run WORKSPACE: every run gets its own directory under ./runs/ (or
    --workspace PATH). The agent can no longer touch files outside it —
    including this script. All artifacts, logs and attempt files land there.
  * More tools: read_file, edit_file (surgical find/replace — no more
    re-emitting whole files to fix one line), list_files, delete_file.
  * Structured review: the goalsmith emits acceptance CRITERIA and the
    reviewer returns machine-parsed JSON (PASS/FAIL per criterion) via
    Ollama structured outputs — with automatic fallback to the old YES/NO
    protocol on servers/models that don't support `format`.
  * Streaming by default: tokens print live as the model generates
    (--no-stream to disable). Great feedback on slow CPU boxes.
  * Real token accounting from Ollama's prompt_eval_count/eval_count,
    plus an end-of-run usage table. The chars-per-token estimate used for
    compaction self-calibrates from observed counts.
  * Robust HTTP layer: retries with backoff on connect errors / 5xx, and
    graceful capability fallbacks when a model rejects `think`, `format`,
    or streaming-with-tools.
  * Stall breaking: near-identical retries now also bump the sampling
    temperature so the model actually explores a different path.
  * Model preflight (/api/tags): typo'd model names fail fast with the
    list of models you actually have.
  * fetch_page hardening: proper private-address blocking via `ipaddress`
    (the old prefix list blocked ALL of 172.* — including public IPs like
    Google's 172.217.*), redirect re-validation, content-type checks and a
    2 MB download cap.
  * Run artifacts: events.jsonl (structured log of every LLM call, tool
    call and verdict), transcript.md (human-readable), attempt_history.json.
  * Ctrl-C safe: an interrupted run still writes its history and summary.

--mcp : connect to the Docker MCP Toolkit gateway ('docker mcp gateway run')
and expose every tool from your enabled MCP servers to the executor, alongside
the built-in tools. Requires Docker Desktop with the MCP Toolkit enabled (or
the standalone docker-mcp CLI plugin on Linux).

--full-context : send the model EVERYTHING — no compaction, no truncation of
tool results, retries, or thinking. Pair it with a big --num-ctx, because
anything past num_ctx is silently dropped by ollama (oldest first).

-g  : use your text as the goal (the task sent to the executor is the same text)
-sg : "smart goal" — the LM rewrites your input into GOAL + TASK + CRITERIA first

Env vars: OLLAMA_URL (or OLLAMA_HOST) overrides the default server address,
AGENT_MODEL overrides the default model.

Optional deps for the research tools:
    pip install ddgs trafilatura
"""

__version__ = "2.0"

import argparse
import base64
import difflib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

import requests

from ui import ui

HERE = os.path.dirname(os.path.abspath(__file__))


def _default_url() -> str:
    """Server address: OLLAMA_URL > OLLAMA_HOST (scheme added if missing) > LAN default."""
    url = os.environ.get("OLLAMA_URL")
    if url:
        return url.rstrip("/")
    host = os.environ.get("OLLAMA_HOST")
    if host:
        if "://" not in host:
            host = "http://" + host
        return host.rstrip("/")
    return "http://192.168.1.134:11434"


URL = _default_url()
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "gemma4:26b")

CONNECT_TIMEOUT = 15   # seconds to establish the HTTP connection
REQUEST_TIMEOUT = 600  # seconds per LLM call — big models on CPU can be slow

# ─── Context budget ─────────────────────────────────────────────────
# Ollama silently truncates anything past num_ctx (default is only 4096!),
# so we (a) request a bigger window explicitly and (b) keep what we SEND
# under budget so the model never loses the system prompt or the task.

NUM_CTX = 16384              # requested context window (more = more RAM/VRAM)
CHARS_PER_TOKEN = 3.0        # budgeting estimate — self-calibrates from real counts
TOOL_RESULT_MAX = 4_000      # chars of any single tool result kept in history
RETRY_PREV_MAX = 6_000       # chars of a failed attempt fed into the retry
PAGE_TEXT_MAX = 6_000        # chars of a fetched web page returned to the model
COMPACT_KEEP_LAST = 6        # never compact the most recent N messages

FULL_CONTEXT = False  # --full-context: disable ALL trimming, send everything

# ─── Run-wide toggles (set from the CLI in __main__) ────────────────

WORKSPACE: Optional[str] = None  # per-run sandbox dir; ALL file tools live here
STREAM = True                    # stream tokens live (--no-stream to disable)
THINK_DEFAULT = True             # --no-think turns extended thinking off
RUN_TEMPERATURE: Optional[float] = None  # --temperature; auto-bumped on stalls
SEED: Optional[int] = None       # --seed for reproducible sampling
MAX_TOKENS: Optional[int] = None  # --max-tokens -> ollama's num_predict
KEEP_ALIVE = "10m"               # keep the model loaded between calls

# Capability flags — flipped off automatically if the server/model rejects
# the corresponding request field, so we only pay for the failed call once.
SUPPORTS_THINK = True
SUPPORTS_FORMAT = True   # structured outputs ("format": <json schema>)


# ─── Run directory: workspace + logs ────────────────────────────────

def init_workspace(path: Optional[str]) -> str:
    """Create (or reuse) the per-run directory. Everything the agent writes,
    plus our own logs and attempt files, lives inside it — so a run can never
    clobber this script, and every run's artifacts stay together."""
    ws = os.path.abspath(path) if path else os.path.join(
        HERE, "runs", time.strftime("run_%Y%m%d_%H%M%S"))
    os.makedirs(ws, exist_ok=True)
    return ws


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_ATTACH_BYTES = 20 * 1024 * 1024


def stage_attachments(paths: list) -> tuple:
    """Copy user-supplied files into WORKSPACE so the file tools can reach them.
    Returns (copied_names, base64_images). Image files are also base64-encoded
    for vision models. Must be called after init_workspace()."""
    copied: list[str] = []
    images: list[str] = []
    for raw in paths:
        src = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(src):
            ui.warning(f"attachment not found, skipping: {raw}")
            continue
        if os.path.getsize(src) > MAX_ATTACH_BYTES:
            ui.warning(f"attachment over {MAX_ATTACH_BYTES // (1024 * 1024)} MB, "
                       f"skipping: {raw}")
            continue
        name = os.path.basename(src)
        base, ext = os.path.splitext(name)
        n = 2
        while os.path.exists(os.path.join(WORKSPACE, name)):
            name = f"{base}_{n}{ext}"
            n += 1
        shutil.copy2(src, os.path.join(WORKSPACE, name))
        copied.append(name)
        is_image = ext.lower() in IMAGE_EXTS
        if is_image:
            with open(src, "rb") as f:
                images.append(base64.b64encode(f.read()).decode("ascii"))
        log_event("attachment", name=name, source=src, image=is_image)
    return copied, images


_AT_TOKEN = re.compile(r'@("[^"]+"|\'[^\']+\'|\S+)')


def extract_at_paths(text: str) -> tuple:
    """Find @path tokens in goal text that resolve to existing files.
    Returns (rewritten_text, absolute_paths). Tokens that don't point at a
    real file (emails, @handles) are left untouched; matches are replaced
    with the bare filename so the goal reads naturally."""
    found: list[str] = []

    def _sub(m):
        cand = m.group(1).strip("\"'").rstrip(".,;:!?")
        p = os.path.abspath(os.path.expanduser(cand))
        if os.path.isfile(p):
            found.append(p)
            return os.path.basename(p)
        return m.group(0)

    return _AT_TOKEN.sub(_sub, text), found


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _short(value, max_chars: int = 1_500) -> str:
    s = str(value)
    return s if len(s) <= max_chars else s[:max_chars] + f"...(+{len(s) - max_chars} chars)"


def log_event(kind: str, **fields) -> None:
    """Append one JSON line to <workspace>/events.jsonl — a structured trace
    of every LLM call, tool call and verdict, for post-mortems and tooling."""
    if not WORKSPACE:
        return
    record = {"ts": _ts(), "event": kind}
    record.update({k: _short(v) if isinstance(v, str) else v for k, v in fields.items()})
    try:
        with open(os.path.join(WORKSPACE, "events.jsonl"), "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # logging must never take the run down


def transcript(text: str) -> None:
    """Append to <workspace>/transcript.md — the human-readable run log."""
    if not WORKSPACE:
        return
    try:
        with open(os.path.join(WORKSPACE, "transcript.md"), "a") as f:
            f.write(text + "\n")
    except OSError:
        pass


# ─── Truncation / compaction ────────────────────────────────────────

def truncate_middle(text: str, max_chars: int) -> str:
    """Cap text length, keeping the head and tail (that's where the signal
    usually is — imports/opening vs. errors/conclusions)."""
    if len(text) <= max_chars:
        return text
    marker = f"\n[... {len(text) - max_chars} chars truncated ...]\n"
    half = max(0, (max_chars - len(marker)) // 2)
    tail = text[len(text) - half:] if half else ""   # NB: text[-0:] would be the whole string
    return text[:half] + marker + tail


def cap(text: str, max_chars: int) -> str:
    """Truncate — unless full-context mode is on, in which case pass through."""
    return text if FULL_CONTEXT else truncate_middle(text, max_chars)


def estimate_tokens(messages: list) -> int:
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return int(total / CHARS_PER_TOKEN)


def compact_messages(messages: list) -> None:
    """In-place: if the history is over budget, shrink OLD tool results and
    assistant turns down to stubs. Never touches the system prompt, the task
    (first two messages), or the most recent COMPACT_KEEP_LAST messages —
    the model always keeps its instructions, its goal, and its recent work."""
    if FULL_CONTEXT:
        # nothing gets touched — but if we're over the window, ollama will
        # silently drop the OLDEST tokens (system prompt first!), so shout
        est = estimate_tokens(messages)
        if est > NUM_CTX:
            ui.warning(f"  [WARNING] full-context mode: sending ~{est} tokens but "
                       f"num_ctx is {NUM_CTX} — ollama will silently drop the oldest. "
                       f"Raise --num-ctx.")
        return
    budget_tokens = int(NUM_CTX * 0.75)  # leave headroom for the reply
    if estimate_tokens(messages) <= budget_tokens:
        return

    protected = 2  # system + user task
    compactable = range(protected, max(protected, len(messages) - COMPACT_KEEP_LAST))
    for i in compactable:
        m = messages[i]
        content = str(m.get("content") or "")
        if len(content) > 500 and m.get("role") in ("tool", "assistant"):
            messages[i] = {**m, "content": content[:300] + "\n[... compacted to save context ...]"}
        if estimate_tokens(messages) <= budget_tokens:
            return

    if estimate_tokens(messages) > budget_tokens:
        ui.warning(f"  [WARNING] history still ~{estimate_tokens(messages)} tokens "
                   f"after compaction (budget {budget_tokens})")


# ─── Prompts ────────────────────────────────────────────────────────

EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output. You are inside a dedicated workspace directory; files persist
between attempts.

Tools:
- write_file / read_file / edit_file / list_files / delete_file operate on the workspace.
  Prefer edit_file for small fixes instead of rewriting a whole file with write_file.
- run_python executes code in the workspace — use it to test everything you write.
- run_shell runs one allowlisted command (e.g. 'pip install flask', 'ls', 'cat app.py').

For research: use web_search to find sources, then fetch_page on the 1-3 most promising URLs
to read them, then synthesize what you learned into your answer. Do not answer research
questions from memory alone when you can verify with a search.

If a test fails, fix the code and test again before finishing. End with a short summary of
what you built, where it lives, and how you verified it."""

REVIEWER_SYSTEM_JSON = """You are a strict reviewer. You will be given a GOAL, acceptance
CRITERIA, and the OUTPUT of an agent that tried to achieve it, plus the real files it wrote.
Judge the FILES on disk, not the agent's claims.

Respond with ONLY a JSON object, nothing else:
{"verdict": "PASS" or "FAIL",
 "criteria": [{"criterion": "...", "met": true or false, "note": "short reason"}],
 "feedback": "one short paragraph: what is missing or broken (empty string if PASS)"}

verdict must be PASS only if EVERY criterion is met."""

REVIEWER_SYSTEM_LEGACY = """You are a strict reviewer. You will be given a GOAL and the OUTPUT
of an agent that tried to achieve it. Decide if the output actually meets the goal.

The FIRST word of your reply must be exactly YES or NO.
If NO, follow it with one short paragraph listing what is missing or broken.
If YES, say nothing else."""

REVIEW_USER = """GOAL:
{goal}

ACCEPTANCE CRITERIA:
{criteria}

AGENT OUTPUT:
{output}

WORKSPACE STATE (actual on-disk content, possibly truncated):
{files}

Did the work meet the goal? Judge the FILES, not just the agent's claims."""

RETRY_NOTE = """

A previous attempt did NOT meet the goal according to the reviewer.

Reviewer feedback so far (fix ALL of it, not just the latest):
{feedback}

Files currently in the workspace (they persist between attempts — read_file /
edit_file them instead of starting from scratch where that makes sense):
{workspace}

Here is the most recent attempt — fix what is missing or broken and finish the goal:

--- PREVIOUS ATTEMPT ---
{previous}
--- END PREVIOUS ATTEMPT ---"""

ATTACHMENT_NOTE = """

The user attached these files; they are already in your workspace — read them with
read_file (or run_python for binary/CSV work) before answering:
{names}"""

GOALSMITH_SYSTEM_JSON = """You turn a rough user request into a plan for an agent that has
write_file / read_file / edit_file / list_files / run_python / run_shell / web_search /
fetch_page tools.

Respond with ONLY a JSON object, nothing else:
{"goal": "one or two sentences — a single concrete, checkable success condition",
 "task": "one paragraph of instructions: what to build, save, run, and verify",
 "criteria": ["3 to 7 short acceptance criteria, each individually checkable"]}"""

GOALSMITH_SYSTEM_LEGACY = """You turn a rough user request into two things:

GOAL: a single, concrete, checkable success condition (what a reviewer will verify).
TASK: instructions for an agent with write_file / run_python / run_shell /
web_search / fetch_page tools, telling it what to build, save, run, and verify.

Reply in EXACTLY this format, nothing before or after:
GOAL: <one or two sentences>
TASK: <one paragraph>"""

# JSON schemas for Ollama structured outputs (the "format" request field).

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "met": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["criterion", "met"],
            },
        },
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "feedback"],
}

GOALSMITH_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "task": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal", "task", "criteria"],
}


# ─── Timing + token accounting ──────────────────────────────────────

total_time: dict[str, list[float]] = defaultdict(list)     # label -> per-call secs
token_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # label -> [in, out]


def print_run_summary() -> None:
    if not total_time:
        return
    ui.timing_summary(total_time, token_totals, CHARS_PER_TOKEN)


# ─── Workspace file tools ───────────────────────────────────────────
# Every path is resolved with realpath and must land inside WORKSPACE, so
# neither the model nor a crafted '../' name can touch anything else —
# including this script (v1 wrote into the script's own directory!).

def _safe_path(name: str):
    """Resolve `name` inside the workspace. Returns (path, None) on success
    or (None, error_string) if it would escape (also catches symlink tricks)."""
    if not WORKSPACE:
        return None, "[ERROR] no workspace initialised"
    root = os.path.realpath(WORKSPACE)
    path = os.path.realpath(os.path.join(root, name))
    if path != root and not path.startswith(root + os.sep):
        return None, f"[ERROR] refusing to touch a path outside the workspace: {name}"
    return path, None


def write_text_file(text: str, name: str) -> str:
    path, err = _safe_path(name)
    if err:
        return err
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            f.write(text)
    except OSError as e:
        return f"[ERROR] could not write {name}: {e}"
    print("WROTE:", path)
    return f"WROTE {len(text)} chars to {name}"


def read_file(name: str, max_chars: int = 6_000) -> str:
    """Read a workspace file back (head+tail if longer than max_chars)."""
    max_chars = max(200, min(int(max_chars), 20_000))
    path, err = _safe_path(name)
    if err:
        return err
    if os.path.isdir(path):
        return f"[ERROR] {name} is a directory — use list_files"
    try:
        with open(path, errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] could not read {name}: {e}"
    return f"--- {name} ({len(content)} chars) ---\n" + cap(content, max_chars)


def edit_file(name: str, find_text: str, replace_text: str,
              replace_all: bool = False) -> str:
    """Surgical edit: replace an exact substring. Cheaper and safer than
    re-emitting a whole file, and it can't silently drop the rest of it."""
    if not find_text:
        return "[ERROR] find_text must not be empty"
    path, err = _safe_path(name)
    if err:
        return err
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        return f"[ERROR] could not read {name}: {e}"
    count = content.count(find_text)
    if count == 0:
        return (f"[ERROR] find_text not found in {name} (the match is exact, "
                f"including whitespace) — read_file it first and copy the text verbatim")
    if count > 1 and not replace_all:
        return (f"[ERROR] find_text occurs {count} times in {name} — include more "
                f"surrounding context to make it unique, or set replace_all=true")
    new = content.replace(find_text, replace_text, -1 if replace_all else 1)
    try:
        with open(path, "w") as f:
            f.write(new)
    except OSError as e:
        return f"[ERROR] could not write {name}: {e}"
    n = count if replace_all else 1
    print(f"EDITED: {path} ({n} occurrence(s))")
    return f"EDITED {name}: replaced {n} occurrence(s); file is now {len(new)} chars"


def list_files(subdir: str = "", max_lines: int = 200) -> str:
    """List the workspace tree (dirs and files with sizes). Hidden entries
    (like our .agent_tmp scratch dir) are skipped."""
    path, err = _safe_path(subdir or ".")
    if err:
        return err
    if not os.path.isdir(path):
        return f"[ERROR] not a directory: {subdir or '.'}"
    root = os.path.realpath(WORKSPACE)
    lines = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            lines.append(rel_dir + "/")
        for fn in sorted(f for f in filenames if not f.startswith(".")):
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            try:
                size = os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                size = -1
            lines.append(f"{rel} ({size} bytes)")
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"[... listing truncated at {max_lines} entries ...]"]
            break
    return "\n".join(lines) if lines else "(workspace is empty)"


def delete_file(name: str) -> str:
    path, err = _safe_path(name)
    if err:
        return err
    if os.path.isdir(path):
        return f"[ERROR] {name} is a directory — refusing to delete directories"
    try:
        os.remove(path)
    except OSError as e:
        return f"[ERROR] could not delete {name}: {e}"
    print("DELETED:", path)
    return f"DELETED {name}"


# ─── Execution tools ────────────────────────────────────────────────

def run_python(code: str, timeout: int = 120) -> str:
    """Execute python code in a subprocess inside the workspace. The code is
    written to a scratch file first, so tracebacks show real line numbers and
    relative paths resolve against the workspace."""
    try:
        timeout = max(5, min(int(timeout), 600))
    except (TypeError, ValueError):
        timeout = 120
    if not WORKSPACE:
        return "[ERROR] no workspace initialised"
    tmp_dir = os.path.join(WORKSPACE, ".agent_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    snippet = os.path.join(tmp_dir, "snippet.py")
    with open(snippet, "w") as f:
        f.write(code)
    try:
        proc = subprocess.run(
            ["python3", snippet],
            capture_output=True, text=True, timeout=timeout,
            cwd=WORKSPACE,
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR] code timed out after {timeout} seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


ALLOWED_COMMANDS = ["pip", "pip3", "python3", "ls", "mkdir", "cat", "echo",
                    "head", "tail", "wc", "grep", "pwd"]
_SHELL_META = set(";|&<>`$\n")


def run_shell(command: str) -> str:
    """Run an allowlisted shell command in the workspace. shell=False + shlex
    so the allowlist can't be bypassed with 'echo hi; curl ... | sh' chaining."""
    if any(ch in _SHELL_META for ch in command):
        return ("[ERROR] shell metacharacters (; | & < > ` $) are not allowed. "
                "Run one plain command at a time.")
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"[ERROR] could not parse command: {e}"
    if not parts:
        return "[ERROR] empty command"
    if parts[0] in ("pip", "pip3"):
        # 'pip' on PATH isn't always the same interpreter as python3 — route
        # installs through python3 -m pip so run_python actually sees them
        parts = ["python3", "-m", "pip"] + parts[1:]
    if parts[0] not in ALLOWED_COMMANDS:
        return (f"[ERROR] command '{parts[0]}' is not allowed. "
                f"Allowed commands: {', '.join(ALLOWED_COMMANDS)}")
    try:
        print("RUN_SHELL", command)
        proc = subprocess.run(
            parts,
            capture_output=True, text=True, timeout=360,
            cwd=WORKSPACE or HERE,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out after 360 seconds"
    except FileNotFoundError:
        return f"[ERROR] program not found: {parts[0]}"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


# ─── Research tools ─────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo search — no API key needed. Returns numbered results with
    title, URL and snippet so the model can pick what to fetch_page next."""
    try:
        from ddgs import DDGS  # pip install ddgs
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # older package name
        except ImportError:
            return ("[ERROR] search needs the 'ddgs' package. "
                    "Install it with run_shell: pip install ddgs")
    max_results = max(1, min(int(max_results), 10))
    try:
        results = list(DDGS().text(query, max_results=max_results))
    except Exception as e:
        return f"[ERROR] search failed: {type(e).__name__}: {e}"
    if not results:
        return f"No results for: {query}"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n"
                     f"   {r.get('href', '')}\n"
                     f"   {r.get('body', '')}")
    return "\n".join(lines)


MAX_DOWNLOAD_BYTES = 2_000_000
_ALLOWED_CTYPES = ("text/", "application/json", "application/xml",
                   "application/xhtml", "application/rss", "application/atom")


def _host_is_public(host: str):
    """Resolve a hostname and check every address it maps to is a public IP.
    Replaces v1's string-prefix list, which over-blocked (all of 172.* — most
    of that is public space, e.g. Google's 172.217.*) and under-blocked
    (any private range not on the list). Returns (ok, error_string)."""
    if not host:
        return False, "empty hostname"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"could not resolve {host}: {e}"
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_text = str(sockaddr[0]).split("%")[0]  # strip IPv6 zone id
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return False, f"{host} resolved to an unparsable address {ip_text!r}"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"{host} resolves to a non-public address ({ip})"
    return True, ""


def fetch_page(url: str) -> str:
    """Fetch a web page and return its readable text, capped at PAGE_TEXT_MAX
    chars so one giant page can't blow the context window. Blocks private
    addresses (including via redirects), skips non-text content types, and
    stops downloading after MAX_DOWNLOAD_BYTES."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "[ERROR] only http/https URLs are allowed"
    ok, why = _host_is_public((parsed.hostname or "").lower())
    if not ok:
        return f"[ERROR] refusing to fetch: {why}"
    try:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, 30), stream=True,
                            allow_redirects=True, headers={
                                "User-Agent": "Mozilla/5.0 (compatible; research-agent/2.0)"})
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"[ERROR] fetch failed: {type(e).__name__}: {e}"

    # re-validate every hop — a public URL that 302s into 127.0.0.1 or the
    # LAN gets dropped before the model ever sees the content
    for hop in list(resp.history) + [resp]:
        ok, why = _host_is_public((urlparse(hop.url).hostname or "").lower())
        if not ok:
            resp.close()
            return f"[ERROR] redirect into a private network blocked: {why}"

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith(_ALLOWED_CTYPES):
        resp.close()
        return f"[skipped] content-type {ctype!r} — fetch_page only reads text/HTML/JSON pages"

    buf = bytearray()
    truncated = False
    try:
        for chunk in resp.iter_content(65_536):
            buf += chunk
            if len(buf) >= MAX_DOWNLOAD_BYTES:
                truncated = True
                break
    except requests.RequestException as e:
        return f"[ERROR] download broke mid-stream: {type(e).__name__}: {e}"
    finally:
        resp.close()

    html = bytes(buf).decode(resp.encoding or "utf-8", errors="replace")
    text = None
    try:
        import trafilatura  # pip install trafilatura — best-quality extraction
        text = trafilatura.extract(html, url=url)
    except ImportError:
        pass
    if not text:
        # crude fallback: strip scripts/styles/tags, collapse whitespace
        html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"[ERROR] no readable text extracted from {url}"
    note = " [download capped at 2MB]" if truncated else ""
    return f"[{resp.url}]{note}\n" + cap(text, PAGE_TEXT_MAX)


# ─── Docker MCP Toolkit ─────────────────────────────────────────────
# Connects to the Docker MCP gateway (`docker mcp gateway run`), which fronts
# every MCP server enabled in Docker Desktop's MCP Toolkit over a single
# stdio JSON-RPC 2.0 connection. Enable with --mcp; the gateway's tools are
# discovered at startup and merged into the normal tool registry.

MCP_TOOL_TIMEOUT = 300  # seconds per MCP request (containers can be slow to cold-start)


class MCPGateway:
    """Minimal synchronous MCP client over stdio (newline-delimited JSON-RPC).

    No SDK dependency: the MCP stdio transport is just JSON-RPC 2.0 messages,
    one per line, on the subprocess's stdin/stdout.
    """

    def __init__(self, command: list[str], timeout: int = MCP_TOOL_TIMEOUT):
        self.timeout = timeout
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # the gateway logs a lot on stderr
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"could not run {command[0]!r} — is Docker installed and on PATH?")
        self._id = 0
        self._initialize()

    # -- wire protocol -------------------------------------------------

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict:
        """Read the next JSON-RPC message, skipping any non-JSON noise."""
        import select
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("MCP gateway did not respond in time")
            # select() works on pipes on POSIX; on Windows fall back to blocking
            try:
                ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("MCP gateway did not respond in time")
            except (OSError, ValueError):
                pass  # non-selectable (e.g. Windows) — just block on readline
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "MCP gateway closed its output — is Docker Desktop running "
                    "with the MCP Toolkit enabled?")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line, ignore

    def _request(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                    "params": params or {}})
        deadline = time.time() + self.timeout
        while True:
            msg = self._read_message(deadline)
            if msg.get("id") != req_id:
                continue  # notification or unrelated message — skip it
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"{method}: {err.get('message', err)}")
            return msg.get("result", {})

    # -- MCP lifecycle ---------------------------------------------------

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agent.py", "version": __version__},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        info = result.get("serverInfo", {})
        print(f"[mcp] connected to {info.get('name', 'gateway')} "
              f"{info.get('version', '')}".rstrip())

    def list_tools(self) -> list[dict]:
        found, cursor = [], None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            found.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return found

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": name,
                                              "arguments": arguments or {}})
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content omitted]")
        text = "\n".join(p for p in parts if p) or "[no content returned]"
        if result.get("isError"):
            text = "[ERROR] " + text
        return text

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


mcp_gateway: Optional[MCPGateway] = None


def _mcp_tool_proxy(tool_name: str):
    def proxy(**kwargs):
        return mcp_gateway.call_tool(tool_name, kwargs)
    proxy.__name__ = f"mcp_{tool_name}"
    return proxy


def setup_mcp_tools(profile: Optional[str] = None) -> list[str]:
    """Start the Docker MCP gateway, discover its tools, and register them
    alongside the built-in tools. Returns the list of tool names added."""
    global mcp_gateway
    command = ["docker", "mcp", "gateway", "run"]
    if profile:
        command += ["--profile", profile]
    print(f"[mcp] starting gateway: {' '.join(command)}")
    mcp_gateway = MCPGateway(command)

    added = []
    for t in mcp_gateway.list_tools():
        name = t.get("name")
        if not name:
            continue
        if name in tools:
            print(f"[mcp] skipping tool '{name}' — name clashes with a built-in tool")
            continue
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        tools[name] = _mcp_tool_proxy(name)
        TOOL_SCHEMAS.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", "") or f"MCP tool {name}",
                "parameters": schema,
            },
        })
        added.append(name)
    print(f"[mcp] registered {len(added)} tool(s): {', '.join(added) or '(none)'}")
    return added


# ─── Per-attempt file tracking ──────────────────────────────────────
# The reviewer used to see only the agent's prose, so it graded CLAIMS
# ("I wrote fizzbuzz.py") instead of artifacts. Track what the agent
# actually writes each attempt and show the reviewer the real content —
# plus the full workspace tree, so even mkdir-created structure counts.

attempt_written_files: list[str] = []


def tracked_write_file(text: str, name: str) -> str:
    result = write_text_file(text, name)
    if result.startswith("WROTE"):
        attempt_written_files.append(name)
    return result


def tracked_edit_file(name: str, find_text: str, replace_text: str,
                      replace_all: bool = False) -> str:
    result = edit_file(name, find_text, replace_text, replace_all)
    if result.startswith("EDITED"):
        attempt_written_files.append(name)
    return result


def snapshot_files(names: list[str], per_file: int = 2_000,
                   total_max: int = 8_000) -> str:
    """Read the files the agent wrote this attempt, capped per-file and in
    total, so the reviewer verifies real on-disk content without the
    snapshot itself blowing the context window."""
    if not names:
        return "(the agent wrote no files this attempt)"
    chunks, used = [], 0
    for name in dict.fromkeys(names):  # dedupe, keep order
        path, err = _safe_path(name)
        if err:
            chunks.append(f"--- {name} --- [unreadable: {err}]")
            continue
        try:
            with open(path, errors="replace") as f:
                content = f.read()
        except OSError as e:
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


tools = {
    "write_file": tracked_write_file,
    "read_file": read_file,
    "edit_file": tracked_edit_file,
    "list_files": list_files,
    "delete_file": delete_file,
    "run_python": run_python,
    "run_shell": run_shell,
    "web_search": web_search,
    "fetch_page": fetch_page,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file in the workspace. Overwrites if the file already exists. Subdirectories in the name are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The full text content of the file."},
                    "name": {"type": "string", "description": "Filename to write, e.g. 'app.py' or 'src/util.py'."},
                },
                "required": ["text", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace. Long files are returned head+tail truncated. Use this before edit_file so your find_text matches exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filename to read, e.g. 'app.py'."},
                    "max_chars": {"type": "integer", "description": "Max characters to return, 200-20000 (default 6000)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a workspace file. Much cheaper than rewriting a whole file with write_file. find_text must match exactly (including whitespace) and must be unique unless replace_all is true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filename to edit."},
                    "find_text": {"type": "string", "description": "Exact text to find (copy it from read_file output)."},
                    "replace_text": {"type": "string", "description": "Text to replace it with (can be empty to delete)."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique match (default false)."},
                },
                "required": ["name", "find_text", "replace_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files and folders in the workspace (or a subfolder), with sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {"type": "string", "description": "Optional subfolder to list (default: the whole workspace)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a single file from the workspace (directories are refused).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filename to delete."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute python code in a subprocess inside the workspace. Returns stdout, stderr and the exit code. Use this to test code you have written.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The python code to execute."},
                    "timeout": {"type": "integer", "description": "Seconds before the run is killed, 5-600 (default 120)."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a single shell command in the workspace. Only these commands are allowed: pip, pip3, python3, ls, mkdir, cat, echo, head, tail, wc, grep, pwd. No pipes, chaining, or redirection. Use this to install packages (e.g. 'pip install flask') or inspect the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "One plain command, e.g. 'pip install flask'."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo). Returns numbered results with title, URL and snippet. Use short keyword queries (2-6 words). Follow up with fetch_page on the most promising URLs to actually read them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short keyword search query."},
                    "max_results": {"type": "integer", "description": "How many results, 1-10 (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Download a web page and return its readable text (truncated if very long). Use on URLs from web_search results to read sources in depth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full http(s) URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]


def execute_tool_call(name: str, arguments) -> str:
    func = tools.get(name)
    if not func:
        return f"[ERROR] Unknown tool: {name}"
    if isinstance(arguments, str):  # some models send arguments as a JSON string
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"[ERROR] Could not parse arguments for {name}: {e}"
    try:
        result = str(func(**arguments))
    except TypeError as e:
        result = f"[ERROR] Bad arguments for {name}: {e}"
    except Exception as e:
        result = f"[ERROR] {name} raised {type(e).__name__}: {e}"
    log_event("tool", name=name, args=json.dumps(arguments, default=str)[:500],
              ok=not result.startswith("[ERROR]"), result_chars=len(result))
    return result


# ────────────────────────────────────────────────────────────────────────────
# TALKING TO OLLAMA
# ────────────────────────────────────────────────────────────────────────────

def _options() -> dict:
    """Build the Ollama options dict from run-wide settings."""
    opts: dict = {"num_ctx": NUM_CTX}
    if RUN_TEMPERATURE is not None:
        opts["temperature"] = RUN_TEMPERATURE
    if SEED is not None:
        opts["seed"] = SEED
    if MAX_TOKENS is not None:
        opts["num_predict"] = MAX_TOKENS
    return opts


def _backoff(i: int, err) -> None:
    wait = 2 ** i
    ui.warning(f"  [retry] {err} — waiting {wait}s and retrying...")
    time.sleep(wait)


def _chat_once(payload: dict, label: str, print_stream: bool) -> dict:
    """One POST to /api/chat with retries, capability fallbacks, and
    token accounting. Returns the assistant message dict."""
    global SUPPORTS_THINK, SUPPORTS_FORMAT, STREAM, CHARS_PER_TOKEN

    if not SUPPORTS_THINK:
        payload.pop("think", None)
    if not SUPPORTS_FORMAT:
        payload.pop("format", None)
    payload.setdefault("options", {}).update(_options())
    payload["keep_alive"] = KEEP_ALIVE

    sent_chars = sum(len(str(m.get("content", ""))) for m in payload["messages"])

    start = time.time()
    last_err: Exception = RuntimeError("no attempts made")
    for i in range(4):
        try:
            resp = requests.post(
                URL + "/api/chat", json=payload, stream=payload.get("stream", False),
                timeout=(CONNECT_TIMEOUT, REQUEST_TIMEOUT))
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            _backoff(i, type(e).__name__)
            continue

        if resp.status_code == 400:
            body = resp.text[:2_000].lower()
            if "does not support tools" in body:
                raise SystemExit(
                    f"\nModel '{payload.get('model')}' does not support tool calling.\n"
                    "This harness needs a tool-capable model (e.g. qwen3, llama3.1, "
                    "mistral-nemo, command-r). Pick one with --model.")
            if "think" in body and "think" in payload:
                ui.note("  [fallback] model rejected 'think' — disabling thinking for this run")
                SUPPORTS_THINK = False
                payload.pop("think", None)
                continue
            if "format" in body and "format" in payload:
                ui.note("  [fallback] model rejected 'format' — falling back to legacy text parsing")
                SUPPORTS_FORMAT = False
                payload.pop("format", None)
                continue
            if payload.get("stream") and payload.get("tools"):
                ui.note("  [fallback] server rejected stream+tools — disabling streaming for this run")
                STREAM = False
                payload["stream"] = False
                continue
            raise RuntimeError(f"Ollama returned 400: {resp.text[:300]}")

        if resp.status_code >= 500:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            _backoff(i, f"HTTP {resp.status_code}")
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")

        if payload.get("stream"):
            msg, metrics = _consume_stream(resp, print_stream)
        else:
            data = resp.json()
            msg = data.get("message", {}) or {}
            metrics = {"prompt_eval_count": data.get("prompt_eval_count", 0),
                       "eval_count": data.get("eval_count", 0)}

        secs = time.time() - start
        p_tok = metrics.get("prompt_eval_count", 0) or 0
        e_tok = metrics.get("eval_count", 0) or 0
        total_time[label].append(secs)
        token_totals[label][0] += p_tok
        token_totals[label][1] += e_tok
        if p_tok > 0 and sent_chars > 0:
            observed = sent_chars / p_tok
            CHARS_PER_TOKEN = max(2.0, min(6.0, 0.8 * CHARS_PER_TOKEN + 0.2 * observed))
        ui.llm_timing(label, secs, p_tok, e_tok)
        log_event("llm", label=label, secs=round(secs, 2),
                  prompt_tokens=p_tok, eval_tokens=e_tok,
                  stream=bool(payload.get("stream")))
        return msg

    raise last_err


def _consume_stream(resp, do_print: bool):
    """Read an NDJSON /api/chat stream, printing thinking/content live.
    Returns (assistant_message_dict, metrics_dict)."""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []
    metrics: dict = {}
    printed_think_head = printed_answer_head = False

    try:
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            msg = chunk.get("message", {}) or {}

            t = msg.get("thinking")
            if t:
                thinking_parts.append(t)
                if do_print:
                    ui.stream_thinking(t, first=not printed_think_head)
                    printed_think_head = True

            c = msg.get("content")
            if c:
                content_parts.append(c)
                if do_print:
                    ui.stream_answer(c, first=not printed_answer_head,
                                     after_thinking=printed_think_head)
                    printed_answer_head = True

            if msg.get("tool_calls"):
                tool_calls.extend(msg["tool_calls"])

            if chunk.get("done"):
                metrics = {"prompt_eval_count": chunk.get("prompt_eval_count", 0),
                           "eval_count": chunk.get("eval_count", 0)}
    except requests.RequestException as e:
        raise RuntimeError(f"connection lost mid-stream: {e}")

    if do_print and (printed_think_head or printed_answer_head):
        ui.stream_end()

    out: dict = {"role": "assistant", "content": "".join(content_parts)}
    if thinking_parts:
        out["thinking"] = "".join(thinking_parts)
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out, metrics


def chat(model: str, system: str, user: str, tool_schemas=None, think: bool = True,
         label: str = "llm", max_tool_rounds: int = 15, fmt=None,
         stream=None, echo: bool = True, images: Optional[list] = None) -> str:
    """Multi-round chat loop: send → maybe execute tool calls → repeat.
    Returns the model's final text answer."""
    user_message: dict = {"role": "user", "content": user}
    if images:
        # Ollama vision format: base64 images ride alongside the text content.
        # Non-vision models ignore the field, so no capability fallback needed.
        user_message["images"] = images
    messages = [{"role": "system", "content": system}, user_message]

    for round_no in range(max_tool_rounds):
        do_stream = STREAM if stream is None else stream
        if fmt is not None:
            do_stream = False  # structured outputs come back as one JSON blob

        compact_messages(messages)
        if echo:
            ui.round_marker(round_no + 1, estimate_tokens(messages))

        payload: dict = {"model": model, "messages": messages, "stream": do_stream}
        if think and SUPPORTS_THINK:
            payload["think"] = True
        if fmt is not None and SUPPORTS_FORMAT:
            payload["format"] = fmt
        if tool_schemas:
            payload["tools"] = tool_schemas

        msg = _chat_once(payload, label, print_stream=(do_stream and echo))
        streamed_live = bool(payload.get("stream"))  # _chat_once may have flipped it

        if echo and not streamed_live:
            if msg.get("thinking"):
                ui.thinking(truncate_middle(msg['thinking'], 500))
            if msg.get("content"):
                ui.answer(msg['content'])

        if not FULL_CONTEXT:
            msg.pop("thinking", None)

        if not msg.get("tool_calls"):
            return msg.get("content", "")

        messages.append(msg)
        for call in msg["tool_calls"]:
            fn = call.get("function", {}) or {}
            name = fn.get("name", "?")
            args = fn.get("arguments", {}) or {}
            if echo:
                ui.tool_call(name, truncate_middle(json.dumps(args, default=str), 300))
            result = execute_tool_call(name, args)
            if echo:
                ui.tool_result(truncate_middle(result, 300))
            messages.append({"role": "tool", "tool_name": name,
                             "content": cap(result, TOOL_RESULT_MAX)})

    # Ran out of tool rounds — force a final, tool-free answer.
    ui.force_final(max_tool_rounds)
    messages.append({"role": "user", "content":
                     "You have used all available tool calls. Give your final answer "
                     "now, based on the work completed so far."})
    compact_messages(messages)
    payload = {"model": model, "messages": messages, "stream": False}
    if think and SUPPORTS_THINK:
        payload["think"] = True
    msg = _chat_once(payload, label, print_stream=False)
    if echo and msg.get("content"):
        ui.answer(msg['content'])
    return msg.get("content", "")


# ────────────────────────────────────────────────────────────────────────────
# REVIEW + GOALSMITH
# ────────────────────────────────────────────────────────────────────────────

YES_PAT = re.compile(r"^\s*yes\b", re.IGNORECASE)
NO_PAT = re.compile(r"^\s*no\b", re.IGNORECASE)
GOAL_TASK_PAT = re.compile(r"GOAL:\s*(?P<goal>.+?)\s*TASK:\s*(?P<task>.+)", re.DOTALL)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def review(model: str, goal: str, output: str, files_text: str,
           criteria: Optional[list]) -> dict:
    """Ask the reviewer whether the goal was met.
    Returns {"passed": bool, "feedback": str, "criteria": list|None, "raw": str}."""
    if criteria:
        crit_text = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    else:
        crit_text = ("(none provided — derive 3-6 concrete, checkable criteria "
                     "from the GOAL yourself)")

    user = REVIEW_USER.format(goal=goal, criteria=crit_text,
                              output=cap(output, 8_000), files=files_text)

    # ── structured path ──
    if SUPPORTS_FORMAT:
        prompt = user
        for attempt in range(2):
            raw = chat(model, REVIEWER_SYSTEM_JSON, prompt, think=False,
                       label="reviewer", fmt=REVIEW_SCHEMA, stream=False, echo=False)
            if not SUPPORTS_FORMAT:
                break  # capability got disabled mid-call — fall through to legacy
            try:
                data = json.loads(_strip_fences(raw))
                verdict = str(data.get("verdict", "")).upper()
                if verdict not in ("PASS", "FAIL"):
                    raise ValueError(f"bad verdict {verdict!r}")
                crits = data.get("criteria") or []
                unmet = [c for c in crits if not c.get("met")]
                passed = verdict == "PASS" and not unmet  # guard inconsistent verdicts
                feedback = str(data.get("feedback", "")).strip()
                if unmet:
                    details = "; ".join(
                        f"{c.get('criterion', '?')} ({c.get('note', 'not met')})"
                        for c in unmet)
                    feedback = (feedback + "\nUnmet: " + details).strip()
                ui.reviewer_verdict(verdict, len(unmet),
                                    truncate_middle(feedback, 600) if feedback else "")
                return {"passed": passed, "feedback": feedback,
                        "criteria": crits, "raw": raw}
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
                ui.note(f"  [reviewer] could not parse structured verdict ({e}) — retrying")
                prompt = (user + "\n\nREMINDER: respond with ONLY the JSON object "
                          "matching the schema. No prose, no code fences.")
        else:
            ui.note("  [reviewer] structured output failed twice — using legacy YES/NO")

    # ── legacy path ──
    raw = chat(model, REVIEWER_SYSTEM_LEGACY, user, think=False,
               label="reviewer", stream=False, echo=False)
    ui.reviewer_says(truncate_middle(raw, 600))
    if YES_PAT.match(raw):
        return {"passed": True, "feedback": "", "criteria": None, "raw": raw}
    if not NO_PAT.match(raw):
        strict = (user + "\n\nIMPORTANT: your reply MUST start with the single word "
                  "YES or NO on the first line, then your reasoning.")
        raw = chat(model, REVIEWER_SYSTEM_LEGACY, strict, think=False,
                   label="reviewer", stream=False, echo=False)
        ui.reviewer_says(truncate_middle(raw, 600), reasked=True)
        if YES_PAT.match(raw):
            return {"passed": True, "feedback": "", "criteria": None, "raw": raw}
    return {"passed": False, "feedback": raw.strip(), "criteria": None, "raw": raw}


def make_goal_task(model: str, prompt: str):
    """Turn a rough user prompt into (goal, task, criteria|None)."""
    ui.goalsmith_start()

    if SUPPORTS_FORMAT:
        raw = chat(model, GOALSMITH_SYSTEM_JSON, prompt, think=False,
                   label="goalsmith", fmt=GOALSMITH_SCHEMA, stream=False, echo=False)
        if SUPPORTS_FORMAT:
            try:
                data = json.loads(_strip_fences(raw))
                goal = str(data["goal"]).strip()
                task = str(data["task"]).strip()
                criteria = [str(c).strip() for c in (data.get("criteria") or []) if str(c).strip()]
                ui.goal_task(goal, task, criteria)
                return goal, task, criteria or None
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                ui.note(f"  [goalsmith] could not parse structured plan ({e}) — using legacy format")

    raw = chat(model, GOALSMITH_SYSTEM_LEGACY, prompt, think=False,
               label="goalsmith", stream=False, echo=False)
    m = GOAL_TASK_PAT.search(raw)
    if not m:
        ui.warning("WARNING: could not parse GOAL/TASK — using your prompt as both.")
        return prompt, prompt, None
    goal, task = m.group("goal").strip(), m.group("task").strip()
    ui.goal_task(goal, task)
    return goal, task, None


# ────────────────────────────────────────────────────────────────────────────
# MAIN LOOP: EXECUTE → REVIEW → RETRY
# ────────────────────────────────────────────────────────────────────────────

def main(model: str, reviewer_model: str, goal: str, task: str,
         criteria: Optional[list], max_attempts: int, max_tool_rounds: int,
         attachments: Optional[list] = None, images: Optional[list] = None) -> None:
    global RUN_TEMPERATURE

    attempts: list[dict] = []
    feedback_history: list[str] = []
    prev_answer = ""
    attach_note = (ATTACHMENT_NOTE.format(
        names="\n".join("- " + n for n in attachments)) if attachments else "")
    user_msg = task + attach_note
    status = "max_attempts"

    log_event("run_start", goal=goal, task=_short(task), model=model,
              reviewer=reviewer_model, max_attempts=max_attempts,
              attachments=attachments or [])
    transcript(f"# Agent run {_ts()}\n\n**Goal:** {goal}\n\n**Task:** {task}\n")

    try:
        for attempt in range(1, max_attempts + 1):
            ui.attempt_banner(attempt, max_attempts)
            attempt_written_files.clear()

            answer = chat(model, EXECUTOR_SYSTEM, user_msg,
                          tool_schemas=TOOL_SCHEMAS, think=THINK_DEFAULT,
                          label="executor", max_tool_rounds=max_tool_rounds,
                          images=images)

            # ── stall detection: near-identical answer to last attempt ──
            stalled = False
            if prev_answer:
                ratio = difflib.SequenceMatcher(
                    None, prev_answer[:5_000], answer[:5_000]).ratio()
                if ratio > 0.95:
                    stalled = True
                    ui.stall(ratio)
            prev_answer = answer

            if stalled:
                RUN_TEMPERATURE = min((RUN_TEMPERATURE or 0.7) + 0.3, 1.3)
                ui.stall_bump(RUN_TEMPERATURE)
                verdict = {"passed": False, "criteria": None, "raw": "(stall)",
                           "feedback": ("Your answer was nearly identical to the previous "
                                        "attempt. It was rejected. Take a DIFFERENT approach: "
                                        "re-read the goal, use different tools or steps, and "
                                        "produce substantively new work.")}
            else:
                files_text = ("WORKSPACE TREE:\n" + list_files(max_lines=60)
                              + "\n\nFILES THE AGENT WROTE THIS ATTEMPT:\n"
                              + snapshot_files(attempt_written_files))
                text_attachments = [n for n in (attachments or [])
                                    if os.path.splitext(n)[1].lower() not in IMAGE_EXTS]
                if text_attachments:
                    files_text += ("\n\nFILES THE USER ATTACHED (inputs, not agent work):\n"
                                   + snapshot_files(text_attachments))
                verdict = review(reviewer_model, goal, answer, files_text, criteria)

            attempts.append({"attempt": attempt, "passed": verdict["passed"],
                             "stalled": stalled,
                             "feedback": verdict["feedback"],
                             "criteria": verdict.get("criteria"),
                             "answer": answer})
            transcript(f"\n## Attempt {attempt} — "
                       f"{'PASSED' if verdict['passed'] else 'FAILED'}\n\n"
                       f"{cap(answer, 4_000)}\n\n"
                       f"**Reviewer:** {cap(verdict['feedback'] or verdict['raw'], 1_500)}\n")
            log_event("attempt", n=attempt, passed=verdict["passed"], stalled=stalled,
                      feedback=_short(verdict["feedback"]))

            if verdict["passed"]:
                ui.success(attempt)
                write_text_file(answer, "final_output.txt")
                status = "passed"
                break

            write_text_file(answer, f"attempt_{attempt}_failed.txt")
            feedback_history.append(
                f"[attempt {attempt}] {cap(verdict['feedback'] or verdict['raw'], 800)}")
            user_msg = task + attach_note + RETRY_NOTE.format(
                feedback="\n".join(feedback_history[-3:]),
                workspace=list_files(max_lines=40),
                previous=cap(answer, RETRY_PREV_MAX))
            if stalled:
                user_msg += ("\n\nIMPORTANT: your last two answers were nearly identical. "
                             "You MUST take a different approach this time.")
        else:
            ui.not_verified(max_attempts)
            write_text_file(prev_answer, "final_output_UNVERIFIED.txt")

    except KeyboardInterrupt:
        status = "interrupted"
        ui.interrupted()
    finally:
        try:
            with open(os.path.join(WORKSPACE, "attempt_history.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"goal": goal, "task": task, "criteria": criteria,
                           "status": status, "attempts": attempts}, f,
                          indent=2, ensure_ascii=False)
        except OSError as e:
            ui.warning(f"WARNING: could not save attempt_history.json: {e}")
        log_event("run_end", status=status, attempts=len(attempts))
        transcript(f"\n---\n**Run finished:** {status} after {len(attempts)} attempt(s)\n")
        print_run_summary()
        ui.run_summary(status, len(attempts), WORKSPACE)


# ────────────────────────────────────────────────────────────────────────────
# STARTUP CHECKS + CLI
# ────────────────────────────────────────────────────────────────────────────

def check_model(model: str) -> None:
    """Fail fast (with a helpful message) if the model isn't on the server."""
    try:
        resp = requests.get(URL + "/api/tags", timeout=10)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except requests.RequestException as e:
        ui.note(f"NOTE: could not list models ({type(e).__name__}) — skipping model check.")
        return
    if model in names:
        return
    if ":" not in model and f"{model}:latest" in names:
        return
    listing = "\n".join(f"  - {n}" for n in sorted(names)) or "  (none)"
    raise SystemExit(
        f"\nModel '{model}' is not available on {URL}.\n"
        f"Available models:\n{listing}\n\n"
        f"Pull it first:  ollama pull {model}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Agent harness for a local Ollama server: execute → review → retry.")
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("-g", "--goal", help="the goal; also used verbatim as the task")
    g.add_argument("-sg", "--smart-goal",
                   help="rough prompt — the model writes the GOAL, TASK, and criteria")
    p.add_argument("--repl", action="store_true",
                   help="start the interactive prompt (also the default when no goal is given)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"executor model (default {DEFAULT_MODEL})")
    p.add_argument("--reviewer-model", default=None,
                   help="reviewer model (default: same as --model)")
    p.add_argument("--attempts", type=int, default=5, help="max attempts (default 5)")
    p.add_argument("--url", default=None, help=f"Ollama base URL (default {URL})")
    p.add_argument("--workspace", default=None,
                   help="directory for all agent files (default ./runs/run_<timestamp>)")
    p.add_argument("--files", nargs="+", default=None, metavar="PATH",
                   help="files to copy into the workspace before the run "
                        "(images also go to vision models)")
    p.add_argument("--num-ctx", type=int, default=NUM_CTX,
                   help=f"context window in tokens (default {NUM_CTX})")
    p.add_argument("--temperature", type=float, default=None, help="sampling temperature")
    p.add_argument("--seed", type=int, default=None, help="sampling seed (reproducibility)")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="max tokens per response (Ollama num_predict)")
    p.add_argument("--keep-alive", default="10m",
                   help="how long the server keeps the model loaded (default 10m)")
    p.add_argument("--max-tool-rounds", type=int, default=15,
                   help="max tool-calling rounds per attempt (default 15)")
    p.add_argument("--no-stream", action="store_true", help="disable live token streaming")
    p.add_argument("--no-think", action="store_true", help="disable model thinking")
    p.add_argument("--full-context", action="store_true",
                   help="disable ALL trimming/compaction — send everything (needs big num_ctx)")
    p.add_argument("--mcp", action="store_true",
                   help="connect to the Docker MCP Toolkit gateway for extra tools")
    p.add_argument("--mcp-profile", default=None,
                   help="MCP profile name passed to the gateway")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.url:
        URL = args.url.rstrip("/")
    NUM_CTX = args.num_ctx
    FULL_CONTEXT = args.full_context
    STREAM = not args.no_stream
    THINK_DEFAULT = not args.no_think
    RUN_TEMPERATURE = args.temperature
    SEED = args.seed
    MAX_TOKENS = args.max_tokens
    KEEP_ALIVE = args.keep_alive

    reviewer_model = args.reviewer_model or args.model
    # No goal on the command line → drop into the interactive prompt.
    repl_mode = args.repl or (not args.goal and not args.smart_goal)

    try:
        check_model(args.model)
        if reviewer_model != args.model:
            check_model(reviewer_model)

        if args.mcp:
            try:
                extra = setup_mcp_tools(args.mcp_profile)
            except (RuntimeError, TimeoutError) as e:
                raise SystemExit(f"\nMCP gateway failed to start: {e}\n"
                                 "Is Docker Desktop running with the MCP Toolkit enabled?")
            if extra:
                EXECUTOR_SYSTEM += ("\n\nYou also have these extra MCP tools available: "
                                    + ", ".join(extra))

        if repl_mode:
            from repl import run_repl
            run_repl(args, reviewer_model)
        else:
            WORKSPACE = init_workspace(args.workspace)
            ui.run_header(version=__version__, server=URL, executor=args.model,
                          reviewer=reviewer_model, workspace=WORKSPACE, num_ctx=NUM_CTX,
                          stream=STREAM, think=THINK_DEFAULT,
                          temp=RUN_TEMPERATURE, seed=SEED)

            # @path tokens in the goal text become attachments too.
            goal_text = args.smart_goal or args.goal
            goal_text, at_paths = extract_at_paths(goal_text)
            copied, images = stage_attachments((args.files or []) + at_paths)

            if args.smart_goal:
                goal, task, criteria = make_goal_task(args.model, goal_text)
            else:
                goal, task, criteria = goal_text, goal_text, None

            main(args.model, reviewer_model, goal, task, criteria,
                 args.attempts, args.max_tool_rounds,
                 attachments=copied, images=images)

    except requests.ConnectionError:
        ui.error(f"\nERROR: could not reach Ollama at {URL}.\n"
                 "Is the server running? Set --url or OLLAMA_URL/OLLAMA_HOST if it lives elsewhere.")
    except requests.Timeout:
        ui.error(f"\nERROR: request timed out after {REQUEST_TIMEOUT}s. The model may be "
                 "too large for this hardware, or the server is stuck.")
    except requests.RequestException as e:
        ui.error(f"\nERROR: HTTP problem talking to Ollama: {e}")
    finally:
        if mcp_gateway is not None:
            mcp_gateway.close()