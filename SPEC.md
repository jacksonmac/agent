# Agent Harness — Specification

A specification of the current codebase: an autonomous **execute → review → retry**
agent harness that drives a local [Ollama](https://ollama.com/) server. Given a goal, it
runs an executor model to do the work in an isolated workspace, has a reviewer model judge
the artifacts against checkable criteria, and retries with feedback until the goal is met
or the attempt budget runs out.

> Scope: this document describes behavior as implemented in `harness/`. `agent.py` at the
> repo root is a thin entry-point shim. The single-file `agent.py` implementation referenced
> in older git history has been refactored into the `harness/` package.

---

## 1. Entry points & invocation

- **`agent.py`** (repo root) — shim that inserts the repo on `sys.path` and calls
  `harness.cli.main()`.
- **`harness/cli.py:main()`** — the real entry point.
  - If `sys.argv[1] == "history"`, dispatches to the history subcommand (`agent.py history
    [--stats] [--limit N]`) **before** the main argument parser runs, because the main
    parser requires a goal.
  - Otherwise parses args, mutates the global `settings`, builds the workspace, and starts
    the run.

**Goal source is a required mutually-exclusive group** (exactly one of):
- `-g/--goal TEXT` — the text is used verbatim as both the goal and the task.
- `-sg/--smart-goal TEXT` — a "goalsmith" model rewrites the rough prompt into a
  structured GOAL + CRITERIA + TASK before running (see §9).
- `-c/--command NAME [args...]` — load a saved prompt template from `commands/NAME.md`
  (see §10).

Representative CLI flags (full list in `harness/cli.py:parse_args`):
`--model`, `-rm/--reviewer-model`, `-em/--executor-model`, `-gm/--goalsmith-model`,
`--attempts`, `--best-of N`, `--no-plan`, `--no-self-check`, `--no-memory`, `--no-skills`,
`--no-stream`,
`--no-notify`, `--yolo`, `--url`, `--num-ctx`, `--full-context`, `--no-reviewer-tools`,
`--mcp`, `--mcp-profile`, `--workspace DIR`.

---

## 2. Configuration (`harness/config.py`)

One module-level `settings` singleton (a `Settings` dataclass); `cli.main()` mutates it once
at startup and everything else reads from it. Notable defaults:

- `url = "http://192.168.1.134:11434"` (a LAN Ollama server), `model = "gemma4:26b"`.
- `reviewer_model`, `executor_model`, `goalsmith_model` default to `None` = "same as `model`".
- `request_timeout = 1600`s, `num_ctx = 10000`, `chars_per_token = 3` (budgeting estimate).
- Truncation caps: `tool_result_max = 4000`, `retry_prev_max = 6000`, `page_text_max = 6000`,
  `compact_keep_last = 6`, `skill_body_max = 8000` (loaded SKILL.md bodies).
- Per-role sampling (`RoleOptions`): executor `temperature=0.7`, reviewer `0.1`,
  goalsmith `0.3`; each has `think=True`.
- Feature toggles: `full_context`, `reviewer_tools`, `memory`, `skills`, `stream`, `notify`,
  `plan_first`, `self_check`, `subagent_max_rounds = 8`.

---

## 3. The core loop (`harness/run.py:main`)

Returns `run_passed: bool`. Sequence:

1. **Setup** — resolve `executor_model`, reset the todo list, log `run_start`, write the
   transcript header. `user_msg` is `"GOAL: {goal}\n\nTASK: {task}"` (or just the task when
   goal == task).
2. **Attempt 1 source** — if `best_of > 1`, run the candidate round (§11) and hold its
   result as `pending`; otherwise create a fresh executor `Session`.
3. **Attempt loop** `for attempt in range(1, max_attempts + 1)`:
   - If a `pending` result exists (from best-of), consume it; else run `_run_attempt`
     (`ui.attempt` / `ui.phase("executing")`).
   - **Stall gates** (avoid spending a review call on a doomed attempt):
     - `no_tools`: the attempt made zero tool calls **and** changed no files → skip review,
       inject a `Verdict(passed=False, ...)` telling the model to actually use its tools.
     - `stalled`: normalized output is >0.95 similar to the previous attempt
       (`difflib.SequenceMatcher`) → skip review, demand a different approach.
     - otherwise → `ui.phase("reviewing")` and call `review(...)` (§8).
   - Record the attempt (append to `attempts`, log `attempt` event, fire `attempt_end`
     hook, append to the transcript).
   - **If passed**: `ui.success("WE DID IT ...")`, save `final_output.txt`, `break`.
   - **Else retry**: save `attempt_N.txt`, append to `feedback_history`, then
     `session.compact_completed_attempts()`. If `session.over_budget()` → start a **fresh**
     session and rebuild `user_msg` from `RETRY_NOTE` (task + capped prior answer + feedback
     digest); else keep the **same** session and set `user_msg` from `RETRY_CONTINUE` (goal +
     unmet criteria + feedback). Append a "take a different approach" note if stalled.
4. **`for-else` (budget exhausted)** — warn, save the last answer as
   `final_output_UNVERIFIED.txt`.
5. **Finalize** — save `attempt_history.json`, render `report.html` (§12), record the run in
   `history.db` (§12), fire the `run_end` hook, write the memory note (§presentation note:
   `ui.phase("writing memory note")`), then `ui.stop()`, print the timing summary, and print
   the run-artifact paths.

### `_run_attempt` (one executor attempt)
`ws.begin_attempt()` marks the mtime baseline. Then:
- If `plan_first` (attempt 1 only): a **no-tool** planning turn (`PLAN_PROMPT`), followed by
  `EXECUTE_AFTER_PLAN` to do the work with tools. Otherwise a single turn on `user_msg`.
- **Self-check** (`settings.self_check`, and only if the attempt used tools or changed
  files): one more turn (`SELF_CHECK_PROMPT`) telling the model to verify against the actual
  workspace and restate its final answer.
- Returns `(answer, changed_files, tool_calls_used)`.

---

## 4. LLM plumbing & sessions (`harness/llm.py`)

- **`_post_chat(payload, label)`** — the single HTTP path to Ollama `/api/chat`. Injects
  `num_ctx`, sets the stream flag, times the call, and on an HTTP 400 "does not support
  thinking" it records the model in `_no_think_models`, drops `think`, and retries. After the
  response it logs an `llm` event, calls `ui.llm_stats(...)`, and warns if the prompt used
  >85% of `num_ctx`.
- **Streaming** — `_consume_stream` aggregates Ollama's line-delimited JSON deltas into one
  message dict of the same shape as the non-streaming reply, feeding text to the UI live via
  `ui.stream_delta` (thinking vs. content) and always calling `ui.stream_end` in a `finally`.
  `_consume_single` handles the non-streaming case.
- **`Session`** — a persistent conversation with tool-calling:
  - Holds `messages` (starts with the system prompt) and an `_allowed` set of tool names
    derived from the advertised schemas — a tool not in the schemas cannot run in this
    session (this is how the reviewer is kept read-only).
  - `send(user, with_tools=True)` runs a tool loop up to `max_tool_rounds` (default 15):
    each round compacts history, reports context tokens, calls `_post_chat`, and — if the
    reply has `tool_calls` — executes each via `execute_tool_call`, appending a `role:"tool"`
    message whose content is capped at `tool_result_max`. When the reply has no tool calls it
    is the final answer. Thinking text is shown once then popped from history (kept only in
    `full_context` mode). If all rounds are exhausted, one final no-tools call forces a text
    answer (`ui.answer(..., forced=True)`).
  - Inter-attempt housekeeping: `compact_completed_attempts()` stubs old turns down to ~300
    chars; `over_budget()` is true when the estimate exceeds 75% of `num_ctx`.
  - `@timed` wraps `send` / `chat_v2` and accumulates per-function timing for
    `print_timing_summary()`.
- **Context budgeting** — `estimate_tokens` (chars/`chars_per_token`), `truncate_middle`
  (keep head+tail), `cap` (no-op under `full_context`), and `compact_messages` (in-place
  shrink of old tool/assistant turns once history exceeds 75% of `num_ctx`, never touching the
  system prompt, the first user task, or the last `compact_keep_last` messages).
- **`chat_v2(...)`** — convenience one-shot: a fresh `Session` with one user turn (used by the
  reviewer, goalsmith, and memory writer).

---

## 5. Tools (`harness/tools/`)

**Registry** (`tools/__init__.py`): a `tools` dict maps name → callable. Filesystem/exec
tools are bound to the run's `Workspace` via `configure(ws)` (which `functools.partial`s the
workspace in). `TOOL_SCHEMAS` is the Ollama tool-schema list advertised to models.

