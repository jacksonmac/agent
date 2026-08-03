# Implementation Plan — Verdict Trustworthiness

Execution plan for **[VERIFICATION.md](VERIFICATION.md)**, which carries the reasoning,
the principles and the rejected alternatives. This document carries only the work: what to
build, in what order, and how to know each piece is done.

**Tracks:** RD I-13 · **Unblocks:** RD I-5

---

## How to use this

- Work items are `P<phase>.<n>`. Tick them as they land.
- **Every item states its acceptance criteria.** An item is done when those hold and the
  suite is green — not when the code exists.
- Sizes are relative, not hours: **S** ≈ one sitting, **M** ≈ a day, **L** ≈ several days.
- **Phase 0 is a gate, not a warm-up.** §Gate below can cancel Phases 1–4. Do not start
  Phase 1 before the gate has been evaluated.

---

## Phase 0 — Measure the two errors

**Goal:** know the false-pass and false-fail rates, and get the list of runs where the
reviewer and reality disagreed.

### P0.1 — Log verdict criteria to `events.jsonl` — **S**

**Prerequisite for P0.3.** `run.py` currently logs `passed` and `feedback` on the `attempt`
event but not `verdict.criteria`, and `events.jsonl` is the only thing `run_evals` reads.
Per-criterion scoring is impossible until this lands.

| | |
| --- | --- |
| Files | `harness/run.py` (the `log.event("attempt", …)` call) |
| Change | Add `criteria=verdict.criteria` |
| Accept | An `attempt` event carries a `criteria` list of `{criterion, met, note}`; existing readers (`report.py`, resume summary) are unaffected by the new key |
| Test | `tests/test_loop_phases.py` — run a scripted loop, assert the event contains the criteria |

### P0.2 — `confusion()` in the eval runner — **S**

| | |
| --- | --- |
| Files | `evals/run_evals.py`, `tests/test_evals.py` |
| Change | Cross `harness_passed` × `checker_passed`; return the four counts plus **PASS precision** (of runs the reviewer approved, the share genuinely good) and **wasted-retry rate** (of genuinely good runs, the share the reviewer failed) |
| Accept | Hand-built rows produce exact counts; rows with `harness_passed = None` (timed out, or no run dir found) are excluded from the matrix and counted separately rather than silently treated as failures |
| Test | Exact counts on a fixed table; a `None`-bearing row does not shift any rate |

### P0.3 — Per-criterion scoring — **M**

| | |
| --- | --- |
| Files | `evals/run_evals.py` (`_stats_from_events` to harvest criteria), `tests/test_evals.py` |
| Change | Emit one observation per criterion per run, tagged with its run, so the sample is ~5× larger and the failure *mode* is visible |
| Accept | A run with 4 criteria yields 4 observations, each carrying its run id; a run with no criteria (stall-gated attempts) contributes none rather than a zero |
| Depends | P0.1 |

### P0.4 — Clustered bootstrap — **S**

**The most important correctness item in Phase 0.** Criteria within a run share a goal, an
attempt and a body of evidence. Treating them as independent produces intervals that are far
too narrow and will manufacture findings that are not there.

| | |
| --- | --- |
| Files | `evals/run_evals.py` (`bootstrap_ci`), `tests/test_evals.py` |
| Change | Resample **runs**, taking all criteria within each drawn run |
| Accept | On correlated input, the clustered interval is **strictly wider** than the naive per-observation interval — asserted, not assumed |
| Test | Construct data where every criterion in a run agrees; assert clustered width > naive width |

### P0.5 — Disagreement report — **S**

| | |
| --- | --- |
| Files | `evals/run_evals.py` |
| Change | Print every disagreeing run with its `run_dir`, the reviewer's verdict, the checker's detail, and which criteria differed |
| Accept | Every cell of the matrix that is non-zero is represented by at least one listed run; the list is directly openable |

### P0.6 — Docs — **S**

`SPEC.md` §23, `Readme.md` eval section, and this plan's checkboxes.

### Gate — evaluate before proceeding

Run `--repeat 5` across all 8 goals and record the baseline.

| Measured PASS precision | Decision |
| --- | --- |
| **≥ 0.95** | Stop. Record the number in VERIFICATION.md, close I-13, do not build Phases 1–4. The problem is smaller than assumed. |
| **0.80 – 0.95** | Build Phase 1 only. Re-measure. Treat Phase 3 as unjustified until Phase 1's effect is known. |
| **< 0.80** | Build Phases 1 → 2 → 3 in order, re-measuring at each. |

Whatever the number, **write it down in VERIFICATION.md**. A proposal whose premise turned
out to be wrong should say so in the document, not be quietly abandoned.

---

## Phase 1 — Make PASS expensive to claim

**Goal:** an approval must cite evidence, and "I could not verify" stops being recorded as
"this is broken".

