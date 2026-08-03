"""Tool guardrails as data: what the agent is allowed to do, in one file.

The rules themselves are not new — a shell allowlist, a permission gate on
code execution, a refusal to fetch private addresses. They just used to be
module constants in three different files, which meant "what was this agent
allowed to do?" could only be answered by reading source, and no run artifact
recorded the answer. Now they live in a repo-root policy.json, the resolved
policy is written into the run_start event, and a non-engineer can read it.

    {"name": "default",
     "shell":     {"mode": "allowlist", "allowed": ["python3", ...]},
     "network":   {"web_search": true, "fetch_page": true,
                   "block_private_addresses": true, "allowed_domains": ["*"]},
     "execution": {"require_approval": ["run_shell", "run_python", "run_script"],
                   "allow_yolo": true, "sandbox": "optional"},
     "limits":    {"max_attempts": 5, "max_tool_rounds": 15,
                   "subagent_max_rounds": 20}}

Two properties this module exists to guarantee:

Defaults equal the old constants. A missing policy.json, or one that sets
only some keys, behaves exactly as the harness did before this file existed.
Nothing silently tightens or loosens because a key was left out.

A policy is a ceiling, not a suggestion. `allow_yolo: false` makes --yolo an
error rather than an override, and `sandbox: "required"` forces the container
on. That is what lets a finished run be *shown* to have been constrained,
instead of relying on what someone typed at the prompt.

Validation is deliberately strict and loud: an unknown key is an error, not a
shrug. A policy that reads strict but isn't — because "run_shel" was a typo,
or "aloud_domains" got quietly ignored — is worse than no policy at all.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field, fields

SHELL_MODES = ("allowlist", "any")
SANDBOX_MODES = ("optional", "required", "forbidden")
# the tools that have a permission gate to require; naming anything else in
# execution.require_approval is a typo, and a silent one if we let it pass
GATEABLE = ("run_shell", "run_python", "run_script")


class PolicyError(ValueError):
    """The policy file exists but is unusable — bad key, type, or value."""


@dataclass(frozen=True)
class Shell:
    """mode 'allowlist' permits only `allowed` as the first word of a command
    (and no shell metacharacters); 'any' hands over a real shell."""
    mode: str = "allowlist"
    allowed: tuple[str, ...] = ("pip", "pip3", "python3", "pytest",
                                "ls", "mkdir", "cat", "echo")


@dataclass(frozen=True)
class Network:
    web_search: bool = True
    fetch_page: bool = True
    block_private_addresses: bool = True
    allowed_domains: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class Execution:
    require_approval: tuple[str, ...] = GATEABLE
    allow_yolo: bool = True
    sandbox: str = "optional"


@dataclass(frozen=True)
class Limits:
    max_attempts: int = 5
    max_tool_rounds: int = 15
    subagent_max_rounds: int = 20


@dataclass(frozen=True)
class Policy:
    name: str = "default"
    shell: Shell = field(default_factory=Shell)
    network: Network = field(default_factory=Network)
    execution: Execution = field(default_factory=Execution)
    limits: Limits = field(default_factory=Limits)

    def as_dict(self) -> dict:
        """JSON-shaped, for the run_start event. Tuples serialize as arrays."""
        return {"name": self.name,
                "shell": {"mode": self.shell.mode,
                          "allowed": list(self.shell.allowed)},
                "network": {"web_search": self.network.web_search,
                            "fetch_page": self.network.fetch_page,
                            "block_private_addresses":
                                self.network.block_private_addresses,
                            "allowed_domains": list(self.network.allowed_domains)},
                "execution": {"require_approval": list(self.execution.require_approval),
                              "allow_yolo": self.execution.allow_yolo,
                              "sandbox": self.execution.sandbox},
                "limits": {"max_attempts": self.limits.max_attempts,
                           "max_tool_rounds": self.limits.max_tool_rounds,
                           "subagent_max_rounds": self.limits.subagent_max_rounds}}


# ── parsing ──────────────────────────────────────────────────────────

_SECTIONS = {"shell": Shell, "network": Network,
             "execution": Execution, "limits": Limits}


def _coerce(default, value, where: str):
    """Check `value` against the type of the field's default and return it.
    Every field has a concrete default, so the default *is* the type spec."""
    if isinstance(default, bool):  # before int — bool is a subclass of int
        if not isinstance(value, bool):
            raise PolicyError(f"{where} must be true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyError(f"{where} must be a whole number")
        if value < 1:
            raise PolicyError(f"{where} must be at least 1 (got {value})")
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise PolicyError(f"{where} must be a string")
        return value
    # tuple: a list of strings in the JSON
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where} must be a list of strings")
    return tuple(value)


def _section(cls, raw, where: str):
    if not isinstance(raw, dict):
        raise PolicyError(f"{where} must be an object, got {type(raw).__name__}")
    valid = {f.name: f for f in fields(cls)}
    unknown = sorted(k for k in raw if k not in valid)
    if unknown:
        raise PolicyError(f"unknown key(s) in {where}: {', '.join(unknown)} — "
                          f"valid keys are {', '.join(valid)}")
    return cls(**{k: _coerce(valid[k].default, v, f"{where}.{k}")
                  for k, v in raw.items()})


def parse(raw, source: str = "policy") -> Policy:
    """Build a Policy from already-decoded JSON. Raises PolicyError with a
    message meant to be read by whoever wrote the file."""
    if not isinstance(raw, dict):
        raise PolicyError(f"{source} must be a JSON object")
    valid = {"name", *_SECTIONS}
    unknown = sorted(k for k in raw if k not in valid)
    if unknown:
        raise PolicyError(f"unknown key(s) in {source}: {', '.join(unknown)} — "
                          f"valid keys are {', '.join(sorted(valid))}")
    if "name" in raw and not isinstance(raw["name"], str):
        raise PolicyError(f"{source}.name must be a string")

    kwargs = {"name": raw.get("name", "default")}
    for key, cls in _SECTIONS.items():
        if key in raw:
            kwargs[key] = _section(cls, raw[key], f"{source}.{key}")
    pol = Policy(**kwargs)

    if pol.shell.mode not in SHELL_MODES:
        raise PolicyError(f"{source}.shell.mode must be one of "
                          f"{', '.join(SHELL_MODES)} (got {pol.shell.mode!r})")
    if pol.execution.sandbox not in SANDBOX_MODES:
        raise PolicyError(f"{source}.execution.sandbox must be one of "
                          f"{', '.join(SANDBOX_MODES)} "
                          f"(got {pol.execution.sandbox!r})")
    bad = sorted(set(pol.execution.require_approval) - set(GATEABLE))
    if bad:
        raise PolicyError(f"{source}.execution.require_approval names tool(s) "
                          f"with no permission gate: {', '.join(bad)} — "
                          f"gateable tools are {', '.join(GATEABLE)}")
    return pol


def load(path: str) -> Policy:
    """The policy at `path`. A missing file is the default policy (same
    contract as hooks.configure); anything else wrong raises PolicyError."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return Policy()
    except OSError as e:
        raise PolicyError(f"could not read {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise PolicyError(f"{os.path.basename(path)} is not valid JSON: {e}") from e
    return parse(raw, source=os.path.basename(path))


# ── the active policy ────────────────────────────────────────────────
# Module state, like permissions.py and hooks.py: the tool functions are
# called from deep inside the loop, and threading a policy through every
# signature would buy nothing.

current: Policy = Policy()


def configure(policy: Policy) -> None:
    global current
    current = policy


def reset() -> None:
    """Back to the built-in defaults (used by tests)."""
    configure(Policy())


def domain_allowed(host: str, policy: Policy | None = None) -> bool:
    """Is this hostname permitted by network.allowed_domains? A pattern
    containing '*' is matched with fnmatch; a plain domain matches itself and
    its subdomains, so "example.com" covers "www.example.com"."""
    pol = policy or current
    host = (host or "").lower()
    for pattern in pol.network.allowed_domains:
        pattern = pattern.lower()
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(host, pattern):
                return True
        elif host == pattern or host.endswith("." + pattern):
            return True
    return False
