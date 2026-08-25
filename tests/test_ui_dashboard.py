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
    d.ledger = []
    d.last_checks = ""
    d.roles = {}
    d.attempt_tokens = {}
    d.reclaimed = []
    d.loop_warn = None
    d.loop_sig = None
    d.last_change = time.monotonic()
    d.tools_since_change = 0
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
    # the expanded hint is one no-wrap line: narrow terminals ellipsize the
    # tail rather than growing the dashboard by a row
    assert "solo" in out and "[m]essage" in out


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
    assert "[m]essage" in out            # docked input placeholder


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
    assert "[m]essage" in out
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


# ─── [a] attempt ledger ─────────────────────────────────────────────

def _review(dash, n, passed, crits, summary="verdict"):
    ui.criteria(crits, source=f"reviewer, attempt {n}")
    ui.attempt_result(n, passed, summary, crits)


def test_ledger_keeps_every_attempt(dash):
    _review(dash, 1, False, [{"criterion": "converts", "met": False}])
    _review(dash, 2, False, [{"criterion": "converts", "met": True}])
    assert [e["n"] for e in dash.ledger] == [1, 2]
    # the latest review overwrites criteria_items; the ledger does not
    assert dash.ledger[0]["criteria"][0]["met"] is False


def test_ledger_replaces_a_re_reported_attempt(dash):
    _review(dash, 1, False, [{"criterion": "a", "met": False}])
    _review(dash, 1, True, [{"criterion": "a", "met": True}])
    assert len(dash.ledger) == 1 and dash.ledger[0]["passed"] is True


def test_regressions_need_a_criterion_that_was_met_before():
    assert ui._regressions([]) == []
    one = [{"n": 1, "criteria": [{"criterion": "a", "met": False}]}]
    assert ui._regressions(one) == []  # a single review can't regress
    ledger = one + [{"n": 2, "criteria": [{"criterion": "a", "met": True},
                                          {"criterion": "b", "met": True}]},
                    {"n": 3, "criteria": [{"criterion": "a", "met": True},
                                          {"criterion": "b", "met": False}]}]
    assert ui._regressions(ledger) == ["b"]


