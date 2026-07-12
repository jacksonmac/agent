---
description: build the strkit package via three parallel subagents, then integrate + test
em: qwen3.5:9b
rm: gemma4:26b
attempts: 10
num_ctx: 32768
---
Build a small Python string-utilities package 'strkit' in the workspace, composed
of three independent modules plus a CLI that ties them together. You MUST build the
three modules by delegating each to a separate spawn_subagent call (three children
total), because they are independent.

Child 1: create slugify.py exposing 'def slugify(text: str) -> str' that lowercases,
strips punctuation, and joins words with single hyphens.

Child 2: create wordfreq.py exposing 'def top_words(text: str, n: int) ->
list[tuple[str, int]]' returning the n most common words (case-insensitive, ties
broken alphabetically).

Child 3: create titlecase.py exposing 'def smart_title(text: str) -> str' that
title-cases but keeps the small words a, an, the, of, and, or, to, in lowercase
unless first.

Tell each child to END its summary with the EXACT filename and function signature
it created, because you will import them.

After all three return, YOU write strkit.py: a CLI 'python3 strkit.py
<slug|freq|title> "text"' (freq prints the top 5) that imports and calls the three
functions. Then write test_strkit.py with pytest covering each function's happy path
plus one edge case each, and a smoke test that runs the CLI for all three
subcommands. Run pytest and confirm every test passes before finishing. {args}
