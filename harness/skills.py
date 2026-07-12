"""Model-invoked skills: skills/<name>/SKILL.md files the executor can load
on demand (progressive disclosure, like Claude Code skills).

A skill is a directory under skills/ containing a SKILL.md:

    ---
    name: pytest-debugging
    description: diagnose and fix failing pytest suites methodically
    ---
    Full markdown instructions the model should follow...

A compact index (name + description per skill) is appended to the executor
and subagent system prompts; the model calls the load_skill tool to pull in
a skill's full body when the description matches the work at hand. The
reviewer and goalsmith never see skills. --no-skills disables everything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import runlog
from .commands import parse_frontmatter
from .config import HERE, settings
from .llm import truncate_middle

SKILLS_DIR = os.path.join(HERE, "skills")


@dataclass
class Skill:
    name: str
    description: str = ""
    body: str = ""
    always: bool = False  # frontmatter `always: true` — body is injected
                          # into the system prompt instead of waiting for
                          # the model to call load_skill


def discover(skills_dir: str | None = None) -> list[Skill]:
    """All valid skills, sorted by name. A skill is a subdirectory holding a
    readable SKILL.md; frontmatter `name` falls back to the directory name.
    A missing or unreadable skills dir is simply no skills."""
    skills_dir = skills_dir or SKILLS_DIR
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError:
        return []
    skills = []
    for entry in entries:
        path = os.path.join(skills_dir, entry, "SKILL.md")
        try:
            with open(path) as f:
                meta, body = parse_frontmatter(f.read())
        except OSError:
            continue  # not a skill dir, or unreadable — skip quietly
        name = meta.get("name", "").strip() or entry
        always = meta.get("always", "").strip().lower() in ("true", "yes", "1")
        skills.append(Skill(name=name, description=meta.get("description", ""),
                            body=body, always=always))
    return sorted(skills, key=lambda s: s.name)


def index_text(skills: list[Skill]) -> str:
    """The system-prompt block advertising on-demand skills, or '' if none.
    Leading newlines so callers can plain-concatenate (like _load_agent_md)."""
    skills = [s for s in skills if not s.always]
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return ("\n\nSKILLS — reusable expert instructions you can load on demand. "
            "When a skill's description matches the work at hand, call "
            "load_skill(name) and FOLLOW the loaded instructions:\n" + lines)


def _always_text(skills: list[Skill]) -> str:
    """always: true skills are injected in full — the model never has to
    decide to load a standing preference (small models won't)."""
    blocks = [f"\n\nSKILL (always applies): {s.name}\n"
              + truncate_middle(s.body, settings.skill_body_max)
              for s in skills if s.always]
    return "".join(blocks)


def system_prompt_block(skills_dir: str | None = None) -> str:
    """Single gate for prompt injection: '' when disabled or no skills."""
    if not settings.skills:
        return ""
    skills = discover(skills_dir)
    return index_text(skills) + _always_text(skills)


def load_skill(name: str) -> str:
    """The load_skill tool: return a skill's full instructions (capped)."""
    if not settings.skills:
        return "[ERROR] skills are disabled for this run (--no-skills)"
    name = str(name or "").strip()
    skills = discover()
    for s in skills:
        if s.name == name:
            body = truncate_middle(s.body, settings.skill_body_max)
            runlog.log_event("skill", name=s.name, chars=len(body))
            return f"SKILL: {s.name}\n\n{body}"
    names = ", ".join(s.name for s in skills) or "none"
    return f"[ERROR] no such skill: {name} (available: {names})"
