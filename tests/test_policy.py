"""Tests for policy.json — the guardrails as data.

Two properties carry the design and are tested first: an absent policy file
behaves exactly like the constants it replaced, and a policy is a ceiling
(it can refuse --yolo, not merely default it off).
"""

import json
import os
import sys

import pytest

from harness import cli, permissions, policy
from harness import run as run_mod
from harness import tools as tools_mod
from harness.config import HERE, settings
from harness.review import Verdict
from harness.runlog import RunLog
from harness.tools import execute_tool_call
from harness.tools import web as web_mod
from harness.workspace import Workspace


@pytest.fixture(autouse=True)
def _restore_policy():
    """Every test here fiddles with module state that the rest of the suite
    reads; put it back, schemas included."""
    yield
    policy.reset()
    tools_mod._apply_policy_to_schemas()


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "run"))
    tools_mod.configure(w)
    return w


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)


def _args(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["agent.py", "-g", "goal", *argv])
    return cli.parse_args()


# ─── defaults are the old constants ─────────────────────────────────

def test_missing_file_is_the_default_policy(tmp_path):
    pol = policy.load(str(tmp_path / "nope.json"))
    assert pol == policy.Policy()
    assert pol.name == "default"


def test_defaults_match_the_constants_they_replaced():
    """The values that used to live in execute.py, permissions.py, llm.py
    and cli.py. If this test needs changing, behaviour changed with it."""
    pol = policy.Policy()
    assert pol.shell.allowed == ("pip", "pip3", "python3", "pytest",
                                 "ls", "mkdir", "cat", "echo")
    assert set(pol.execution.require_approval) == {"run_shell", "run_python",
                                                   "run_script"}
    assert pol.network.block_private_addresses is True
    assert pol.limits.max_tool_rounds == 15
    assert pol.limits.subagent_max_rounds == 20
    assert pol.limits.max_attempts == 5


def test_shipped_policy_json_is_a_no_op():
    """The committed policy.json must resolve to the built-in defaults —
    otherwise installing this repo silently changes how it behaves."""
    shipped = policy.load(os.path.join(HERE, "policy.json"))
    assert shipped.as_dict() == policy.Policy(name="default").as_dict()


def test_partial_policy_keeps_defaults_for_omitted_sections(tmp_path):
    path = _write(tmp_path, "p.json", {"name": "half", "shell": {"allowed": ["ls"]}})
    pol = policy.load(path)
    assert pol.shell.allowed == ("ls",)
    assert pol.limits == policy.Limits()          # untouched
    assert pol.execution == policy.Execution()    # untouched


def test_shipped_example_policies_parse():
    for name in ("policy.strict.json", "policy.open.json"):
        pol = policy.load(os.path.join(HERE, name))
        assert pol.name == name.split(".")[1]


# ─── validation is loud ─────────────────────────────────────────────

def test_unknown_top_level_key_names_the_key(tmp_path):
    path = _write(tmp_path, "p.json", {"name": "x", "netwrok": {}})
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "netwrok" in str(e.value)
    assert "valid keys are" in str(e.value)


def test_unknown_section_key_names_the_key(tmp_path):
    path = _write(tmp_path, "p.json", {"network": {"aloud_domains": ["*"]}})
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "aloud_domains" in str(e.value)
    assert "p.json.network" in str(e.value)


def test_bad_enum_lists_the_valid_values(tmp_path):
    path = _write(tmp_path, "p.json", {"shell": {"mode": "allowlst"}})
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "allowlist" in str(e.value) and "any" in str(e.value)


def test_bad_sandbox_mode_raises(tmp_path):
    path = _write(tmp_path, "p.json", {"execution": {"sandbox": "maybe"}})
    with pytest.raises(policy.PolicyError):
        policy.load(path)


def test_wrong_type_raises(tmp_path):
    path = _write(tmp_path, "p.json", {"limits": {"max_attempts": "three"}})
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "max_attempts" in str(e.value)


def test_zero_limit_raises(tmp_path):
    path = _write(tmp_path, "p.json", {"limits": {"max_tool_rounds": 0}})
    with pytest.raises(policy.PolicyError):
        policy.load(path)


def test_malformed_json_raises(tmp_path):
    path = _write(tmp_path, "p.json", "{not json")
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "not valid JSON" in str(e.value)


def test_ungateable_tool_in_require_approval_raises(tmp_path):
    """A typo here would silently un-gate the tool it meant to gate."""
    path = _write(tmp_path, "p.json",
                  {"execution": {"require_approval": ["run_shel"]}})
    with pytest.raises(policy.PolicyError) as e:
        policy.load(path)
    assert "run_shel" in str(e.value)


