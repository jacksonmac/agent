"""Execute -> review -> retry agent harness for a local Ollama server.

Usage:
    python3 agent.py -g "A script fizzbuzz.py that prints FizzBuzz for 1-30"
    python3 agent.py -sg "I need a folder called output with a readme in it"
    python3 agent.py -g "..." --model qwen3.5:9b --attempts 3 --num-ctx 32768
    python3 agent.py -g "..." --full-context --num-ctx 65536
    python3 agent.py -g "..." --mcp                    # + Docker MCP Toolkit tools
    python3 agent.py -g "..." --mcp --mcp-profile dev  # a specific Toolkit profile

--mcp : connect to the Docker MCP Toolkit gateway ('docker mcp gateway run')
and expose every tool from your enabled MCP servers to the executor, alongside
the built-in tools. Requires Docker Desktop with the MCP Toolkit enabled (or
the standalone docker-mcp CLI plugin on Linux).

--full-context : send the model EVERYTHING — no compaction, no truncation of
tool results, retries, or thinking. Pair it with a big --num-ctx, because
anything past num_ctx is silently dropped by ollama (oldest first).

-g  : use your text as the goal (the task sent to the executor is the same text)
-sg : "smart goal" — the LM rewrites your input into a proper GOAL + TASK first

Optional deps for the research tools:
    pip install ddgs trafilatura
"""

import argparse
import difflib
import functools
import json
import os
import re
import shlex
import subprocess
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://192.168.1.134:11434"
DEFAULT_MODEL = "gemma4:26b"

REQUEST_TIMEOUT = 600  # seconds per LLM call — big models on CPU can be slow

# ─── Context budget ─────────────────────────────────────────────────
# Ollama silently truncates anything past num_ctx (default is only 4096!),
# so we (a) request a bigger window explicitly and (b) keep what we SEND
# under budget so the model never loses the system prompt or the task.

NUM_CTX = 16384              # requested context window (more = more RAM/VRAM)
CHARS_PER_TOKEN = 3          # conservative estimate for budgeting
TOOL_RESULT_MAX = 4_000      # chars of any single tool result kept in history
RETRY_PREV_MAX = 6_000       # chars of a failed attempt fed into the retry
PAGE_TEXT_MAX = 6_000        # chars of a fetched web page returned to the model
COMPACT_KEEP_LAST = 6        # never compact the most recent N messages

FULL_CONTEXT = False  # --full-context: disable ALL trimming, send everything


def truncate_middle(text: str, max_chars: int) -> str:
    """Cap text length, keeping the head and tail (that's where the signal
    usually is — imports/opening vs. errors/conclusions)."""
    if len(text) <= max_chars:
        return text
    marker = f"\n[... {len(text) - max_chars} chars truncated ...]\n"
    half = max(0, (max_chars - len(marker)) // 2)
    return text[:half] + marker + text[-half:]


def cap(text: str, max_chars: int) -> str:
    """Truncate — unless full-context mode is on, in which case pass through."""
    return text if FULL_CONTEXT else truncate_middle(text, max_chars)


def estimate_tokens(messages: list) -> int:
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return total // CHARS_PER_TOKEN


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
            print(f"  [WARNING] full-context mode: sending ~{est} tokens but "
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
        print(f"  [WARNING] history still ~{estimate_tokens(messages)} tokens "
              f"after compaction (budget {budget_tokens})")


# ─── Prompts ────────────────────────────────────────────────────────

EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output. Use the tools available: write files with write_file, test code with
run_python, install packages or inspect the project with run_shell (e.g. 'pip install flask').

For research: use web_search to find sources, then fetch_page on the 1-3 most promising URLs
to read them, then synthesize what you learned into your answer. Do not answer research
questions from memory alone when you can verify with a search.

If a test fails, fix the code and test again before finishing."""

REVIEWER_SYSTEM = """You are a strict reviewer. You will be given a GOAL and the OUTPUT
of an agent that tried to achieve it. Decide if the output actually meets the goal.

The FIRST word of your reply must be exactly YES or NO.
If NO, follow it with one short paragraph listing what is missing or broken.
If YES, say nothing else."""

REVIEW_USER = """GOAL:
{goal}

AGENT OUTPUT:
{output}

FILES THE AGENT WROTE THIS ATTEMPT (actual on-disk content, possibly truncated):
{files}

Did the output meet the goal? Judge the FILES, not just the agent's claims."""

RETRY_NOTE = """

A previous attempt did NOT meet the goal according to the reviewer.

Reviewer feedback so far (fix ALL of it, not just the latest):
{feedback}

Here is the most recent attempt — fix what is missing or broken and finish the goal:

--- PREVIOUS ATTEMPT ---
{previous}
--- END PREVIOUS ATTEMPT ---"""

GOALSMITH_SYSTEM = """You turn a rough user request into two things:

GOAL: a single, concrete, checkable success condition (what a reviewer will verify).
TASK: instructions for an agent with write_file / run_python / run_shell /
web_search / fetch_page tools, telling it what to build, save, run, and verify.

Reply in EXACTLY this format, nothing before or after:
GOAL: <one or two sentences>
TASK: <one paragraph>"""

# ─── Timing ─────────────────────────────────────────────────────────

total_time: dict[str, list[float]] = defaultdict(list)


def timed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        if elapsed > 60:
            print(f"[{func.__name__}] took {elapsed:.2f}s (~{elapsed / 60:.1f} min)")
        else:
            print(f"[{func.__name__}] took {elapsed:.2f}s")
        total_time[func.__name__].append(elapsed)
        return result
    return wrapper


def print_timing_summary():
    if not total_time:
        return
    print("\n─── timing summary ───")
    for name, times in total_time.items():
        print(f"  {name}: {len(times)} call(s), total {sum(times):.1f}s, "
              f"avg {sum(times) / len(times):.1f}s")


# ─── Tools ──────────────────────────────────────────────────────────

def run_python(code: str) -> str:
    """Execute python code in a subprocess."""
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=120,
            cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] code timed out after 120 seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


ALLOWED_COMMANDS = ["pip", "pip3", "python3", "ls", "mkdir", "cat", "echo"]
_SHELL_META = set(";|&<>`$\n")


def run_shell(command: str) -> str:
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
            cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out after 360 seconds"
    except FileNotFoundError:
        return f"[ERROR] program not found: {parts[0]}"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


def write_text_file(text: str, name: str) -> str:
    path = os.path.abspath(os.path.join(HERE, name))
    if not path.startswith(HERE + os.sep):
        return f"[ERROR] refusing to write outside the project directory: {name}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print("WROTE:", path)
    return f"WROTE {len(text)} chars to {name}"


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


_BLOCKED_HOSTS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "169.254.", "172.")


