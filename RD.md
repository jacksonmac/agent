# Requirements Document

# Execute → Review → Retry Agent Harness

**Designer Organization:** Jackson McAdams

**Date Created:** July 7, 2026

---

## Revision History

| Name | Date | Reason for Changes | Version |
|---|---|---|---|
| Jackson McAdams | 2026-07-07 | Initial version, written against the codebase as implemented in `harness/` | 1.0 |

---

## 1 Introduction

### 1.1 Purpose

This document specifies the software requirements for the **Execute → Review → Retry Agent Harness** (the "harness"), version 1.0. The harness is a command-line autonomous agent system that drives large language models served by a local [Ollama](https://ollama.com/) instance to complete user-specified goals with real tools, then independently verifies the results.

This RD covers the entire system: the CLI entry point, the core attempt loop, the LLM session layer, the tool belt, the permission and hook subsystems, workspace isolation, persistence/reporting, and the evaluation suite. It does not cover the Ollama server itself, which is an external dependency.

### 1.2 Project Scope

The harness turns small, locally hosted LLMs into a usable autonomous coding agent. Given a natural-language goal, an **executor** model performs the work with filesystem, execution, and web tools inside an isolated per-run workspace; a **reviewer** model then judges the actual files on disk against checkable criteria; failed attempts are retried with the reviewer's feedback in the same conversation, so the model retains memory of what it already tried.

Objectives and benefits:

- **Reliability from weak models** — the review/retry loop, planning turn, and self-check turn compensate for the lower first-shot accuracy of small local models.
- **Verifiable results** — pass/fail verdicts are grounded in real workspace artifacts (file snapshots, pytest runs), not the model's claims.
- **Privacy and cost** — everything runs against a self-hosted Ollama server on the local network; no cloud API keys or per-token fees.
- **Measurability** — a bundled eval suite lets harness changes be benchmarked objectively before and after.

### 1.3 Glossary of Terms

| Term | Definition |
|---|---|
| **Agent / Harness** | This software: the orchestration layer around the LLMs. |
| **Ollama** | An open-source server that hosts and serves local LLMs over HTTP (`/api/chat`). |
| **Executor** | The LLM role that performs the work: plans, calls tools, writes files. |
| **Reviewer** | The LLM role that judges an attempt's artifacts against the goal criteria and returns a JSON verdict. |
| **Goalsmith** | The LLM role that rewrites a rough user prompt into a structured GOAL / CRITERIA / TASK (`-sg` mode); also writes the end-of-run memory note. |
| **Attempt** | One full executor pass (optional plan turn, execution, optional self-check turn) followed by a verdict. |
| **Verdict** | The reviewer's structured result: `passed`, per-criterion met/unmet notes, and feedback. |
| **Workspace** | The per-run directory jail — the only part of the filesystem the agent's tools can see or modify. |
| **Session** | A persistent conversation with one model, including its tool-calling loop and context budgeting. |
| **Subagent** | A scoped child session spawned by the executor for a self-contained subtask; only its summary returns to the parent. |
| **Compaction** | Shrinking older conversation turns to stubs so retries fit in a small context window. |
| **Hook** | A user-authored shell command fired on run events (observe-only). |
| **MCP** | Model Context Protocol; the harness can attach tools from the Docker MCP Toolkit gateway. |
| **Best-of-N** | Running N independent first attempts in parallel candidate workspaces and continuing from the best-scoring one. |
| **CLI** | Command-line interface. |
| **TTY** | An interactive terminal; several behaviors (dashboard, permission prompts) depend on whether one is present. |
| **RD** | Requirements Document (this document). |

### 1.4 References

1. Project README — `Readme.md` (repo root): architecture diagrams, usage, and configuration.
2. Codebase specification — `SPEC.md` (repo root): behavior as implemented, section-by-section.
3. Ollama API documentation — https://github.com/ollama/ollama/blob/main/docs/api.md (the `/api/chat` endpoint, streaming format, and tool-calling schema the harness targets).
4. Docker MCP Toolkit — https://docs.docker.com/ai/mcp-catalog-and-toolkit/ (source of optional MCP tools, §3.8).
5. Model Context Protocol specification — https://modelcontextprotocol.io (JSON-RPC 2.0 protocol spoken to the MCP gateway).
6. `rich` library — https://github.com/Textualize/rich (terminal dashboard rendering).

### 1.5 Overview

Section 2 describes the product context, features, users, environment, and constraints. Section 3 itemizes functional requirements grouped by system feature. Section 4 covers external interfaces (CLI, hardware, software, communications). Section 5 states non-functional requirements (performance, safety, security, quality). Section 6 lists remaining requirements, and the appendix tracks open issues.

---

## 2 Overall Description

### 2.1 Product Perspective

The harness is a new, self-contained product, originally implemented as a single `agent.py` script and since refactored into the `harness/` Python package (the root `agent.py` remains as a thin entry-point shim). It deliberately borrows interaction patterns from Anthropic's Claude Code (streaming output, permission prompts, persistent memory, saved commands, hooks, subagents, todo checklists) and adapts them to small local models.

The system sits between the user's terminal and an Ollama server:

```
User (CLI) ──► Harness (this product) ──HTTP──► Ollama server (LAN)
                    │
                    ├─► per-run workspace (file/exec/web tools, jailed)
                    ├─► optional Docker MCP gateway (subprocess, JSON-RPC)
                    └─► run artifacts (transcript, events, report.html, history.db)
```

### 2.2 Product Features

- **F1 — Goal intake:** verbatim goals (`-g`), goalsmith-structured goals (`-sg`), and saved command templates (`-c`).
- **F2 — Execute → review → retry loop:** attempt loop with planning turn, self-check turn, stall detection, and same-session retries with reviewer feedback.
- **F3 — Independent review:** a reviewer model with read-only tools judges the real workspace (file snapshots + automated pytest) and returns a resilient JSON verdict.
- **F4 — Tool belt:** jailed file tools, permission-gated execution tools, web search/fetch, a self-maintained todo checklist, and subagent delegation.
- **F5 — Workspace isolation:** a per-run path jail with mtime-based change tracking and workspace reuse for session resume.
- **F6 — Context management:** token budgeting, mid-loop compaction, between-attempt stubbing, and fresh-session fallback with a feedback digest.
- **F7 — Multi-model roles:** independent model selection for executor, reviewer, and goalsmith, with a common default.
- **F8 — Extensibility:** MCP gateway tools, user hooks, and saved commands.
- **F9 — Persistence & reporting:** transcript, machine-readable event log, self-contained HTML report, sqlite run history, and AGENT.md memory notes.
- **F10 — Presentation:** live rich terminal dashboard with a plain-print fallback, streaming, diffs, and notifications.
- **F11 — Evaluation suite:** 8 benchmark goals with programmatic checkers for benchmarking harness changes and models. Statistically sound A/B comparison (repeats, paired arms, significance) is I-12.

### 2.3 User Classes and Characteristics

| User class | Characteristics | Priority |
|---|---|---|
| **Operator (primary)** | A technically proficient developer running goals from a terminal. Comfortable with CLI flags, Python, and git. Uses the product frequently and interactively; expects the dashboard, permission prompts, and readable reports. | Favored |
| **Automation user** | The same developer (or a scheduler such as cron/CI) invoking the harness non-interactively with `--yolo`. Needs deterministic non-TTY behavior: plain output, auto-deny or auto-allow permissions, machine-readable artifacts. | Favored |
| **Harness developer** | A contributor modifying `harness/` itself. Relies on the pytest suite (372 tests) and the eval suite to validate changes. | Secondary |
| **Extension author** | A user writing `commands/*.md` templates, `hooks.json` entries, or attaching MCP tools. Needs stable placeholder/frontmatter contracts. | Secondary |

### 2.4 Operating Environment

- **OE-1:** Client machine: macOS or Linux with Python 3.10+. Tested primarily on macOS (Darwin); the finish-notification banner is macOS-specific and degrades gracefully elsewhere.
- **OE-2:** An Ollama server ≥ 0.9 reachable over HTTP, typically on another machine on the LAN (default `http://192.168.1.134:11434`, configurable via `--url` / `config.py`).
- **OE-3:** Python packages: `requests` and `rich>=13` required; `ddgs`, `trafilatura` optional (web tools); `pytest` for development and for the reviewer's automated checks.
- **OE-4:** Optional: Docker Desktop with the MCP Toolkit for `--mcp` mode.
- **OE-5:** Runs both in a real TTY (interactive dashboard) and headless (pipes, cron, pytest, evals).

### 2.5 Design and Implementation Constraints

- **CO-1:** Target models are **small local LLMs** with limited context windows (~30k tokens default); all conversation handling must budget tokens explicitly.
- **CO-2:** All model I/O goes through Ollama's `/api/chat` endpoint and its tool-calling schema; no other LLM providers are supported.
- **CO-3:** Python 3.10+ standard library preferred; runtime dependencies are limited to `requests` and `rich`, with optional extras degrading gracefully.
- **CO-4:** All agent file/exec activity must stay inside the per-run workspace jail; nothing is written to the repo root during a run.
- **CO-5:** Code execution safety relies by default on a command allowlist and metacharacter rejection rather than OS-level isolation; `--sandbox` adds a per-workspace Docker container (I-1), but it is opt-in, so the allowlist is what carries the guarantee in the default configuration.
- **CO-6:** Hooks are strictly observational: they must never be able to block or mutate a run.
- **CO-7:** The UI must function without `rich` and without a TTY (plain-print fallback), since the eval suite and tests run headless.
- **CO-8:** Configuration is a single module-level `Settings` singleton mutated once at CLI startup and read-only everywhere else.

### 2.6 Assumptions and Dependencies

- **AS-1:** An Ollama server is running, reachable, and has the requested models pulled; the harness does not manage the server or pull models.
- **AS-2:** Models advertised as tool-capable actually emit Ollama-format `tool_calls`; models that reject the `think` parameter are handled automatically, but models with no tool support will produce degraded runs.
- **AS-3:** The reviewer model is capable of following the JSON-verdict instructions most of the time; fallback parsing covers the remainder.
- **AS-4:** The user authoring `hooks.json` and `commands/*.md` is trusted — hook commands run through the shell deliberately.
- **AS-5:** DuckDuckGo (`ddgs`) and target web pages remain accessible for the optional web tools.
- **DE-1:** Depends on the external Ollama project's API stability (streaming line-JSON, `prompt_eval_count`, HTTP 400 semantics for unsupported `think`).
- **DE-2:** `--mcp` depends on Docker Desktop's `docker mcp gateway run` command and its JSON-RPC stdio contract.

---

## 3 System Features

### 3.1 Goal Intake & Smart Goals

#### 3.1.1 Description and Priority

The user supplies exactly one goal source. In smart-goal mode, a goalsmith model restructures a rough prompt into a checkable GOAL / CRITERIA / TASK triple that then drives the executor's self-check and the reviewer's checklist. **Priority: High.**

#### 3.1.2 Functional Requirements

- **REQ-1:** The CLI shall require exactly one of `-g/--goal`, `-sg/--smart-goal`, or `-c/--command` (a mutually exclusive required group).
- **REQ-2:** With `-g`, the harness shall use the supplied text verbatim as both goal and task.
- **REQ-3:** With `-sg`, the harness shall call the goalsmith model to produce a structured GOAL, a list of binary-checkable CRITERIA, and a TASK; parsing shall fall back to a GOAL/TASK-only format and finally to using the raw prompt as both, never aborting the run.
- **REQ-4:** Criteria produced by the goalsmith shall be passed to both the executor's self-check turn and the reviewer.
- **REQ-5:** With `-c NAME [extra words]`, the harness shall load `commands/NAME.md`, substitute `{args}` in the body, use the body as the goal, and apply frontmatter values (`description`, `em`, `rm`, `gm`, `model`, `attempts`, `best_of`, `url`, `num_ctx`) as defaults; explicit CLI flags shall override frontmatter.
- **REQ-6:** `agent.py history` shall be dispatched before the main parser runs, so it works without a goal argument, and shall support `--stats` (pass-rate per executor model) and `--limit N` (default 20).

### 3.2 Execute → Review → Retry Loop

#### 3.2.1 Description and Priority

The core control flow: run an executor attempt, judge it, and retry with feedback until the verdict passes or the attempt budget is exhausted. **Priority: High.**

#### 3.2.2 Functional Requirements

- **REQ-7:** The harness shall run up to `--attempts` N executor attempts (configurable), stopping early on the first passing verdict.
- **REQ-8:** On attempt 1 (unless `--no-plan`), the executor shall first produce a plan in a turn with **no tool schemas advertised**, committing to filenames and a verification step before execution.
- **REQ-9:** After execution (unless `--no-self-check`, and only when the attempt used tools or changed files), the harness shall send a self-check turn instructing the model to verify each criterion against the actual workspace with tools and fix failures before review.
- **REQ-10:** The harness shall skip the review call and inject a synthetic failing verdict when (a) the attempt made zero tool calls and changed no files, or (b) the attempt's normalized output is more than 0.95 similar to the previous attempt (stall detection); the injected feedback shall demand tool use or a different approach respectively.
- **REQ-11:** On a failed verdict, the harness shall retry **in the same session** with a `RETRY_CONTINUE` message containing the unmet criteria and reviewer feedback, after compacting completed attempts to stubs.
- **REQ-12:** If the compacted history still exceeds the context budget (75% of `num_ctx`), the harness shall start a fresh session seeded with the task, a capped copy of the prior answer, and a digest of all reviewer feedback, logging a `context_reset` event.
- **REQ-13:** On a passing verdict, the harness shall write `final_output.txt`; if all attempts are exhausted without a pass, it shall write `final_output_UNVERIFIED.txt` and warn.
- **REQ-14:** With `--best-of N`, the harness shall run N independent first attempts in separate copied candidate workspaces, review each, score by `(passed, criteria_met)` with ties going to the earliest candidate, promote the winner's files into the main workspace, and continue the retry loop from the winner's session and verdict.
- **REQ-15:** Every run shall end by writing `report.html`, recording a row in `runs/history.db`, firing the `run_end` hook, appending the memory note (unless `--no-memory`), and printing a timing summary and artifact paths — including on failed runs.

### 3.3 Independent Review

#### 3.3.1 Description and Priority

A reviewer model judges the workspace artifacts — not the executor's claims — and returns a structured verdict that drives the retry loop. **Priority: High.**

#### 3.3.2 Functional Requirements

- **REQ-16:** The review prompt shall include the goal, the criteria (or an instruction to derive 3–6 binary-checkable ones), the capped executor output, the todo checklist labeled *self-reported — verify*, a workspace file listing, the evidence for what changed this attempt, and automated check output. Change evidence shall be the attempt's unified git diff where git evidence is enabled, falling back to capped per-file snapshots otherwise.
- **REQ-17:** Automated checks shall run pytest against any `test_*.py` files in the workspace and include capped output as evidence.
- **REQ-18:** The reviewer shall by default receive a read-only tool subset (`read_file`, `list_files`, `run_script`, `run_shell`) so it can gather evidence but not modify the work; `--no-reviewer-tools` shall remove even those. Tool restriction shall be enforced by the session's allowed-tool set, not by prompt text alone.
- **REQ-19:** Verdict parsing shall be resilient: extract the first balanced JSON object (tolerating code fences); on failure re-ask once in strict mode; then fall back to YES/NO regex matching; then default to `passed = False`. Review shall never raise and never abort the run.
- **REQ-20:** The `Verdict` shall expose `passed`, per-criterion `{criterion, met, note}` entries, `feedback`, and helpers for unmet criteria and a summary line.
- **REQ-21:** The reviewer shall run at low temperature (default 0.1) for consistency.

### 3.4 Tool Belt

#### 3.4.1 Description and Priority

The executor's capabilities: file manipulation, code execution, web access, self-organization, and delegation — all dispatched through a single choke point. **Priority: High.**

#### 3.4.2 Functional Requirements

- **REQ-22:** All tool invocations shall pass through a single dispatch function that: rejects unknown tools with `[ERROR]`, parses JSON-string arguments (some models send strings), applies the permission gate, fires `pre_tool`/`post_tool` hooks, executes the tool, and logs a `tool` event with an ok/error flag.
- **REQ-23:** File tools (`write_file`, `read_file` with offset/`max_chars` paging, `list_files` recursive with sizes and junk filtering, `edit_file` exact unique-snippet replace with a close-match hint on miss, `grep_files` regex or literal returning `file:line` results) shall operate only on paths resolved through the workspace jail.
- **REQ-24:** `write_file` and `edit_file` shall emit a display-only unified diff to the UI; the string returned to the model shall be unchanged by this.
- **REQ-25:** Execution tools shall run with the workspace as cwd and per-call timeouts: `run_python` (`python3 -c`, 120 s), `run_script` (saved `.py`, 120 s), `run_shell` (360 s).
- **REQ-26:** Under the default `shell.mode: "allowlist"`, `run_shell` shall accept only a single plain command whose executable is on `policy.shell.allowed` (by default `pip`, `pip3`, `python3`, `pytest`, `ls`, `mkdir`, `cat`, `echo`), shall reject shell metacharacters (`;`, `|`, `&`, `<`, `>`, backtick, `$`, newline), and shall execute with `shell=False` via `shlex` splitting. An empty `allowed` list shall refuse every command with an error naming the policy. Under `shell.mode: "any"`, or under `--sandbox` where the container is the guardrail, the command shall be handed to a real shell (`sh -c`) with no allowlist or metacharacter filtering.
- **REQ-27:** `web_search` shall use DuckDuckGo via the optional `ddgs` package; `fetch_page` shall extract readable text via optional `trafilatura` with a tag-stripping fallback, shall refuse local and private-network addresses, and shall cap returned text at `page_text_max`. Missing optional packages shall produce actionable error strings, not crashes.
- **REQ-28:** `set_todos` shall replace the executor's checklist wholesale after validation, update the live dashboard, be logged as `todos` events, and deliver its final state to the reviewer marked as self-reported.
- **REQ-29:** Tool results returned to the model shall be capped at `tool_result_max` characters.
- **REQ-30:** Filesystem/exec tools shall receive their workspace binding at configure time (partial application); the model shall never see or pass filesystem paths outside the jail.

### 3.5 Subagents

#### 3.5.1 Description and Priority

Delegation of self-contained subtasks to a fresh child session, keeping the parent's context small. **Priority: Medium.**

#### 3.5.2 Functional Requirements

- **REQ-31:** `spawn_subagent(task, kind)` shall run a child session under `SUBAGENT_SYSTEM` on the executor model with its own tool-round budget (`subagent_max_rounds`, default 20).
- **REQ-32:** The child's tool belt shall exclude `spawn_subagent` (depth guard — no recursive spawning) and `set_todos` (the checklist belongs to the parent).
- **REQ-33:** Only the child's final text shall be returned to the parent, capped like any tool result; each spawn shall be bracketed by `subagent_start`/`subagent_end` events, and the UI shall nest the child's tool lines under a `└` prefix.

### 3.6 Workspace Isolation & Resume

#### 3.6.1 Description and Priority

Per-run filesystem containment and continuation of earlier work. **Priority: High.**

#### 3.6.2 Functional Requirements

- **REQ-34:** Each run shall create `runs/run_TIMESTAMP/` containing a `workspace/` subdirectory and shall maintain a `runs/latest` symlink; harness artifacts (transcript, events, report) live beside — never inside — the workspace.
- **REQ-35:** Path resolution shall raise on any name that escapes the workspace root (`../`, absolute paths); every file/exec tool shall use this resolver.
- **REQ-36:** Changed-file detection shall be mtime-based (with 1 s slack) from an attempt-start baseline, so files written by any means (file tool, script, shell) are caught.
- **REQ-37:** `snapshot_files` shall read changed files for the reviewer with per-file and total character caps.
- **REQ-38:** `--workspace DIR` shall reuse an existing workspace, and the harness shall inject a mechanical summary of the previous run (goal, verdict, feedback, files — derived from its `events.jsonl` / `attempt_history.json`) into the executor system prompt. `-r/--resume` shall select that workspace by run id, by directory, or — bare — by the latest run, and shall reuse the previous run's goal when no goal source is given.

### 3.7 LLM Sessions & Context Management

#### 3.7.1 Description and Priority

The plumbing that makes small context windows workable. **Priority: High.**

#### 3.7.2 Functional Requirements

- **REQ-39:** All model calls shall go through a single HTTP path to Ollama `/api/chat` that injects `num_ctx`, handles streaming and non-streaming modes, logs an `llm` event with real token counts (`prompt_eval_count`), and warns when a prompt exceeds 85% of `num_ctx`.
- **REQ-40:** On an HTTP 400 "does not support thinking" response, the harness shall record the model in a no-think set, drop the `think` parameter, and retry transparently.
- **REQ-41:** Streaming shall aggregate Ollama's line-JSON deltas into a message of the same shape as the non-streaming reply, feeding thinking and content deltas to the UI live, and shall always signal stream end even on error.
- **REQ-42:** A `Session` shall hold conversation history and an allowed-tool set derived from its advertised schemas; a tool not in the schemas shall be unexecutable within that session.
- **REQ-43:** `Session.send` shall run a tool loop of at most `max_tool_rounds` (default 15) rounds; if exhausted, one final no-tools call shall force a text answer, flagged as forced in the UI.
- **REQ-44:** Thinking text shall be displayed once and then removed from history (retained only under `--full-context`).
- **REQ-45:** When history exceeds 75% of `num_ctx`, in-place compaction shall shrink old tool/assistant turns while never touching the system prompt, the first user task, or the last `compact_keep_last` messages; `--full-context` shall disable all trimming and caps.
- **REQ-46:** Each model role shall support independent model override (`-em`, `-rm`, `-gm`) falling back to `--model`, with per-role sampling defaults (executor 0.7, reviewer 0.1, goalsmith 0.3); resolved models shall be logged in the `run_start` event and shown in the dashboard header.

### 3.8 Extensibility: MCP, Hooks, Saved Commands

#### 3.8.1 Description and Priority

Integration points for user-supplied capability. **Priority: Medium.**

#### 3.8.2 Functional Requirements

- **REQ-47:** With `--mcp`, the harness shall start the Docker MCP Toolkit gateway (`docker mcp gateway run`, optional `--mcp-profile`), speak JSON-RPC 2.0 over the subprocess's stdio without an SDK dependency, discover its tools, skip tools whose names clash with built-ins, register the rest into the tool registry and schema list, append a note listing them to the executor system prompt, and close the gateway on exit.
- **REQ-48:** A repo-root `hooks.json` shall map events (`pre_tool`, `post_tool`, `attempt_end`, `run_end`) to shell commands with placeholder substitution (`{tool}`, `{file}`, `{run_dir}`, `{workspace}`, `{attempt}`, `{passed}` as applicable); tool events shall support an optional `fnmatch` `match` glob.
- **REQ-49:** Hooks shall be observe-only: exit codes and output shall never block or modify the run; failures shall warn and continue; each hook shall run with a 10 s timeout and be logged as a `hook` event.

### 3.9 Persistence, Memory & Reporting

#### 3.9.1 Description and Priority

Everything a run leaves behind. **Priority: Medium.**

#### 3.9.2 Functional Requirements

- **REQ-50:** Each run shall write `events.jsonl` (one JSON object per line covering `run_start`, `llm`, `tool`, `attempt`, `permission`, `hook`, `todos`, `memory`, `subagent_*`, …) and a human-readable `transcript.md`; each failed attempt's prose shall be saved as `attempt_N.txt` alongside `attempt_history.json`.
- **REQ-51:** Each run shall render a self-contained `report.html` (pure stdlib, inline CSS, collapsible sections) from the event log and attempt history; report generation shall never raise, and shall be regenerable by hand via `python3 -m harness.report <run_dir>`.
- **REQ-52:** Each run shall append one row to a sqlite database at `runs/history.db`, queryable via the `history` subcommand.
- **REQ-53:** After each run (unless `--no-memory`), the harness shall ask the goalsmith model for 3–6 durable lessons and append them as a dated section to `<workspace>/AGENT.md`, trimming oldest sections to a size budget while preserving any user-authored preamble; the file shall be injected into the executor system prompt on the next run against that workspace. Memory writing shall never raise.

### 3.10 Presentation Layer

#### 3.10.1 Description and Priority

Terminal UX for interactive and headless use. **Priority: Medium.**

#### 3.10.2 Functional Requirements

- **REQ-54:** When `rich` is importable and stdout is a TTY, the harness shall render a live dashboard showing goal/model/attempt/phase with elapsed time, a color-coded context-token progress bar, a rolling recent-tools panel, a live streaming tail, the todo checklist, the criteria checklist, per-file change totals, and the last verdict; it shall warn inline when a tool call repeats or no file has changed for an extended period; and it shall offer on-demand overlays for the transcript, tool history, last diff, the per-attempt criteria ledger, and the per-role token budget. Persistent lines (answers, verdicts, warnings) shall print above the live region.
- **REQ-55:** Without `rich` or a TTY, all the same information shall degrade to plain progressive printing; no harness feature may depend on the rich backend being present.
- **REQ-56:** Final answers shall render as Markdown (with a plain-text fallback on parse failure); file writes/edits shall show unified diff previews capped at ~80 lines; per-call LLM stats (seconds, prompt→eval tokens) shall be displayed.
- **REQ-57:** On run completion the harness shall ring the terminal bell and, on macOS, post a notification banner; both best-effort and suppressed by `--no-notify`.
- **REQ-58:** `--no-stream` shall switch all model calls to non-streaming mode.

### 3.11 Evaluation Suite

#### 3.11.1 Description and Priority

Objective measurement of harness and model changes. **Priority: Medium.**

#### 3.11.2 Functional Requirements

- **REQ-59:** `evals/` shall provide 8 benchmark goals (4 multi-file projects, 4 data/file-processing tasks), each with seeded input files and a programmatic checker that judges the workspace independently of the harness's own reviewer.
- **REQ-60:** `evals/run_evals.py` shall support `--label` (names the results JSON), `--goals` (subset), `--repeat N` (N runs per goal, each in its own workspace, recorded per run and aggregated into a per-goal pass rate), `--compare <baseline.json>` (before/after diff reporting a pass-rate delta, naming goals that both pass and fail across repeats, and warning explicitly when either side has only one run per goal), model overrides (e.g. `--reviewer-model`), and `--agent-args` for arbitrary harness-flag A/B tests; eval runs shall pass `--yolo` automatically.

---

## 4 External Interface Requirements

### 4.1 User Interfaces

- **UI-1:** The sole user interface is the command line: `python3 agent.py <goal-source> [flags]` plus the `history` subcommand. There is no GUI; the per-run `report.html` is a static artifact opened in a browser.
- **UI-2:** Interactive elements: the live rich dashboard (§3.10), streaming output, y/n/a/c permission prompts, single-key dashboard controls (pause, message, interrupt, attempt ledger, budget, transcript, quit), and terminal-bell/banner notifications. All are TTY-conditional with plain fallbacks.
- **UI-3:** Errors surfaced to the model use a uniform `[ERROR] ...` string convention; errors surfaced to the user are printed as warnings above the live region and never silently swallowed at run level.

### 4.2 Hardware Interfaces

- **HW-1:** No direct hardware interfaces. The Ollama server's GPU/CPU is managed entirely by Ollama; the harness only needs a network route to it (§4.4).

### 4.3 Software Interfaces

- **SW-1 — Ollama server (≥ 0.9):** HTTP POST `/api/chat` with JSON payloads (messages, tool schemas, `options.num_ctx`, `think`, `stream`); consumes streamed line-JSON deltas or single JSON replies, including `tool_calls` and `prompt_eval_count`.
- **SW-2 — Docker MCP gateway (optional):** subprocess `docker mcp gateway run`; JSON-RPC 2.0 over stdio for tool discovery (`tools/list`) and invocation (`tools/call`).
- **SW-3 — Python packages:** `requests` (HTTP), `rich>=13` (dashboard), optional `ddgs` (search), `trafilatura` (page extraction), `pytest` (automated checks and the harness's own test suite).
- **SW-4 — sqlite3 (stdlib):** the `runs` table in `runs/history.db`.
- **SW-5 — User-authored config files:** `hooks.json` (event → shell command map) and `commands/*.md` (frontmatter + goal template).
- **SW-6 — Filesystem contract:** the `runs/` tree described in §3.6/§3.9 is a stable output interface consumed by `report.py`, `evals/run_evals.py`, and the resume-summary loader.

### 4.4 Communications Interfaces

- **CI-1:** HTTP/1.1 over TCP to the Ollama server (default port 11434), plain HTTP on a trusted LAN; per-request timeout `request_timeout` (default 600 s).
- **CI-2:** Outbound HTTPS for `web_search`/`fetch_page`; `fetch_page` shall refuse localhost and private-network address ranges.
- **CI-3:** Local stdio (pipes) to the MCP gateway subprocess.
- **CI-4:** No inbound network listeners of any kind.

---

## 5 Other Non-Functional Requirements

### 5.1 Performance Requirements

- **PR-1:** LLM inference dominates wall-clock time; the harness shall not add avoidable model calls — specifically, doomed attempts (no tools used, or stalled output) shall skip the review call entirely (REQ-10).
- **PR-2:** Prompt construction shall respect the configured context window: warn at 85% usage, compact at 75%, and never exceed `num_ctx` by design (REQ-39, REQ-45).
- **PR-3:** All unbounded text (tool results, page text, snapshots, retry context) shall be capped by the `config.py` knobs so a single large artifact cannot blow the budget or the UI.
- **PR-4:** Streaming shall render tokens as they arrive; the dashboard refresh runs at ~4 Hz without noticeable CPU load.
- **PR-5:** Per-call subprocess timeouts (120 s / 120 s / 360 s exec tools, 10 s hooks) shall guarantee that no child process can hang a run indefinitely.
- **PR-6:** Per-function timing shall be accumulated and printed at run end so the operator can see where time went.

### 5.2 Safety Requirements

- **SF-1:** The workspace jail (REQ-35) shall prevent the agent from reading or writing any file outside its per-run `workspace/`, protecting the host system and the harness's own code from agent action.
- **SF-2:** Under the default policy, arbitrary shell execution shall be impossible via `run_shell`: allowlist + metacharacter rejection + `shell=False` (REQ-26). This guarantee is explicitly waived by `shell.mode: "any"` and by `--sandbox`, both of which hand over a real shell — the container, not the allowlist, is the boundary in the sandboxed case.
- **SF-3:** Code-executing tools (the policy's `execution.require_approval`, by default `run_shell`, `run_python`, `run_script`) shall require explicit interactive approval (y/n/a, plus `c` to grant a base command for the run) unless `--yolo` was given — and a policy may forbid `--yolo` outright via `allow_yolo: false`; non-interactive sessions shall auto-deny with an actionable error rather than silently executing (see §5.3).
- **SF-4:** `fetch_page`'s private-address block shall prevent the agent from being steered into probing the local network (SSRF-style).
- **SF-5:** Failed runs shall be clearly labeled (`final_output_UNVERIFIED.txt`) so unverified output is never mistaken for verified output.

### 5.3 Security Requirements

- **SE-1:** The permission gate shall cover exactly the subprocess-spawning tools and shall apply uniformly to executor, reviewer, and subagents (single choke point in tool dispatch); every allow/deny decision shall be logged as a `permission` event.
- **SE-2:** An "always" grant shall be scoped to the current run only.
- **SE-3:** Reviewer tool restriction shall be enforced structurally (session allowed-set, REQ-18/REQ-42), not by prompt instructions.
- **SE-4:** Subagent recursion shall be structurally blocked (REQ-32).
- **SE-5:** `hooks.json` commands run through the shell **by design** and are therefore trusted user-authored configuration; this trust boundary shall be documented (AS-4) and hooks shall remain unable to alter run behavior (REQ-49).
- **SE-6:** No credentials, API keys, or user data are collected, stored, or transmitted; the only network peers are the user-configured Ollama server and, when web tools are used, public web endpoints.
- **SE-7:** LAN traffic to Ollama is unencrypted HTTP; deployment on untrusted networks is out of scope (see Appendix, issue I-2).

### 5.4 Software Quality Attributes

- **QA-1 — Robustness:** Non-core subsystems (review parsing, report generation, memory writing, notifications, hooks, optional web tools) shall degrade or fall back rather than abort a run; the run loop is the only component allowed to end a run.
- **QA-2 — Testability:** The harness shall be fully exercisable headless; the pytest suite (372 tests under `tests/`) covers the loop, sessions, streaming, review, tools, permissions, policy, hooks, memory, history, resume, subagents, todos, commands, skills, git evidence, sandbox, reporting, and office fidelity. Dashboard rendering and the interactive key controls are covered headlessly, so a TTY is not required to exercise them.
- **QA-3 — Measurability:** Any behavior change shall be benchmarkable with the eval suite against a baseline (REQ-59/60); real token counts and per-phase timings shall be logged per run.
- **QA-4 — Observability:** Every significant action (LLM call, tool call, permission decision, hook, attempt, verdict) shall appear in `events.jsonl`; a human shall be able to reconstruct a run from `transcript.md` or `report.html` alone.
- **QA-5 — Portability:** macOS and Linux, TTY and non-TTY, with and without optional packages.
- **QA-6 — Maintainability:** One file per concern in `harness/` (see the Readme architecture diagram); exactly two deliberate global handles (`config.settings`, `runlog.current`).
- **QA-7 — Usability preference:** interactive clarity (live dashboard, diffs, notifications) is favored over minimal output, but never at the cost of headless correctness (plain fallback is the contract, rich is the enhancement).

---

## 6 Other Requirements

- **OR-1 — Licensing:** The project is MIT-licensed; dependencies must remain compatible with MIT distribution.
- **OR-2 — Database:** `runs/history.db` shall remain a single-file sqlite database with one row per run; schema changes must tolerate rows written by earlier versions (additive columns only).
- **OR-3 — Artifact stability:** `events.jsonl` event names and the `runs/` directory layout are consumed by `report.py`, the resume loader, and the eval runner; changes require updating all three consumers in the same change.
- **OR-4 — Internationalization:** Not required; all prompts, UI text, and artifacts are English-only.
- **OR-5 — Reuse objective:** Claude-Code-inspired mechanisms (todos, subagents, hooks, memory, saved commands, permission modes) shall keep semantics familiar to Claude Code users where practical.

---

## Appendix: Issues List

| # | Issue | Status |
|---|---|---|
| I-1 | **Sandboxed execution** — container-based isolation for the exec tools. | **Closed** — `--sandbox` runs them in a per-workspace Docker container (`tools/execute.py`, `tests/test_sandbox.py`). The allowlist remains the default: the container is opt-in, so `policy.shell` still carries the guarantee when it is off. |
| I-2 | **Untrusted-network deployment** — plain-HTTP LAN transport to Ollama; TLS/auth story is TBD if the server ever leaves the trusted LAN. | Open |
| I-3 | **Multi-phase planning** — plan → execute-per-phase → review-per-phase for large goals. | Open (roadmap) |
| I-4 | **Git-aware workspace** — commit per attempt and give the reviewer real diffs instead of mtime-based snapshots. | **Closed** — `workspace_git` (on by default, `--no-git`): `init_git`/`commit_attempt`/`attempt_diff`, consumed by `review.py` with snapshots as the no-git fallback (`tests/test_git_evidence.py`). |
| I-5 | **Reviewer model selection** — the default reviewer is the executor's model; a benchmarked ~30B-class default is TBD pending eval A/B runs. Blocked on I-12: with one run per goal the suite cannot currently tell a better reviewer from a lucky one. | Open |
| I-6 | **Subagent discoverability** — smaller executor models rarely call `spawn_subagent` unless the goal names delegation explicitly; whether the harness should suggest delegation automatically is TBD. | Open |
| I-7 | **Windows client support** — the harness targets macOS/Linux; Windows-as-client (path handling, notifications, symlinks for `runs/latest`) is untested and TBD. | Open |
| I-8 | **Office-document tools** — first-class read/edit/write tools for `.xlsx`/`.csv` and `.docx` (presentations later) in `harness/tools/docs.py`, with `policy.json` entries. Editing an existing file with full round-trip fidelity is the requirement, not just authoring a new one. | Open (roadmap) |
| I-9 | **Binary-aware review evidence** — `review.py` currently snapshots deliverables as text; office formats need extraction (docx/xlsx/pptx → text) in `snapshot_files` and `automated_checks`, or the verdict degrades to "the file exists". Blocks I-8. | Open (roadmap) |
| I-10 | **xlsx edit fidelity guard** — measured (openpyxl 3.1.5): charts, images, conditional formatting, data validation, defined names, comments and merges survive a round-trip, but `<extLst>` constructs (sparklines, slicers, x14 rules) are dropped from *inside* surviving parts, `customXml/` is dropped, `vbaProject.bin` needs `keep_vba=True`, and `data_only=True` destroys every formula. Edit tools must fingerprint before/after (part inventory + unmodelled-construct scan + formula count) and refuse the write on any unrequested loss. Blocks I-8 for xlsx only — python-docx 1.2.0 round-trips docx losslessly, including content controls, tracked changes and fields. | **Closed** — `harness/office.py` + `tests/test_office.py` |
| I-11 | **Presentations (`.pptx`) and surgical part-level patching** — deferred until I-8 is in real use. Patching (rewrite one OPC part, leave the rest byte-identical) is the fallback if I-10's guard refuses edits users legitimately want; there is no point building it before that is observed. | Open (deferred) |
| I-12 | **A/B testing for the main loop** — `evals/run_evals.py` runs each of the 8 goals once per label and labels any single flip `improved`/`REGRESSED`, which at `temperature 0.7` cannot separate a real gain from sampling noise (one goal = 12.5 points of pass rate). **Steps 1-2 of 5 landed** — `--repeat N`, per-repeat workspaces, per-goal pass rates, flaky-goal detection, a comparison that states when it cannot tell signal from noise, and tool-efficiency metrics (call count, error rate, repeated-call share). Remaining design (see Readme roadmap): three metrics — checker pass rate, cost (tokens/wall/llm secs) and tool efficiency (call count, error rate, repeated-call share) — all derivable from existing `events.jsonl` fields; arms defined as `agent.py` flags plus optional `prompts.py` template overrides; arms interleaved and paired by `(goal, repeat)` so server drift is shared; results reported as an effect with a bootstrap confidence interval and **no** ship/don't-ship verdict; and the minimum detectable effect printed before the run starts, since an overnight budget (8 goals × 5 repeats × 2 arms ≈ 80 runs) gives only 40 paired observations per arm and therefore cannot resolve pass-rate differences below roughly 15 points — the continuous metrics are where that budget pays. Only harness-side change is a prompt-override hook in `prompts.py`. Prerequisite for deciding I-5 and any prompt or loop-structure change on evidence. | Open (roadmap) |
