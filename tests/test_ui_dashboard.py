"""Dashboard redesign: tool timeline capture, attempt dots, render layout,
and the [c] per-command permission grant."""

import time
from collections import deque

import pytest

from harness import permissions, ui

pytest.importorskip("rich")  # render tests need rich; the harness itself doesn't


def _bare_dashboard(**extra):
    import threading
    d = object.__new__(ui._Dashboard)  # skip __init__: no Live needed
    d._lock = threading.Lock()
    d.goal = "test goal"
    d.model = "m"
    d.reviewer_model = "m"
    d.max_attempts = 5
    d.num_ctx = 10000
    d.attempt_n = 0
    d.phase_text = "executing"
    d.phase_started = time.monotonic()
    d.last_llm = ""
    d.tokens = 0
    d.other_label = ""
    d.other_tokens = 0
    d.tool_rows = deque(maxlen=10)
    d.tool_history = []
    d.last_diff = None
    d.tool_count = 0
    d.files_touched = {}
    d.llm_calls = 0
    d.llm_secs = 0.0
    d.criteria_items = []
    d.criteria_source = ""
    d.todo_items = []
    d.stream_buf = ""
    d.stream_thinking = ""
    d.last_verdict = None
    d.verdict_passed = None
    d.tint = "cyan"
    d.rail = []
    d.burn = deque(maxlen=6)
    d.subagent = None
    d.quiet = False
    d.run_started = time.monotonic()
    d.llm_durs = deque(maxlen=16)
    d.pulse = deque(maxlen=12)
    d.trend = []
    d.ctx_hist = deque(maxlen=24)
    d.paused = False
    d._transcript = deque()
    d._transcript_len = 0
    d._transcript_dropped = 0
    d.refresh = lambda: None
    d.print = lambda *a, **k: None  # ui.diff prints a panel above the region
    from rich.console import Console
    d.console = Console(width=100, force_terminal=False)
    for k, v in extra.items():
        setattr(d, k, v)
    return d


@pytest.fixture
def dash(monkeypatch):
    d = _bare_dashboard()
    monkeypatch.setattr(ui, "_dash", d)
    yield d
    ui._subagent_depth = 0


def _render_text(renderable, width=100):
    from rich.console import Console
    console = Console(record=True, width=width, force_terminal=False)
    console.print(renderable)
    return console.export_text()


# ─── _short_args ────────────────────────────────────────────────────

def test_short_args_per_tool():
    assert ui._short_args("run_shell", {"command": "pytest -q tests/"}) == \
        "pytest -q tests/"
    assert ui._short_args("write_file", {"name": "a.txt", "text": "x" * 500}) == \
        "a.txt"
    assert ui._short_args("run_python", {"code": "import os\nprint(1)"}) == \
        "import os"
    assert ui._short_args("web_search", {"query": "ollama api"}) == "ollama api"


def test_short_args_json_string_and_fallbacks():
    assert ui._short_args("run_shell", '{"command": "ls -la"}') == "ls -la"
    # unknown tool falls back to compact JSON
    assert ui._short_args("mystery_tool", {"a": 1}) == '{"a": 1}'
    # non-dict args don't crash
    assert ui._short_args("run_shell", "not json") == "not json"


def test_short_args_capped_and_collapsed():
    long = ui._short_args("run_shell", {"command": "x  y\n z" + "a" * 200})
    assert len(long) <= 80
    assert "\n" not in long and "  " not in long


# ─── timeline capture ───────────────────────────────────────────────

def test_tool_result_completes_row_with_duration(dash):
    ui.tool("write_file", {"name": "a.txt", "text": "hi"})
    row = dash.tool_rows[-1]
    assert row.status == "running" and row.args_short == "a.txt"
    ui.tool_result("wrote a.txt")
    assert row.status == "done"
    assert row.duration is not None and row.duration >= 0


def test_error_result_marks_row_error(dash):
    ui.tool("run_shell", {"command": "boom"})
    ui.tool_result("[ERROR] exit 1")
    assert dash.tool_rows[-1].status == "error"