**`execute_tool_call(name, arguments)`** is the single dispatch point:
1. Unknown tool → `[ERROR]`.
2. If `arguments` is a JSON string, parse it (some models send strings).
3. **Permission gate** (`permissions.check`) — a denial short-circuits and is returned to the
   model as an `[ERROR]` string.
4. Fire the `pre_tool` hook, run the tool, fire `post_tool`.
5. Log a `tool` event with `ok = not result.startswith("[ERROR]")`.

**Built-in tools:**
- File tools (`tools/files.py`, jailed to the workspace): `write_file`, `read_file`
  (offset/`max_chars` paging), `list_files` (recursive, sizes, junk filtered), `edit_file`
  (exact unique-snippet replace, with a close-match hint on miss), `grep_files` (regex or
  literal, `file:line` results). `write_file`/`edit_file` now emit a **display-only** unified
  diff via `ui.diff(...)`; the string returned to the model is unchanged.
- Exec tools (`tools/execute.py`, cwd = workspace): `run_python` (`python3 -c`, 120s),
  `run_script` (a saved `.py`, 120s), `run_shell` (allowlisted: `pip pip3 python3 pytest ls
  mkdir cat echo`; shell metacharacters `; | & < > ` $` rejected; `shell=False` + `shlex`;
  360s). These three are the permission-gated set.
- `set_todos` (`todos.py`) — the executor's self-maintained checklist; replaces the list
  wholesale, validates before mutating, and updates the UI and reviewer view.
- `spawn_subagent` (`tools/subagent.py`) — see §13.
- `load_skill` (`skills.py`) — see §20. Read-only, never permission-gated.
- Web tools (`tools/web.py`): `web_search` (DuckDuckGo via optional `ddgs`), `fetch_page`
  (readability via optional `trafilatura`, else a tag-stripping fallback; blocks
  local/private hosts; capped at `page_text_max`).

---

## 6. Permission gate (`harness/permissions.py`)

The gated set is `{run_shell, run_python, run_script}` (subprocess spawners; file tools are
jailed and reversible, web tools are read-only). `check()`:
- `--yolo` or a prior "always" grant → allow.
- Non-interactive session (`stdin` not a TTY) → auto-deny with an actionable error (warns
  once, suggests `--yolo`).
- Otherwise an interactive `ui.confirm` y/n/a prompt; "always" grants the tool for the whole
  run. Every decision is logged as a `permission` event. The gate lives in
  `execute_tool_call`, so executor, reviewer, and subagents all share it.

---

## 7. Workspace isolation (`harness/workspace.py`)

Each run gets `runs/run_TIMESTAMP/` with a `workspace/` subdirectory (plus a `latest`
symlink). **Tools can only see/touch `workspace/`**; harness artifacts live beside it in the
run dir so runs can't contaminate each other.
- `resolve(name)` is the path jail — raises `ValueError` if a name escapes the workspace root.
- `begin_attempt()` / `files_changed_this_attempt()` detect changed files by mtime (with 1s
  slack), regardless of which tool wrote them — the reviewer judges real files, not claims.
- `snapshot_files(names)` reads changed files with per-file and total caps for the reviewer.
- `--workspace DIR` reuses an existing directory to continue earlier work.

---

## 8. Reviewer (`harness/review.py`)

`review(model, goal, output, ws, criteria, changed_files)` always returns a `Verdict`:
- Builds `REVIEW_USER` from the goal, criteria (or an instruction to derive 3–6
  binary-checkable ones), the capped output, the todo list, the file listing, the changed-file
  snapshot, and `automated_checks(ws)` — which runs pytest on any `test_*.py` in the workspace
  and returns capped output.
- By default the reviewer gets a **read-only-ish tool subset**
  (`read_file, list_files, run_script, run_shell`) so it can inspect/run code itself;
  `--no-reviewer-tools` disables that.
- Parsing is resilient: it extracts the first balanced JSON object (tolerating code fences),
  and if that fails it re-asks once in strict mode, then falls back to YES/NO regex, then to a
  default `passed=False`. It never aborts the run.
- `Verdict` (dataclass) exposes `passed`, `criteria` (list of `{criterion, met, note}`),
  `feedback`, `unmet()`, and `summary()`.

---

## 9. Smart goals (`harness/goalsmith.py`)

`-sg` calls `make_goal_task(model, prompt)`: a `chat_v2` turn with `GOALSMITH_SYSTEM` asks the
model to emit `GOAL` / `CRITERIA` / `TASK`. It parses the structured block (falling back to a
GOAL/TASK-only format, then to using the raw prompt as both) and returns `(goal, task,
criteria)`. The criteria then drive both the executor's self-check and the reviewer.

---

## 10. Saved commands (`harness/commands.py`)

`commands/NAME.md` is markdown with optional frontmatter (`description` + flag defaults like
`em`, `attempts`). `-c NAME [extra...]` loads it, substitutes `{args}` in the body, uses the
body as the goal, and applies frontmatter values as defaults for flags the user didn't set
explicitly (explicit CLI flags win). Frontmatter keys map to argparse dests via
`_FRONTMATTER_DESTS`.

---

## 11. Best-of-N (`harness/run.py:_run_candidates`)

`--best-of N` runs N **independent first attempts**, each in its own copied candidate
workspace, reviews each, scores by `(passed, criteria_met)` (ties → earliest candidate),
promotes the winner's files into the main workspace, and continues the normal retry loop from
that session/verdict.

---

## 12. Persistence & reporting

- **`runlog.py`** — per run: `events.jsonl` (machine-readable events: `run_start`, `llm`,
  `tool`, `attempt`, `permission`, `hook`, `todos`, `memory`, `subagent_*`, `skills`, `skill`, …) and
  `transcript.md` (human-readable). A module-level `current` handle keeps deep-loop call
  signatures simple.
- **`history.py`** — a sqlite `runs` table (one row per run) under `runs/history.db`, queried
  by `agent.py history` (listing) and `agent.py history --stats` (pass-rate per executor
  model).
- **`report.py`** — a self-contained `report.html` per run (pure stdlib, inline CSS,
  `<details>` collapsibles), built from `events.jsonl` + `attempt_history.json` +
  `final_output*.txt`. Regenerate by hand with `python3 -m harness.report <run_dir>`. Never
  raises.

---

## 13. Subagents (`harness/tools/subagent.py`)

`spawn_subagent(task, kind)` runs a scoped child `Session` with `SUBAGENT_SYSTEM` (plus the
skills index when skills exist, §20), the executor model, and the normal tool belt **minus** `spawn_subagent` (a depth guard blocks
recursion) and `set_todos` (the checklist belongs to the parent). Only the child's final text
is returned to the parent, capped like any tool result. The UI nests the child's tool lines
under a `└` prefix.

---

## 14. Memory (`harness/memory.py`)

After each run (unless `--no-memory`), `update_agent_md` asks the goalsmith model (no tools,
`think=False`) for 3–6 durable bullet-point lessons and appends them as a dated section to
`<workspace>/AGENT.md`, trimming to `AGENT_MD_MAX` while preserving any user-authored preamble
(text before the first `## ` section). On the next run against that workspace,
`cli._load_agent_md` injects the file back into the executor system prompt. Never raises.

---

## 15. Hooks (`harness/hooks.py`)

A repo-root `hooks.json` maps events (`pre_tool`, `post_tool`, `attempt_end`, `run_end`) to
shell commands with placeholder substitution (`{tool} {file} {run_dir} {workspace} {attempt}
{passed}` as applicable; `pre/post_tool` also filter by an `fnmatch` `match`). Hooks are
**strict observers** — their exit codes and output never block or modify the run; failures
warn and continue. Commands run through the shell (trusted, user-authored config), each with a
10s timeout, and are logged as `hook` events.

---

## 16. MCP (`harness/tools/mcp.py`)

`--mcp` starts the Docker MCP Toolkit gateway (`docker mcp gateway run`, optional
`--mcp-profile`) and speaks JSON-RPC 2.0 over the subprocess's stdio (no SDK dependency). It
discovers the gateway's tools, skips any whose name clashes with a built-in, registers the
rest into the tool registry and schema list, and appends a note to the executor system prompt
listing them. The gateway is closed in `cli.main`'s `finally`.

---

## 17. Presentation layer (`harness/ui.py`)

A single module-level singleton with a swappable backend: a **rich `Live` dashboard** when
`rich` is importable and stdout is a real TTY, and a **plain-`print` fallback** otherwise
(evals, pytest, pipes). All harness code calls module functions (`ui.phase`, `ui.tool`,
`ui.answer`, …). The dashboard shows a header (goal/model/attempt/phase with a spinner), a
context-token progress bar (green/yellow/red), a rolling recent-tools panel, a live streaming
tail, the todo checklist, and the last verdict. Persistent lines (answers, verdicts, warnings)
print above the live region.

Recent UI additions (all display-only, plain-mode-safe):
- **Live status line** — the phase header shows elapsed `m:ss` (the 4 Hz refresh ticks it),
  and `ui.llm_stats` shows the last call's `secs` and `prompt→eval` token counts.
- **Markdown answers** — `ui.answer` renders the final answer through `rich.Markdown` (with a
  plain-text fallback on parse failure).
- **Diff previews** — `ui.diff` renders unified diffs via `rich.Syntax("diff")`, capped at
  ~80 lines; driven by `write_file`/`edit_file`.
- **Finish notification** — `ui.notify` rings the terminal bell and, on macOS, posts an
  `osascript` banner; best-effort and suppressed by `--no-notify`.
- **Attempt ledger (`[a]`)** — `ui.attempt_result(n, passed, summary, criteria)` records one
  row per attempt instead of letting the newest review overwrite the last; the panel grids
  criteria × attempt, flags any criterion that was met before and isn't now (`_regressions`),
  and shows the reviewer's pytest digest (`ui.checks`) next to the retry instruction the
  executor actually received (`ui.retry_focus`). The regression count also surfaces inline
  in the plan panel.
- **Budget (`[b]`)** — `ui.llm_stats` accumulates calls/prompt/eval/secs per role label
  (executor, reviewer, goalsmith, subagent, memory) plus tokens per attempt; the panel adds
  share bars, marks an attempt that cost >1.25× the one before it, and reports how much each
  compaction dip in the context sawtooth gave back.
- **Loop banner** — a repeated `(tool, args)` inside the last 8 calls, or 2 minutes of tool
  calls with no file mutation, raises a yellow line above the timeline; any write clears it.
- **Steering** — queued messages render as a chip (`[e]` edit, `[c]` cancel), `[/]` opens the
  `STEER_PRESETS` picker, and `[i]` composes an interrupt: `ui.interrupt_requested()` makes
  `Session.send` skip the tool calls it hasn't run yet (each still gets a `[ERROR] skipped`
  result so the history stays well-formed) and go straight back to the model with the message.

