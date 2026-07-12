---
description: build roman.py, then delegate a fresh-eyes adversarial audit + fix loop
em: qwen3.6:27b
attempts: 10
num_ctx: 32768
---
In the workspace, first YOU write roman.py exposing 'def to_roman(n: int) -> str'
and 'def from_roman(s: str) -> int' for values 1..3999. Correct Roman rules:
subtractive forms IV IX XL XC CD CM; to_roman raises ValueError outside 1..3999;
from_roman raises ValueError on malformed input (e.g. 'IIII', 'VV', 'IC',
lowercase, empty).

After writing it, spawn_subagent to run an INDEPENDENT audit: tell the child the
full spec above and instruct it to read roman.py from the workspace, write and run
adversarial pytest cases against both functions (round-trip 1..3999, every
subtractive form, and the invalid-input cases), and END its summary with a list of
every failing case as 'input -> expected vs actual', or 'NO FAILURES' if clean. The
child shares your workspace files but cannot see this conversation, so its task must
contain the spec.

When it returns, fix every failure it reports and re-verify by re-running the tests
yourself. Finally write test_roman.py capturing those same cases, run pytest, and
confirm all pass before finishing. {args}
