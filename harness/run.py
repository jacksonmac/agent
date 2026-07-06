"""The execute → review → retry loop."""

from __future__ import annotations

import difflib
import json
import time

from .config import settings
from .llm import Session, _role_options, cap, print_timing_summary
from .prompts import EXECUTOR_SYSTEM, RETRY_CONTINUE, RETRY_NOTE
from .review import Verdict, review
from .runlog import RunLog
from .tools import TOOL_SCHEMAS
from .workspace import Workspace


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _feedback_digest(history: list[str]) -> str:
    """All reviewer feedback, nothing silently dropped: the last two verdicts
    in full, older ones as one-line stubs."""
    if len(history) <= 2:
        return "\n".join(history)
    stubs = [h.splitlines()[0][:120] for h in history[:-2]]
    return "\n".join(stubs + history[-2:])


def _new_session(model: str, executor_system: str) -> Session:
    return Session(model, executor_system, TOOL_SCHEMAS,
                   think=settings.executor.think, label="executor",
                   options=_role_options(settings.executor))


def main(model: str, goal: str, task: str, ws: Workspace, log: RunLog,
         max_attempts: int = 5, executor_system: str = EXECUTOR_SYSTEM,
         criteria: list[str] | None = None):
    attempts = []            # keep EVERY attempt + verdict, nothing gets overwritten
    feedback_history = []    # ALL reviewer feedback, so retries fix everything at once
    prev_answer = None
    answer = None
    session = _new_session(model, executor_system)
    # the executor sees the GOAL as well as the task — the reviewer judges
    # against the goal, so the worker should know it too
    user_msg = f"GOAL: {goal}\n\nTASK: {task}" if goal != task else task

    log.event("run_start", goal=goal, task=task, model=model,
              reviewer=settings.reviewer_model or model, max_attempts=max_attempts,
              criteria=criteria or [])
    log.transcript(f"# Agent run {time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n"
                   f"**Goal:** {goal}\n\n**Task:** {task}\n\n")

    for attempt in range(1, max_attempts + 1):
        print(f"\n=== EXECUTING (attempt {attempt}/{max_attempts}) ===")
        ws.begin_attempt()
        answer = session.send(user_msg)
        changed_files = ws.files_changed_this_attempt()

        # Stall detection: reviewing a do-nothing or repeat attempt wastes an
        # expensive LLM call — skip straight to a retry that redirects it.
        no_tools = (session.last_tool_calls == 0 and not changed_files)
        stalled = (prev_answer is not None and
                   difflib.SequenceMatcher(None, _normalized(answer),
                                           _normalized(prev_answer)).ratio() > 0.95)
        if no_tools:
            print(f"attempt {attempt} made zero tool calls — skipping review, "
                  f"telling it to actually do the work")
            verdict = Verdict(passed=False, feedback=(
                "skipped review: you made no tool calls, so nothing was actually done. "
                "You MUST use your tools — start with list_files to see the workspace, "
                "then do the work. Never ask for clarification; make a sensible "
                "assumption and proceed."))
        elif stalled:
            print(f"attempt {attempt} is nearly identical to the previous one — "
                  f"skipping review, demanding a new approach")
            verdict = Verdict(passed=False, feedback=(
                "skipped review: output nearly identical to the previous failed attempt"))
        else:
            print(f"\n=== REVIEWING (attempt {attempt}) ===")
            reviewer_model = settings.reviewer_model or model
            verdict = review(reviewer_model, goal, answer, ws,
                             criteria=criteria, changed_files=changed_files)
            print(f"reviewer said: passed={verdict.passed} {verdict.summary()!r}")

        passed = verdict.passed
        attempts.append({"attempt": attempt, "output": answer,
                         "files": changed_files, "passed": passed,
                         "verdict": verdict.summary(),
                         "criteria": verdict.criteria})
        log.event("attempt", n=attempt, passed=passed, stalled=stalled,
                  no_tools=no_tools, files=changed_files, feedback=verdict.summary())
        log.transcript(f"\n## Attempt {attempt} — {'PASSED' if passed else 'FAILED'}\n\n"
                       f"{answer}\n\n"
                       + (f"**Files changed:** {', '.join(changed_files)}\n\n"
                          if changed_files else "")
                       + (f"**Reviewer:** {verdict.summary()}\n" if not passed else ""))

        if passed:
            print(f"WE DID IT on attempt {attempt}")
            ws.save_artifact("final_output.txt", answer)
            break

        # everything else (NO or malformed-treated-as-NO) → retry
        print(f"goal not met on attempt {attempt}, saving output and retrying")
        ws.save_artifact(f"attempt_{attempt}.txt", answer)
        feedback_history.append(f"[attempt {attempt}] {cap(verdict.summary(), 800)}")

        # Preferred path: continue the SAME session — the model keeps its own
        # memory of what it read, wrote, and saw fail. Compact the finished
        # attempt down to stubs first so the history stays under budget.
        session.compact_completed_attempts()
        if session.over_budget():
            # bounded worst case: fall back to the old fresh-conversation retry
            print("  [context] history over budget even after compaction — "
                  "starting a fresh session for the next attempt")
            log.event("context_reset", after_attempt=attempt)
            session = _new_session(model, executor_system)
            user_msg = (f"GOAL: {goal}\n\nTASK: {task}" if goal != task else task) \
                + RETRY_NOTE.format(
                    feedback=_feedback_digest(feedback_history),
                    previous=cap(answer, settings.retry_prev_max),
                )
        else:
            unmet = verdict.unmet()
            unmet_text = ("Unmet criteria:\n" + "\n".join(f"- {u}" for u in unmet)
                          if unmet else "")
            user_msg = RETRY_CONTINUE.format(goal=goal, unmet=unmet_text,
                                             feedback=verdict.feedback or verdict.summary())
        if stalled:
            user_msg += ("\n\nIMPORTANT: your last two attempts were nearly "
                         "identical. Take a DIFFERENT approach this time.")
        prev_answer = answer
    else:
        print(f"[WARNING] hit max attempts ({max_attempts}) without meeting the goal")
        if answer is not None:
            # don't leave the user empty-handed — the last attempt is still
            # the best artifact we have, just unverified
            ws.save_artifact("final_output_UNVERIFIED.txt", answer)

    ws.save_artifact("attempt_history.json", json.dumps(attempts, indent=2))
    print_timing_summary()
    print(f"\nRun artifacts: {ws.run_dir}\nWorkspace files: {ws.root}")