---

## 18. Testing

`pytest` suite under `tests/` (181 tests) covering the loop phases, sessions, streaming,
review, tools, permissions, hooks, memory, history, resume, subagents, todos, commands,
skills, and report. Because tests run without a TTY, the UI uses its plain fallback and no rich behavior is
exercised. Run with `venv/bin/python -m pytest tests/`.

---

## 19. Dependencies (`requirements.txt`)

Required: `requests`, `rich>=13`. Dev: `pytest`. Optional (enable web tools): `ddgs`,
`trafilatura`; (office round-trip tests only): `openpyxl`, `python-docx`. Everything else is stdlib. The harness runs without the optional packages
(tools degrade with actionable errors) and without `rich` (plain UI).

---

## 20. Skills (`harness/skills.py`)

Claude-Code-style skills with progressive disclosure: reusable expert instructions the
**model** discovers and loads itself (commands are user-invoked; skills are model-invoked).

- **Files**: `skills/<name>/SKILL.md` under the repo root. `---`-fenced frontmatter with
  `name` (falls back to the directory name), `description`, and optional `always: true`;
  the markdown body is the instructions. Parsed with `commands.parse_frontmatter`.
  Directories without a readable `SKILL.md` are skipped; a missing `skills/` dir means no
  skills.
- **`always: true` skills** are standing preferences: their full body is injected into the
  system prompt (as `SKILL (always applies): <name>`) instead of being listed in the
  on-demand index — small local models won't reliably decide to load meta-instructions
  themselves. `load_skill` still works on them.
