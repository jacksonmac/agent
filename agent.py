#TODO REVIEW WHAT WE ARE USING ALL THE LIBRARIES FOR THIS PROJECT
import requests
import re
import functools
import time
import os
import math
import json #FIX: this was missing, chat_v2 uses json.dumps
import subprocess
from typing import Optional




#this should be taken out
#TODO CHANGE THIS
HERE = os.path.dirname(__file__) #going to do this a diffent way later

URL = "http://192.168.1.134:11434"

# ─── Prompts ────────────────────────────────────────────────────────

#NEW: mentions run_shell so the model knows it can install packages
EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output. Use the tools available: write files with write_file, test code with
run_python, install packages or inspect the project with run_shell (e.g. 'pip install flask').
If a test fails, fix the code and test again before finishing."""

#NEW: reviewer is back, but as a proper system prompt this time.
#the old code did chat(model, message, exe1) which passed the review QUESTION
#as the SYSTEM message and the executor output as the USER message — backwards.
#now: system = standing reviewer instructions, user = the actual goal + output.
REVIEWER_SYSTEM = """You are a strict reviewer. You will be given a GOAL and the OUTPUT
of an agent that tried to achieve it. Decide if the output actually meets the goal.

Respond with exactly one word: YES or NO. No punctuation, no explanation."""

REVIEW_USER = """GOAL:
{goal}

AGENT OUTPUT:
{output}

Did the output meet the goal? Answer YES or NO."""

#NEW: on a failed attempt, the next run gets the previous output stapled to the
#task so the model fixes instead of starting blind from scratch.
RETRY_NOTE = """

A previous attempt did NOT meet the goal according to the reviewer.
Here is that previous attempt — fix what is missing or broken and finish the goal:

