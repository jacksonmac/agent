---
name: pytest-debugging
description: diagnose and fix failing pytest suites methodically
---
When tests are failing, work in this order — do not skip steps:

1. **See the real failures first.** Run `run_shell` with `pytest -q` and read
   the tail of the output. Note how many tests fail and whether they are
   assertion failures, collection errors (import/syntax problems), or
   fixture/setup errors — each needs a different fix.
2. **Reproduce one failure in isolation.** Re-run just the first failing test:
   `pytest -q path/to/test_file.py::test_name`. Isolated runs separate real
   bugs from test-ordering or shared-state problems.
3. **Read before you write.** Use `read_file` on BOTH the failing test and the
   code under test. Understand what the test expects and why the code
   disagrees before editing anything.
4. **Fix the root cause, not the test.** Only change the test if it is
   genuinely wrong about the intended behavior. Never weaken an assertion
   just to make it pass.
5. **Collection errors outrank everything.** If pytest can't even collect
   (ImportError, SyntaxError), fix that first — the other results are
   meaningless until collection succeeds.
6. **Re-verify in widening circles.** After a fix: re-run the single test,
   then the file, then the full suite with `pytest -q`. A fix that breaks a
   different test is not done.
7. **Stop conditions.** Finished means the full suite exits 0. If a failure
   depends on something unavailable in the workspace (network, missing
   binary), say so explicitly in your answer instead of guessing.
