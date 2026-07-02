"""Execute -> review -> retry agent harness for a local Ollama server.

Usage:
    python3 agent.py -g "A script fizzbuzz.py that prints FizzBuzz for 1-30"
    python3 agent.py -sg "I need a folder called output with a readme in it"
    python3 agent.py -g "..." --model qwen3.5:9b --attempts 3

-g  : use your text as the goal (the task sent to the executor is the same text)
-sg : "smart goal" — the LM rewrites your input into a proper GOAL + TASK first
"""

import argparse
import functools
import json
import os
import re
import shlex
import subprocess
import time
from collections import defaultdict
from typing import Optional

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://192.168.1.134:11434"
#DEFAULT_MODEL = "gemma4:26b"
DEFAULT_MODEL = "qwen3.5:9b"


REQUEST_TIMEOUT = 6000  # seconds per LLM call — big models on CPU can be slow

# ─── Prompts ────────────────────────────────────────────────────────

EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output. Use the tools available: write files with write_file, test code with
run_python, install packages or inspect the project with run_shell (e.g. 'pip install flask').
If a test fails, fix the code and test again before finishing."""

# Reviewer now gives a reason on NO. The first word must still be YES or NO so
# the YES_PAT / NO_PAT matching keeps working, but the reason gets fed back
# into the retry so the next attempt knows WHAT to fix, not just that it failed.
REVIEWER_SYSTEM = """You are a strict reviewer. You will be given a GOAL and the OUTPUT
of an agent that tried to achieve it. Decide if the output actually meets the goal.

The FIRST word of your reply must be exactly YES or NO.
If NO, follow it with one short paragraph listing what is missing or broken.
If YES, say nothing else."""

REVIEW_USER = """GOAL:
{goal}

AGENT OUTPUT:
{output}

Did the output meet the goal?"""

RETRY_NOTE = """

A previous attempt did NOT meet the goal according to the reviewer.

Reviewer feedback:
{feedback}

Here is that previous attempt — fix what is missing or broken and finish the goal:

--- PREVIOUS ATTEMPT ---
{previous}
--- END PREVIOUS ATTEMPT ---"""

# For -sg: the LM turns a loose user prompt into a measurable GOAL and an
# actionable TASK. Delimited lines make the parsing regex reliable.
GOALSMITH_SYSTEM = """You turn a rough user request into two things:

GOAL: a single, concrete, checkable success condition (what a reviewer will verify).
TASK: instructions for an agent with write_file / run_python / run_shell tools,
telling it what to build, save, run, and verify.

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
        total_time[func.__name__].append(elapsed)
        if elapsed > 60:
            print(f"[{func.__name__}] took {elapsed:.2f}s (~{elapsed / 60:.1f} min)")
        else:
            print(f"[{func.__name__}] took {elapsed:.2f}s")
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

# shell metacharacters that would let a command chain past the allowlist
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
            parts,  # a list, shell=False — no shell interpretation at all
            capture_output=True, text=True, timeout=360,  # pip can be slow
            cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out after 360 seconds"
    except FileNotFoundError:
        return f"[ERROR] program not found: {parts[0]}"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


def write_text_file(text: str, name: str) -> str:
    # keep writes inside the project dir — reject "../../etc/passwd" style names
    path = os.path.abspath(os.path.join(HERE, name))
    if not path.startswith(HERE + os.sep):
        return f"[ERROR] refusing to write outside the project directory: {name}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print("WROTE:", path)
    return f"WROTE {len(text)} chars to {name}"


tools = {
    "write_file": write_text_file,
    "run_python": run_python,
    "run_shell": run_shell,
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
        # a tool crashing should feed the error back so the model can
        # self-correct, not kill the loop
        return f"[ERROR] {name} raised {type(e).__name__}: {e}"


def _post_chat(payload: dict) -> dict:
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
        payload = {"model": model, "messages": messages, "think": think, "stream": False}
        if tool_schemas:
            payload["tools"] = tool_schemas

        msg = _post_chat(payload)

        if msg.get("thinking"):
            print(f"  [thinking round {round_num}]:\n", msg["thinking"][:500], "\n")

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
                "content": result,
            })

    # Exhausted all rounds: one final call with no tools so it must answer in text
    print("[WARNING] Hit max tool rounds, forcing final response")
    msg = _post_chat({"model": model, "messages": messages, "think": think, "stream": False})
    print("Answer (forced):\n", msg["content"], "\n")
    return msg["content"]


# ─── Reviewer ───────────────────────────────────────────────────────

YES_PAT = re.compile(r"^(yes|y|yeah|yep|yup)\b", re.IGNORECASE)
NO_PAT = re.compile(r"^(no|n|nah|nope)\b", re.IGNORECASE)


def review(model: str, goal: str, output: str) -> str:
    """Ask the reviewer if the goal was met. Returns the raw verdict text."""
    verdict = chat_v2(
        model,
        REVIEWER_SYSTEM,
        REVIEW_USER.format(goal=goal, output=output),
        tool_schemas=None,
        think=False,
    )
    return verdict.strip()


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
    attempts = []  # keep EVERY attempt + verdict, nothing gets overwritten
    user_msg = task  # first attempt gets the plain task

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== EXECUTING (attempt {attempt}/{max_attempts}) ===")
        answer = chat_v2(model, EXECUTOR_SYSTEM, user_msg, tool_schemas=TOOL_SCHEMAS)

        print(f"\n=== REVIEWING (attempt {attempt}) ===")
        verdict = review(model, goal, answer)
        print(f"reviewer said: {verdict!r}")

        attempts.append({"attempt": attempt, "output": answer, "verdict": verdict})

        if YES_PAT.match(verdict):
            print(f"WE DID IT on attempt {attempt}")
            write_text_file(answer, "final_output.txt")
            break

        elif NO_PAT.match(verdict):
            print(f"goal not met on attempt {attempt}, saving output and retrying")
            write_text_file(answer, f"attempt_{attempt}_failed.txt")
            # feed both the failed output AND the reviewer's reason back in
            user_msg = task + RETRY_NOTE.format(feedback=verdict, previous=answer)

        else:
            # verdict wasn't YES or NO — save everything and stop for a human
            print("reviewer verdict was not YES/NO, saving for manual review")
            write_text_file(f"VERDICT: {verdict}\n\n{answer}",
                            f"attempt_{attempt}_needs_review.txt")
            break
    else:
        print(f"[WARNING] hit max attempts ({max_attempts}) without meeting the goal")

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
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.url:
        URL = args.url

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