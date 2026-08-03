"""The execute → review → retry loop."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import time

from . import hooks as hooks_mod
from . import permissions
from . import todos as todos_mod
from . import ui
from .config import settings
from .llm import Session, _role_options, cap, print_timing_summary
from .prompts import (EXECUTE_AFTER_PLAN, EXECUTOR_SYSTEM, PLAN_PROMPT,
                      RETRY_CONTINUE, RETRY_NOTE, SELF_CHECK_PROMPT)
from .review import Verdict, review
from .runlog import RunLog
from .tools import TOOL_SCHEMAS
from .tools import configure as configure_tools
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


def _criteria_text(criteria: list[str] | None) -> str:
    if not criteria:
        return ""
    return ("SUCCESS CRITERIA:\n"
            + "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1)) + "\n")


def _new_session(model: str, executor_system: str) -> Session:
    return Session(model, executor_system, TOOL_SCHEMAS,
                   think=settings.executor.think, label="executor",
                   options=_role_options(settings.executor),
                   accept_user_messages=True)


def _run_attempt(session: Session, ws: Workspace, log: RunLog, user_msg: str,
                 goal: str, criteria: list[str] | None, attempt: int,
                 plan_first: bool) -> tuple[str, list[str], int]:
    """One executor attempt: optional plan turn, execution, optional
    self-check turn. Returns (answer, changed_files, tool_calls_used)."""
    ws.begin_attempt()

    if plan_first:
        ui.phase("planning (no tools)")
        plan = session.send(PLAN_PROMPT.format(task=user_msg), with_tools=False)
        log.event("plan", n=attempt, chars=len(plan))
        log.transcript(f"\n### Plan (attempt {attempt})\n\n{plan}\n")
        seeded = todos_mod.seed_from_plan(plan)
        if seeded:
            ui.info(f"seeded {seeded} todos from the plan")
        answer = session.send(EXECUTE_AFTER_PLAN)
    else:
        answer = session.send(user_msg)
    tool_calls = session.last_tool_calls
    changed = ws.files_changed_this_attempt()

    # verify-and-fix turn: cheaper than burning a review on an obvious miss.
    # Pointless when nothing was done — the stall gate handles that case.
    if settings.self_check and (tool_calls or changed):
        ui.phase("self-check")
        answer = session.send(SELF_CHECK_PROMPT.format(
            goal=goal, criteria=_criteria_text(criteria)))
        tool_calls += session.last_tool_calls
        changed = ws.files_changed_this_attempt()
        log.event("self_check", n=attempt, tools=session.last_tool_calls)

    return answer, changed, tool_calls


def _copy_workspace(src: str, dst: str) -> None:
    # .git stays with the main workspace: candidates are judged by snapshot,
    # and promotion must not clobber the run's attempt history
    shutil.copytree(src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git"))


def _run_candidates(model: str, goal: str, user_msg: str, ws: Workspace,
                    log: RunLog, executor_system: str,
                    criteria: list[str] | None, best_of: int
                    ) -> tuple[Session, str, list[str], Verdict]:
    """--best-of N: run N independent first attempts, each in its own
    workspace, review each, promote the winner's files into the main
    workspace and return its session/answer/verdict for the loop to continue
    from."""
    reviewer_model = settings.reviewer_model or settings.model
    best = None  # (score, -i, session, answer, changed, verdict, cand_ws)

    for i in range(1, best_of + 1):
        todos_mod.reset()  # candidates must not inherit each other's checklists
        ui.phase(f"candidate {i}/{best_of}")
        ui.info(f"=== candidate {i}/{best_of} ===")
        cand_ws = Workspace(ws.run_dir,
                            workspace_dir=os.path.join(ws.run_dir, f"candidate_{i}"))
        _copy_workspace(ws.root, cand_ws.root)  # inherit any seeded files
        configure_tools(cand_ws)
        session = _new_session(model, executor_system)
        answer, changed, _ = _run_attempt(session, cand_ws, log, user_msg,
                                          goal, criteria, attempt=1,
                                          plan_first=settings.plan_first)
        verdict = review(reviewer_model, goal, answer, cand_ws,
                         criteria=criteria, changed_files=changed)
        if verdict.criteria:
            ui.criteria(verdict.criteria, source=f"candidate {i}")
        met = sum(1 for c in verdict.criteria if c.get("met"))
        score = (int(verdict.passed), met)
        ui.info(f"candidate {i}: passed={verdict.passed}, criteria met={met}")
        log.event("candidate", i=i, passed=verdict.passed, criteria_met=met,
                  files=changed)
        # ties go to the earliest candidate (hence -i)
        key = (score, -i)
        if best is None or key > best[0]:
            best = (key, session, answer, changed, verdict, cand_ws)

    _, session, answer, changed, verdict, cand_ws = best
    winner = cand_ws.root.rsplit(os.sep, 1)[-1]
    ui.info(f"promoting {winner} to the main workspace")
    log.event("candidate_selected", winner=winner, passed=verdict.passed)
    log.transcript(f"\n**Best-of-{best_of}:** promoted {winner} "
                   f"(passed={verdict.passed})\n")
    _copy_workspace(cand_ws.root, ws.root)
    configure_tools(ws)  # rebind the disk tools to the main workspace
    return session, answer, changed, verdict


def main(model: str, goal: str, task: str, ws: Workspace, log: RunLog,
         max_attempts: int = 5, executor_system: str = EXECUTOR_SYSTEM,
         criteria: list[str] | None = None, best_of: int = 1):
    t0 = time.time()
    executor_model = settings.executor_model or model
    todos_mod.reset()
    if settings.workspace_git and ws.init_git():
        log.event("git_evidence", enabled=True)
    attempts = []            # keep EVERY attempt + verdict, nothing gets overwritten
    feedback_history = []    # ALL reviewer feedback, so retries fix everything at once
    prev_answer = None
    answer = None
    # the executor sees the GOAL as well as the task — the reviewer judges
    # against the goal, so the worker should know it too
    user_msg = f"GOAL: {goal}\n\nTASK: {task}" if goal != task else task

    # the guardrails go in the log, not just in memory: what a run was allowed
    # to do has to be answerable from events.jsonl afterwards, by someone who
    # wasn't there
    log.event("run_start", goal=goal, task=task, model=model,
              executor=executor_model,
              reviewer=settings.reviewer_model or settings.model,
              max_attempts=max_attempts,
              criteria=criteria or [], best_of=best_of,
              plan_first=settings.plan_first, self_check=settings.self_check,
              policy=settings.policy.as_dict(), yolo=permissions.is_yolo(),
              sandbox=settings.sandbox)
    log.transcript(f"# Agent run {time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n"
                   f"**Goal:** {goal}\n\n**Task:** {task}\n\n")
    if criteria:
        ui.criteria(criteria)  # all pending until the first review

    # attempt 1 either comes from the candidate round (--best-of) or runs inline.
    # The whole loop sits in a try: [q] raises ui.QuitRequested from any safe
    # point (including inside review or a subagent) and we still fall through
    # to the finalization below — artifacts, report, and history all get written.
    attempt = 0
    try:
        pending: tuple[str, list[str], Verdict] | None = None
        if best_of > 1:
            session, answer, changed, verdict = _run_candidates(
                executor_model, goal, user_msg, ws, log, executor_system, criteria, best_of)
            pending = (answer, changed, verdict)
        else:
            session = _new_session(executor_model, executor_system)

        for attempt in range(1, max_attempts + 1):
            ui.poll_controls()
            no_tools = stalled = False
            if pending is not None:
                answer, changed_files, verdict = pending
                pending = None
                ws.commit_attempt(attempt)  # record the promoted candidate
            else:
                ui.attempt(attempt, max_attempts)
                ui.phase("executing")
                plan_first = settings.plan_first and attempt == 1
                answer, changed_files, tool_calls = _run_attempt(
                    session, ws, log, user_msg, goal, criteria, attempt, plan_first)
                ws.commit_attempt(attempt)

                # Stall detection: reviewing a do-nothing or repeat attempt wastes an
                # expensive LLM call — skip straight to a retry that redirects it.
                no_tools = (tool_calls == 0 and not changed_files)
                stalled = (prev_answer is not None and
                           difflib.SequenceMatcher(None, _normalized(answer),
                                                   _normalized(prev_answer)).ratio() > 0.95)
                if no_tools:
                    ui.warn(f"attempt {attempt} made zero tool calls — skipping review, "
                            f"telling it to actually do the work")
                    verdict = Verdict(passed=False, feedback=(
                        "skipped review: you made no tool calls, so nothing was actually done. "
                        "You MUST use your tools — start with list_files to see the workspace, "
                        "then do the work. Never ask for clarification; make a sensible "
                        "assumption and proceed."))
                elif stalled:
                    ui.warn(f"attempt {attempt} is nearly identical to the previous one — "
                            f"skipping review, demanding a new approach")
                    verdict = Verdict(passed=False, feedback=(
                        "skipped review: output nearly identical to the previous failed attempt"))
                else:
                    ui.poll_controls()
                    ui.phase("reviewing")
                    reviewer_model = settings.reviewer_model or settings.model
                    verdict = review(reviewer_model, goal, answer, ws,
                                     criteria=criteria, changed_files=changed_files)

            passed = verdict.passed
            ui.verdict(passed, verdict.summary())
            if verdict.criteria:
                ui.criteria(verdict.criteria,
                            source=f"reviewer, attempt {attempt}")
            ui.attempt_result(attempt, passed, verdict.summary(),
                              verdict.criteria)
            attempts.append({"attempt": attempt, "output": answer,
                             "files": changed_files, "passed": passed,
                             "verdict": verdict.summary(),
                             "criteria": verdict.criteria})
            log.event("attempt", n=attempt, passed=passed, stalled=stalled,
                      no_tools=no_tools, files=changed_files, feedback=verdict.summary())
            hooks_mod.fire("attempt_end", attempt=attempt, passed=passed)
            log.transcript(f"\n## Attempt {attempt} — {'PASSED' if passed else 'FAILED'}\n\n"
                           f"{answer}\n\n"
                           + (f"**Files changed:** {', '.join(changed_files)}\n\n"
                              if changed_files else "")
                           + (f"**Reviewer:** {verdict.summary()}\n" if not passed else ""))

            if passed:
                ui.success(f"WE DID IT on attempt {attempt}")
                ws.save_artifact("final_output.txt", answer)
                break

            # everything else (NO or malformed-treated-as-NO) → retry
            ui.info(f"goal not met on attempt {attempt}, saving output and retrying")
            ws.save_artifact(f"attempt_{attempt}.txt", answer)
            feedback_history.append(f"[attempt {attempt}] {cap(verdict.summary(), 800)}")

            # --interactive: let the user steer the retry (may raise
            # QuitRequested, landing in the same finalization as [q])
            guidance = None
            if settings.interactive and attempt < max_attempts:
                guidance = ui.steer()

            # Preferred path: continue the SAME session — the model keeps its own
            # memory of what it read, wrote, and saw fail. Compact the finished
            # attempt down to stubs first so the history stays under budget.
            session.compact_completed_attempts()
            if session.over_budget():
                # bounded worst case: fall back to the old fresh-conversation retry
                ui.warn("history over budget even after compaction — "
                        "starting a fresh session for the next attempt")
                log.event("context_reset", after_attempt=attempt)
                session = _new_session(executor_model, executor_system)
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
            if guidance:
                log.event("user_steer", chars=len(guidance))
                log.transcript(f"\n**User guidance:** {guidance}\n")
                user_msg += ("\n\nUSER GUIDANCE for this retry (follow it, it "
                             "overrides the feedback above where they conflict):\n"
                             + guidance)
            # the instruction the executor is about to receive, in the ledger:
            # a retry aimed at the wrong thing is invisible otherwise
            ui.retry_focus(user_msg)
            prev_answer = answer
        else:
            ui.warn(f"hit max attempts ({max_attempts}) without meeting the goal")
            if answer is not None:
                # don't leave the user empty-handed — the last attempt is still
                # the best artifact we have, just unverified
                ws.save_artifact("final_output_UNVERIFIED.txt", answer)
    except ui.QuitRequested:
        attempt = max(attempt, 1)
        ui.warn(f"quit requested — ending run during attempt {attempt}")
        log.event("user_quit", attempt=attempt)
        attempts.append({"attempt": attempt, "output": answer or "",
                         "files": [], "passed": False,
                         "verdict": "aborted by user (q)", "criteria": []})
        if answer:
            ws.save_artifact(f"attempt_{attempt}_ABORTED.txt", answer)

    ws.save_artifact("attempt_history.json", json.dumps(attempts, indent=2))
    from .report import write_report  # late import: report is optional plumbing
    report_path = write_report(ws.run_dir)

    run_passed = bool(attempts and attempts[-1]["passed"])
    try:
        from . import history  # late import, same pattern as report
        history.record(history.db_path(), ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       goal=goal, model=model,
                       executor_model=settings.executor_model,
                       reviewer_model=settings.reviewer_model,
                       passed=run_passed, attempts=len(attempts),
                       duration_secs=round(time.time() - t0, 1),
                       run_dir=ws.run_dir)
        log.event("history_recorded", passed=run_passed)
    except Exception as e:
        ui.warn(f"could not record run history: {e}")
    hooks_mod.fire("run_end", passed=run_passed)

    if settings.memory:
        from .memory import update_agent_md  # late import, matches report pattern
        ui.phase("writing memory note")
        update_agent_md(ws, goal, run_passed, files=ws.list_all_files(),
                        feedback_history=feedback_history)
    ui.stop()  # leave the terminal clean before the closing summary
    print_timing_summary()
    print(f"\nRun artifacts: {ws.run_dir}\nWorkspace files: {ws.root}"
          + (f"\nReport: {report_path}" if report_path else ""))
    return run_passed
