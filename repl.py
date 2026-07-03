"""Interactive prompt for agent.py.

Reached by running `python agent.py` with no goal (or with `--repl`). Type a
goal and it runs the normal execute→review→retry loop via agent.main(); the
loop code itself is untouched — the REPL just calls it once per goal. Slash
commands tweak settings between runs.

All output flows through the same ui layer as one-shot mode, so the two look
identical.
"""

import os
import shlex
import sys

import agent
from ui import ui

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    _PTK_AVAILABLE = True
except ImportError:  # prompt_toolkit is optional — fall back to input()
    _PTK_AVAILABLE = False

HISTORY_DIR = os.path.expanduser("~/.agent")
HISTORY_FILE = os.path.join(HISTORY_DIR, "history")

HELP = """\
Commands:
  <text>              run <text> as a goal (@path attaches a file inline)
  /attach [<path>...] stage files for the next goal; no args lists, 'clear' resets
  /smart              toggle smart-goal rewriting (model writes GOAL/TASK/criteria)
  /model <name>       set the executor model
  /reviewer <name>    set the reviewer model
  /attempts <n>       set max attempts per goal
  /set <key> <value>  set temperature | num-ctx | seed | max-tool-rounds | stream | think
  /config             show current settings
  /runs               list past run directories
  /last               show the most recent run directory
  /clear              clear the screen
  /help               show this help
  /exit               quit (also Ctrl-D)\
"""


class _Settings:
    """Mutable per-session settings. Numeric/loop knobs live here; the rest are
    mirrored straight onto agent's module globals so the loop picks them up."""

    def __init__(self, args, reviewer_model):
        self.model = args.model
        self.reviewer_model = reviewer_model
        self.attempts = args.attempts
        self.max_tool_rounds = args.max_tool_rounds
        self.smart = bool(args.smart_goal)
        self.staged = []  # source paths staged by /attach; copied at run start

    def show(self):
        ui.info("Settings:")
        ui.info(f"  server:         {agent.URL}")
        ui.info(f"  model:          {self.model}")
        ui.info(f"  reviewer:       {self.reviewer_model}")
        ui.info(f"  attempts:       {self.attempts}")
        ui.info(f"  max_tool_rounds:{self.max_tool_rounds}")
        ui.info(f"  smart-goal:     {'on' if self.smart else 'off'}")
        ui.info(f"  num_ctx:        {agent.NUM_CTX}")
        ui.info(f"  stream:         {'on' if agent.STREAM else 'off'}")
        ui.info(f"  think:          {'on' if agent.THINK_DEFAULT else 'off'}")
        ui.info(f"  temperature:    {agent.RUN_TEMPERATURE}")
        ui.info(f"  seed:           {agent.SEED}")
        ui.info(f"  attached:       {len(self.staged)} file(s)")


def _list_runs():
    runs_dir = os.path.join(agent.HERE, "runs")
    if not os.path.isdir(runs_dir):
        return []
    dirs = [os.path.join(runs_dir, d) for d in os.listdir(runs_dir)
            if os.path.isdir(os.path.join(runs_dir, d))]
    return sorted(dirs, key=os.path.getmtime)


def _handle_set(key, value):
    """/set <key> <value> — mutate the matching agent global."""
    try:
        if key in ("temperature", "temp"):
            agent.RUN_TEMPERATURE = float(value)
        elif key in ("num-ctx", "num_ctx"):
            agent.NUM_CTX = int(value)
        elif key == "seed":
            agent.SEED = int(value)
        elif key in ("max-tool-rounds", "max_tool_rounds"):
            return int(value)  # caller stores it on settings
        elif key == "stream":
            agent.STREAM = value.lower() in ("on", "true", "1", "yes")
        elif key == "think":
            agent.THINK_DEFAULT = value.lower() in ("on", "true", "1", "yes")
        else:
            ui.warning(f"unknown setting '{key}' — try: temperature, num-ctx, seed, "
                       "max-tool-rounds, stream, think")
            return None
    except ValueError:
        ui.warning(f"invalid value for {key}: {value!r}")
        return None
    ui.info(f"set {key} = {value}")
    return None


