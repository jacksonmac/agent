"""Tests for --sandbox: execute tools routed through a per-run Docker
container, with docker itself faked — no daemon needed."""

import subprocess

import pytest

from harness.config import settings
from harness.tools import execute
from harness.workspace import Workspace


class FakeDocker:
    """Records every argv; scripts docker run / docker exec results."""

    def __init__(self, run_fails=False, docker_missing=False):
        self.calls: list[list[str]] = []
        self.run_fails = run_fails
        self.docker_missing = docker_missing

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "run"] and self.docker_missing:
            raise FileNotFoundError("docker")
        rc = 1 if (argv[:2] == ["docker", "run"] and self.run_fails) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="out", stderr="err")


@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path / "run"))


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "sandbox", True)
    execute._containers.clear()
    fake = FakeDocker()
    monkeypatch.setattr(execute.subprocess, "run", fake)
    yield fake
    execute._containers.clear()


def _exec_calls(fake):
    return [c for c in fake.calls if c[:2] == ["docker", "exec"]]


def test_run_python_goes_through_docker_exec(sandbox, ws):
    out = execute.run_python(ws, "print(1)")
    assert "exit code: 0" in out
    run_call = sandbox.calls[0]
    assert run_call[:4] == ["docker", "run", "-d", "--rm"]
    assert f"{ws.root}:/ws" in run_call
    assert settings.sandbox_image in run_call
    assert _exec_calls(sandbox)[0][3:] == ["python3", "-c", "print(1)"]


def test_container_reused_across_calls(sandbox, ws):
    execute.run_python(ws, "1")
    execute.run_python(ws, "2")
    execute.run_shell(ws, "ls")
    assert sum(1 for c in sandbox.calls if c[:2] == ["docker", "run"]) == 1
    assert len(_exec_calls(sandbox)) == 3


def test_each_workspace_gets_its_own_container(sandbox, ws, tmp_path):
    other = Workspace(str(tmp_path / "other"))
    execute.run_python(ws, "1")
    execute.run_python(other, "1")
    runs = [c for c in sandbox.calls if c[:2] == ["docker", "run"]]
    assert len(runs) == 2
    names = {_exec_calls(sandbox)[0][2], _exec_calls(sandbox)[1][2]}
    assert len(names) == 2


def test_sandbox_shell_lifts_the_allowlist(sandbox, ws):
    out = execute.run_shell(ws, "pip install requests && curl example.com | head")
    assert "[ERROR]" not in out
    assert _exec_calls(sandbox)[0][3:] == \
        ["sh", "-c", "pip install requests && curl example.com | head"]


def test_run_script_uses_workspace_relative_path(sandbox, ws, tmp_path):
    (tmp_path / "run" / "workspace" / "tool.py").write_text("print('x')")
    execute.run_script(ws, "tool.py", args=["--fast"])
    assert _exec_calls(sandbox)[0][3:] == ["python3", "tool.py", "--fast"]


def test_docker_missing_reports_error(monkeypatch, ws):
    monkeypatch.setattr(settings, "sandbox", True)
    execute._containers.clear()
    monkeypatch.setattr(execute.subprocess, "run",
                        FakeDocker(docker_missing=True))
    out = execute.run_python(ws, "1")
    assert out.startswith("[ERROR] --sandbox: docker is not installed")


def test_container_start_failure_reports_error(monkeypatch, ws):
    monkeypatch.setattr(settings, "sandbox", True)
    execute._containers.clear()
    monkeypatch.setattr(execute.subprocess, "run", FakeDocker(run_fails=True))
    out = execute.run_shell(ws, "ls")
    assert out.startswith("[ERROR] --sandbox: could not start the container")
    assert "err" in out  # docker's stderr is surfaced


# ── non-sandbox behavior is unchanged ────────────────────────────────

def test_allowlist_still_enforced_outside_sandbox(monkeypatch, ws):
    monkeypatch.setattr(settings, "sandbox", False)
    assert "not allowed" in execute.run_shell(ws, "curl example.com")
    assert "metacharacters" in execute.run_shell(ws, "echo hi | cat")


def test_direct_execution_untouched(monkeypatch, ws):
    monkeypatch.setattr(settings, "sandbox", False)
    out = execute.run_python(ws, "print('direct')")
    assert "direct" in out and "exit code: 0" in out
