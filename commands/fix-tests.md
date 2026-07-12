---
description: run pytest and fix every failing test
em: qwen2.5-coder:7b
attempts: 3
---
Run the test suite in the workspace with pytest (run_shell 'pytest -q'). Fix every
failing test by editing the code under test — do not delete or skip tests. Re-run
until everything passes, then give your final answer. {args}
