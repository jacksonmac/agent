"""CLI entry point: argument parsing and wiring."""

import argparse
import os
import sys

import requests

from . import hooks as hooks_mod
from . import llm as llm_mod
from . import permissions
from . import run as run_mod
from . import runlog
from . import skills as skills_mod
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
    g = p.add_mutually_exclusive_group()  # not required: --resume can supply the goal
    g.add_argument("-g", "--goal", help="use this text as the goal (and the task)")
    g.add_argument("-sg", "--smart-goal",
                   help="LM rewrites your input into a proper GOAL + TASK, then runs")
    g.add_argument("-c", "--command", nargs="+", metavar="NAME",
                   help="run a saved command from commands/<NAME>.md; extra words "
                        "are substituted for {args} in its body")
    p.add_argument("-r", "--resume", nargs="?", const="latest", default=None,
                   metavar="ID|DIR",
                   help="continue a previous session: reuse its workspace (and, if "
                        "no goal is given, its goal). Bare --resume = the latest "
                        "run; an id from `agent.py history`; or a run/workspace "
                        "directory path")
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
    p.add_argument("--no-memory", action="store_true",
                   help="don't write a lessons note to the workspace AGENT.md at run end")
    p.add_argument("--no-skills", action="store_true",
                   help="don't advertise skills/ or the load_skill tool to the model")
    p.add_argument("--yolo", action="store_true",
                   help="skip permission prompts for code-executing tools "
                        "(run_shell/run_python/run_script)")
    p.add_argument("--no-stream", action="store_true",
                   help="wait for complete responses instead of streaming tokens live")
    p.add_argument("--no-notify", action="store_true",
                   help="skip the terminal bell / desktop notification at run end")
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
    return p.parse_args(), p


# frontmatter key → argparse dest, for command-file defaults
_FRONTMATTER_DESTS = {"em": "executor_model", "rm": "reviewer_model",
                      "gm": "goalsmith_model", "model": "model",
                      "attempts": "attempts", "best_of": "best_of",
                      "url": "url", "num_ctx": "num_ctx"}


def _apply_command(args, parser) -> None:
    """-c NAME [extra args]: load commands/NAME.md, set args.goal from its body,
    and apply frontmatter defaults for flags the user didn't pass explicitly."""
    from . import commands
    cmd = commands.load_command(args.command[0])
    args.goal = commands.render_goal(cmd, args.command[1:])
    for key, val in cmd.defaults.items():
        dest = _FRONTMATTER_DESTS.get(key, key)
        if not hasattr(args, dest):
            print(f"[WARNING] command '{cmd.name}': unknown default '{key}' ignored")
            continue
        default = parser.get_default(dest)
        if getattr(args, dest) == default:  # explicit CLI flags win
            setattr(args, dest, type(default)(val) if default is not None else val)
    desc = f": {cmd.description}" if cmd.description else ""
    print(f"[command] {cmd.name}{desc}")


def _load_agent_md(ws_root: str) -> str:
    """Project context from an AGENT.md in the workspace, capped and formatted
    for appending to the executor system prompt. Empty string if absent."""
    path = os.path.join(ws_root, "AGENT.md")
    if not os.path.isfile(path):
        return ""
    with open(path) as f:
        context = f.read()[:8_000]  # room for user preamble + memory sections
    return ("\n\nPROJECT CONTEXT (from AGENT.md in the workspace — follow these "
            "instructions):\n" + context)


def _previous_goal(workspace_dir: str) -> str:
    """The run_start goal from the events.jsonl next to a reused workspace.
    Empty string when there is none (fresh dir, missing/corrupt log)."""
    import json as _json
    prev_run_dir = os.path.dirname(os.path.abspath(workspace_dir))
    try:
        with open(os.path.join(prev_run_dir, "events.jsonl")) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if e.get("event") == "run_start":
                    return e.get("goal", "")
    except OSError:
        pass
    return ""


def _resolve_resume(value: str) -> str:
    """--resume → the workspace directory to reuse. 'latest' and numeric ids
    resolve through runs/history.db; anything else is a path — either a run
    directory (its workspace/ is used) or a workspace directory itself."""
    from . import history
    if value == "latest":
        run_dir = history.latest_run_dir(history.db_path())
        if not run_dir:
            raise SystemExit("[ERROR] --resume: no previous runs recorded — "
                             "see `agent.py history`")
    elif value.isdigit():
        run_dir = history.run_dir_for(history.db_path(), int(value))
        if not run_dir:
            raise SystemExit(f"[ERROR] --resume: no run with id {value} — "
                             f"see `agent.py history`")
    else:
        run_dir = value
    ws_dir = os.path.join(run_dir, "workspace")
    if not os.path.isdir(ws_dir):
        ws_dir = run_dir  # the path already points at a workspace
    if not os.path.isdir(ws_dir):
        raise SystemExit(f"[ERROR] --resume: no workspace directory at "
                         f"{run_dir}")
    return ws_dir