- **Index injection**: `system_prompt_block()` renders a compact `SKILLS — …` block (one
  `- name: description` line per skill). `cli.main` appends it to the executor system
  prompt (after AGENT.md/resume context, before the MCP advert) and logs a `skills` event;
  `spawn_subagent` appends the same block to `SUBAGENT_SYSTEM`. The block lives in message 0,
  so compaction never evicts it. Empty/absent skills → nothing injected.
- **`load_skill(name)` tool**: returns `SKILL: <name>` + the body, truncated middle-out to
  `skill_body_max` (default 8000). Unknown name → `[ERROR] no such skill: … (available: …)`.
  Not gated by the policy's `execution.require_approval` (read-only, never prompts). Each successful load logs a `skill`
  event (name, chars). In `llm.Session`, `load_skill` results are capped at `skill_body_max`
  instead of `tool_result_max`, so instructions arrive intact; old loaded bodies are still
  compacted to stubs later like any tool result (the model can reload).
- **Role scoping**: executor and subagents only. The reviewer's schema subset (§8) excludes
  `load_skill` and its session rejects unadvertised tools; the goalsmith makes no-tool calls.
- **Disable**: `--no-skills` (or `settings.skills = False`) removes the index and makes the
  tool return a disabled error.
- Ships with three starter skills: `pytest-debugging`, `python-packaging` (on-demand), and `hi-jackson` (`always: true`).

