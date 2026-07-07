"""CLI entry point: argument parsing and wiring."""

import argparse
import os

import requests

from . import run as run_mod
from . import runlog
from . import tools as tools_mod
from . import ui
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
    p.add_argument("-rm", "--reviewer-model", default=None,
                   help="separate (e.g. larger) model for reviewing (default: same as --model)")
    p.add_argument("-em", "--executor-model", default=None,
                   help="separate (e.g. coding) model for the executor phase, including "
                        "its plan and self-check turns (default: same as --model)")
    p.add_argument("-gm", "--goalsmith-model", default=None,
                   help="separate model for -sg goal rewriting (default: same as --model)")
    p.add_argument("--no-reviewer-tools", action="store_true",
                   help="don't let the reviewer inspect the workspace with tools "
                        "(faster, but it judges only the inlined snapshot)")
    p.add_argument("--attempts", type=int, default=5, help="max execute/review attempts (default: 5)")
    p.add_argument("--best-of", type=int, default=1, metavar="N",
                   help="run N independent first attempts, review each, and continue "
                        "from the best one (default: 1 = off; N multiplies wall time)")
    p.add_argument("--no-plan", action="store_true",
                   help="skip the no-tool planning turn at the start of attempt 1")
    p.add_argument("--no-self-check", action="store_true",
                   help="skip the verify-and-fix turn that runs before each review")
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


def _load_agent_md(ws_root: str) -> str:
    """Project context from an AGENT.md in the workspace, capped and formatted
    for appending to the executor system prompt. Empty string if absent."""
    path = os.path.join(ws_root, "AGENT.md")
    if not os.path.isfile(path):
        return ""
    with open(path) as f:
        context = f.read()[:4_000]
    return ("\n\nPROJECT CONTEXT (from AGENT.md in the workspace — follow these "
            "instructions):\n" + context)


def main():
    args = parse_args()
    if args.url:
        settings.url = args.url
    settings.model = args.model
    settings.reviewer_model = args.reviewer_model
    settings.executor_model = args.executor_model
    settings.goalsmith_model = args.goalsmith_model
    settings.reviewer_tools = not args.no_reviewer_tools
    settings.num_ctx = args.num_ctx
    settings.full_context = args.full_context
    settings.plan_first = not args.no_plan
    settings.self_check = not args.no_self_check
    if args.best_of < 1:
        raise SystemExit("[ERROR] --best-of must be >= 1")
    if settings.full_context:
        print(f"[full-context mode] no trimming; num_ctx={settings.num_ctx}")

    ws = Workspace.create(os.path.join(HERE, "runs"), workspace_dir=args.workspace)
    log = RunLog(ws.run_dir)
    runlog.current = log
    tools_mod.configure(ws)
    print(f"[run] {ws.run_dir}")

    executor_system = EXECUTOR_SYSTEM
    agent_md = _load_agent_md(ws.root)
    if agent_md:
        executor_system += agent_md
        print("[context] loaded AGENT.md from the workspace")
        log.event("agent_md", chars=len(agent_md))
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
            goal, task, criteria = make_goal_task(
                settings.goalsmith_model or args.model, args.smart_goal)
        else:
            goal, task, criteria = args.goal, args.goal, []
        ui.start(goal, settings.executor_model or args.model, settings.reviewer_model,
                 max_attempts=args.attempts, num_ctx=settings.num_ctx)
        run_mod.main(args.model, goal, task, ws, log, max_attempts=args.attempts,
                     executor_system=executor_system, criteria=criteria,
                     best_of=args.best_of)
    except requests.ConnectionError:
        print(f"[ERROR] could not reach the ollama server at {settings.url} — "
              f"is it running? (override with --url)")
    except requests.Timeout:
        print(f"[ERROR] the model took longer than {settings.request_timeout}s to respond")
    finally:
        ui.stop()  # idempotent; restores the terminal even on errors
        if mcp_mod.mcp_gateway is not None:
            mcp_mod.mcp_gateway.close()
