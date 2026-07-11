"""Central settings for the harness.

One module-level `settings` instance; the CLI mutates it once at startup.
Everything that used to be a mutated module global (URL, NUM_CTX,
FULL_CONTEXT, ...) lives here now.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# repo root — tools are rooted here until the Workspace lands (Phase 2)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class RoleOptions:
    """Per-role sampling options sent to Ollama."""
    temperature: float | None = None  # None = model default
    think: bool = True


@dataclass
class Settings:
    url: str = "http://192.168.1.134:11434"
    model: str = "gemma4:26b"
    reviewer_model: str | None = None   # None = same as model
    executor_model: str | None = None   # None = same as model (covers plan/self-check too)
    goalsmith_model: str | None = None  # None = same as model

    request_timeout: int = 1600   # seconds per LLM call (used to be 1600 only doing this becuase of how big the model is)
    num_ctx: int = 10000         # requested context window (used to be 16500)
    full_context: bool = False   # --full-context: disable ALL trimming

    # context budget knobs
    chars_per_token: int = 3     # conservative estimate for budgeting
    tool_result_max: int = 4_000
    retry_prev_max: int = 6_000
    page_text_max: int = 6_000
    compact_keep_last: int = 6

    # executor stays creative-ish; the reviewer should be near-deterministic
    executor: RoleOptions = field(default_factory=lambda: RoleOptions(temperature=0.7))
    reviewer: RoleOptions = field(default_factory=lambda: RoleOptions(temperature=0.1))
    goalsmith: RoleOptions = field(default_factory=lambda: RoleOptions(temperature=0.3))
    reviewer_tools: bool = True  # --no-reviewer-tools disables

    subagent_max_rounds: int = 8  # tool rounds a spawn_subagent child gets
    memory: bool = True           # write an AGENT.md lessons note after each run
    skills: bool = True           # advertise skills/ + honor load_skill (--no-skills)
    skill_body_max: int = 8_000   # cap on a loaded SKILL.md body (AGENT.md-sized)
    stream: bool = True           # stream tokens live from Ollama (--no-stream)
    notify: bool = True           # bell/desktop notification at run end (--no-notify)

    # attempt-quality phases (see run.py); --no-plan / --no-self-check disable
    plan_first: bool = True   # attempt 1 opens with a no-tool planning turn
    self_check: bool = True   # verify-and-fix turn before each review


settings = Settings()