def _load_resume_context(workspace_dir: str, files: list[str],
                         max_chars: int = 3_000) -> str:
    """If the reused workspace sits inside a previous run dir (sibling
    events.jsonl / attempt_history.json / final_output*.txt), build a
    mechanical summary of that run — no LLM call. Empty string otherwise."""
    import json as _json

    from .llm import truncate_middle
    prev_run_dir = os.path.dirname(os.path.abspath(workspace_dir))
    artifacts = ["events.jsonl", "attempt_history.json",
                 "final_output.txt", "final_output_UNVERIFIED.txt"]
    if not any(os.path.isfile(os.path.join(prev_run_dir, a)) for a in artifacts):
        return ""

    prev_goal = _previous_goal(workspace_dir)
    outcome = feedback = ""
    try:
        with open(os.path.join(prev_run_dir, "attempt_history.json")) as f:
            history = _json.load(f)
        if history:
            last = history[-1]
            n = len(history)
            outcome = (f"PASSED on attempt {n}" if last.get("passed")
                       else f"FAILED after {n} attempt(s) (unverified output saved)")
            if not last.get("passed"):
                feedback = last.get("verdict", "")
    except (OSError, _json.JSONDecodeError, KeyError):
        pass

    parts = ["PREVIOUS SESSION IN THIS WORKSPACE (mechanical summary — verify "
             "with your tools):"]
    if prev_goal:
        parts.append(f"- previous goal: {prev_goal}")
    if outcome:
        parts.append(f"- outcome: {outcome}")
    if feedback:
        parts.append(f"- last reviewer feedback: {' '.join(feedback.split())}")
    if files:
        shown = files[:30]
        more = f" ({len(shown)} shown of {len(files)})" if len(files) > 30 else ""
        parts.append(f"- files present: {', '.join(shown)}{more}")
    parts.append("Build on this work; do not blindly redo it.")
    return truncate_middle("\n".join(parts), max_chars)


def _history_command(argv: list[str]) -> None:
    """`agent.py history [--stats] [--limit N]` — dispatched before the main
    parser, whose -g/-sg group is required."""
    from . import history
    hp = argparse.ArgumentParser(prog="agent.py history",
                                 description="show past runs from runs/history.db")
    hp.add_argument("--stats", action="store_true",
                    help="pass-rate per executor model instead of a run listing")
    hp.add_argument("--limit", type=int, default=20, help="rows to show (default: 20)")
    hargs = hp.parse_args(argv)
    if hargs.stats:
        history.print_stats(history.db_path())
    else:
        history.print_history(history.db_path(), limit=hargs.limit)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "history":
        _history_command(sys.argv[2:])
        return
    args, parser = parse_args()
    if not (args.goal or args.smart_goal or args.command or args.resume):
        parser.error("one of -g/--goal, -sg/--smart-goal, -c/--command or "
                     "-r/--resume is required")
    if args.command:
        _apply_command(args, parser)
    if args.resume:
        if args.workspace:
            raise SystemExit("[ERROR] --resume already picks the workspace — "
                             "drop --workspace (or use it alone)")
        args.workspace = _resolve_resume(args.resume)
        if not (args.goal or args.smart_goal):
            prev = _previous_goal(args.workspace)
            if not prev:
                raise SystemExit("[ERROR] --resume: couldn't recover the "
                                 "previous goal from that run — pass -g/-sg "
                                 "alongside --resume")
            args.goal = ("Continue the previous session in this workspace. "
                         "Its goal was: " + prev)
        print(f"[resume] {os.path.dirname(args.workspace)}")
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
    settings.memory = not args.no_memory
    settings.skills = not args.no_skills
    permissions.configure(yolo=args.yolo)
    settings.stream = not args.no_stream
    settings.notify = not args.no_notify
    if args.best_of < 1:
        raise SystemExit("[ERROR] --best-of must be >= 1")
    if settings.full_context:
        print(f"[full-context mode] no trimming; num_ctx={settings.num_ctx}")

    ws = Workspace.create(os.path.join(HERE, "runs"), workspace_dir=args.workspace)
    log = RunLog(ws.run_dir)
    runlog.current = log
    tools_mod.configure(ws)
    hooks_mod.configure(os.path.join(HERE, "hooks.json"),
                        run_dir=ws.run_dir, workspace=ws.root)
    print(f"[run] {ws.run_dir}")

    executor_system = EXECUTOR_SYSTEM
    agent_md = _load_agent_md(ws.root)
    if agent_md:
        executor_system += agent_md
        print("[context] loaded AGENT.md from the workspace")
        log.event("agent_md", chars=len(agent_md))
    if args.workspace:
        resume = _load_resume_context(args.workspace, ws.list_all_files())
        if resume:
            executor_system += "\n\n" + resume
            print("[context] resuming: found previous run artifacts next to the workspace")
            log.event("resume_context", chars=len(resume))
    skills_block = skills_mod.system_prompt_block()
    if skills_block:
        executor_system += skills_block
        n_skills = skills_block.count("\n- ")
        print(f"[context] {n_skills} skill(s) available via load_skill")
        log.event("skills", count=n_skills, chars=len(skills_block))
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
        run_passed = run_mod.main(args.model, goal, task, ws, log,
                                  max_attempts=args.attempts,
                                  executor_system=executor_system,
                                  criteria=criteria, best_of=args.best_of)
        if settings.notify:
            ui.notify("agent run " + ("passed" if run_passed else "failed"),
                      " ".join(goal.split())[:120], success=run_passed)
    except (requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as e:
        if llm_mod.had_successful_call:
            # the server answered earlier this run, then dropped the
            # connection mid-call and retries were exhausted — that's a
            # server-side crash/restart, not "not running"
            print(f"[ERROR] the ollama server at {settings.url} dropped the "
                  f"connection mid-run ({type(e).__name__}: {e}). Its runner "
                  f"likely crashed under load — check the server logs (OOM?), "
                  f"lower --num-ctx, or use a smaller model.")
        else:
            print(f"[ERROR] could not reach the ollama server at {settings.url} — "
                  f"is it running? (override with --url) ({e})")
    except requests.Timeout:
        print(f"[ERROR] the model took longer than {settings.request_timeout}s to respond")
    finally:
        ui.stop()  # idempotent; restores the terminal even on errors
        if mcp_mod.mcp_gateway is not None:
            mcp_mod.mcp_gateway.close()
