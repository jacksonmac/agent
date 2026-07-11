---
description: build a wc.py CLI clone with tests, using a subagent for the tests
em: qwen3.5:9b
attempts: 3
---
Build a small CLI tool wc.py in the workspace that counts lines, words, and
characters in a text file, mimicking Unix 'wc' (usage: python3 wc.py <path>
[--lines] [--words] [--chars]; no flags = all three counts plus filename in the
order "lines words chars filename"; flags = only those counts in that same fixed
order; missing file = print "wc: <path>: No such file" to stderr and exit 1).

First implement wc.py yourself. Then use spawn_subagent to delegate ONE scoped
task: "Write test_wc.py in the workspace with pytest tests covering all-counts
default, each flag alone, two flags combined, an empty file, and the missing-file
error case; create any fixture files you need; run pytest and report the exact
results." Incorporate its summary, make sure pytest passes, then give your final
answer. {args}