---

## 21. Office-document fidelity (`harness/office.py`)

Support module for the planned document tools (RD I-8), landed ahead of them because it
is what makes editing a user-supplied file defensible.

**Why it exists.** Measured against openpyxl 3.1.5 and python-docx 1.2.0: python-docx
repackages parts it has no model for, so docx round-trips losslessly (content controls,
tracked changes, fields, headers, images all survive an edit). openpyxl preserves charts,
images, conditional formatting, data validation, defined names, comments and merges — but
silently drops what it cannot model, and not all of it at the package level: an `<extLst>`
sparkline group vanishes from *inside* `sheet1.xml` while the part count is unchanged.
`customXml/` is dropped, `vbaProject.bin` needs `keep_vba=True`, and `data_only=True`
replaces every formula with a cached value.

**Design.** `fingerprint(path) -> Fingerprint(parts, constructs, formulas)` reads the OPC
zip directly — no third-party dependency, so it runs whether or not openpyxl is installed
and can judge a file written by any library. Three signals because one is not enough:
the part inventory, a substring scan for `MARKERS` (sparklines, slicers, pivots, macros,
custom XML, tracked changes, content controls, fields, …) across every textual part, and a
formula count over `xl/worksheets/*.xml`. Matching is deliberately crude: a false positive
costs one refused edit, a false negative ships a damaged file.

`compare(before, after, allow=())` / `check_edit(original, edited)` return the losses as
readable lines; `allow` exists because deleting the sheet that held the sparklines is a
legitimate edit and only the calling tool knows that. `describe(problems)` renders the
refusal the model receives — it states that the original is untouched and that retrying the
same call will not help, since a bare error otherwise invites an identical retry.

**Tests** (`tests/test_office.py`): two tiers. Hand-built OPC packages exercise the logic
with no third-party dependency (including the loss-inside-a-surviving-part case); real
openpyxl/python-docx round-trips pin the measurements above and skip via
`pytest.importorskip` when those optional packages are absent.