# ─── shell allowlist ────────────────────────────────────────────────

def test_strict_policy_denies_what_default_allows(ws):
    call = ("run_shell", {"command": "pip --version"})
    assert not execute_tool_call(*call).startswith("[ERROR] command")

    policy.configure(policy.load(os.path.join(HERE, "policy.strict.json")))
    out = execute_tool_call(*call)
    assert out.startswith("[ERROR] command 'pip' is not allowed")
    assert "no shell commands at all" in out


def test_policy_allowlist_is_quoted_in_the_error(ws):
    policy.configure(policy.Policy(shell=policy.Shell(allowed=("ls",))))
    out = execute_tool_call("run_shell", {"command": "curl example.com"})
    assert "Allowed commands: ls" in out


def test_shell_mode_any_permits_chaining(ws):
    policy.configure(policy.Policy(shell=policy.Shell(mode="any")))
    out = execute_tool_call("run_shell", {"command": "echo one; echo two"})
    assert "one" in out and "two" in out
    assert "metacharacters" not in out


# ─── the permission gate ────────────────────────────────────────────

def test_require_approval_drives_the_gate(monkeypatch):
    monkeypatch.setattr(permissions, "_interactive", lambda: False)
    policy.configure(policy.Policy(
        execution=policy.Execution(require_approval=("run_python",))))
    permissions.configure(yolo=False)
    assert permissions.check("run_python", {"code": "1"}).startswith("[ERROR]")
    assert permissions.check("run_shell", {"command": "ls"}) is None
    permissions.configure(yolo=True)


def test_allow_yolo_false_keeps_the_gate_closed(monkeypatch):
    monkeypatch.setattr(permissions, "_interactive", lambda: False)
    policy.configure(policy.Policy(execution=policy.Execution(allow_yolo=False)))
    permissions.configure(yolo=True)   # asked for; policy says no
    assert permissions.is_yolo() is False
    assert permissions.check("run_shell", {"command": "ls"}).startswith("[ERROR]")
    policy.reset()
    permissions.configure(yolo=True)


def test_permission_event_records_the_command(monkeypatch, ws):
    """'run_shell was denied' doesn't tell a later reader what was denied."""
    from harness import runlog
    monkeypatch.setattr(permissions, "_interactive", lambda: False)
    permissions.configure(yolo=False)
    log = RunLog(ws.run_dir)
    monkeypatch.setattr(runlog, "current", log)
    permissions.check("run_shell", {"command": "rm -rf /tmp/x"})
    permissions.configure(yolo=True)

    with open(log.events_path) as f:
        events = [json.loads(line) for line in f]
    perm = [e for e in events if e["event"] == "permission"][0]
    assert perm["decision"] == "auto-deny"
    assert "rm -rf /tmp/x" in perm["detail"]


# ─── network ────────────────────────────────────────────────────────

