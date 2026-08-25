# Proposal — Making the Verdict Trustworthy

**Status:** proposal, nothing implemented
**Execution plan:** [PLAN.md](PLAN.md) — the work items, acceptance criteria and sequencing
**Scope:** `harness/review.py`, `harness/prompts.py`, `harness/run.py`, `evals/`
**Tracks:** RD I-13 (new), and unblocks RD I-5

---

## 1. The claim we are defending

The harness rests on one claim:

> A verdict means something, because an independent model judged the real artifacts
> rather than the executor's description of them.

Everything else — the retry loop, the attempt budget, `final_output.txt` versus
`final_output_UNVERIFIED.txt` — is downstream of that claim. If a verdict is unreliable,
the harness is an expensive way to produce unverified output with a green tick on it.

That claim has never been tested. All 438 tests drive a scripted fake, which proves the
plumbing is correct and says nothing about whether the loop improves outcomes. The eval
suite could answer the question and has never been run. `harness_passed` — the reviewer's
own verdict — is recorded on every eval row and never compared against `checker_passed`,
the programmatic ground truth sitting in the adjacent column.

This proposal makes the claim measurable, then improves it.

---

## 2. The problem, stated precisely

### 2.1 Two errors, not one

| | ground truth: good | ground truth: broken |
| --- | --- | --- |
| **verdict: PASS** | correct | **false pass** |
| **verdict: FAIL** | **false fail** | correct |

They are not symmetric, and this asymmetry drives most of the design below:

- A **false pass** ends the run. The loop stops, the artifact is written as
  `final_output.txt`, and the user receives broken work carrying a verdict that says it is
  fine. The damage is *irreversible within the run* — there is no later stage that catches it.
- A **false fail** costs one attempt. The executor is told to fix working code, wastes
  tokens, and may make it worse — but the loop continues and can still converge.

A false pass is therefore worse than having no reviewer at all, because it converts
"unverified output" into "output the system vouched for".

### 2.2 Three judges, none of them checked

The system contains three judges, arranged in a chain, and **not one of them has been shown
to discriminate**:

| Judge | Decides | Currently validated by |
| --- | --- | --- |
| Reviewer model | did the attempt meet the goal | nothing |
| `automated_checks` (pytest) | do the workspace's own tests pass | nothing — and usually there are no tests to run |
| Eval checkers (`goals.py`) | did the benchmark goal actually get done | only that they reject an untouched workspace |

The third row is the closest to good practice and is the model for the other two.

### 2.3 The oracle problem

Testing theory has a name for the underlying difficulty: the **oracle problem** — to know
whether output is correct, you need an independent source of truth about what correct means.
The eval suite solves it by hand-writing an oracle per goal. Real user runs have no oracle
at all. Any proposal that only measures accuracy on the eight benchmark goals is measuring a
laboratory, not the product.

---

## 3. Principles

These are the engineering principles the design is derived from. Each one is load-bearing;
where a decision below looks arbitrary, it comes from here.

**P1 — Any judge must itself be shown to discriminate.**
The core insight of mutation testing: a test suite that passes against broken code is
worthless, and you only learn that by feeding it broken code. This applies recursively to
every judge in §2.2. It is the single unifying idea of this proposal, and three separate
mechanisms below are the same rule applied at different levels.

**P2 — Prefer the cheapest judge that can decide the question.**
Execution beats judgement. A criterion expressible as a program that exits non-zero should
never be routed to a language model, because the program cannot be talked round, costs
nothing, and gives the same answer every time. Reserve the expensive, fallible judge for the
questions that genuinely require it.

**P3 — When failure modes have unequal cost, bias toward the cheap one.**
Standard safety engineering. Because a false pass is irreversible within the run and a false
fail costs one attempt, the burden of proof belongs on PASS, not on FAIL.

**P4 — Make invalid states unrepresentable.**
A verdict asserting that a criterion is met, with no evidence attached, should fail to parse
— not be accepted and regretted later. Validation at the boundary, not vigilance downstream.

**P5 — Separate "did something happen" from "was it the right thing".**
Two different questions with two different best answerers. Conflating them is why the
current single verdict is hard to improve: every proposed fix helps one and hurts the other.

**P6 — Distinguish "wrong" from "unknown".**
Collapsing "I could not verify this" into "this is broken" loses the information needed to
respond correctly. One calls for more evidence; the other calls for different work.

**P7 — Independence is a property of causes, not of instances.**
Two judgements from the same model family, given the same evidence and the same framing, are
not two samples. They share their blind spots. This is why "use a bigger reviewer model" is
worth less than it appears, and why two checkers written by one author on one afternoon are
close to one checker.

