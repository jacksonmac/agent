"""Interactive controls: docked input ([m] focus / Enter / Esc), [p]ause,
[q]uit, [o]/[t]/[d] panels, and the reader-thread lifecycle.

Dispatch tests drive the pure per-key state machine `ui._feed_key` directly —
no thread, no TTY, no sleeps. Thread tests use an injectable FakeSource.
Plain mode (_state is None) must stay a no-op so the rest of the suite is
unaffected.
"""

import os
import queue
import threading
import time
from collections import deque

import pytest

from harness import run as run_mod
from harness import tools as tools_mod
from harness import ui
from harness.keys import HAVE_TERMIOS, KeyReader
from harness.llm import Session
from harness.runlog import RunLog
from harness.workspace import Workspace

SYS = "you are a test executor"


class FakeSource:
    """Queue-backed stand-in for KeyReader.read_token."""

    def __init__(self, tokens=()):
        self.q = queue.Queue()
        for t in tokens:
            self.q.put(t)

    def read_token(self, timeout=0.1):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


class DummyDash:
    """Just enough _Dashboard surface for _feed_key and the o/t/d panels."""

    def __init__(self):
        self.paused = False
        self.quiet = False
        self.printed = []
        self.attempt_n = 1
        self.tool_history = []
        self.last_diff = None
        self.tokens = 0        # the [t] panel's llm-stats footer reads these
        self.num_ctx = 10000
        self.llm_calls = 0
        self.llm_secs = 0.0
        self.last_llm = ""
        self.run_started = time.monotonic()
        self.llm_durs = deque(maxlen=16)
        self.ledger = []       # the [a] panel
        self.roles = {}        # the [b] panel
        self.attempt_tokens = {}
        self.reclaimed = []
        self._lock = threading.Lock()

    def refresh(self):
        pass

    def print(self, *args, **kwargs):
        self.printed.append(args)

    def transcript_text(self):
        return ""


@pytest.fixture
def state(monkeypatch):
    st = ui.ControlState()
    monkeypatch.setattr(ui, "_state", st)
    return st


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


@pytest.fixture
def log(ws):
    return RunLog(ws.run_dir)


def feed(state, keys, dash=None):
    for ch in keys:
        ui._feed_key(state, ch, dash)


# ─── plain mode stays inert ─────────────────────────────────────────

def test_poll_controls_noop_in_plain_mode():
    assert ui._state is None and ui._dash is None
    ui.poll_controls()  # must not raise
    assert ui.drain_messages() == []


# ─── unfocused command keys ─────────────────────────────────────────

def test_quit_key_raises_once(state):
    feed(state, "q")
    with pytest.raises(ui.QuitRequested):
        ui.poll_controls()
    ui.poll_controls()  # flag consumed — no re-raise


def test_unfocused_printables_are_ignored(state):
    feed(state, "xz?hello")
    assert state.buffer == "" and not state.focused
    assert ui.drain_messages() == []


# ─── docked input: focus / type / Enter / Esc / Backspace ───────────

def test_focus_type_enter_queues(state):
    feed(state, "m")
    assert state.focused
    feed(state, "hi\r")
    assert ui.drain_messages() == ["hi"]
    assert ui.drain_messages() == []      # cleared on read
    assert not state.focused and state.buffer == ""


def test_focused_command_keys_are_literal_text(state):
    feed(state, "m")
    feed(state, "pqotd\r")
    assert ui.drain_messages() == ["pqotd"]
    assert not state.paused and not state.quit_requested


def test_focused_case_preserved(state):
    feed(state, "m")
    feed(state, "Fix THIS\r")
    assert ui.drain_messages() == ["Fix THIS"]


def test_backspace_edits(state):
    feed(state, "m")
    feed(state, "abc\x7f\x7fd\r")
    assert ui.drain_messages() == ["ad"]


def test_esc_cancels(state):
    feed(state, "m")
    feed(state, "abc\x1b")
    assert not state.focused and state.buffer == ""
    assert ui.drain_messages() == []


def test_whitespace_only_enter_queues_nothing(state):
    feed(state, "m")
    feed(state, "   \r")
    assert ui.drain_messages() == []
    assert not state.focused