def test_allowed_domains_refuses_before_fetching(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("fetch_page hit the network on a blocked domain")
    monkeypatch.setattr(web_mod.requests, "get", boom)
    policy.configure(policy.Policy(
        network=policy.Network(allowed_domains=("example.com",))))
    out = web_mod.fetch_page("https://evil.test/page")
    assert out.startswith("[ERROR] policy 'default' does not allow")
    assert "evil.test" in out


def test_allowed_domains_covers_subdomains():
    pol = policy.Policy(network=policy.Network(allowed_domains=("example.com",)))
    assert policy.domain_allowed("example.com", pol)
    assert policy.domain_allowed("docs.example.com", pol)
    assert not policy.domain_allowed("notexample.com", pol)
    assert not policy.domain_allowed("evil.test", pol)


def test_wildcard_allows_everything():
    assert policy.domain_allowed("anything.test", policy.Policy())


def test_disabled_web_tools_refuse_and_are_unadvertised(ws):
    policy.configure(policy.Policy(
        name="offline",
        network=policy.Network(web_search=False, fetch_page=False)))
    tools_mod.configure(ws)

    advertised = {s["function"]["name"] for s in tools_mod.TOOL_SCHEMAS}
    assert "web_search" not in advertised and "fetch_page" not in advertised
    assert "write_file" in advertised          # everything else survives
    out = execute_tool_call("web_search", {"query": "x"})
    assert out == ("[ERROR] web_search is disabled by policy 'offline'. "
                   "Work without it.")


def test_schemas_come_back_when_the_policy_allows_them(ws):
    policy.configure(policy.Policy(network=policy.Network(fetch_page=False)))
    tools_mod.configure(ws)
    assert "fetch_page" not in {s["function"]["name"]
                                for s in tools_mod.TOOL_SCHEMAS}
    policy.reset()
    tools_mod.configure(ws)
    assert "fetch_page" in {s["function"]["name"] for s in tools_mod.TOOL_SCHEMAS}


# ─── the ceiling, applied in the CLI ────────────────────────────────

def test_yolo_is_refused_under_a_strict_policy(monkeypatch):
    args, parser = _args(monkeypatch, "--yolo")
    pol = policy.load(os.path.join(HERE, "policy.strict.json"))
    policy.configure(pol)
    with pytest.raises(SystemExit) as e:
        cli._enforce_policy(args, parser, pol)
    assert "does not allow --yolo" in str(e.value)


def test_sandbox_required_turns_it_on(monkeypatch):
    args, parser = _args(monkeypatch)
    monkeypatch.setattr(settings, "sandbox", False)
    pol = policy.Policy(execution=policy.Execution(sandbox="required"))
    cli._enforce_policy(args, parser, pol)
    assert settings.sandbox is True


def test_sandbox_forbidden_rejects_the_flag(monkeypatch):
    args, parser = _args(monkeypatch, "--sandbox")
    pol = policy.Policy(execution=policy.Execution(sandbox="forbidden"))
    with pytest.raises(SystemExit) as e:
        cli._enforce_policy(args, parser, pol)
    assert "forbids --sandbox" in str(e.value)


def test_max_attempts_clamps_an_explicit_flag(monkeypatch):
    warned = []
    monkeypatch.setattr(cli.ui, "warn", warned.append)
    args, parser = _args(monkeypatch, "--attempts", "9")
    cli._enforce_policy(args, parser,
                        policy.Policy(limits=policy.Limits(max_attempts=3)))
    assert args.attempts == 3
    assert len(warned) == 1 and "caps attempts at 3" in warned[0]


def test_max_attempts_clamps_the_default_quietly(monkeypatch):
    warned = []
    monkeypatch.setattr(cli.ui, "warn", warned.append)
    args, parser = _args(monkeypatch)          # --attempts left at its default
    cli._enforce_policy(args, parser,
                        policy.Policy(limits=policy.Limits(max_attempts=3)))
    assert args.attempts == 3
    assert warned == []


def test_lower_attempts_than_the_ceiling_pass_through(monkeypatch):
    args, parser = _args(monkeypatch, "--attempts", "2")
    cli._enforce_policy(args, parser,
                        policy.Policy(limits=policy.Limits(max_attempts=5)))
    assert args.attempts == 2


def test_subagent_rounds_come_from_the_policy(monkeypatch):
    args, parser = _args(monkeypatch)
    monkeypatch.setattr(settings, "subagent_max_rounds", 20)
    cli._enforce_policy(args, parser,
                        policy.Policy(limits=policy.Limits(subagent_max_rounds=8)))
    assert settings.subagent_max_rounds == 8


def test_max_tool_rounds_defaults_to_the_policy():
    from harness.llm import Session
    policy.configure(policy.Policy(limits=policy.Limits(max_tool_rounds=4)))
    assert Session("m", "sys", None).max_tool_rounds == 4
    assert Session("m", "sys", None, max_tool_rounds=99).max_tool_rounds == 99


# ─── the run log ────────────────────────────────────────────────────

def test_run_start_records_the_policy(scripted_llm, ws, monkeypatch):
    monkeypatch.setattr(settings, "self_check", False)
    monkeypatch.setattr(settings, "plan_first", False)
    monkeypatch.setattr(run_mod, "review",
                        lambda *a, **kw: Verdict(passed=True, feedback=""))
    pol = policy.Policy(name="strict-ish", shell=policy.Shell(allowed=()))
    policy.configure(pol)
    monkeypatch.setattr(settings, "policy", pol)

    scripted_llm.queue_tool_call("write_file", {"text": "x", "name": "a.txt"})
    scripted_llm.queue_text("done")
    log = RunLog(ws.run_dir)
    run_mod.main("m", "goal", "goal", ws, log, max_attempts=1)

    with open(log.events_path) as f:
        events = [json.loads(line) for line in f]
    start = [e for e in events if e["event"] == "run_start"][0]
    assert start["policy"]["name"] == "strict-ish"
    assert start["policy"]["shell"]["allowed"] == []
    assert start["yolo"] is True          # set by the suite-wide fixture
    assert start["sandbox"] is False