def fetch_page(url: str) -> str:
    """Fetch a web page and return its readable text, capped at PAGE_TEXT_MAX
    chars so one giant page can't blow the context window."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "[ERROR] only http/https URLs are allowed"
    host = (parsed.hostname or "").lower()
    if any(host == b.rstrip(".") or host.startswith(b) for b in _BLOCKED_HOSTS):
        return "[ERROR] refusing to fetch local/private network addresses"
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; research-agent/1.0)"})
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"[ERROR] fetch failed: {type(e).__name__}: {e}"

    html = resp.text
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
    return f"[{url}]\n" + truncate_middle(text, PAGE_TEXT_MAX)


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
            "clientInfo": {"name": "agent.py", "version": "1.0"},
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
# actually writes each attempt and show the reviewer the real content.

attempt_written_files: list[str] = []


def tracked_write_file(text: str, name: str) -> str:
    result = write_text_file(text, name)
    if result.startswith("WROTE"):
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
        path = os.path.join(HERE, name)
        try:
            with open(path) as f:
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
            "description": "Write text content to a file in the project directory. Overwrites if the file already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The full text content of the file."},
                    "name": {"type": "string", "description": "Filename to write, e.g. 'app.py'."},
                },
                "required": ["text", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute python code in a subprocess. Returns stdout, stderr and the exit code. Use this to test code you have written.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The python code to execute."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a single shell command in the project directory. Only these commands are allowed: pip, pip3, python3, ls, mkdir, cat, echo. No pipes, chaining, or redirection. Use this to install packages (e.g. 'pip install flask') or inspect the project.",
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


# ─── Core chat with tool loop ───────────────────────────────────────

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
        return str(func(**arguments))
    except TypeError as e:
        return f"[ERROR] Bad arguments for {name}: {e}"
    except Exception as e:
        return f"[ERROR] {name} raised {type(e).__name__}: {e}"


def _post_chat(payload: dict) -> dict:
    payload.setdefault("options", {})["num_ctx"] = NUM_CTX
    resp = requests.post(URL + "/api/chat", json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["message"]


@timed
def chat_v2(model: str, system: str, user: str, tool_schemas: Optional[list],
            think: bool = True, max_tool_rounds: int = 15) -> str:
    """One system + one user turn, with an optional tool-calling loop."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    for round_num in range(max_tool_rounds):
        compact_messages(messages)
        print(f"  [context ~{estimate_tokens(messages)} tokens / {NUM_CTX}]")

        payload = {"model": model, "messages": messages, "think": think, "stream": False}
        if tool_schemas:
            payload["tools"] = tool_schemas

        msg = _post_chat(payload)

        if msg.get("thinking"):
            print(f"  [thinking round {round_num}]:\n", msg["thinking"][:500], "\n")
        # don't resend the thinking text every round — it's context we pay
        # for on every subsequent call and the model doesn't need it back
        # (in full-context mode we keep it: the model gets everything)
        if not FULL_CONTEXT:
            msg.pop("thinking", None)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            print("Answer:\n", msg["content"], "\n")
            return msg["content"]

        messages.append(msg)
        for tc in tool_calls:
            func_info = tc["function"]
            tool_name = func_info["name"]
            tool_args = func_info.get("arguments", {})
            print(f"  [tool call] {tool_name}({json.dumps(tool_args)[:200]})")
            result = execute_tool_call(tool_name, tool_args)
            print(f"  [tool result] {result[:300]}")
            messages.append({
                "role": "tool",
                "tool_name": tool_name,  # ollama matches the result to the call by this
                # cap what enters history — a huge stdout or web page stays
                # useful (head + tail) without eating the whole window
                "content": cap(result, TOOL_RESULT_MAX),
            })

    # Exhausted all rounds: one final call with no tools so it must answer in text
    print("[WARNING] Hit max tool rounds, forcing final response")
    compact_messages(messages)
    msg = _post_chat({"model": model, "messages": messages, "think": think, "stream": False})
    print("Answer (forced):\n", msg["content"], "\n")
    return msg["content"]