**P8 — Measure before optimising; be willing to be told there is no problem.**
Phase 0 exists partly to cancel Phases 1–4. See §6.

**P9 — Observability: a wrong decision must be diagnosable afterwards.**
The repo already holds this value — "what a run was allowed to do has to be answerable from
`events.jsonl` afterwards, by someone who wasn't there". Extend it to *why a verdict was
reached*.

---

## 4. The design

Five phases, ordered so that each is useful alone and each makes the next measurable.

```
                    ┌─ Phase 0 ─ measurement ─────────────────┐
                    │  confusion matrix, per-criterion,       │
                    │  clustered intervals, disagreement list │
                    └─────────────┬───────────────────────────┘
                                  │ gates everything below
   ┌──────────────────────────────┼──────────────────────────────┐
   │                              │                              │
Phase 1                       Phase 2                        Phase 3
evidence-bearing              checkers must                  agent verifier
verdicts                      discriminate                   must discriminate
(P3, P4, P6)                  (P1)                           (P1, P2, P5)
   │                              │                              │
   └──────────────────────────────┼──────────────────────────────┘
                                  │
                             Phase 4 — adversarial recheck on PASS only (P3, P7)
                                  │
                             Phase 5 — choose between them by experiment (P8)
```

---

### Phase 0 — Measure the two errors separately

**What.** In `evals/run_evals.py`, cross `harness_passed` with `checker_passed` and report:

- the four counts;
- **PASS precision** — of the runs the reviewer approved, the share that were genuinely
  good. This is the number that maps to the harm: *when it says PASS, can I trust it?*
- **wasted-retry rate** — of the runs that were genuinely good, the share the reviewer
  failed anyway;
- the **run directories of every disagreement**, which at current sample sizes is the real
  deliverable (§5.3).

Score **per criterion as well as per run**. Each run carries 3–6 criteria with individual
`met` flags, which is a 5× sample multiplier over the same runtime, and it reveals *which
kind* of criterion the reviewer misjudges rather than only how often.

**Cost.** Small. The data is already recorded on every eval row; this is aggregation plus a
new confusion function and a per-criterion pass through `verdict.criteria`.

| Pro | Con |
| --- | --- |
| Uses data already collected — nothing new to instrument | Only measurable on the 8 benchmark goals; real runs still have no oracle (addressed in Phase 3) |
| Selects the treatment: leniency and harshness need opposite fixes | `checker_passed` is itself a proxy (addressed in Phase 2) |
| Makes every later phase evaluable — without it they are unmeasurable | Sample is small; see the statistical trap below |
| Per-criterion scoring is free sample and free diagnosis | Criteria wording varies run to run, so grouping them into kinds needs care |

**The statistical trap, called out explicitly.** Criteria within one run are correlated —
same goal, same attempt, same evidence. Treating ~200 criterion judgements as 200
independent observations will produce intervals that are far too narrow and will
manufacture findings that are not there. `bootstrap_ci` must resample **runs** and take all
criteria within each drawn run (a clustered bootstrap). This is the easiest way for this
whole proposal to fool us, so it belongs in a test, not a comment.

---

### Phase 1 — Make PASS expensive to claim

**What.** Three changes, all at the verdict boundary.

1. **Evidence required for approval (P3, P4).** `parse_verdict` rejects any verdict where a
   criterion carries `met: true` with an empty or boilerplate `note`. A rejected verdict
   takes the existing re-ask path, which already exists for malformed JSON. Approval must
   cite something; denial need not.
2. **A third state (P6).** `met` becomes `true | false | "unverified"`. `Verdict` gains
   `unverified()` beside `unmet()`, and `run.py` routes them differently: unmet means *the
   work is wrong, change it*; unverified means *the evidence was missing, go and get it —
   run the code, read the file*. Today both produce the same retry instruction, so an
   evidence gap sends the executor off to rewrite code that was already correct.
3. **Asymmetric parsing default.** An unparseable verdict already defaults to `passed=False`.
   Keep that, and document it as a deliberate application of P3 rather than an accident.

| Pro | Con |
| --- | --- |
| Directly attacks the expensive error | A model that writes fluent but hollow notes defeats the check — it raises the bar, it does not close the hole |
| Small and local: `parse_verdict`, `Verdict`, one branch in `run.py` | Adds a re-ask round, so a little latency and cost per attempt |
| The unverified state removes a whole class of wasted retries | Three-state logic is more to get wrong than two-state; needs its own tests |
| No new model calls in the common path | Older verdict JSON without the new field must still parse — back-compat care needed |

---

### Phase 2 — The eval checkers must discriminate

