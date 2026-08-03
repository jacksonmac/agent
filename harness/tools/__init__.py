"""Tool registry: name → callable, plus the Ollama tool schemas.

Disk/exec tools need the run's Workspace; configure(ws) binds it via
functools.partial before the run starts. Web and MCP tools don't touch
the filesystem and are registered as-is.
"""

import json
from functools import partial

from .. import hooks, permissions, policy, runlog, ui
from ..skills import load_skill
from ..todos import set_todos
from ..workspace import Workspace
from .docs import edit_cells, read_sheet, write_sheet
from .execute import run_python, run_script, run_shell
from .files import edit_file, grep_files, list_files, read_file, write_file
from .subagent import spawn_subagent
from .web import fetch_page, web_search

tools: dict = {
    "web_search": web_search,
    "fetch_page": fetch_page,
    "set_todos": set_todos,
    "spawn_subagent": spawn_subagent,
    "load_skill": load_skill,
}

_WORKSPACE_TOOLS = {
    "write_file": write_file,
    "read_file": read_file,
    "list_files": list_files,
    "edit_file": edit_file,
    "grep_files": grep_files,
    "run_python": run_python,
    "run_script": run_script,
    "run_shell": run_shell,
    "read_sheet": read_sheet,
    "edit_cells": edit_cells,
    "write_sheet": write_sheet,
}


def configure(ws: Workspace) -> None:
    """Bind the workspace into every tool that touches disk, and stop
    advertising tools the active policy has switched off."""
    permissions.set_workspace(ws.root)  # shown on the permission card
    for name, func in _WORKSPACE_TOOLS.items():
        tools[name] = partial(func, ws)
    _apply_policy_to_schemas()