# ─── Reviewer ───────────────────────────────────────────────────────

YES_PAT = re.compile(r"^(yes|y|yeah|yep|yup)\b", re.IGNORECASE)
NO_PAT = re.compile(r"^(no|n|nah|nope)\b", re.IGNORECASE)


def review(model: str, goal: str, output: str, files_text: str = "(none)") -> str:
    """Ask the reviewer if the goal was met. Returns the raw verdict text.
    A verdict that doesn't start with YES/NO gets ONE stricter re-ask; if it's
    still malformed we treat it as NO (with the text as feedback) rather than
    aborting the whole run over a formatting slip."""
    user = REVIEW_USER.format(goal=goal,
                              output=cap(output, RETRY_PREV_MAX),
                              files=files_text)
    for strict in (False, True):
        verdict = chat_v2(
            model,
            REVIEWER_SYSTEM,
            user + ("\n\nREMINDER: the FIRST word of your reply MUST be exactly "
                    "YES or NO." if strict else ""),
            tool_schemas=None,
            think=False,
        ).strip()
        if YES_PAT.match(verdict) or NO_PAT.match(verdict):
            return verdict
        print("  [review] verdict didn't start with YES/NO, re-asking once")
    print("  [review] still malformed — treating as NO")
    return "NO (reviewer verdict was malformed) " + verdict


# ─── Smart goal (-sg) ───────────────────────────────────────────────

GOAL_TASK_PAT = re.compile(r"GOAL:\s*(.+?)\s*TASK:\s*(.+)", re.DOTALL | re.IGNORECASE)


def make_goal_task(model: str, prompt: str) -> tuple[str, str]:
    """Have the LM rewrite a rough user prompt into a (goal, task) pair."""
    reply = chat_v2(
        model,
        GOALSMITH_SYSTEM,
        f"User request: {prompt}",
        tool_schemas=None,
        think=True,
    )
    m = GOAL_TASK_PAT.search(reply)
    if not m:
        print("[WARNING] could not parse GOAL/TASK from model reply, "
              "using your prompt as both")
        return prompt, prompt
    goal, task = m.group(1).strip(), m.group(2).strip()
    print(f"\nGOAL: {goal}\nTASK: {task}\n")
    return goal, task


# ─── Main loop: execute -> review -> retry ──────────────────────────

