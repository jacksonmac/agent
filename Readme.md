# Execute → Review → Retry Agent

A lightweight autonomous agent harness powered by a local [Ollama](https://ollama.com/) instance
(designed to run against an Ollama server on another computer on your network).

An **executor** LLM does the work with real tools in an isolated per-run workspace, a
**reviewer** LLM checks the actual files on disk against the goal, and failed attempts are
retried with the reviewer's feedback — in the *same* conversation, so the model remembers
what it already tried.

## How it works

1. **(Optional) Goalsmith** — with `-sg`, an LLM rewrites your rough request into a GOAL,
   a checklist of binary success CRITERIA, and a TASK briefing. With `-g`, your text is used
   as both goal and task and the reviewer derives its own criteria.
2. **Execute** — the executor gets the goal + task and a tool belt:
   `list_files`, `read_file`, `write_file`, `edit_file` (targeted replace),
   `run_python`, `run_script`, `run_shell` (allowlisted), `web_search`, `fetch_page`,
   plus any Docker MCP Toolkit tools with `--mcp`. All file access is jailed to the
   run's workspace.
3. **Review** — the reviewer sees the workspace listing, the content of every file the
   attempt created or modified (however it was written), and automated `pytest` output if
   tests exist. It can also inspect the workspace with read-only tools. It replies with a
   JSON verdict: pass/fail per criterion plus actionable feedback.
4. **Retry** — on a failed verdict the same session continues with the unmet criteria and
   feedback (earlier attempts are compacted to stubs to stay inside the context window).
   If the history won't fit, the harness falls back to a fresh conversation and logs a
   `context_reset` event. Attempts that make zero tool calls or repeat the previous
   attempt are caught early without wasting a review call.

## Per-run isolation

Every run gets its own directory — nothing is written to the repo root, and leftovers
from one run can't confuse the next:

```
runs/run_20260703_154139/
├── workspace/           # the ONLY directory the agent can see and touch
├── transcript.md        # human-readable log of every attempt + verdict
├── events.jsonl         # machine log: llm calls (real token counts), tool calls, attempts
├── attempt_1.txt        # prose of each failed attempt
├── attempt_history.json
└── final_output.txt     # or final_output_UNVERIFIED.txt if attempts ran out
runs/latest              # symlink to the most recent run
```

Reuse a previous workspace (to continue earlier work) with
`--workspace runs/run_.../workspace`.

## Requirements

- **Python 3.10+**
- **Ollama ≥ 0.9** (for the `think` parameter)
- `pip install requests` (plus optional `ddgs` and `trafilatura` for the web tools,
  and `pytest` to run the harness's own tests)

## Usage

```bash
python3 agent.py -g "Write fizzbuzz.py that prints FizzBuzz for 1-30, run it, and confirm the output"
python3 agent.py -sg "I need a small flask api for notes"          # goalsmith mode
python3 agent.py -g "..." --model qwen3.5:9b --attempts 3
python3 agent.py -g "..." --reviewer-model qwen3.6:27b             # bigger model as judge
python3 agent.py -g "..." --num-ctx 32768 --full-context           # no trimming at all
python3 agent.py -g "..." --mcp                                    # + Docker MCP Toolkit tools
python3 agent.py -g "..." --workspace runs/latest/workspace        # continue earlier work
python3 agent.py -g "..." --no-reviewer-tools                      # faster, snapshot-only review
```

## Project structure

```
agent.py              # entry-point shim (python3 agent.py -g ...)
harness/
├── cli.py            # argument parsing and wiring
├── config.py         # all settings, incl. per-role temperature (reviewer runs at 0.1)
├── prompts.py        # executor / reviewer / goalsmith system prompts
├── llm.py            # Ollama chat, Session (persistent retry memory), context budgeting
├── workspace.py      # per-run dirs, path jail, mtime-based change tracking
├── runlog.py         # transcript.md + events.jsonl writers
├── review.py         # JSON verdicts with fallback parsing, automated pytest checks
├── goalsmith.py      # -sg: request → goal + criteria + task
├── run.py            # the execute → review → retry loop
└── tools/            # tool registry, file/exec/web tools, Docker MCP gateway client
tests/                # pytest suite for the harness itself (python3 -m pytest)
```

## Ollama server setup (separate computer)

1. Allow LAN access to Ollama on the server (Windows PowerShell, admin):
```powershell
New-NetFirewallRule -DisplayName "Ollama LAN Access" -Direction Inbound -LocalPort 11434 -Protocol TCP -Action Allow -Profile Private
# this allows any computer on your network to access ollama
```
2. Find the server's IP with `ipconfig`.
3. Test it in a browser: `http://<server-ip>:11434` should answer "Ollama is running".
4. Point the agent at it: `--url http://<server-ip>:11434` (or change the default in
   `harness/config.py`).

## Configuration

Defaults live in `harness/config.py` (`Settings` dataclass): server URL, default model,
context window, truncation caps, and per-role sampling options. Everything relevant is
also overridable per run via CLI flags (`--url`, `--model`, `--reviewer-model`,
`--num-ctx`, `--attempts`, ...).

## Roadmap

- Multi-phase planning for big goals (plan → execute each phase → review each phase)
- Streaming output so long generations show progress
- Persist run history to sqlite for cross-run "what did I do last time" queries
- Sandboxed execution (containers) instead of the command allowlist

## License

MIT License
