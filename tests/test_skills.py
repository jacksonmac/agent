"""Tests for model-invoked skills (skills/<name>/SKILL.md + load_skill)."""

import pytest

from harness import permissions, review, skills
from harness.config import settings
from harness.llm import Session
from harness.skills import Skill, discover, index_text, load_skill, system_prompt_block


def _write_skill(root, dirname, text):
    d = root / dirname
    d.mkdir()
    (d / "SKILL.md").write_text(text)


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """A temp skills dir with two skills, wired in as SKILLS_DIR."""
    _write_skill(tmp_path, "alpha",
                 "---\nname: alpha\ndescription: does alpha things\n---\nAlpha body.")
    _write_skill(tmp_path, "beta",
                 "---\nname: beta\ndescription: does beta things\n---\nBeta body.")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "skills", True)
    return tmp_path


# ── discovery / parsing ──────────────────────────────────────────────

def test_discover_parses_frontmatter(skills_dir):
    found = discover()
    assert [s.name for s in found] == ["alpha", "beta"]
    assert found[0].description == "does alpha things"
    assert found[0].body == "Alpha body."


def test_discover_name_falls_back_to_dirname(tmp_path, monkeypatch):
    _write_skill(tmp_path, "no-name", "---\ndescription: d\n---\nbody")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    assert [s.name for s in discover()] == ["no-name"]


def test_discover_skips_dirs_without_skill_md(tmp_path, monkeypatch):
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "stray.md").write_text("not a skill either")
    _write_skill(tmp_path, "real", "---\nname: real\n---\nbody")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    assert [s.name for s in discover()] == ["real"]


def test_discover_missing_dir_is_empty(tmp_path):
    assert discover(str(tmp_path / "nowhere")) == []


def test_unterminated_frontmatter_whole_file_is_body(tmp_path, monkeypatch):
    _write_skill(tmp_path, "broken", "---\nname: x\nno closing fence")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    (s,) = discover()
    assert s.name == "broken"  # meta lost -> dirname fallback
    assert "no closing fence" in s.body


# ── index / system prompt block ──────────────────────────────────────

def test_index_text_lists_all_and_empty_is_blank(skills_dir):
    text = index_text(discover())
    assert "- alpha: does alpha things" in text
    assert "- beta: does beta things" in text
    assert "load_skill" in text
    assert index_text([]) == ""


def test_system_prompt_block_respects_settings(skills_dir, monkeypatch):
    assert "alpha" in system_prompt_block()
    monkeypatch.setattr(settings, "skills", False)
    assert system_prompt_block() == ""


def test_always_skill_body_injected_not_indexed(tmp_path, monkeypatch):
    _write_skill(tmp_path, "greet",
                 "---\nname: greet\ndescription: greet them\nalways: true\n---\n"
                 "Start every answer with Hi Jackson.")
    _write_skill(tmp_path, "ondemand",
                 "---\nname: ondemand\ndescription: sometimes useful\n---\nBody.")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "skills", True)
    block = system_prompt_block()
    # the always skill arrives in full, outside the on-demand index
    assert "SKILL (always applies): greet" in block
    assert "Start every answer with Hi Jackson." in block
    assert "- greet:" not in block
    # the on-demand skill is still just an index line
    assert "- ondemand: sometimes useful" in block
    assert "Body." not in block
    # and load_skill still works for an always skill
    assert "Hi Jackson" in load_skill("greet")


def test_only_always_skills_still_injects(tmp_path, monkeypatch):
    _write_skill(tmp_path, "greet", "---\nalways: yes\n---\nGreeting body.")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "skills", True)
    block = system_prompt_block()
    assert "Greeting body." in block
    assert "load_skill" not in block  # no on-demand skills -> no index


# ── load_skill tool ──────────────────────────────────────────────────

def test_load_skill_happy(skills_dir):
    out = load_skill("alpha")
    assert out.startswith("SKILL: alpha")
    assert "Alpha body." in out


def test_load_skill_missing_lists_available(skills_dir):
    out = load_skill("nope")
    assert out.startswith("[ERROR] no such skill: nope")
    assert "alpha, beta" in out


def test_load_skill_disabled(skills_dir, monkeypatch):
    monkeypatch.setattr(settings, "skills", False)
    assert "--no-skills" in load_skill("alpha")


def test_load_skill_oversized_body_truncated(tmp_path, monkeypatch):
    _write_skill(tmp_path, "big",
                 "---\nname: big\n---\n" + "x" * (settings.skill_body_max + 5_000))
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "skills", True)
    out = load_skill("big")
    assert "chars truncated" in out
    assert len(out) <= settings.skill_body_max + 100  # header + marker slack


# ── gating / role visibility ─────────────────────────────────────────

def test_load_skill_not_gated(skills_dir):
    permissions.configure(yolo=False)  # non-TTY: a gated tool would auto-deny
    assert permissions.check("load_skill", {"name": "alpha"}) is None


def test_reviewer_schemas_exclude_load_skill():
    names = [s["function"]["name"] for s in review.reviewer_tool_schemas()]
    assert "load_skill" not in names


def test_subagent_gets_index_and_tool(skills_dir, scripted_llm):
    from harness.tools.subagent import spawn_subagent
    scripted_llm.queue_text("done")
    spawn_subagent("some subtask")
    payload = scripted_llm.payloads[0]
    system = payload["messages"][0]["content"]
    assert "SKILLS" in system and "alpha" in system
    assert "load_skill" in [t["function"]["name"] for t in payload["tools"]]


# ── end-to-end through a Session (cap bypass) ────────────────────────

def test_session_load_skill_end_to_end_and_cap_bypass(tmp_path, monkeypatch,
                                                      scripted_llm):
    body = "y" * 6_000  # between tool_result_max (4,000) and skill_body_max (8,000)
    _write_skill(tmp_path, "deep", "---\nname: deep\n---\n" + body)
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "skills", True)

    from harness.tools import TOOL_SCHEMAS
    session = Session("m", "sys", TOOL_SCHEMAS)
    scripted_llm.queue_tool_call("load_skill", {"name": "deep"})
    scripted_llm.queue_text("done")
    assert session.send("go") == "done"

    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert content.startswith("SKILL: deep")
    assert len(content) > 4_000            # survived the generic tool cap
    assert "chars truncated" not in content  # and was not middle-truncated
