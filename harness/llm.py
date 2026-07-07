"""Ollama chat plumbing: HTTP, context budgeting, the tool-calling loop, timing."""

import functools
import json
import time
from collections import defaultdict
from typing import Optional

import requests

from . import ui
from .config import settings


# ─── Text budgeting helpers ─────────────────────────────────────────

def truncate_middle(text: str, max_chars: int) -> str:
    """Cap text length, keeping the head and tail (that's where the signal
    usually is — imports/opening vs. errors/conclusions)."""
    if len(text) <= max_chars:
        return text
    marker = f"\n[... {len(text) - max_chars} chars truncated ...]\n"
    half = max(0, (max_chars - len(marker)) // 2)
    return text[:half] + marker + text[-half:]


def cap(text: str, max_chars: int) -> str:
    """Truncate — unless full-context mode is on, in which case pass through."""
    return text if settings.full_context else truncate_middle(text, max_chars)


def estimate_tokens(messages: list) -> int:
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return total // settings.chars_per_token


def compact_messages(messages: list) -> None:
    """In-place: if the history is over budget, shrink OLD tool results and
    assistant turns down to stubs. Never touches the system prompt, the task
    (first two messages), or the most recent compact_keep_last messages —
    the model always keeps its instructions, its goal, and its recent work."""
    if settings.full_context:
        # nothing gets touched — but if we're over the window, ollama will
        # silently drop the OLDEST tokens (system prompt first!), so shout
        est = estimate_tokens(messages)
        if est > settings.num_ctx:
            ui.warn(f"full-context mode: sending ~{est} tokens but "
                    f"num_ctx is {settings.num_ctx} — ollama will silently drop "
                    f"the oldest. Raise --num-ctx.")
        return
    budget_tokens = int(settings.num_ctx * 0.75)  # leave headroom for the reply
    if estimate_tokens(messages) <= budget_tokens:
        return

    protected = 2  # system + user task
    keep_last = settings.compact_keep_last
    compactable = range(protected, max(protected, len(messages) - keep_last))
    for i in compactable:
        m = messages[i]
        content = str(m.get("content") or "")
        if len(content) > 500 and m.get("role") in ("tool", "assistant"):
            messages[i] = {**m, "content": content[:300] + "\n[... compacted to save context ...]"}
        if estimate_tokens(messages) <= budget_tokens:
            return

    if estimate_tokens(messages) > budget_tokens:
        ui.warn(f"history still ~{estimate_tokens(messages)} tokens "
                f"after compaction (budget {budget_tokens})")


# ─── Timing ─────────────────────────────────────────────────────────

total_time: dict[str, list[float]] = defaultdict(list)


def timed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        if elapsed > 60:
            ui.info(f"[{func.__name__}] took {elapsed:.2f}s (~{elapsed / 60:.1f} min)")
        else:
            ui.info(f"[{func.__name__}] took {elapsed:.2f}s")
        total_time[func.__name__].append(elapsed)
        return result
    return wrapper


def print_timing_summary():
    if not total_time:
        return
    print("\n─── timing summary ───")
    for name, times in total_time.items():
        print(f"  {name}: {len(times)} call(s), total {sum(times):.1f}s, "
              f"avg {sum(times) / len(times):.1f}s")


# ─── HTTP + tool-calling loop ───────────────────────────────────────

def _role_options(role) -> dict:
    """Ollama options dict for a config.RoleOptions."""
    opts = {}
    if role.temperature is not None:
        opts["temperature"] = role.temperature
    return opts


# models that rejected the think parameter this run — don't send it again
_no_think_models: set = set()


def _post_chat(payload: dict, label: str = "llm") -> dict:
    from . import runlog
    payload.setdefault("options", {})["num_ctx"] = settings.num_ctx
    start = time.perf_counter()
    resp = requests.post(settings.url + "/api/chat", json=payload,
                         timeout=settings.request_timeout)
    if resp.status_code == 400 and payload.get("think") \
            and "does not support thinking" in resp.text:
        # non-thinking model (e.g. qwen2.5-coder) — remember and retry without
        _no_think_models.add(payload["model"])
        ui.warn(f"{payload['model']} does not support thinking — disabling it")
        payload.pop("think")
        resp = requests.post(settings.url + "/api/chat", json=payload,
                             timeout=settings.request_timeout)
    resp.raise_for_status()
    data = resp.json()
    prompt_tokens = data.get("prompt_eval_count")
    runlog.log_event("llm", label=label,
                     secs=round(time.perf_counter() - start, 2),
                     prompt_tokens=prompt_tokens,
                     eval_tokens=data.get("eval_count"))
    if prompt_tokens and prompt_tokens > 0.85 * settings.num_ctx:
        ui.warn(f"prompt used {prompt_tokens} of {settings.num_ctx} "
                f"num_ctx tokens — raise --num-ctx before things get dropped")
    return data["message"]


class Session:
    """A persistent conversation with tool-calling.

    Unlike the old one-shot chat, a Session keeps its message history, so a
    retry can continue where the last attempt left off (the model remembers
    what it read, wrote, and saw fail) instead of starting blind."""

    def __init__(self, model: str, system: str, tool_schemas: Optional[list],
                 think: bool = True, max_tool_rounds: int = 15,
                 label: str = "llm", options: Optional[dict] = None):
        self.model = model
        self.tool_schemas = tool_schemas
        self.think = think
        self.max_tool_rounds = max_tool_rounds
        self.label = label
        self.options = options
        self.messages: list = [{"role": "system", "content": system}]
        # only tools we actually advertised may run (the reviewer, for example,
        # gets a read-only subset — it must not be able to write files)
        self._allowed = {s["function"]["name"] for s in tool_schemas} if tool_schemas else set()
        self.last_tool_calls = 0  # how many tool calls the latest send() made

    def _payload(self, with_tools: bool = True) -> dict:
        payload = {"model": self.model, "messages": self.messages,
                   "stream": False}
        if self.think and self.model not in _no_think_models:
            payload["think"] = self.think
        if self.options:
            payload["options"] = dict(self.options)
        if with_tools and self.tool_schemas:
            payload["tools"] = self.tool_schemas
        return payload

    @timed
    def send(self, user: str, with_tools: bool = True) -> str:
        """Add a user turn and run the tool loop until the model answers in text.

        with_tools=False makes a plain conversational turn (no tool schemas
        sent, so no tool calls possible) — used for the planning turn."""
        from .tools import execute_tool_call  # late import: tools pull in llm helpers
        self.messages.append({"role": "user", "content": user})
        self.last_tool_calls = 0

        for round_num in range(self.max_tool_rounds):
            compact_messages(self.messages)
            ui.context_tokens(estimate_tokens(self.messages), settings.num_ctx)

            msg = _post_chat(self._payload(with_tools=with_tools), label=self.label)

            if msg.get("thinking"):
                ui.thinking(msg["thinking"])
            # don't resend the thinking text every round — it's context we pay
            # for on every subsequent call and the model doesn't need it back
            # (in full-context mode we keep it: the model gets everything)
            if not settings.full_context:
                msg.pop("thinking", None)

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                ui.answer(msg["content"])
                self.messages.append(msg)
                return msg["content"]

            self.messages.append(msg)
            for tc in tool_calls:
                func_info = tc["function"]
                tool_name = func_info["name"]
                tool_args = func_info.get("arguments", {})
                self.last_tool_calls += 1
                ui.tool(tool_name, tool_args)
                if tool_name not in self._allowed:
                    result = f"[ERROR] tool '{tool_name}' is not available in this context"
                else:
                    result = execute_tool_call(tool_name, tool_args)
                ui.tool_result(result)
                self.messages.append({
                    "role": "tool",
                    "tool_name": tool_name,  # ollama matches the result to the call by this
                    # cap what enters history — a huge stdout or web page stays
                    # useful (head + tail) without eating the whole window
                    "content": cap(result, settings.tool_result_max),
                })

        # Exhausted all rounds: one final call with no tools so it must answer in text
        ui.warn("hit max tool rounds, forcing final response")
        compact_messages(self.messages)
        msg = _post_chat(self._payload(with_tools=False), label=self.label)
        ui.answer(msg["content"], forced=True)
        self.messages.append(msg)
        return msg["content"]

    # ─── inter-attempt housekeeping ─────────────────────────────────

    def compact_completed_attempts(self) -> None:
        """Aggressively stub everything from finished attempts down to 300
        chars — except the system prompt, the original task (message 1), and
        the final answer of the latest attempt. The next retry keeps the
        story of what happened without paying full price for it."""
        if settings.full_context:
            return
        last = len(self.messages) - 1
        for i in range(2, last):
            m = self.messages[i]
            content = str(m.get("content") or "")
            if m.get("role") in ("tool", "assistant") and len(content) > 300:
                self.messages[i] = {
                    **m, "content": content[:300] + "\n[... compacted (earlier attempt) ...]"}

    def over_budget(self) -> bool:
        return estimate_tokens(self.messages) > int(settings.num_ctx * 0.75)


@timed
def chat_v2(model: str, system: str, user: str, tool_schemas: Optional[list],
            think: bool = True, max_tool_rounds: int = 15,
            label: str = "llm", options: Optional[dict] = None) -> str:
    """One system + one user turn, with an optional tool-calling loop."""
    return Session(model, system, tool_schemas, think=think,
                   max_tool_rounds=max_tool_rounds, label=label,
                   options=options).send(user)