def test_ledger_panel_grids_attempts_and_marks_regression(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    _review(dash, 1, False, [{"criterion": "converts csv", "met": True},
                             {"criterion": "pytest passes", "met": True}])
    _review(dash, 2, False, [{"criterion": "converts csv", "met": True},
                             {"criterion": "pytest passes", "met": False}],
            summary="tests broke")
    ui._show_ledger()
    out = _render_text(printed[-1][0], width=120)
    assert "a1" in out and "a2" in out
    assert "converts csv" in out and "pytest passes" in out
    assert "regressed" in out
    assert "tests broke" in out
    assert ui._regressions(dash.ledger) == ["pytest passes"]


def test_ledger_carries_check_evidence_and_retry_focus(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    ui.checks("$ pytest -q\nFAILED test_nulls.py::test_empty - AssertionError\n"
              "1 failed, 3 passed")
    _review(dash, 1, False, [{"criterion": "pytest passes", "met": False}])
    assert dash.last_checks == ""  # consumed by the attempt it belongs to
    ui.retry_focus("fix null handling, keep the schema work")
    ui._show_ledger()
    out = _render_text(printed[-1][0], width=120)
    assert "test_nulls.py::test_empty" in out
    assert "retry focus" in out and "fix null handling" in out


def test_regression_hint_surfaces_in_the_plan_panel(dash):
    _review(dash, 1, False, [{"criterion": "pytest passes", "met": True}])
    _review(dash, 2, False, [{"criterion": "pytest passes", "met": False}])
    dash.last_verdict = "tests broke"
    out = _render_text(dash._render_plan(), width=120)
    assert "1 regressed" in out and "[a] ledger" in out


def test_ledger_panel_empty_before_any_review(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    ui._show_ledger()
    assert "no reviewed attempts yet" in _render_text(printed[-1][0])


def test_digest_checks_prefers_the_failure():
    assert "FAILED t.py::x" in ui._digest_checks("$ pytest\nFAILED t.py::x - boom\n"
                                                 "1 failed, 2 passed")
    assert ui._digest_checks("$ pytest\n5 passed in 0.1s") == "5 passed in 0.1s"
    assert ui._digest_checks("") == ""


# ─── [b] budget ─────────────────────────────────────────────────────

def test_budget_accumulates_per_role_and_attempt(dash):
    dash.attempt_n = 1
    ui.llm_stats("executor", 4.0, 1000, 200)
    ui.llm_stats("executor", 2.0, 500, 100)
    ui.llm_stats("reviewer", 1.0, 300, 50)
    dash.attempt_n = 2
    ui.llm_stats("executor", 3.0, 900, 100)
    assert dash.roles["executor"] == {"calls": 3, "tin": 2400, "tout": 400,
                                      "secs": 9.0}
    assert dash.roles["reviewer"]["calls"] == 1
    assert dash.attempt_tokens == {1: 2150, 2: 1000}


def test_budget_tolerates_missing_token_counts(dash):
    ui.llm_stats("executor", 1.0, None, None)  # ollama omitted the meta
    assert dash.roles["executor"]["tin"] == 0


def test_budget_panel_renders_shares_and_rising_attempts(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    dash.attempt_n = 1
    ui.llm_stats("executor", 10.0, 8000, 1000)
    ui.llm_stats("reviewer", 2.0, 900, 100)
    dash.attempt_n = 2
    ui.llm_stats("executor", 12.0, 20000, 2000)
    ui._show_budget()
    out = _render_text(printed[-1][0], width=120)
    assert "executor" in out and "reviewer" in out
    assert "%" in out and "calls" in out
    assert "per attempt" in out and "↗" in out  # a2 cost far more than a1


def test_budget_panel_empty_before_any_call(dash):
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    ui._show_budget()
    assert "no model calls yet" in _render_text(printed[-1][0])


def test_compaction_dip_recorded_as_reclaimed(dash):
    ui.context_tokens(20000, 32000)
    ui.context_tokens(24000, 32000)   # growing: not a dip
    ui.context_tokens(9000, 32000)    # compaction
    ui.context_tokens(8800, 32000)    # noise below the threshold
    assert dash.reclaimed == [15000]


# ─── loop banner ────────────────────────────────────────────────────

def test_repeated_call_raises_the_loop_banner(dash):
    for _ in range(2):
        ui.tool("read_file", {"name": "convert.py"})
        ui.tool_result("ok")
    assert dash.loop_warn is None
    ui.tool("read_file", {"name": "convert.py"})
    assert "read_file convert.py ×3" in dash.loop_warn
    out = _render_text(dash._render_loop_banner())
    assert "read_file convert.py" in out and "[i] interrupt" in out


def test_different_args_do_not_trip_the_banner(dash):
    for i in range(6):
        ui.tool("read_file", {"name": f"f{i}.py"})
        ui.tool_result("ok")
    assert dash.loop_warn is None
    assert dash._render_loop_banner() is None


def test_writing_a_file_clears_the_banner(dash):
    for _ in range(3):
        ui.tool("read_file", {"name": "convert.py"})
        ui.tool_result("ok")
    assert dash.loop_warn is not None
    ui.file_created("convert.py", added=12)
    assert dash.loop_warn is None
    assert dash._render_loop_banner() is None


def test_idle_banner_after_a_stretch_with_no_edits(dash):
    for i in range(5):
        ui.tool("grep_files", {"pattern": f"p{i}"})
        ui.tool_result("ok")
    dash.last_change = time.monotonic() - 200
    out = _render_text(dash._render_loop_banner())
    assert "no file changes for 3:20" in out


# ─── review fixes: banner decay, setup cost, locked ledger ──────────

def test_banner_clears_when_the_agent_moves_on(dash):
    """Regression: loop_warn was only cleared by a file write, so a banner
    naming a call the agent had long since stopped making stayed on screen."""
    for _ in range(3):
        ui.tool("read_file", {"name": "convert.py"})
        ui.tool_result("ok")
    assert dash.loop_warn is not None
    ui.tool("grep_files", {"pattern": "nulls"})   # different work, no write
    assert dash.loop_warn is None and dash.loop_sig is None
    assert dash._render_loop_banner() is None


def test_repeat_of_a_different_call_replaces_the_banner(dash):
    for _ in range(3):
        ui.tool("read_file", {"name": "a.py"})
        ui.tool_result("ok")
    first = dash.loop_warn
    for _ in range(3):
        ui.tool("read_file", {"name": "b.py"})
        ui.tool_result("ok")
    assert dash.loop_warn != first and "b.py" in dash.loop_warn


def test_idle_banner_counts_tools_since_the_last_write(dash):
    """Regression: tool_count is cumulative, so once a run had made four
    calls the idle banner could fire forever regardless of later writes."""
    for i in range(5):
        ui.tool("grep_files", {"pattern": f"p{i}"})
        ui.tool_result("ok")
    dash.last_change = time.monotonic() - 200
    assert dash._render_loop_banner() is not None      # stalled: nothing written
    ui.file_created("out.py", added=10)                # progress resets both
    assert dash.tools_since_change == 0
    dash.last_change = time.monotonic() - 200          # idle again, but quiet
    assert dash._render_loop_banner() is None
    for i in range(4):
        ui.tool("grep_files", {"pattern": f"q{i}"})
        ui.tool_result("ok")
    assert dash._render_loop_banner() is not None      # four more, still nothing


def test_budget_shows_pre_attempt_cost(dash):
    """Regression: --best-of spends its whole candidate round before attempt 1
    is announced, so its tokens landed in bucket 0 and were filtered out of
    the panel that exists to show where tokens went."""
    printed = []
    dash.print = lambda *a, **k: printed.append(a)
    dash.attempt_n = 0                       # goalsmith + candidate round
    ui.llm_stats("executor", 30.0, 50000, 4000)
    dash.attempt_n = 1
    ui.llm_stats("executor", 10.0, 9000, 500)
    assert dash.attempt_tokens[0] == 54000
    ui._show_budget()
    out = _render_text(printed[-1][0], width=120)
    assert "setup" in out and "54.0k" in out


def test_ledger_reads_are_taken_under_the_lock(dash):
    """The render and reader threads must snapshot rather than iterate the
    live list — attempt_result sorts it, and CPython empties a list while
    sorting. Asserts the lock is actually held during the read."""
    import threading
    holder = threading.Lock()
    seen = []

    class WatchedLock:
        def __enter__(self):
            seen.append("locked")
            return holder.__enter__()

        def __exit__(self, *a):
            return holder.__exit__(*a)

    _review(dash, 1, False, [{"criterion": "a", "met": True}])
    _review(dash, 2, False, [{"criterion": "a", "met": False}])
    dash._lock = WatchedLock()
    dash.print = lambda *a, **k: None
    seen.clear()
    ui._show_ledger()
    assert seen, "_show_ledger read the ledger without taking the lock"
    seen.clear()
    _render_text(dash._render_plan())
    assert seen, "_render_plan read the ledger without taking the lock"


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
