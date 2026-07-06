"""CLI entry point: argument parsing and wiring."""

import argparse
import os

import requests

from . import run as run_mod
from . import runlog
from . import tools as tools_mod
from .config import HERE, settings
from .goalsmith import make_goal_task
from .prompts import EXECUTOR_SYSTEM
from .runlog import RunLog
from .tools import TOOL_SCHEMAS, tools
from .tools import mcp as mcp_mod
from .workspace import Workspace


def parse_args():
    p = argparse.ArgumentParser(description="execute -> review -> retry agent harness")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-g", "--goal", help="use this text as the goal (and the task)")
    g.add_argument("-sg", "--smart-goal",
                   help="LM rewrites your input into a proper GOAL + TASK, then runs")
    p.add_argument("--model", default=settings.model,
                   help=f"ollama model (default: {settings.model})")
    p.add_argument("--reviewer-model", default=None,
                   help="separate (e.g. larger) model for reviewing (default: same as --model)")
    p.add_argument("--no-reviewer-tools", action="store_true",
                   help="don't let the reviewer inspect the workspace with tools "
                        "(faster, but it judges only the inlined snapshot)")
    p.add_argument("--attempts", type=int, default=5, help="max execute/review attempts (default: 5)")
    p.add_argument("--url", default=None, help=f"ollama server URL (default: {settings.url})")
    p.add_argument("--full-context", action="store_true",
                   help="send everything: no compaction or truncation of tool results, "
                        "retries, or thinking (pair with a big --num-ctx)")
    p.add_argument("--num-ctx", type=int, default=settings.num_ctx,
                   help=f"context window to request from ollama (default: {settings.num_ctx}; "
                        f"more = more RAM/VRAM)")
    p.add_argument("--mcp", action="store_true",
                   help="connect to the Docker MCP Toolkit gateway "
                        "('docker mcp gateway run') and expose its tools to the agent")
    p.add_argument("--mcp-profile", default=None,
                   help="MCP Toolkit profile to use (passed as --profile to the gateway)")
    p.add_argument("--workspace", default=None,
                   help="reuse an existing workspace directory instead of a fresh one "
                        "(e.g. runs/latest/workspace to continue earlier work)")
    return p.parse_args()


def main():
    args = parse_args()
    if args.url:
        settings.url = args.url
    settings.model = args.model
    settings.reviewer_model = args.reviewer_model
    settings.reviewer_tools = not args.no_reviewer_tools
    settings.num_ctx = args.num_ctx
    settings.full_context = args.full_context
    if settings.full_context:
        print(f"[full-context mode] no trimming; num_ctx={settings.num_ctx}")

    ws = Workspace.create(os.path.join(HERE, "runs"), workspace_dir=args.workspace)
    log = RunLog(ws.run_dir)
    runlog.current = log
    tools_mod.configure(ws)
    print(f"[run] {ws.run_dir}")

    executor_system = EXECUTOR_SYSTEM
    if args.mcp:
        try:
            mcp_tool_names = mcp_mod.setup_mcp_tools(tools, TOOL_SCHEMAS,
                                                     profile=args.mcp_profile)
        except (RuntimeError, TimeoutError) as e:
            raise SystemExit(f"[ERROR] Docker MCP gateway: {e}")
        if mcp_tool_names:
            # tell the executor these extra tools exist so it reaches for them
            executor_system += (
                "\n\nAdditional tools are available via the Docker MCP Toolkit: "
                + ", ".join(mcp_tool_names)
                + ". Use them when they fit the task better than the built-in tools."
            )

    try:
        if args.smart_goal:
            goal, task, criteria = make_goal_task(args.model, args.smart_goal)
        else:
            goal, task, criteria = args.goal, args.goal, []
        run_mod.main(args.model, goal, task, ws, log, max_attempts=args.attempts,
                     executor_system=executor_system, criteria=criteria)
    except requests.ConnectionError:
        print(f"[ERROR] could not reach the ollama server at {settings.url} — "
              f"is it running? (override with --url)")
    except requests.Timeout:
        print(f"[ERROR] the model took longer than {settings.request_timeout}s to respond")
    finally:
        if mcp_mod.mcp_gateway is not None:
            mcp_mod.mcp_gateway.close()