def _run_goal(text, s):
    """Run one goal through the standard loop with a fresh workspace."""
    agent.WORKSPACE = agent.init_workspace(None)
    ui.run_header(version=agent.__version__, server=agent.URL, executor=s.model,
                  reviewer=s.reviewer_model, workspace=agent.WORKSPACE,
                  num_ctx=agent.NUM_CTX, stream=agent.STREAM,
                  think=agent.THINK_DEFAULT, temp=agent.RUN_TEMPERATURE, seed=agent.SEED)
    text, at_paths = agent.extract_at_paths(text)
    copied, images = agent.stage_attachments(s.staged + at_paths)
    if s.smart:
        goal, task, criteria = agent.make_goal_task(s.model, text)
    else:
        goal, task, criteria = text, text, None
    agent.main(s.model, s.reviewer_model, goal, task, criteria,
               s.attempts, s.max_tool_rounds, attachments=copied, images=images)
    if s.staged:
        s.staged.clear()
        ui.info("staged attachments cleared (staging is per-goal — /attach again to reuse)")


def _dispatch(line, s):
    """Handle one command line. Returns False to quit, True to keep going."""
    cmd, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    cmd = cmd.lower()

    if cmd in ("exit", "quit", "q"):
        return False
    elif cmd == "help":
        ui.info(HELP)
    elif cmd == "config":
        s.show()
    elif cmd == "smart":
        s.smart = not s.smart
        ui.info(f"smart-goal rewriting {'on' if s.smart else 'off'}")
    elif cmd == "model":
        if not rest:
            ui.warning("usage: /model <name>")
        else:
            try:
                agent.check_model(rest)
                s.model = rest
                ui.info(f"executor model → {rest}")
            except SystemExit as e:
                ui.warning(str(e))
    elif cmd == "reviewer":
        if not rest:
            ui.warning("usage: /reviewer <name>")
        else:
            try:
                agent.check_model(rest)
                s.reviewer_model = rest
                ui.info(f"reviewer model → {rest}")
            except SystemExit as e:
                ui.warning(str(e))
    elif cmd == "attach":
        if not rest:
            if s.staged:
                ui.info("Staged for the next goal:")
                for p in s.staged:
                    ui.info(f"  - {p}")
            else:
                ui.info("no files staged — /attach <path> to add some")
        elif rest.lower() == "clear":
            s.staged.clear()
            ui.info("staged attachments cleared")
        else:
            try:
                paths = shlex.split(rest)
            except ValueError as e:
                ui.warning(f"could not parse paths: {e}")
                paths = []
            for p in paths:
                full = os.path.abspath(os.path.expanduser(p))
                if os.path.isfile(full):
                    s.staged.append(full)
                    ui.info(f"staged: {full}")
                else:
                    ui.warning(f"not a file, skipping: {p}")
    elif cmd == "attempts":
        try:
            s.attempts = int(rest)
            ui.info(f"attempts → {s.attempts}")
        except ValueError:
            ui.warning("usage: /attempts <n>")
    elif cmd == "set":
        key, _, value = rest.partition(" ")
        if not key or not value.strip():
            ui.warning("usage: /set <key> <value>")
        else:
            new_rounds = _handle_set(key, value.strip())
            if new_rounds is not None:
                s.max_tool_rounds = new_rounds
                ui.info(f"set max-tool-rounds = {new_rounds}")
    elif cmd == "runs":
        runs = _list_runs()
        if not runs:
            ui.info("(no runs yet)")
        else:
            for d in runs[-20:]:
                ui.info(f"  {d}")
    elif cmd == "last":
        runs = _list_runs()
        ui.info(runs[-1] if runs else "(no runs yet)")
    elif cmd == "clear":
        os.system("cls" if os.name == "nt" else "clear")
    else:
        ui.warning(f"unknown command '/{cmd}' — type /help")
    return True


def run_repl(args, reviewer_model):
    s = _Settings(args, reviewer_model)

    ui.info(f"agent.py v{agent.__version__} — interactive mode")
    ui.info(f"server: {agent.URL} · model: {s.model}")
    ui.info("Type a goal, or /help for commands. Ctrl-D to quit.\n")

    # prompt_toolkit needs a real terminal; on piped/redirected stdin use input().
    if _PTK_AVAILABLE and sys.stdin.isatty():
        os.makedirs(HISTORY_DIR, exist_ok=True)
        session = PromptSession(history=FileHistory(HISTORY_FILE))

        def read():
            return session.prompt("agent› ")
    else:
        def read():
            return input("agent› ")

    while True:
        try:
            line = read().strip()
        except EOFError:       # Ctrl-D
            break
        except KeyboardInterrupt:  # Ctrl-C at the prompt: cancel this line
            continue

        if not line:
            continue
        if line.startswith("/"):
            if not _dispatch(line, s):
                break
            continue

        try:
            _run_goal(line, s)
        except KeyboardInterrupt:
            # agent.main handles its own Ctrl-C; this catches one raised between runs.
            ui.warning("\n(interrupted — back to prompt)")

    ui.info("bye.")
