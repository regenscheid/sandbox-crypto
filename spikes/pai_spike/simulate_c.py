# SPDX-License-Identifier: MIT
"""Option C on the same session: our loop's own requests (the whole history, as
``loop.py`` sends it today), with clearing applied either by the gateway (Anthropic's
``clear_tool_uses`` edit, which LiteLLM's ``/v1/messages`` passes through) or by our own
loop, and a recall tool over the run records.

No framework runs here: the requests are what our loop would send at each recorded
model call, and each strategy is a function of that request (the gateway's, stateless)
or of the loop's state (ours, persistent).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai_harness.conversation_search._toolset import (  # the same BM25 as Harness
    _bm25_rank,
    _Bm25Params,
)

from .replay import Call, Probe, Segment, Stats, measure, probes, segments, tokens
from .transcript import Message, load

PLACEHOLDER = "[cleared: call history_search or read the saved file to see it again]"
TOOLS = [{"type": "function", "function": {"name": "replay"}}]


def baseline(found: list[Segment], system: str) -> list[list[Message]]:
    """The message list of every model call our loop makes, in order."""
    history: list[Message] = [{"role": "system", "content": system}]
    requests: list[list[Message]] = []
    number = 0
    for segment in found:
        history.append({"role": "user", "content": segment.prompt})
        for turn in segment.turns:
            requests.append(list(history))
            calls = []
            results = []
            for name, args, result in turn.calls:
                number += 1
                call_id = f"c{number}"
                calls.append(
                    {"id": call_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                )  # fmt: skip
                results.append({"role": "tool", "tool_call_id": call_id, "content": result})
            message: Message = {"role": "assistant", "content": turn.text}
            if calls:
                message["tool_calls"] = calls
            history.append(message)
            history.extend(results)
    return requests


def size(messages: list[Message]) -> int:
    return tokens(json.dumps({"messages": messages, "tools": TOOLS})) + 14_000


def _tool_indices(messages: list[Message]) -> list[int]:
    return [i for i, m in enumerate(messages) if m["role"] == "tool"]


def clear_tool_uses(trigger: int, keep: int = 3, clear_at_least: int = 0) -> Any:
    """Anthropic's ``clear_tool_uses_20250919`` as the server applies it to each request:
    past ``trigger`` input tokens, every tool result but the last ``keep`` is replaced,
    unless that clears fewer than ``clear_at_least`` tokens. Stateless: the client keeps
    sending the whole history."""

    def edit(requests: list[list[Message]]) -> list[list[Message]]:
        edited = []
        for messages in requests:
            if size(messages) <= trigger:
                edited.append(messages)
                continue
            indices = _tool_indices(messages)
            cut = indices[:-keep] if keep else indices
            out = list(messages)
            cleared = 0
            for i in cut:
                if out[i]["content"] != PLACEHOLDER:
                    cleared += tokens(out[i]["content"]) - tokens(PLACEHOLDER)
                    out[i] = {**out[i], "content": PLACEHOLDER}
            edited.append(out if cleared >= clear_at_least else messages)
        return edited

    return edit


@dataclass
class OurClearing:
    """Our loop's own clearing: past ``high`` tokens, the oldest tool results not among
    the last ``keep`` are replaced until the request is under ``low``. What is cleared
    stays cleared, so the cache is rebuilt once per clearing, not on every call."""

    high: int
    low: int
    keep: int = 3
    cleared: set[int] = field(default_factory=set[int])
    spill_over: int | None = None
    """Tool results longer than this many characters reach the model as their first
    1,000 characters and a pointer to the saved file, as our tools' sampled output does."""

    def __call__(self, requests: list[list[Message]]) -> list[list[Message]]:
        edited = []
        for messages in requests:
            out = [self._spilled(m) for m in messages]
            for i in self.cleared:
                out[i] = {**out[i], "content": PLACEHOLDER}
            if size(out) > self.high:
                indices = _tool_indices(out)
                for i in indices[: -self.keep] if self.keep else indices:
                    if size(out) <= self.low:
                        break
                    if i not in self.cleared:
                        self.cleared.add(i)
                        out[i] = {**out[i], "content": PLACEHOLDER}
            edited.append(out)
        return edited

    def _spilled(self, message: Message) -> Message:
        content = message.get("content") or ""
        if self.spill_over is None or message["role"] != "tool" or len(content) <= self.spill_over:
            return message
        return {**message, "content": content[:1000] + "\n[output continues in the saved file]"}


def recall(probe: Probe, transcript: list[Message], *, words: bool) -> bool:
    """Whether a BM25 search of the run records (Harness's own ranking) finds the value:
    by the words before it, or by the value itself (what was kept at all)."""
    documents = [json.dumps(m) for m in transcript]
    ranked = _bm25_rank(probe.query if words else probe.value, documents, _Bm25Params(1.5, 0.75))
    return any(probe.value in documents[i] for i, _score in ranked[:10])


def main(labs: Path) -> None:
    session = load([(n, labs / f"control-{n}.sqlite3") for n in ("39", "40", "41")])
    found = segments(session)
    picked = probes(found)
    requests = baseline(found, session.system)
    transcript = requests[-1]
    configs: list[tuple[str, Any, bool]] = [
        ("C0 our loop today", lambda r: r, False),
        ("C1 clear_tool_uses 100k", clear_tool_uses(100_000), True),
        ("C1 clear_tool_uses 60k", clear_tool_uses(60_000), True),
        ("C1 clear 60k, at least 20k", clear_tool_uses(60_000, clear_at_least=20_000), True),
        ("C2 ours 100k->60k", OurClearing(100_000, 60_000), True),
        ("C2 ours 60k->40k", OurClearing(60_000, 40_000), True),
        ("C3 spill 4k + ours 60k->40k", OurClearing(60_000, 40_000, spill_over=4_000), True),
    ]
    for name, transform, searchable in configs:
        edited = transform(requests)
        stats: Stats = measure([Call({"messages": m, "tools": TOOLS}, False) for m in edited])
        final = {"messages": edited[-1]}
        in_final = sum(p.value in json.dumps(final) for p in picked)
        found_words = sum(recall(p, transcript, words=True) for p in picked) if searchable else 0
        found_value = sum(recall(p, transcript, words=False) for p in picked) if searchable else 0
        print(
            f"{name:30} {stats.line()} in_final={in_final}/{len(picked)} "
            f"recovered={found_words}/{len(picked)} persisted={found_value}/{len(picked)}"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
