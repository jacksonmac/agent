# Skills

Skills are reusable expert instructions that the **model** pulls in on its own,
mid-run, when they're relevant. They are the harness's version of
[Claude Code skills](https://www.anthropic.com/news): a directory of markdown
files, each teaching the agent how to do one thing well, surfaced through
*progressive disclosure* so unused skills cost almost nothing.

```
skills/
├── pytest-debugging/
│   └── SKILL.md
├── python-packaging/
│   └── SKILL.md
└── hi-jackson/
    └── SKILL.md
```

## Skills vs. commands

The harness has two markdown-with-frontmatter mechanisms. They look similar and
are easy to confuse:

| | **Commands** (`commands/*.md`) | **Skills** (`skills/<name>/SKILL.md`) |
|---|---|---|
| Who invokes it | **You**, via `-c <name>` on the CLI | The **model**, via the `load_skill` tool |
| What it is | A goal template (the *task* itself) | Instructions for *how* to do a task |
| When it applies | The one run you launch with it | Any run where the description matches |
| Where it lands | Becomes `args.goal` | Injected into the system prompt / loaded on demand |

Rule of thumb: if it's "the thing I want done," it's a command; if it's "how to
do things well whenever they come up," it's a skill.

## The two kinds of skill

### On-demand skills (the default)

Only a one-line **teaser** — the skill's `name` and `description` — goes into the
system prompt, collected under a `SKILLS —` heading. The full body stays on disk
until the model decides it's relevant and calls `load_skill(name)`. This is
progressive disclosure: ten skills add ten short lines to the prompt, and you
only pay for the body of the one the model actually loads.

What the executor sees in its system prompt:

```
SKILLS — reusable expert instructions you can load on demand. When a skill's
description matches the work at hand, call load_skill(name) and FOLLOW the
loaded instructions:
- pytest-debugging: diagnose and fix failing pytest suites methodically
- python-packaging: lay out an installable Python package with pyproject.toml and verify with pip
```

When the goal involves failing tests, the model calls
`load_skill("pytest-debugging")` and the full SKILL.md body comes back as a tool
result it then follows.

### Always-on skills (`always: true`)

Some instructions aren't triggered by the *work* — they're standing preferences
that apply to every run regardless of topic (output format, a greeting, a house
style). A small local model will never think to `load_skill` a meta-instruction,
because nothing in the goal "matches" it.

Mark such a skill with `always: true` in its frontmatter and its **entire body**
is injected straight into the system prompt instead of being listed in the
on-demand index. The model never has to decide to load it. The shipped
`hi-jackson` skill works this way.

What the executor sees for an always-on skill:

```
SKILL (always applies): hi-jackson
This is a standing personalization preference from Jackson, the operator...
```

(`load_skill("hi-jackson")` still works too — the flag only changes how it's
advertised, not whether it can be loaded.)

## Writing a skill

Create `skills/<name>/SKILL.md`. The file is `---`-fenced frontmatter followed by
a markdown body:

```markdown
---
name: pytest-debugging
description: diagnose and fix failing pytest suites methodically
---
When tests are failing, work in this order — do not skip steps:

1. Run `pytest -q` and read the tail of the output first...
2. Reproduce one failure in isolation...
```

**Frontmatter keys** (all optional):

| Key | Meaning |
|---|---|
| `name` | The name the model loads by. Falls back to the **directory name** if omitted. |
| `description` | The one-line teaser in the index. This is what the model matches against — make it say *when* to use the skill, not just what it is. |
| `always` | `true` / `yes` / `1` → inject the body into every prompt instead of listing it on demand. |

**Body**: plain markdown instructions. Write them as an imperative procedure the
model should follow, and reference the harness's actual tool belt
(`run_shell`, `read_file`, `run_python`, …) so the steps are executable. Keep it
under `skill_body_max` (8,000 chars); longer bodies are truncated middle-out.

That's the whole authoring loop — no registration, no code. `discover()` scans
`skills/` fresh on every run, so a new directory is picked up immediately.

### Tips for small local models

- The **description does the work.** The model only loads a skill if its
  description reads as relevant, so phrase it around the trigger ("diagnose and
  fix failing pytest suites") rather than the noun ("pytest info").
- For anything that must apply unconditionally, use `always: true` — don't rely
  on the model choosing to load it.
- Keep bodies focused and ordered. A numbered procedure beats prose.

## How it flows through a run

1. **Discovery** — at startup `cli.main()` calls `skills.system_prompt_block()`,
   which scans `skills/`, builds the on-demand index, and appends the full body
   of any `always: true` skills.
2. **Injection** — that block is appended to the **executor** system prompt
   (right after AGENT.md/resume context, before the MCP tool advert). Subagents
   get the same block appended to `SUBAGENT_SYSTEM`. You'll see
   `[context] N skill(s) available via load_skill` printed, and a `skills` event
   logged.
3. **Loading** — when the model calls `load_skill(name)`, the tool returns
   `SKILL: <name>` plus the body and logs a `skill` event. Unknown names return
   a readable `[ERROR] no such skill: X (available: …)`.
4. **Context handling** — a loaded body can be larger than a normal tool result,
   so `load_skill` results are capped at `skill_body_max` (8,000) instead of the
   usual `tool_result_max` (4,000). Once an attempt is old, a loaded body may be
   compacted to a stub like any tool result — that's fine, the model can reload.

### Who sees skills

| Role | Skills index? | `load_skill` tool? |
|---|---|---|
| Executor | ✅ | ✅ |
| Subagents | ✅ | ✅ |
| Reviewer | ❌ | ❌ |
| Goalsmith | ❌ | ❌ |

The reviewer is deliberately kept out: its job is to judge the workspace on its
merits, not to be primed by the same instructions the executor followed. Its
restricted tool subset excludes `load_skill`, and the session layer rejects any
tool that wasn't advertised to it.

## Configuration

| Setting (`harness/config.py`) | Default | Meaning |
|---|---|---|
| `skills` | `True` | Master switch. `False` = no index, and `load_skill` returns a disabled error. |
| `skill_body_max` | `8000` | Max chars of a loaded/injected skill body (AGENT.md-sized). |

CLI: **`--no-skills`** turns the whole feature off for a run (sets
`settings.skills = False`).

Read-only and cheap: `load_skill` is **not** permission-gated — it never triggers
a y/n/a prompt, because loading instructions can't touch the workspace.

## Observability

Skills show up in `events.jsonl` (and therefore the run report):

- `skills` — logged once at startup: `count` of on-demand skills advertised and
  total `chars` of the injected block.
- `skill` — logged each time `load_skill` succeeds: the `name` and `chars`
  returned.
- Each load is also a generic `tool` event (name `load_skill`), so it appears in
  the report's tool-usage table.

## Shipped skills

| Skill | Kind | Purpose |
|---|---|---|
| `pytest-debugging` | on-demand | A methodical failing-suite workflow: run first, isolate one failure, read test *and* code, fix the root cause, re-verify in widening circles. |
| `python-packaging` | on-demand | Lay out an installable package with `pyproject.toml`, verify with `pip install -e .` and an import check. |
| `hi-jackson` | `always: true` | Standing preference: begin every final answer with "Hi Jackson". |

## Implementation reference

Everything lives in one small module, [`harness/skills.py`](harness/skills.py):

| Function | Role |
|---|---|
| `discover(skills_dir=None)` | Scan `skills/`, parse each `SKILL.md`, return `list[Skill]` sorted by name. Missing dir → `[]`. |
| `index_text(skills)` | Build the on-demand `SKILLS —` block (skips `always` skills). |
| `system_prompt_block(skills_dir=None)` | The single injection entry point: index + always-bodies, or `""` when disabled/empty. |
| `load_skill(name)` | The tool callable. Returns the capped body or an `[ERROR]`. |

Wiring points:

- Registered as a tool in [`harness/tools/__init__.py`](harness/tools/__init__.py)
  (`tools` dict + `TOOL_SCHEMAS`); **not** in `permissions.GATED`.
- Injected into the executor prompt in [`harness/cli.py`](harness/cli.py) and the
  subagent prompt in [`harness/tools/subagent.py`](harness/tools/subagent.py).
- Cap exception for loaded bodies in [`harness/llm.py`](harness/llm.py).
- Frontmatter parsing is reused from
  [`harness/commands.py`](harness/commands.py) (`parse_frontmatter`).

Tests: [`tests/test_skills.py`](tests/test_skills.py) covers discovery,
name fallback, the index, on-demand vs. `always` injection, `load_skill`
happy/missing/disabled/oversized paths, the not-gated guarantee, reviewer
exclusion, subagent visibility, and an end-to-end session proving the cap bypass.

See also **SPEC.md §20** for the specification-level summary and **Readme.md**
("Claude-Code-style extras") for the feature in context.