def test_queued_ack_printed(state):
    dash = DummyDash()
    feed(state, "m", dash)
    feed(state, "x\r", dash)
    assert any("message queued" in str(a) for a in dash.printed)


# ─── pause ──────────────────────────────────────────────────────────

def test_pause_toggles_and_any_key_resumes(state):
    dash = DummyDash()
    feed(state, "p", dash)
    assert state.paused and dash.paused
    feed(state, "x", dash)
    assert not state.paused and not dash.paused


def test_pause_key_itself_toggles_back(state):
    feed(state, "pp")
    assert not state.paused


def test_poll_controls_blocks_while_paused_then_resumes(state):
    feed(state, "p")
    done = threading.Event()

    def waiter():
        ui.poll_controls()
        done.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert not done.wait(timeout=0.15)   # genuinely blocked
    feed(state, "x")                     # resume; notify_all wakes the waiter
    assert done.wait(timeout=2.0)
    t.join(timeout=1.0)


def test_quit_while_paused_raises(state):
    dash = DummyDash()
    ui._dash = dash
    try:
        feed(state, "p", dash)
        feed(state, "q", dash)
        with pytest.raises(ui.QuitRequested):
            ui.poll_controls()
        assert dash.paused is False
    finally:
        ui._dash = None


def test_queuing_a_message_while_paused_resumes(state):
    feed(state, "p")
    feed(state, "m")
    assert state.paused and state.focused
    feed(state, "note\r")
    assert ui.drain_messages() == ["note"]
    # delivery happens before the next model call and pause blocks exactly
    # there — queuing must resume or the message would never arrive
    assert not state.paused


def test_esc_while_paused_stays_paused(state):
    feed(state, "p")
    feed(state, "m")
    feed(state, "abc\x1b")  # cancelled: nothing queued
    assert state.paused
    assert ui.drain_messages() == []


# ─── o/t/d panels ───────────────────────────────────────────────────

def test_tool_history_key_prints(state, monkeypatch):
    dash = DummyDash()
    dash.tool_history = [ui.ToolRow("write_file", "a.txt", status="done",
                                    duration=0.2)]
    monkeypatch.setattr(ui, "_dash", dash)
    feed(state, "t", dash)
    assert dash.printed  # the history panel went above the Live region


def test_diff_key_with_no_diff(state, monkeypatch):
    dash = DummyDash()
    monkeypatch.setattr(ui, "_dash", dash)
    feed(state, "d", dash)
    assert any("no diff yet" in str(a) for a in dash.printed)


def test_z_key_toggles_quiet_mode(state):
    dash = DummyDash()
    feed(state, "z", dash)
    assert dash.quiet
    feed(state, "z", dash)
    assert not dash.quiet
    assert not state.paused and not state.quit_requested  # z is quiet-only


# ─── queue management: [e]dit, [c]ancel ─────────────────────────────

def test_cancel_discards_the_queue(state):
    dash = DummyDash()
    feed(state, "m", dash)
    feed(state, "wrong thing\r", dash)
    feed(state, "c", dash)
    assert ui.drain_messages() == []
    assert any("discarded" in str(a) for a in dash.printed)


def test_cancel_with_nothing_queued_is_inert(state):
    feed(state, "c")
    assert not state.focused and not state.paused


def test_edit_reopens_the_last_queued_message(state):
    feed(state, "m")
    feed(state, "use pandas\r")
    feed(state, "e")
    assert state.focused and state.buffer == "use pandas"
    feed(state, "\x7f" * 6 + "pyarrow\r")
    assert ui.drain_messages() == ["use pyarrow"]


def test_edit_only_pulls_back_the_newest(state):
    feed(state, "m")
    feed(state, "first\r")
    feed(state, "m")
    feed(state, "second\r")
    feed(state, "e")
    assert state.buffer == "second"
    feed(state, "\x1b")                      # cancelled: it stays dropped
    assert ui.drain_messages() == ["first"]


# ─── [i] interrupt ──────────────────────────────────────────────────