def main(model: str, goal: str, task: str, max_attempts: int = 5):
    attempts = []            # keep EVERY attempt + verdict, nothing gets overwritten
    feedback_history = []    # ALL reviewer feedback, so retries fix everything at once
    prev_answer = None
    answer = None
    user_msg = task          # first attempt gets the plain task

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== EXECUTING (attempt {attempt}/{max_attempts}) ===")
        attempt_written_files.clear()
        answer = chat_v2(model, EXECUTOR_SYSTEM, user_msg, tool_schemas=TOOL_SCHEMAS)

        # Stall detection: if this attempt is nearly identical to the last
        # failed one, reviewing it again is a waste of an expensive LLM call —
        # skip straight to a retry that demands a different approach.
        stalled = (prev_answer is not None and
                   difflib.SequenceMatcher(None, answer[:5_000],
                                           prev_answer[:5_000]).ratio() > 0.95)
        if stalled:
            print(f"attempt {attempt} is nearly identical to the previous one — "
                  f"skipping review, demanding a new approach")
            verdict = "NO (skipped review: output nearly identical to the previous failed attempt)"
        else:
            print(f"\n=== REVIEWING (attempt {attempt}) ===")
            files_text = snapshot_files(attempt_written_files)
            verdict = review(model, goal, answer, files_text)
            print(f"reviewer said: {verdict!r}")

        attempts.append({"attempt": attempt, "output": answer,
                         "files": list(dict.fromkeys(attempt_written_files)),
                         "verdict": verdict})

        if YES_PAT.match(verdict):
            print(f"WE DID IT on attempt {attempt}")
            write_text_file(answer, "final_output.txt")
            break

        # everything else (NO or malformed-treated-as-NO) → retry
        print(f"goal not met on attempt {attempt}, saving output and retrying")
        write_text_file(answer, f"attempt_{attempt}_failed.txt")
        feedback_history.append(f"[attempt {attempt}] {cap(verdict, 800)}")
        # feed ALL recent feedback (not just the latest) + a CAPPED slice of
        # the failure back in — an uncapped 30KB failed attempt would
        # dominate the window
        user_msg = task + RETRY_NOTE.format(
            feedback="\n".join(feedback_history[-3:]),
            previous=cap(answer, RETRY_PREV_MAX),
        )
        if stalled:
            user_msg += ("\n\nIMPORTANT: your last two attempts were nearly "
                         "identical. Take a DIFFERENT approach this time.")
        prev_answer = answer
    else:
        print(f"[WARNING] hit max attempts ({max_attempts}) without meeting the goal")
        if answer is not None:
            # don't leave the user empty-handed — the last attempt is still
            # the best artifact we have, just unverified
            write_text_file(answer, "final_output_UNVERIFIED.txt")

    write_text_file(json.dumps(attempts, indent=2), "attempt_history.json")
    print_timing_summary()


def parse_args():
    p = argparse.ArgumentParser(description="execute -> review -> retry agent harness")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-g", "--goal", help="use this text as the goal (and the task)")
    g.add_argument("-sg", "--smart-goal",
                   help="LM rewrites your input into a proper GOAL + TASK, then runs")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"ollama model (default: {DEFAULT_MODEL})")
    p.add_argument("--attempts", type=int, default=5, help="max execute/review attempts (default: 5)")
    p.add_argument("--url", default=None, help=f"ollama server URL (default: {URL})")
    p.add_argument("--full-context", action="store_true",
                   help="send everything: no compaction or truncation of tool results, "
                        "retries, or thinking (pair with a big --num-ctx)")
    p.add_argument("--num-ctx", type=int, default=NUM_CTX,
                   help=f"context window to request from ollama (default: {NUM_CTX}; more = more RAM/VRAM)")
    p.add_argument("--mcp", action="store_true",
                   help="connect to the Docker MCP Toolkit gateway "
                        "('docker mcp gateway run') and expose its tools to the agent")
    p.add_argument("--mcp-profile", default=None,
                   help="MCP Toolkit profile to use (passed as --profile to the gateway)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.url:
        URL = args.url
    NUM_CTX = args.num_ctx
    FULL_CONTEXT = args.full_context
    if FULL_CONTEXT:
        print(f"[full-context mode] no trimming; num_ctx={NUM_CTX}")

    if args.mcp:
        try:
            mcp_tool_names = setup_mcp_tools(profile=args.mcp_profile)
        except (RuntimeError, TimeoutError) as e:
            raise SystemExit(f"[ERROR] Docker MCP gateway: {e}")
        if mcp_tool_names:
            # tell the executor these extra tools exist so it reaches for them
            EXECUTOR_SYSTEM += (
                "\n\nAdditional tools are available via the Docker MCP Toolkit: "
                + ", ".join(mcp_tool_names)
                + ". Use them when they fit the task better than the built-in tools."
            )

    try:
        if args.smart_goal:
            goal, task = make_goal_task(args.model, args.smart_goal)
        else:
            goal, task = args.goal, args.goal
        main(args.model, goal, task, max_attempts=args.attempts)
    except requests.ConnectionError:
        print(f"[ERROR] could not reach the ollama server at {URL} — is it running? "
              f"(override with --url)")
    except requests.Timeout:
        print(f"[ERROR] the model took longer than {REQUEST_TIMEOUT}s to respond")
    finally:
        if mcp_gateway is not None:
            mcp_gateway.close()