def _apply_policy_to_schemas() -> None:
    """Drop policy-disabled tools from TOOL_SCHEMAS. The tool functions
    refuse on their own too — this is so a disabled tool isn't dangled in
    front of the model, where reaching for it costs a whole tool round.

    Mutates the list in place: run.py, cli.py and review.py all hold a
    reference to this exact object. Schemas registered after import (the
    MCP gateway's) are kept as they are."""
    net = policy.current.network
    off = {name for name, on in (("web_search", net.web_search),
                                 ("fetch_page", net.fetch_page)) if not on}
    extra = [s for s in TOOL_SCHEMAS if s not in _BUILTIN_SCHEMAS]
    TOOL_SCHEMAS[:] = [s for s in _BUILTIN_SCHEMAS
                       if s["function"]["name"] not in off] + extra


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet",
            "description": "Read a worksheet from an .xlsx/.xlsm file in your workspace as text. Formula cells show as '=B2+C2 -> 260' so you can see both the formula and its value. Use this instead of read_file for spreadsheets — they are binary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workbook filename, e.g. 'sales.xlsx'."},
                    "sheet": {"type": "string", "description": "Worksheet name (default: the first sheet)."},
                    "max_rows": {"type": "integer", "description": "Rows to read (default 200)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_cells",
            "description": "Set individual cells in an existing workbook, leaving everything else untouched. Values starting with '=' are written as formulas. The edit is discarded with an explanation if saving it would destroy anything else in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workbook filename, e.g. 'sales.xlsx'."},
                    "cells": {"type": "object", "description": "A1-style references to values, e.g. {\"B2\": 42, \"D2\": \"=B2+C2\"}."},
                    "sheet": {"type": "string", "description": "Worksheet name (default: the first sheet)."},
                },
                "required": ["name", "cells"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_sheet",
            "description": "Create a new workbook, or replace one worksheet of an existing one, from a list of rows. Other sheets in an existing workbook are preserved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workbook filename, e.g. 'report.xlsx'."},
                    "rows": {"type": "array", "items": {"type": "array"}, "description": "Rows, each a list of cell values."},
                    "sheet": {"type": "string", "description": "Worksheet name (default: the first/active sheet)."},
                },
                "required": ["name", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a NEW file in your workspace (or fully rewrite an existing one). For small changes to an existing file, prefer edit_file.",
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
            "name": "read_file",
            "description": "Read a file from your workspace. Returns up to max_chars from the given offset; the header tells you if there is more to read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filename to read, e.g. 'app.py'."},
                    "offset": {"type": "integer", "description": "Character offset to start from (default 0)."},
                    "max_chars": {"type": "integer", "description": "How many characters to return (default 6000)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List every file in your workspace (recursively) with sizes. Call this FIRST to see what already exists before creating or modifying anything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Subdirectory to list (default: the whole workspace)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact text snippet in an existing workspace file. Prefer this over write_file for small changes — write_file overwrites the whole file. old_text must appear exactly once; include surrounding lines to make it unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "File to edit."},
                    "old_text": {"type": "string", "description": "Exact existing text to replace (must be unique in the file)."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["name", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "Search every workspace file for a regex or plain-text pattern. Returns file:line matches. Use this to find where something is defined or used instead of reading whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex (or plain text) to search for."},
                    "path": {"type": "string", "description": "Subdirectory to search (default: the whole workspace)."},
                    "max_results": {"type": "integer", "description": "Stop after this many matches (default 50)."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Run a python file saved in your workspace, optionally with command-line arguments. Use after write_file to test a script.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Python file to run, e.g. 'app.py'."},
                    "args": {"type": "array", "items": {"type": "string"},
                             "description": "Command-line arguments (optional)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute python code in a subprocess (cwd = your workspace). Returns stdout, stderr and the exit code. Use this to test code you have written.",
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
            "description": "Run a single shell command in your workspace. Only these commands are allowed: pip, pip3, python3, pytest, ls, mkdir, cat, echo. No pipes, chaining, or redirection. Use this to install packages (e.g. 'pip install flask') or run tests (e.g. 'pytest -q').",
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
            "name": "set_todos",
            "description": "Declare or update your task checklist. Send the FULL list every time (it replaces the previous one). Keep it to 3-8 items; mark exactly one item in_progress at a time and update statuses as you complete work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "The step, a short imperative phrase."},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Delegate a well-scoped subtask (exploration, research, a contained build step) to a focused subagent with its own fresh context. You receive ONLY its final summary, keeping your own context small. Give it complete, self-contained instructions — it cannot see this conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Complete, self-contained instructions for the subagent."},
                    "kind": {"type": "string", "description": "Optional label, e.g. 'research' or 'general'."},
                },
                "required": ["task"],
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
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full instructions for a skill listed under SKILLS in your system prompt. The one-line description there is only a teaser — call this BEFORE starting work the skill covers, then follow the loaded instructions. Read-only and cheap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name exactly as it appears in the SKILLS list."},
                },
                "required": ["name"],
            },
        },
    },
]

# the schemas this module ships, before any policy filtering or MCP additions
_BUILTIN_SCHEMAS = list(TOOL_SCHEMAS)


def execute_tool_call(name: str, arguments) -> str:
    func = tools.get(name)
    if not func:
        return f"[ERROR] Unknown tool: {name}"
    if isinstance(arguments, str):  # some models send arguments as a JSON string
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"[ERROR] Could not parse arguments for {name}: {e}"
    denial = permissions.check(name, arguments)
    if denial:
        runlog.log_event("tool", name=name, args=json.dumps(arguments)[:500],
                         ok=False, result_chars=len(denial))
        return denial
    file_arg = (arguments.get("name") or arguments.get("file") or "") \
        if isinstance(arguments, dict) else ""
    hooks.fire("pre_tool", tool=name, file=file_arg)
    try:
        result = str(func(**arguments))
    except ui.QuitRequested:
        # [q] pressed while a subagent (or any tool) polled controls — a
        # user abort, not a tool failure; it must reach run.py's handler
        raise
    except TypeError as e:
        result = f"[ERROR] Bad arguments for {name}: {e}"
    except Exception as e:
        result = f"[ERROR] {name} raised {type(e).__name__}: {e}"
    hooks.fire("post_tool", tool=name, file=file_arg)
    runlog.log_event("tool", name=name, args=json.dumps(arguments)[:500],
                     ok=not result.startswith("[ERROR]"),
                     result_chars=len(result))
    return result