def test_interrupt_queues_and_sets_the_flag(state):
    dash = DummyDash()
    feed(state, "i", dash)
    assert state.focused and state.compose_interrupt
    feed(state, "use pyarrow\r", dash)
    assert ui.drain_messages() == ["use pyarrow"]
    assert ui.interrupt_requested() is True
    assert ui.interrupt_requested() is False   # consumed on read
    assert not state.compose_interrupt
    assert any("interrupting" in str(a) for a in dash.printed)


def test_plain_m_does_not_interrupt(state):
    feed(state, "m")
    feed(state, "just a note\r")
    assert ui.drain_messages() == ["just a note"]
    assert ui.interrupt_requested() is False


def test_escaping_an_interrupt_clears_the_flag(state):
    feed(state, "i")
    feed(state, "never mind\x1b")
    assert not state.focused and not state.compose_interrupt
    assert ui.drain_messages() == []
    assert ui.interrupt_requested() is False


def test_interrupt_requested_is_noop_in_plain_mode():
    assert ui._state is None
    assert ui.interrupt_requested() is False


def test_interrupt_skips_the_rest_of_the_tool_round(scripted_llm, state, ws):
    """[i] mid-round: the remaining calls the model asked for are abandoned,
    but each still gets a result message so the history stays well-formed."""
    ran = []

    def fake_exec(name, args):
        ran.append(args.get("name"))
        feed(state, "i")           # the user hits [i] while this call runs
        feed(state, "stop that\r")
        return "ok"

    import harness.tools as tools_mod
    original = tools_mod.execute_tool_call
    tools_mod.execute_tool_call = fake_exec
    try:
        scripted_llm.replies.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "read_file",
                                         "arguments": {"name": f"f{i}.py"}}}
                           for i in range(3)]})
        scripted_llm.queue_text("stopped")
        schemas = [{"function": {"name": "read_file"}}]
        session = Session("m", SYS, schemas, accept_user_messages=True)
        session.send("task")
    finally:
        tools_mod.execute_tool_call = original

    assert ran == ["f0.py"]                 # f1/f2 never executed
    assert session.last_tool_calls == 1
    results = [m for m in session.messages if m["role"] == "tool"]
    assert len(results) == 3                # every issued call still answered
    assert "user interrupted" in results[1]["content"]
    # and the message the user typed reaches the model on the next call
    assert any("stop that" in str(m) for m in scripted_llm.payloads[-1]["messages"])


# ─── [/] steering presets ───────────────────────────────────────────

def test_slash_opens_the_menu_and_a_digit_sends(state):
    dash = DummyDash()
    feed(state, "/", dash)
    assert state.menu
    feed(state, "2", dash)
    assert not state.menu
    assert ui.drain_messages() == [ui.STEER_PRESETS[1]]


def test_menu_ignores_out_of_range_and_closes(state):
    feed(state, "/")
    feed(state, "9")
    assert not state.menu and ui.drain_messages() == []


def test_esc_closes_the_menu(state):
    feed(state, "/")
    feed(state, "\x1b")
    assert not state.menu and ui.drain_messages() == []


def test_menu_digits_are_literal_text_while_composing(state):
    feed(state, "m")
    feed(state, "/2\r")
    assert ui.drain_messages() == ["/2"]


# ─── [a] / [b] panels ───────────────────────────────────────────────

def test_ledger_and_budget_keys_print(state, monkeypatch):
    dash = DummyDash()
    monkeypatch.setattr(ui, "_dash", dash)
    feed(state, "a", dash)
    feed(state, "b", dash)
    assert any("no reviewed attempts yet" in str(a) for a in dash.printed)
    assert any("no model calls yet" in str(a) for a in dash.printed)


# ─── message injection into Session ─────────────────────────────────

def test_session_injects_message_before_model_call(scripted_llm, state):
    state.pending_msgs.append("prefer pathlib over os.path")
    session = Session("m", SYS, None, accept_user_messages=True)
    scripted_llm.queue_text("done")
    session.send("task")
    sent = scripted_llm.payloads[0]["messages"]
    injected = [m for m in sent if m["role"] == "user"
                and "USER INTERJECTION" in m["content"]]
    assert len(injected) == 1
    assert "prefer pathlib over os.path" in injected[0]["content"]
    assert ui.drain_messages() == []  # consumed by the executor session