**What.** Apply P1 to the oracles themselves. For each goal in `evals/goals.py`, add
fixtures the checker must reject and fixtures it must accept:

- **plausible-but-wrong** solutions — right shape, wrong result — which the checker must fail;
- **valid-but-different** solutions — a different correct implementation — which it must pass.

`tests/test_evals.py` already contains the first instance of this idea:

> `"""Seeds alone must never pass — otherwise the benchmark is free points."""`

This extends the same rule from "empty workspace" to "wrong answer" and "different right
answer", which are the two ways an oracle actually goes wrong.

| Pro | Con |
| --- | --- |
| Measures the checker's own false-pass and false-fail behaviour | Fixtures are hand-written, one set per goal |
| No model, no runtime — plain pytest, runs in CI forever | Only covers failure modes we thought of (P1 is a floor, not a ceiling) |
| Turns "the checker is a proxy" from an unknown into a bounded, tested claim | A blind spot shared between us and the checker stays invisible |
| Protects against checkers silently rotting as goals evolve | Adds authoring cost to every future goal |

**Rejected alternative:** two independent checkers per goal with disagreement flagged.
Double the authoring for correlated blind spots — P7 says two oracles by one author on one
afternoon are close to one oracle.

---

### Phase 3 — The agent's verifier must discriminate

This is the largest change and the only one that reduces the error rather than measuring it.
It is also the only one that produces ground truth on **real** runs.

**What.** The executor must leave behind a check — `verify.py`, or any `test_*.py`, which
`automated_checks` already runs. The harness then gates it:

1. Run the verifier against the current workspace. It must **pass**.
2. Check out the *previous* attempt's commit into a scratch directory and run the verifier
   there. It must **fail**.
3. If it passes in both places, it does not discriminate: treat it as absent, and tell the
   executor exactly that.

Step 2 is nearly free because the machinery exists. `init_git()` already commits
`"workspace before the run"`, and `commit_attempt(n)` commits after each attempt **with
`--allow-empty`, specifically so `HEAD~1` stays meaningful even for do-nothing attempts**.
The baseline this gate needs is already being recorded on every run.

This is P1 applied to the agent, and P2 applied to the goal: the mechanical part of "did it
work" is decided by a program, and only the residue — did it do the *right* thing — reaches
the model.

| Pro | Con |
| --- | --- |
| Produces an oracle on **every real run**, not just benchmarks | **Not independent (P7):** the agent writes it, so a model that misunderstands the goal writes a verifier encoding the same misunderstanding |
| Kills the `assert True` failure mode that otherwise sinks "make the agent write its own test" | Catches *lazy* verifiers, not *wrong* ones — it proves something changed, not that the change was correct |
| Reuses git snapshots already taken; cost is one subprocess and a checkout, far below one LLM call | Some legitimate goals ("don't break anything", pure refactors) have verifiers that correctly pass on the prior state — needs an explicit escape hatch, and the escape hatch is abusable |
| Converts the verdict from opinion to observation for the mechanisable part of the goal | Adds a required artifact the executor can fail to produce, costing attempts on small models |
| Failure is loud and early rather than at review time (fail fast) | Running agent-authored code is already inside the sandbox/permission story, but this makes it routine rather than incidental |

**Honest limit.** This raises the floor from "the model says it works" to "something
mechanical distinguishes before from after". That is a large gain and it is not the same as
correctness. Only a human, or a hand-written oracle, closes the last gap — see §5.1.

---

### Phase 4 — Adversarial recheck, on the PASS path only

**What.** When the reviewer returns PASS, re-ask once in a fresh session at temperature 0
with inverted framing — *"find the strongest reason this does not meet the goal"* — and
accept PASS only if that pass also finds nothing citable. Objections without concrete
evidence are discarded, following P4.

Spending is targeted by P3: most attempts fail on the first review, so this fires at most
once per run, on exactly the decision whose error is irreversible.

| Pro | Con |
| --- | --- |
| Concentrates extra cost precisely where the expensive error lives | **Most likely to backfire of anything here:** models are agreeable to framing, and an adversarial prompt may always find *something*, converting false passes into false fails wholesale |
| Cheap in practice — at most one extra call per run | Same model, same evidence: not an independent sample (P7), so it may simply repeat the original mistake with more confidence |
| Fits existing machinery; `review()` is already re-askable | Adds latency to the happy path, which is the path users feel |
| Ships as an experiment arm, so it can be measured and dropped | Requires the evidence-citing rule from Phase 1 to be usable at all |

Because this is the phase most likely to be wrong, it ships **last among the interventions**
and only as an arm.

---

### Phase 5 — Decide by experiment, not by argument

Every phase above is an arm for the machinery already built:

