"""Tests for seeding the todo checklist from the plan turn's numbered steps."""

from harness import run as run_mod
from harness import todos
from harness import tools as tools_mod
from harness.runlog import RunLog
from harness.workspace import Workspace

SYS = "you are a test executor"


def setup_function(_):
    todos.reset()


# ── seed_from_plan parsing ───────────────────────────────────────────

def test_numbered_steps_become_pending_todos():
    plan = ("Here is my plan:\n"
            "1. Create hello.py with a greet() function\n"
            "2) Write test_hello.py\n"
            "3. Run pytest -q to verify\n")
    assert todos.seed_from_plan(plan) == 3
    assert [t["status"] for t in todos.current] == ["pending"] * 3
    assert todos.current[0]["text"] == "Create hello.py with a greet() function"
    assert todos.current[1]["text"] == "Write test_hello.py"


def test_markdown_bold_and_indent_stripped():
    plan = "  1. **Create files**\n  2. *verify*\n"
    todos.seed_from_plan(plan)
    assert todos.current[0]["text"] == "Create files"
    assert todos.current[1]["text"] == "verify"


def test_bullets_and_prose_ignored():
    plan = ("1. real step\n"
            "- a sub-bullet that is not a step\n"
            "some prose\n"
            "2. another real step\n")
    assert todos.seed_from_plan(plan) == 2


def test_unparseable_plan_leaves_todos_alone():
    todos.set_todos([{"text": "existing", "status": "in_progress"}])
    assert todos.seed_from_plan("I will just write the file and test it.") == 0
    assert todos.current[0]["text"] == "existing"


def test_single_step_not_seeded():
    assert todos.seed_from_plan("1. do everything") == 0


def test_capped_at_12_steps():
    plan = "\n".join(f"{i}. step {i}" for i in range(1, 20))
    assert todos.seed_from_plan(plan) == 12


# ── loop wiring: the plan turn seeds before execution ────────────────

def test_plan_turn_seeds_todos(scripted_llm, tmp_path):
    ws = Workspace(str(tmp_path / "run"))
    tools_mod.configure(ws)
    log = RunLog(ws.run_dir)
    session = run_mod._new_session("m", SYS)

    scripted_llm.queue_text("1. write hello.txt\n2. verify it")  # plan turn
    scripted_llm.queue_text("done")                              # execute answer

    run_mod._run_attempt(session, ws, log, "do it", "goal", None,
                         attempt=1, plan_first=True)
    assert [t["text"] for t in todos.current] == ["write hello.txt", "verify it"]
