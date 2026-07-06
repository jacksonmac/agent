"""Run logging: events.jsonl (machine-readable) + transcript.md (human-readable).

Same format as the pre-refactor logs (see runs/run_20260703_143037/):
    {"ts": ..., "event": "run_start", "goal": ..., "task": ..., "model": ...}
    {"ts": ..., "event": "llm", "label": "executor", "secs": ..., "prompt_tokens": ...}
    {"ts": ..., "event": "tool", "name": ..., "args": ..., "ok": ..., "result_chars": ...}
    {"ts": ..., "event": "attempt", "n": ..., "passed": ..., "stalled": ..., "feedback": ...}
"""

import json
import os
import time
from typing import Optional


class RunLog:
    def __init__(self, run_dir: str):
        self.events_path = os.path.join(run_dir, "events.jsonl")
        self.transcript_path = os.path.join(run_dir, "transcript.md")

    def event(self, event: str, **fields) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(self.events_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def transcript(self, text: str) -> None:
        with open(self.transcript_path, "a") as f:
            f.write(text)


# The active log for this process. chat_v2 / execute_tool_call are called from
# deep inside the loop; a module-level handle keeps their signatures simple.
current: Optional[RunLog] = None


def log_event(event: str, **fields) -> None:
    if current is not None:
        current.event(event, **fields)
