"""Custom prompt commands: reusable goal templates in commands/*.md.

A command file is markdown with optional frontmatter (like a slash command):

    ---
    description: run pytest and fix failures
    em: qwen2.5-coder:7b
    attempts: 3
    ---
    Run the test suite with pytest. Fix every failing test. {args}

Frontmatter keys map onto CLI flags as defaults; flags passed explicitly on
the command line still win. {args} in the body is replaced by whatever extra
words follow the command name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import HERE

COMMANDS_DIR = os.path.join(HERE, "commands")


@dataclass
class Command:
    name: str
    description: str = ""
    defaults: dict = field(default_factory=dict)  # frontmatter keys minus description
    body: str = ""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body). Files without a leading --- fence are all body.
    key: value pairs split on the FIRST colon, so values may contain colons."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    # unterminated fence: treat the whole file as body to avoid eating it
    return {}, text.strip()


def available(commands_dir: str | None = None) -> list[str]:
    commands_dir = commands_dir or COMMANDS_DIR
    try:
        return sorted(f[:-3] for f in os.listdir(commands_dir) if f.endswith(".md"))
    except OSError:
        return []


def load_command(name: str, commands_dir: str | None = None) -> Command:
    commands_dir = commands_dir or COMMANDS_DIR  # resolved at call time (testable)
    path = os.path.join(commands_dir, name + ".md")
    if not os.path.isfile(path):
        names = ", ".join(available(commands_dir)) or "none"
        raise SystemExit(f"[ERROR] no such command: {name} (available: {names})")
    with open(path) as f:
        meta, body = parse_frontmatter(f.read())
    description = meta.pop("description", "")
    return Command(name=name, description=description, defaults=meta, body=body)


def render_goal(cmd: Command, extra_args: list[str]) -> str:
    # str.replace, not .format — command bodies may legitimately contain braces
    return cmd.body.replace("{args}", " ".join(extra_args)).strip()