def test_diff_attaches_stat_to_running_row_only(dash):
    ui.tool("read_file", {"name": "a.txt"})
    ui.tool_result("old content")           # completed row — must not get a stat
    ui.tool("edit_file", {"name": "a.txt"})
    ui.diff("a.txt", "--- a\n+++ b\n+new line\n+another\n-old line\n")
    assert dash.tool_rows[-1].diff_stat == "+2 −1"
    assert dash.tool_rows[0].diff_stat is None
    ui.tool_result("edited")
    assert dash.tool_rows[-1].status == "done"


# ─── subagent nesting ───────────────────────────────────────────────

def test_subagent_flow_nests_in_timeline(dash):
    ui.tool("spawn_subagent", {"task": "research the API"})
    ui.subagent_start("research", "research the API")
    header = dash.tool_rows[-1]
    assert header.is_subagent_header
    assert header.args_short.startswith("research:")
    assert ui._subagent_depth == 1

    ui.tool("web_search", {"query": "docs"})     # the subagent's own call
    child = dash.tool_rows[-1]
    assert child.depth == 1
    ui.tool_result("found it")
    assert child.status == "done"

    ui.subagent_end()
    assert ui._subagent_depth == 0
    ui.tool_result("subagent summary")           # parent's spawn result
    assert header.status == "done" and header.duration is not None


def test_subagent_start_without_spawn_row_is_defensive(dash):
    ui.subagent_start("verify", "check things")
    assert dash.tool_rows[-1].is_subagent_header
    ui.subagent_end()


# ─── attempt dots & render ──────────────────────────────────────────

def test_context_bar_empty_looks_empty():
    t = ui._context_bar(0, 10000, width=20)
    assert t.plain == "░" * 20  # no filled glyphs at zero tokens
    t = ui._context_bar(5000, 10000, width=20)
    assert t.plain == "█" * 10 + "░" * 10
    t = ui._context_bar(15000, 10000, width=20)  # over budget clamps
    assert t.plain == "█" * 20


def test_context_bar_color_thresholds():
    def style_of(tokens):
        return str(ui._context_bar(tokens, 100, width=10)._spans[0].style)
    assert style_of(50) == "green"
    assert style_of(75) == "yellow"
    assert style_of(90) == "red"


def test_executor_bar_is_pinned(dash):
    ui.context_tokens(4000, 10000, label="executor")
    assert dash.tokens == 4000 and dash.other_label == ""
    ui.context_tokens(700, 10000, label="reviewer")   # must not move the bar
    assert dash.tokens == 4000
    assert dash.other_label == "reviewer" and dash.other_tokens == 700
    ui.context_tokens(4200, 10000, label="executor")  # reviewer row disappears
    assert dash.tokens == 4200 and dash.other_label == ""


def test_context_bar_rows_render(dash):
    ui.context_tokens(4000, 10000, label="executor")
    out = _render_text(dash._render_context_bar())
    assert "ctx" in out and "executor" not in out  # alone: generic label
    assert "40%" in out
    ui.context_tokens(700, 10000, label="subagent")
    out = _render_text(dash._render_context_bar())
    assert "executor" in out and "subagent" in out
    assert "40%" in out and "7%" in out


def test_rail_tracks_phase_loop(dash):
    ui.phase("planning (no tools)")
    ui.phase("executing")
    ui.phase("reviewing")
    ui.verdict(False, "missing case")
    ui.phase("executing")
    assert dash.rail == [["plan", "done"], ["exec", "done"],
                         ["review", "fail"], ["exec", "running"]]
    out = _render_text(dash._render_status_line())
    assert "plan ✓" in out and "review ✗" in out and "exec 0:00" in out


def test_rail_caps_display_with_ellipsis(dash):
    for _ in range(4):
        ui.phase("executing")
        ui.phase("reviewing")
    out = _render_text(dash._render_status_line())
    assert "…" in out  # only the last 5 entries render