### P1.1 — Third criterion state — **M**

| | |
| --- | --- |
| Files | `harness/review.py` (`Verdict`, `parse_verdict`) |
| Change | `met` accepts `true \| false \| "unverified"`; add `Verdict.unverified()` beside `unmet()` |
| Accept | `pass` requires every criterion `true`; `unverified` never counts as met; `summary()` lists the two groups separately |
| Risk | Three-state logic is easier to get wrong than two-state — it needs its own tests, not just adapted ones |

### P1.2 — Back-compatible parsing — **S**

| | |
| --- | --- |
| Accept | A verdict in the old two-state shape parses unchanged; a missing `met` is treated as `false` (P3: bias to the cheap error), not as unverified |
| Test | Old-shape fixture from a real past run still parses to the same `Verdict` |

### P1.3 — Evidence required for approval — **M**

| | |
| --- | --- |
| Files | `harness/review.py` |
| Change | Reject a verdict where any `met: true` has an empty or boilerplate `note`; route through the existing strict re-ask, then the existing fallbacks |
| Accept | A verdict with an unevidenced approval triggers exactly one re-ask; if the re-ask also fails, the result is `passed=False` (never a silent accept) |
| Test | Unevidenced PASS → re-ask; twice-unevidenced → fail; evidenced PASS → accepted first time |
| Honest limit | A model that writes fluent but hollow notes defeats this. It raises the bar; it does not close the hole. |

### P1.4 — Retry routing — **S**

| | |
| --- | --- |
| Files | `harness/run.py`, `harness/prompts.py` |
| Change | Unverified criteria produce *"the evidence was missing — run it, read the file, show the output"*; unmet criteria keep the existing *"the work is wrong, change it"* |
| Accept | A verdict with only unverified criteria produces a retry message that does not tell the executor its work is wrong |

### P1.5 — Reviewer prompt — **S**

Describe the three states in `REVIEWER_SYSTEM`. Ship the wording change as a **prompt
override arm** (`AGENT_PROMPT_OVERRIDES`) so it is measurable rather than assumed.

---

## Phase 2 — The eval checkers must discriminate

**Goal:** the oracles are shown to reject wrong answers and accept different-but-correct
ones, so Phase 0's ground truth is itself trustworthy.

### P2.1 — Fixture format — **S**

| | |
| --- | --- |
| Files | `evals/goals.py` |
| Change | Each `Goal` gains `wrong_solutions` and `alt_solutions` — small dicts of `{filename: content}` applied over the seed |
| Accept | Format holds for at least two goals of each category before being rolled out |

### P2.2 — Author the fixtures — **L**

Two per goal minimum, sixteen total: one plausible-but-wrong (right shape, wrong result),
one valid-but-different (a different correct implementation).

| | |
| --- | --- |
| Accept | Every goal has ≥1 of each |
| Cost | This is hand-written work and the largest time sink in Phase 2. It is also permanent: the fixtures protect the checkers from silently rotting as goals change. |

### P2.3 — Discrimination tests — **S**

| | |
| --- | --- |
| Files | `tests/test_evals.py` |
| Accept | Every checker **fails** every `wrong_solution` and **passes** every `alt_solution`; a checker that cannot do both is a bug in the checker, reported as such |
| Note | Extends the existing `test_checkers_fail_untouched_workspace` from "empty" to "wrong" and "differently right" — the two ways an oracle actually fails |

---

## Phase 3 — The agent's verifier must discriminate

**Goal:** ground truth on **real** runs, not just benchmarks. The largest item here, and the
only one that reduces the error rather than measuring it.

### P3.1 — Run a command against a previous commit — **M**

| | |
| --- | --- |
| Files | `harness/workspace.py` |
| Change | `run_at_commit(rev, argv)` — materialise `rev` into a scratch dir (`git worktree add --detach`) and run there |
| Accept | Works for `HEAD~1`; cleans up the worktree on success, failure and exception; a no-git workspace returns a clear "unavailable" rather than raising |
| Note | The baseline already exists: `init_git()` commits `"workspace before the run"` and `commit_attempt()` uses `--allow-empty` **specifically so `HEAD~1` stays meaningful** |

### P3.2 — The discrimination gate — **M**

| | |
| --- | --- |
| Files | `harness/review.py` (beside `automated_checks`) |
| Change | Locate the verifier; run current (must pass) and baseline (must fail) |
| Accept | Passes in both places → reported to the executor as *"your check does not discriminate: it passes on the previous state too"*, and treated as **no verifier**, not as a pass |
| Test | Three fixtures — a discriminating check, an `assert True` check, a check that fails on both |

### P3.3 — Escape hatch — **S**

Some goals legitimately have verifiers that pass on the prior state: refactors, "don't break
anything", pure documentation.