--- PREVIOUS ATTEMPT ---
{previous}
--- END PREVIOUS ATTEMPT ---"""

total_time = {}
counter_runs = 0 #TODO, THIS IS NEEDED FOR TIMED, BUT IS A SUPER LAZEY WAY TO DO IT AND REALLY SHOULDNT BE DOING IT THIS WAY

# ─── Time function ──────────────────────────────────────────────────

#TODO
#fuctnion to get current, time and run the code, and find out how much time has gone by
#STUDY THIS CODE
def timed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs) #function caller
        elapsed = time.perf_counter() - start
        global counter_runs #TODO, SUPER LAZY WAY OF DOING THIS, WILL DEAL WITH LATER
        global total_time
        print(f"[{func.__name__}] took {elapsed:.2f}s")
        if elapsed > 60:
            min = int(math.ceil(elapsed/60))
            print(f"The amount of minutes it took {min}mins")
        #if total_time[func.__name__] == None: this is key error, i need coffee
        #if total_time[func.__name__] not in total_time:
        if func.__name__ not in total_time:
            total_time[func.__name__] = elapsed
            counter_runs += 1
        else:
            time_name = str(func.__name__) + str(counter_runs)
            total_time[time_name] = elapsed
            counter_runs += 1
        return result
    return wrapper

def run_python_old(code: str) -> str:
    proc = subprocess.run(
        ["python3", "-c", code],
        capture_output=True, text=True, timeout=30, #TIMEOUT COULD BE to small
    )
    #return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit: {proc.returncode}
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"

def run_python(code: str) -> str:
    #FIX: wrapped in try/except — TimeoutExpired RAISES instead of returning,
    #so one hung script would kill the whole agent loop
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=120, #TIMEOUT COULD BE to small
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] code timed out after 30 seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


#NEW: run_shell tool
#commands the model is allowed to run, first word of the command is checked
#TODO ADD MORE AS NEEDED, keep this small on purpose
ALLOWED_COMMANDS = ["pip", "pip3", "python3", "ls", "mkdir", "cat", "echo"]

def run_shell(command: str) -> str:
    """run a shell command in a subprocess, restricted to ALLOWED_COMMANDS"""
    #first word of the command is the program, check it against the allowlist
    #so the model cant run rm -rf or curl something sketchy
    first_word = command.strip().split()[0] if command.strip() else ""
    if first_word not in ALLOWED_COMMANDS:
        #dont raise — return the error as a string so the model sees it
        #and can pick a different command (same pattern as execute_tool_call)
        return (f"[ERROR] command '{first_word}' is not allowed. "
                f"Allowed commands: {', '.join(ALLOWED_COMMANDS)}")

    try:
        print("RUN_SHELL", command)
        proc = subprocess.run(
            command,
            shell=True, #needed so "pip install flask" works as one string
            capture_output=True, text=True, timeout=360, #pip installs can be slow, longer than run_python
            cwd=HERE, #run in the project dir so ls/mkdir land in the right place
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out after 360 seconds"
    return f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nexit code: {proc.returncode}"


def write_text_file(text: str, name: str):
    path = os.path.join(HERE, name)
    with open(path, "w") as f:
        f.write(text)
    print("WROTE:", path)
    #FIX: tools have to RETURN a string, that string becomes the tool result
    #message the model sees. before this returned None
    return f"WROTE {len(text)} chars to {name}"


# ─── CORE CHAT WITH TOOL LOOP ───────────────────────────────────────

def execute_tool_call(name: str, arguments: dict) -> str:
    """Look up a tool by name and execute it with the given arguments."""
    func = tools.get(name)
    if not func:
        return f"[ERROR] Unknown tool: {name}"
    #FIX: some models send arguments as a json STRING not a dict
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"[ERROR] Could not parse arguments for {name}: {e}"
    try:
        return str(func(**arguments))
    except TypeError as e:
        return f"[ERROR] Bad arguments for {name}: {e}"
    #FIX: catch everything else too — a tool crashing should feed the error
    #back to the model so it can self correct, not kill the loop
    except Exception as e:
        return f"[ERROR] {name} raised {type(e).__name__}: {e}"


def chat_v2(model: str, system: str, user: str, tool_schemas: Optional[list],
         think: bool = True, max_tool_rounds: int = 15) -> str:
    """one system + one user turn, with an optional tool-calling loop

    If tools are given, the model can call tools. each time it does we
    execute them and feed results back until the model gives a final
    text response or hits max_tool_rounds.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    for round_num in range(max_tool_rounds):
        payload = {
            "model": model,
            "messages": messages,
            "think": think,
            "stream": False,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas

        resp = requests.post(URL + "/api/chat", json=payload)
        resp.raise_for_status()
        msg = resp.json()["message"]

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
                "tool_name": tool_name, #FIX: ollama needs this to match the result to the call
                "content": result,
            })

    # If we exhaust all rounds, force a final text response
    print("[WARNING] Hit max tool rounds, forcing final response")
    payload = {
        "model": model,
        "messages": messages,
        "think": think,
        "stream": False,
        # no "tools" key at all -> model cant request more calls
    }
    resp = requests.post(URL + "/api/chat", json=payload)
    resp.raise_for_status()
    msg = resp.json()["message"]
    print("Answer (forced):\n", msg["content"], "\n")
    return msg["content"]


# ─── REVIEWER ───────────────────────────────────────────────────────

#NEW: tolerant verdict parsing. the old regex was ^(yes|...)$ which fails on
#"YES." or "Yes " or a trailing newline — that was the "else: pass" hole in
#the old diagram. this matches yes/no at the START of the (stripped) reply
#so "NO, because..." still counts as a NO.
YES_PAT = re.compile(r"^(yes|y|yeah|yep|yup)\b", re.IGNORECASE)
NO_PAT = re.compile(r"^(no|n|nah|nope)\b", re.IGNORECASE)


def review(model: str, goal: str, output: str) -> str:
    """Ask the reviewer if the goal was met. Returns the raw verdict text."""
    #NEW: reviewer gets NO tools and no thinking — its one job is YES/NO.
    #reuses chat_v2 with tool_schemas=None so its just a plain single call.
    verdict = chat_v2(
        model,
        REVIEWER_SYSTEM,
        REVIEW_USER.format(goal=goal, output=output),
        tool_schemas=None,
        think=False,
    )
    return verdict.strip()


# ─── TOOL REGISTRY ──────────────────────────────────────────────────
tools = {
    "write_file": write_text_file,
    "run_python": run_python,
    "run_shell": run_shell, #NEW
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
                    "text": {
                        "type": "string",
                        "description": "The full text content of the file.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Filename to write, e.g. 'app.py'.",
                    },
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
                    "code": {
                        "type": "string",
                        "description": "The python code to execute.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    #NEW: run_shell schema — property name "command" matches the function param EXACTLY
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in the project directory. Only these commands are allowed: pip, pip3, python3, ls, mkdir, cat, echo. Use this to install packages (e.g. 'pip install flask') or inspect the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The full shell command to run, e.g. 'pip install flask'.",
                    },
                },
                "required": ["command"],
            },
        },
    },
]