def test_default_session_does_not_drain_messages(scripted_llm, state):
    state.pending_msgs.append("meant for the executor")
    session = Session("m", SYS, None)  # reviewer/subagent default
    scripted_llm.queue_text("verdict")
    session.send("review this")
    assert state.pending_msgs == ["meant for the executor"]
    assert not any("USER INTERJECTION" in str(m)
                   for m in scripted_llm.payloads[0]["messages"])


# ─── quit mid-run still finalizes ───────────────────────────────────

def test_quit_mid_run_still_writes_artifacts(scripted_llm, ws, log, monkeypatch):
    from harness.config import settings
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)

    calls = {"n": 0}

    def fake_poll():
        calls["n"] += 1
        if calls["n"] >= 2:  # first poll passes, quit inside the tool loop
            raise ui.QuitRequested()

    monkeypatch.setattr(ui, "poll_controls", fake_poll)

    passed = run_mod.main("m", "goal", "goal", ws, log, max_attempts=3)

    assert passed is False
    import json
    history = json.load(open(os.path.join(ws.run_dir, "attempt_history.json")))
    assert len(history) == 1
    assert history[0]["verdict"] == "aborted by user (q)"
    assert history[0]["passed"] is False
    assert os.path.exists(os.path.join(ws.run_dir, "report.html"))


def test_execute_tool_call_propagates_quit(ws, monkeypatch):
    # [q] during a subagent raises from the child's poll_controls; the
    # blanket except in execute_tool_call must not turn it into a string
    from harness.tools import execute_tool_call
    monkeypatch.setitem(tools_mod.tools, "read_file",
                        lambda **kw: (_ for _ in ()).throw(ui.QuitRequested()))
    with pytest.raises(ui.QuitRequested):
        execute_tool_call("read_file", {"name": "a.txt"})


def test_subagent_header_completes_after_deque_eviction(monkeypatch):
    from tests.test_ui_dashboard import _bare_dashboard
    dash = _bare_dashboard()
    monkeypatch.setattr(ui, "_dash", dash)
    ui.tool("spawn_subagent", {"task": "big job"})
    ui.subagent_start("worker", "big job")
    header = dash.tool_history[0]
    for i in range(12):  # evicts the header from the maxlen-10 visible deque
        ui.tool("read_file", {"name": f"f{i}.txt"})
        ui.tool_result("ok")
    assert header not in dash.tool_rows
    ui.subagent_end()
    ui.tool_result("subagent summary")  # parent's spawn result
    assert header.status == "done" and header.duration is not None
    ui._subagent_depth = 0


# ─── reader thread lifecycle ────────────────────────────────────────

def test_input_thread_feeds_keys(state):
    src = FakeSource("mhi\r")
    t = ui._InputThread(src, state)
    t.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if ui.drain_messages() == ["hi"]:
            break
        time.sleep(0.02)
    else:
        pytest.fail("thread never delivered the typed message")
    t.stop()
    assert not t.is_alive()


def test_input_thread_park_stops_consumption(state):
    src = FakeSource()
    t = ui._InputThread(src, state)
    t.start()
    t.park()                      # returns only after the thread acks
    src.q.put("q")                # arrives while parked
    time.sleep(0.15)
    assert not state.quit_requested  # not consumed while parked
    t.unpark()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not state.quit_requested:
        time.sleep(0.02)
    assert state.quit_requested   # consumed after unpark
    t.stop()


# ─── transcript buffer (unchanged behavior) ─────────────────────────

def _bare_dashboard():
    d = object.__new__(ui._Dashboard)  # skip __init__: no Live/Console needed
    d.stream_buf = ""
    d.stream_thinking = ""
    d.pulse = deque(maxlen=12)
    d._transcript = deque()
    d._transcript_len = 0
    d._transcript_dropped = 0
    d._lock = threading.Lock()
    d.tool_rows = deque(maxlen=10)
    d.refresh = lambda: None
    return d