```bash
venv/bin/python evals/run_evals.py --label verify-gate --repeat 5 \
    --experiment evals/experiments/verdict-quality.json
```

with **PASS precision** as the reported metric rather than raw pass rate. The prompt-override
arms make Phase 1's wording changes A/B-able without touching the code path.

| Pro | Con |
| --- | --- |
| The instrument already exists and reports intervals rather than verdicts | An overnight run is ~80 runs; the pass-rate arm will often be inconclusive by design |
| Prevents shipping interventions that feel right and do nothing | Per-criterion scoring helps, but the clustered bootstrap keeps intervals honestly wide |
| Ranks the phases against each other instead of guessing | Costs a night of the Ollama box per question asked |

---

## 5. What this does *not* solve

Stating the residue plainly, because a proposal that claims to close every gap is not to
be trusted.

### 5.1 Real runs still have no independent oracle

Phase 3 gives every real run a *mechanical* check, but the agent authored it. The gap
between "something changed measurably" and "the right thing happened" is closed only by a
human or a hand-written oracle.

**Mitigation, deliberately small:** sample roughly one finished run in five and ask the user
a single question — *did this actually work?* — stored in `history.db` beside the existing
row. Over weeks that accumulates genuine ground truth on real goals.

| Pro | Con |
| --- | --- |
| Genuinely independent, and on real goals rather than benchmarks | Needs a human; volume will be tiny |
| Almost free to build — one prompt, one column | Biased toward runs that annoyed someone enough to answer |
| Directly calibrates whether benchmark accuracy generalises | Slow: months before the sample means much |

### 5.2 External validity

Eight synthetic goals are not your workload. Every accuracy figure this produces is a
statement about the benchmark, and generalisation to real work is an assumption. Expanding
the goal set improves this and costs runtime on every experiment forever; §5.1's sampled
human adjudication is the cheaper corrective.

### 5.3 Statistical power

At 8 goals × 5 repeats you get roughly a handful of disagreements. Per-criterion scoring
multiplies the sample; clustering honestly takes much of that back. **At this size the list
of disagreeing runs is worth more than the rate** — three to eight cases, each with a run
directory, a transcript, and the reviewer's own stated reasoning, tell you *why* it was
wrong. That is directly actionable in a way a rate with a wide interval is not. The rate
becomes the point only once there are enough runs to trust it.

---

## 6. Phase 0 must be able to cancel Phases 1–4

If the measured PASS precision is already ~0.95, most of this proposal is not worth
building, and the correct response is to stop and say so.

Phase 0 is therefore not a prelude to a decision already taken. It is the decision. Any
phase that follows should be justified by a number Phase 0 produced, and this document
should be revised — not quietly ignored — if those numbers say the problem is smaller than
assumed.

---

## 7. Deliberately rejected

| Idea | Why not |
| --- | --- |
| Make the reviewer prompt stricter | Already says *"judge the artifacts, not the agent's claims"* and *"a criterion is met only if you can point to concrete evidence"*. The ask-nicely lever is exhausted; further tightening trades false passes for false fails roughly one for one and feels like progress. |
| Start with a bigger reviewer model (RD I-5) | Buys capability where the problem is structure (P7). Worth measuring as one arm, but it is not the first thing to try, and it raises cost on every attempt forever. |
| Ensemble of N reviewers, majority vote | Same family, same evidence, same framing — correlated errors (P7). Pays N× for far less than N× the independence. |
| Two hand-written checkers per eval goal | Doubles authoring for correlated blind spots. Phase 2 gets more per unit effort. |
| Have the reviewer re-run the executor's work from scratch | Enormous cost, and reproduces the executor's misreading of the goal rather than checking it. |

---

## 8. Sequencing

| Phase | Size | Depends on | Useful alone? |
| --- | --- | --- | --- |
| 0 — measurement | small | nothing | yes — produces the disagreement list immediately |
| 1 — evidence-bearing verdicts | small | 0, to know if it helped | yes — the unverified state removes wasted retries regardless |
| 2 — checker discrimination | medium | nothing | yes — protects the oracles permanently |
| 3 — verifier discrimination | large | 0 and 1 | yes — the only phase that reduces the error on real runs |
| 4 — adversarial recheck | small | 0, 1, and evidence that false passes are common | no — must not ship unmeasured |
| 5 — experiments | none | the above | it is how the above are judged |

Recommended order: **0 → 1 → 2 → 3 → 4**, with Phase 5 running continuously from the end of
Phase 0 onward.

The first two are cheap enough to do together and both operate on data already being
recorded. Phase 3 is the one worth real effort, and it should not start until Phase 0 has
shown there is something to fix.
