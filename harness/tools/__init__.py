"""Tool registry: name → callable, plus the Ollama tool schemas.

Disk/exec tools need the run's Workspace; configure(ws) binds it via
functools.partial before the run starts. Web and MCP tools don't touch
the filesystem and are registered as-is.
"""

import json
from functools import partial

from .. import runlog
from ..workspace import Workspace
from .execute import run_python, run_script, run_shell
from .files import edit_file, list_files, read_file, write_file
from .web import fetch_page, web_search

tools: dict = {
    "web_search": web_search,
    "fetch_page": fetch_page,
}

_WORKSPACE_TOOLS = {
    "write_file": write_file,
    "read_file": read_file,
    "list_files": list_files,
    "edit_file": edit_file,
    "run_python": run_python,
    "run_script": run_script,
    "run_shell": run_shell,
}


def configure(ws: Workspace) -> None:
    """Bind the workspace into every tool that touches disk."""
    for name, func in _WORKSPACE_TOOLS.items():
        tools[name] = partial(func, ws)


TOOL_SCHEMAS = [
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
    runlog.log_event("tool", name=name, args=json.dumps(arguments)[:500],
                     ok=not result.startswith("[ERROR]"),
                     result_chars=len(result))
    return result