def test_files_panel_accumulates_per_file(dash):
    ui.tool("edit_file", {"name": "a.txt"})
    ui.diff("a.txt", "+++ b\n+one\n+two\n-old\n")
    ui.tool_result("ok")
    ui.tool("edit_file", {"name": "a.txt"})
    ui.diff("a.txt", "+++ b\n+three\n")
    ui.tool_result("ok")
    assert dash.files_touched["a.txt"] == \
        {"add": 3, "rm": 1, "edits": 2, "hist": [3, 1]}
    out = _render_text(dash._render_files())
    assert "a.txt" in out and "+3 −1" in out and "2 edits" in out


def test_new_file_records_additions_not_phantom(dash):
    # write_file for a new file routes to file_created (no diff), so the
    # panel must show the real line count, not the old "+0 −0 · 0 edits"
    ui.tool("write_file", {"name": "new.py"})
    assert "new.py" not in dash.files_touched  # tool() no longer seeds zeros
    ui.file_created("new.py", 42)
    ui.tool_result("WROTE")
    assert dash.files_touched["new.py"] == \
        {"add": 42, "rm": 0, "edits": 1, "hist": [42]}
    out = _render_text(dash._render_files())
    assert "new.py" in out and "+42 −0" in out and "1 edit" in out


def test_noop_edit_leaves_no_files_row(dash):
    # an edit that produces an empty diff never calls ui.diff(); with the
    # tool() seed gone, it must not leave a phantom file entry
    ui.tool("edit_file", {"name": "same.py"})
    ui.tool_result("REPLACED")
    assert dash.files_touched == {}
    assert dash._render_files() is None


def test_burn_rate_and_eta_render(dash):
    dash.tokens = 4000
    dash.burn.extend([(0.0, 1000), (60.0, 4000)])  # 3k over a minute
    out = _render_text(dash._render_context_bar())
    assert "↗ 3.0k/min" in out
    assert "full ~2m" in out  # (10000 - 4000) / 3000 per min
    dash.burn.clear()
    dash.burn.extend([(0.0, 4000), (60.0, 4000)])  # flat: no trend shown
    out = _render_text(dash._render_context_bar())
    assert "↗" not in out


def test_subagent_lane_panel(dash):
    ui.tool("spawn_subagent", {"task": "research the API"})
    ui.subagent_start("research", "research the API")
    out = _render_text(dash._render())
    assert "subagent · research" in out and "research the API" in out
    ui.subagent_end()
    ui.tool_result("done")
    assert dash.subagent is None
    assert dash._render_subagent() is None


def test_spark_maps_range():
    assert ui._spark([0, 7]) == "▁█"
    assert ui._spark([1, 1, 1]) == "▄▄▄"     # flat: midline
    assert ui._spark([5]) == ""              # too few points
    assert ui._spark(range(100), width=8) == "▁▂▃▄▅▆▇█"  # last 8, monotonic


def test_tint_resets_on_new_phase(dash):
    ui.verdict(False, "missing case")
    assert dash.tint == "red"
    ui.phase("executing")   # retry begins: back to actively-working cyan
    assert dash.tint == "cyan"


def test_cadence_ticker_in_tools_title(dash):
    ui.tool("read_file", {"name": "a"})
    ui.tool_result("ok")
    ui.tool("run_shell", {"command": "boom"})
    ui.tool_result("[ERROR] exit 1")
    out = _render_text(dash._render_timeline())
    assert "tools · " in out and "·✗" in out


def test_review_trend_recorded_and_rendered(dash):
    ui.criteria(["a", "b"])  # bare strings: no review yet, no trend entry
    assert dash.trend == []
    ui.criteria([{"criterion": "a", "met": True},
                 {"criterion": "b", "met": False}])
    ui.criteria([{"criterion": "a", "met": True},
                 {"criterion": "b", "met": True}])
    assert dash.trend == [(1, 2), (2, 2)]
    out = _render_text(dash._render_plan())
    assert "1/2 → 2/2" in out