| | |
| --- | --- |
| Accept | The executor can declare the goal non-discriminating with a stated reason; the reason is logged and surfaced in the verdict evidence |
| Risk | The hatch is abusable — it is the obvious way for a model to dodge the gate. Log every use; if the gate is to be trusted, hatch frequency must be watched. |

### P3.4 — Ask for the verifier — **S**

`EXECUTOR_SYSTEM` / `SELF_CHECK_PROMPT` instruct the agent to leave a check behind. Ship as
a prompt-override arm.

### P3.5 — Observability — **S**

| | |
| --- | --- |
| Accept | A `verifier` event records present/absent, discriminating yes/no, and hatch-used; the dashboard shows it beside the verdict; P9 — a wrong verdict must be diagnosable afterwards |

---

## Phase 4 — Adversarial recheck on the PASS path

**Do not build before Phase 0 shows false passes are common.** Ships as an arm only.

### P4.1 — Prompt and call — **M**

| | |
| --- | --- |
| Files | `harness/prompts.py`, `harness/review.py`, `harness/run.py` |
| Change | On PASS only: one fresh-session re-ask at temperature 0, inverted framing, objections must cite evidence or be discarded |
| Accept | Fires at most once per run and only on the PASS path; a run that never passes makes zero extra calls |
| Flag | `--no-recheck`, defaulting **off** until an experiment justifies it |

**Primary risk:** models are agreeable to framing. An adversarial prompt may always find
*something*, converting false passes into false fails wholesale. The evidence-citing rule
from P1.3 is what makes this survivable, which is why Phase 1 is a hard dependency.

---

## Phase 5 — Decide by experiment

### P5.1 — `evals/experiments/verdict-quality.json` — **S**

Arms: baseline · evidence-required (P1) · verifier-gate (P3) · adversarial-recheck (P4) ·
reviewer-model (RD I-5, for comparison).

### P5.2 — PASS precision as a reported metric — **S**

`paired_deltas` gains the confusion-derived metrics so arms are compared on the error that
matters, not on raw pass rate.

### P5.3 — Run and record — **M**

One overnight run per question. Record results in VERIFICATION.md, including the arms that
did nothing.

---

## Risk register

| Risk | Trigger to watch | Response |
| --- | --- | --- |
| Phase 0 shows there is no problem | PASS precision ≥ 0.95 at the gate | Stop, record, close I-13. This is a success, not a wasted phase. |
| Naive bootstrap manufactures findings | Intervals suspiciously tight on correlated data | P0.4 asserts clustered > naive; treat any failure of that test as blocking |
| Evidence rule defeated by fluent hollow notes | PASS precision unchanged after Phase 1 | Expected ceiling, not a bug — fall through to Phase 3, which does not rely on the model's own words |
| Adversarial recheck floods false fails | Wasted-retry rate jumps in the P4 arm | Arm is off by default; drop it |
| Verifier hatch abused | Hatch-used frequency climbs across runs | P3.5 logs it; if common, tighten to require reviewer agreement |
| 8 goals do not represent real work | Benchmark and user-sampled accuracy diverge | VERIFICATION.md §5.1 sampled adjudication; expand the goal set |
| Phases 1 and 3 interact | Combined arm worse than either alone | Measure each alone before combining; the experiment supports it |

---

## Effort summary

| Phase | Items | Size | Blocked by |
| --- | --- | --- | --- |
| 0 — measurement | 6 | S–M | nothing |
| Gate | — | — | P0 complete |
| 1 — evidence-bearing verdicts | 5 | S–M | gate |
| 2 — checker discrimination | 3 | **L** (fixture authoring) | nothing; can run parallel to 1 |
| 3 — verifier gate | 5 | **L** | 1 |
| 4 — adversarial recheck | 1 | M | 1, and evidence from the gate |
| 5 — experiments | 3 | S–M | whatever is being measured |

**Critical path:** P0.1 → P0.3 → gate → P1 → P3. Phase 2 is independent and can proceed
alongside.

---

## Definition of done

The effort is complete when all of the following hold:

1. PASS precision and wasted-retry rate are reported by the eval suite, with clustered
   intervals, at both run and criterion granularity.
2. Every disagreement is listed with a run directory that can be opened and read.
3. Each intervention built has been measured as an experiment arm, and the results —
   including the null results — are recorded in VERIFICATION.md.
4. Every judge in the system has a test proving it rejects something it should reject:
   the eval checkers (P2.3), the agent's verifier (P3.2), and the reviewer itself via the
   measured matrix.
5. `SPEC.md`, `RD.md` and `Readme.md` describe what was actually built, and RD I-13 is
   closed with its outcome stated.

Item 4 is the one that matters. It is the whole proposal in a sentence: **nothing in this
system gets to judge without first being shown to discriminate.**
