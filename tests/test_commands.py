"""Tests for custom prompt commands (commands/*.md)."""

import sys

import pytest

from harness import cli, commands
from harness.commands import Command, load_command, parse_frontmatter, render_goal


def test_frontmatter_parsed():
    meta, body = parse_frontmatter(
        "---\ndescription: fix stuff\nem: coder:7b\n# comment\n\n"
        "url: http://x:11434\n---\nDo the thing. {args}")
    assert meta == {"description": "fix stuff", "em": "coder:7b",
                    "url": "http://x:11434"}  # colon values survive
    assert body == "Do the thing. {args}"


def test_no_fence_is_all_body():
    meta, body = parse_frontmatter("just a goal template")
    assert meta == {} and body == "just a goal template"


def test_unterminated_fence_is_all_body():
    meta, body = parse_frontmatter("---\ndescription: oops\nno closing fence")
    assert meta == {}
    assert "no closing fence" in body


def test_render_goal_args_and_braces():
    cmd = Command(name="x", body="fix {args} in {foo} and dict {'a': 1}")
    out = render_goal(cmd, ["the", "tests"])
    assert out == "fix the tests in {foo} and dict {'a': 1}"
    assert render_goal(cmd, []) == "fix  in {foo} and dict {'a': 1}"


def test_load_command_missing_lists_available(tmp_path):
    (tmp_path / "one.md").write_text("body")
    (tmp_path / "two.md").write_text("body")
    with pytest.raises(SystemExit) as e:
        load_command("nope", commands_dir=str(tmp_path))
    assert "one, two" in str(e.value)


def test_load_command_strips_description(tmp_path):
    (tmp_path / "f.md").write_text("---\ndescription: d\nattempts: 3\n---\nbody")
    cmd = load_command("f", commands_dir=str(tmp_path))
    assert cmd.description == "d"
    assert cmd.defaults == {"attempts": "3"}
    assert cmd.body == "body"


def _run_cli_parse(monkeypatch, tmp_path, argv):
    (tmp_path / "fix.md").write_text(
        "---\ndescription: fix\nem: coder\nattempts: 3\n---\nfix tests {args}")
    monkeypatch.setattr(commands, "COMMANDS_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", argv)
    args, parser = cli.parse_args()
    cli._apply_command(args, parser)
    return args


def test_command_sets_goal_and_defaults(monkeypatch, tmp_path):
    args = _run_cli_parse(monkeypatch, tmp_path,
                          ["prog", "-c", "fix", "focus", "on", "x"])
    assert args.goal == "fix tests focus on x"
    assert args.executor_model == "coder"
    assert args.attempts == 3  # int coercion from frontmatter string


def test_explicit_flags_beat_frontmatter(monkeypatch, tmp_path):
    args = _run_cli_parse(monkeypatch, tmp_path,
                          ["prog", "--attempts", "7", "-c", "fix"])
    assert args.attempts == 7
    assert args.executor_model == "coder"  # untouched default still applied