def test_stream_pulse_rate_and_stall(dash):
    now = time.monotonic()
    dash.stream_buf = "hello"
    dash.pulse.extend([(now - 1.8, 40), (now - 0.2, 60)])
    out = _render_text(dash._render_stream())
    assert "tok/s" in out and "stalled" not in out
    dash.pulse.clear()
    dash.pulse.extend([(now - 8, 40), (now - 6, 60)])  # nothing for 6s
    out = _render_text(dash._render_stream())
    assert "stalled" in out and "tok/s" not in out


def test_ctx_sawtooth_renders(dash):
    dash.ctx_hist.extend([1000, 4000, 8000, 3000])
    out = _render_text(dash._render_context_bar())
    assert ui._spark([1000, 4000, 8000, 3000], width=6) in out


def test_tool_history_footer_spark_and_time_split(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    dash.run_started = time.monotonic() - 100
    dash.llm_secs = 60.0
    dash.llm_calls = 5
    dash.llm_durs.extend([1.0, 8.0, 3.0])
    ui.tool("read_file", {"name": "a"})
    ui.tool_result("ok")
    ui._show_tool_history()
    out = _render_text(printed[-1][0])
    assert ui._spark([1.0, 8.0, 3.0]) in out
    assert "spent llm 60% · tools 0% · other 40%" in out


def test_quiet_mode_collapses_render(dash, monkeypatch):
    monkeypatch.setattr(ui, "_state", ui.ControlState())
    dash.todo_items = [{"status": "pending", "text": "solo"}]
    ui.tool("run_shell", {"command": "pytest"})
    dash.quiet = True
    out = _render_text(dash._render())
    assert "solo" not in out and "pytest" not in out  # panels collapsed
    assert "[z] expand" in out
    dash.quiet = False
    out = _render_text(dash._render())
    assert "solo" in out and "[z]quiet" in out


def test_render_smoke_all_sections(dash, monkeypatch):
    monkeypatch.setattr(ui, "_state", ui.ControlState())  # keys active
    dash.attempt_n = 2
    dash.todo_items = [{"status": "in_progress", "text": "do the thing"}]
    dash.last_verdict = "needs jitter"  # summary() carries no PASSED/FAILED tag
    dash.stream_buf = "streaming body"
    ui.tool("write_file", {"name": "a.txt"})
    ui.diff("a.txt", "+++ b\n+one\n")
    ui.tool_result("ok")
    ui.tool("run_shell", {"command": "pytest"})

    dash.criteria_items = [{"criterion": "has jitter", "met": True}]
    from rich.console import Console
    dash.console = Console(width=130, force_terminal=False)  # wide layout
    out = _render_text(dash._render(), width=130)
    assert "attempt 2/5" in out
    assert "✓" in out and "a.txt" in out and "+1 −0" in out
    assert "pytest" in out               # running row present
    assert "[o] full transcript" in out  # streaming subtitle
    assert "plan" in out                 # merged criteria/todos/verdict panel
    assert "do the thing" in out and "has jitter" in out
    assert "review: FAILED — needs jitter" in out  # verdict folded in
    assert "ctx" in out                  # context bar inline in the status line
    assert "tools 2" in out and "files 1" in out  # packed into the status line
    assert "agent · m" in out            # model in the header title
    assert "[m] to type" in out          # docked input placeholder


def test_render_plan_without_verdict(dash):
    dash.todo_items = [{"status": "pending", "text": "solo"}]
    dash.last_verdict = None
    out = _render_text(dash._render())
    assert "plan" in out and "solo" in out
    assert "review:" not in out  # no verdict line until a review happens


# ─── criteria panel ─────────────────────────────────────────────────

def test_criteria_pending_then_reviewed(dash):
    ui.criteria(["has jitter", "capped at 30s"])
    assert dash.criteria_items == ["has jitter", "capped at 30s"]
    out = _render_text(dash._render_criteria())
    assert "○" in out and "has jitter" in out
    ui.criteria([{"criterion": "has jitter", "met": True},
                 {"criterion": "capped at 30s", "met": False,
                  "note": "no cap found"}],
                source="reviewer, attempt 1")
    out = _render_text(dash._render_criteria())
    assert "✓" in out and "✗" in out
    assert "no cap found" in out and "reviewer, attempt 1" in out


# ─── tool history + input line ──────────────────────────────────────

def test_tool_history_capped_and_hint_rendered(dash):
    for i in range(505):
        ui.tool("read_file", {"name": f"f{i}.txt"})
    assert len(dash.tool_history) == 500
    assert len(dash.tool_rows) == 10
    out = _render_text(dash._render_timeline())
    assert "earlier calls · [t] expand" in out


def test_input_line_placeholder_and_focus(dash, monkeypatch):
    st = ui.ControlState()
    monkeypatch.setattr(ui, "_state", st)
    out = _render_text(dash._render_input_line())
    assert "[m] to type" in out
    st.focused = True
    st.buffer = "fix the tests"
    out = _render_text(dash._render_input_line())
    assert "fix the tests" in out and "█" in out


def test_composer_wraps_long_buffer(dash, monkeypatch):
    st = ui.ControlState()
    st.focused = True
    st.buffer = "x" * 300 + "TAIL"
    monkeypatch.setattr(ui, "_state", st)
    out = _render_text(dash._render_input_line())
    assert "TAIL" in out                 # nothing truncated: the box wraps
    assert "message" in out and "Enter send" in out


# ─── permissions [c] per-command grant ──────────────────────────────

@pytest.fixture
def interactive(monkeypatch):
    permissions.configure(yolo=False)
    monkeypatch.setattr(permissions, "_interactive", lambda: True)
    yield
    permissions.configure(yolo=True)


def test_base_command_derivation():
    assert permissions._base_command("run_shell", {"command": "pytest -q x"}) == "pytest"
    assert permissions._base_command("run_shell", {"command": 'echo "unbalanced'}) == "echo"
    assert permissions._base_command("run_script", {"name": "build.sh"}) == "build.sh"
    assert permissions._base_command("run_python", {"code": "print(1)"}) is None
    assert permissions._base_command("run_shell", {"command": ""}) is None


def test_c_grants_base_command_for_run(interactive, monkeypatch):
    prompts = []
    monkeypatch.setattr(ui, "confirm_tool",
                        lambda *a, **kw: prompts.append(kw) or "c")
    assert permissions.check("run_shell", {"command": "pytest -q"}) is None
    assert permissions.check("run_shell", {"command": "pytest -x tests/"}) is None
    assert len(prompts) == 1  # same base command rode the [c] grant
    assert prompts[0]["command_scope"] == "pytest"
    assert permissions.check("run_shell", {"command": "ls"}) is None  # answers "c"
    assert len(prompts) == 2  # different base command prompted again
    permissions.configure(yolo=False)  # reset clears the grants
    monkeypatch.setattr(permissions, "_interactive", lambda: True)
    monkeypatch.setattr(ui, "confirm_tool",
                        lambda *a, **kw: prompts.append(kw) or "y")
    assert permissions.check("run_shell", {"command": "pytest -q"}) is None
    assert len(prompts) == 3


def test_run_python_gets_no_command_scope(interactive, monkeypatch):
    seen = {}
    monkeypatch.setattr(ui, "confirm_tool",
                        lambda *a, **kw: seen.update(kw) or "c")
    # "c" without a scope must be treated as a deny by check()
    out = permissions.check("run_python", {"code": "print(1)"})
    assert seen["command_scope"] is None
    assert out is not None and out.startswith("[ERROR] permission denied")


def test_confirm_tool_rejects_c_without_scope(monkeypatch, capsys):
    # plain interactive path: answer "c" when no scope is offered → deny
    monkeypatch.setattr(ui, "_dash", None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "c")
    assert ui.confirm_tool("run_python", "print(1)") == "n"
    monkeypatch.setattr("builtins.input", lambda prompt="": "c")
    assert ui.confirm_tool("run_shell", "$ ls", command_scope="ls") == "c"