def test_transcript_accumulates_beyond_the_tail():
    d = _bare_dashboard()
    for i in range(100):
        d.stream_add(f"chunk-{i:03d} ", thinking=False)
    assert len(d.stream_buf) <= 1200  # the live tail stays capped
    text = d.transcript_text()
    assert "chunk-000" in text and "chunk-099" in text


def test_transcript_capped_at_200k_with_drop_marker():
    d = _bare_dashboard()
    for _ in range(60):
        d.stream_add("x" * 5000, thinking=True)  # 300 KB total
    assert d._transcript_len <= 200_000
    assert d.transcript_text().startswith("[…")
    assert "chars dropped" in d.transcript_text()


def test_transcript_reset():
    d = _bare_dashboard()
    d.stream_add("some output", thinking=False)
    d.transcript_reset()
    assert d.transcript_text() == ""
    assert d._transcript_dropped == 0


# ─── the real KeyReader (pty-backed) ────────────────────────────────

def _stable_attrs(fd):
    """tcgetattr with kernel-transient lflag status bits masked out (PENDIN
    gets set on the pty when toggling canonical mode — it isn't ours)."""
    import termios
    attrs = termios.tcgetattr(fd)
    transient = getattr(termios, "PENDIN", 0) | getattr(termios, "FLUSHO", 0)
    attrs[3] &= ~transient
    return attrs


@pytest.mark.skipif(not HAVE_TERMIOS, reason="termios unavailable")
def test_keyreader_restores_termios():
    import pty

    master, slave = pty.openpty()
    try:
        before = _stable_attrs(slave)
        kr = KeyReader(fd=slave)
        kr.start()
        assert kr.active
        assert _stable_attrs(slave) != before  # cbreak actually engaged
        os.write(master, b"p")
        assert kr.poll() == "p"
        assert kr.poll() is None  # buffer empty, never blocks
        kr.stop()
        kr.stop()  # idempotent
        assert not kr.active
        assert _stable_attrs(slave) == before
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not HAVE_TERMIOS, reason="termios unavailable")
def test_keyreader_suspend_restores_cbreak():
    import pty

    master, slave = pty.openpty()
    try:
        kr = KeyReader(fd=slave)
        kr.start()
        in_cbreak = _stable_attrs(slave)
        with kr.suspend():
            assert _stable_attrs(slave) != in_cbreak  # cooked for input()
        assert _stable_attrs(slave) == in_cbreak
        kr.stop()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not HAVE_TERMIOS, reason="termios unavailable")
def test_read_token_printable_esc_and_arrows():
    import pty

    master, slave = pty.openpty()
    try:
        kr = KeyReader(fd=slave)
        kr.start()
        os.write(master, b"a")
        assert kr.read_token(timeout=0.5) == "a"
        os.write(master, b"\x1b")            # lone Esc
        assert kr.read_token(timeout=0.5) == "\x1b"
        os.write(master, b"\x1b[A")          # up arrow: swallowed
        assert kr.read_token(timeout=0.5) is None
        os.write(master, b"b")               # stream intact afterwards
        assert kr.read_token(timeout=0.5) == "b"
        assert kr.read_token(timeout=0.05) is None  # timeout → None
        kr.stop()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not HAVE_TERMIOS, reason="termios unavailable")
def test_read_token_survives_hangup_without_spinning():
    import pty

    master, slave = pty.openpty()
    kr = KeyReader(fd=slave)
    kr.start()
    os.close(master)  # hangup: select reports readable, read gives EOF/EIO
    t0 = time.monotonic()
    assert kr.read_token(timeout=0.1) is None  # no exception
    assert kr._eof
    assert kr.read_token(timeout=0.1) is None  # emulates the timeout...
    assert time.monotonic() - t0 >= 0.1        # ...instead of spinning
    kr.stop()
    os.close(slave)


def test_keyreader_noop_on_non_tty(tmp_path):
    f = open(tmp_path / "not-a-tty", "w")
    try:
        kr = KeyReader(fd=f.fileno())
        kr.start()
        assert not kr.active
        assert kr.poll() is None
        assert kr.read_token(timeout=0.01) is None
        kr.stop()  # safe when never started
    finally:
        f.close()