# ─── MAIN LOOP: execute -> review -> retry ──────────────────────────

def main():
    #model = "qwen3.6:27b"
    #model = "qwen3.5:9b"
    #model = "gemma4:12b"
    model = "gemma4:26b"

    #TODO these should be dynamic / user input later
    goal = "A script fizzbuzz.py that prints FizzBuzz for 1-30, saved to disk, run, and confirmed correct."
    task = ("Write a script fizzbuzz.py that prints FizzBuzz for 1-30, save it with "
            "write_file, run it with run_python, and confirm the output is correct.")
    
    goal = "write me a 5,000 word story about a koala. txt as a the fileout with just the story and no spelling errors"
    task = "make a kids story about a koala, and make it apply to their life"
    
    #goal = "A complete FastAPI blog application with user registration/login, JWT authentication, SQLite database, and full blog post CRUD functionality."

    #task = ("Write FastAPI application files (main.py, models.py, schemas.py, auth.py) with SQLite database, "
           # "implement user signup/login endpoints with password hashing and JWT tokens, create blog post endpoints for creating/reading/updating/deleting posts, "
           # "save all files with write_file, run the server with run_python, and test the entire workflow: register a user, login, create a blog post, retrieve it, and update it.")

    max_attempts = 5 #outer loop cap — each attempt is a full chat_v2 tool loop inside (THIS WAS 5)
    attempts = [] #keep EVERY attempt + verdict, nothing gets overwritten

    user_msg = task #first attempt gets the plain task

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== EXECUTING (attempt {attempt}/{max_attempts}) ===")
        answer = chat_v2(model, EXECUTOR_SYSTEM, user_msg, tool_schemas=TOOL_SCHEMAS)

        print(f"\n=== REVIEWING (attempt {attempt}) ===")
        verdict = review(model, goal, answer)
        print(f"reviewer said: {verdict!r}")

        attempts.append({"attempt": attempt, "output": answer, "verdict": verdict})

        if YES_PAT.match(verdict):
            #goal met -> write the final files and stop
            print(f"WE DID IT on attempt {attempt}")
            write_text_file(answer, "final_output.txt")
            write_text_file(json.dumps(attempts, indent=2), "attempt_history.json")
            return

        elif NO_PAT.match(verdict):
            #goal NOT met -> save this attempts output, then loop again with
            #the failed attempt fed back in so the model fixes it
            print(f"goal not met on attempt {attempt}, saving output and retrying")
            write_text_file(answer, f"attempt_{attempt}_failed.txt")
            user_msg = task + RETRY_NOTE.format(previous=answer)

        else:
            #reviewer said something that isnt yes or no. the old code fell
            #through silently (else: pass). now: save everything and stop so
            #a human can look at it.
            print(f"reviewer verdict was not YES/NO, saving for manual review")
            write_text_file(
                f"VERDICT: {verdict}\n\n{answer}",
                f"attempt_{attempt}_needs_review.txt",
            )
            write_text_file(json.dumps(attempts, indent=2), "attempt_history.json")
            return

    #ran out of attempts without a YES
    print(f"[WARNING] hit max attempts ({max_attempts}) without meeting the goal")
    write_text_file(json.dumps(attempts, indent=2), "attempt_history.json")


def make_goal_task(model, prompt):
    messages = [
        {
            "role": "user",
            "content": f"You need to write a goal and a task to achieve this: {prompt}. "
                       f"Your output should be GOAL: X TASK: Y",
        }
    ]
    payload = {
        "model": model,
        "messages": messages,
        "think": True,
        "stream": False,
    }
    resp = requests.post(URL + "/api/chat", json=payload)
    resp.raise_for_status()
    message = resp.json()
    goal_regex = re.compile()
    
    #print(resp.json())

if __name__ == "__main__":
    #main()
    model = "gemma4:26b"
    probt = "I NEED YOU TO MAKE A FOLDER called output"
    make_goal_task(model, probt)
