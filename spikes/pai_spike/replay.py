# SPDX-License-Identifier: MIT
"""Replaying a recorded session through a framework, against the fake gateway, and
measuring what each model call was sent.

The framework under test runs its real loop: its model client calls the fake LiteLLM
proxy, which answers each call with the next recorded assistant turn, and the tools
return the recorded results. Whatever the framework's context management does to the
history shows up in the request bodies the fake receives, which is what is measured:

- tokens per call (characters / 4, the harness's own heuristic; the labs' usage shows
  about 4.2 characters per token on this data), plus a fixed 14,000 tokens for the
  tool schemas the real runs carried (lab 40's first call: 15,452 input tokens);
- the prompt prefix shared with the previous call, element by element (tools, then each
  message), which is what a provider's prefix cache can reuse; a call whose prefix does
  not include all of the previous request is a cache bust;
- the summarizer's calls, answered by a deterministic stand-in (``standin_summary``),
  which keeps identifiers and the latest text, not values: a lower bound on what a real
  model's summary keeps.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .fake_litellm import Reply
from .transcript import Message, Session

TOOL_SCHEMA_TOKENS = 14_000
CHARS_PER_TOKEN = 4


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass
class Turn:
    text: str | None
    calls: list[tuple[str, dict[str, Any], str]]
    """(tool name, arguments, the recorded result the tool returns)."""


@dataclass
class Segment:
    """One framework run: a user prompt, then model turns until an answer."""

    prompt: str
    turns: list[Turn]


def segments(session: Session) -> list[Segment]:
    """The session as framework runs. Notices that arrived between tool results ride on
    the preceding result, as the loop's tool-point notices do; a model answer followed by
    more user messages ends a segment."""
    found: list[Segment] = []
    messages = session.messages[1:]
    i = 0
    while i < len(messages):
        prompt: list[str] = []
        while i < len(messages) and messages[i]["role"] == "user":
            prompt.append(messages[i]["content"])
            i += 1
        turns: list[Turn] = []
        while i < len(messages) and messages[i]["role"] == "assistant":
            m = messages[i]
            i += 1
            calls = m.get("tool_calls") or []
            results: dict[str, str] = {}
            last: str | None = None
            while i < len(messages) and messages[i]["role"] in ("tool", "user"):
                r = messages[i]
                if r["role"] == "tool":
                    results[r["tool_call_id"]] = r["content"]
                    last = r["tool_call_id"]
                elif last is not None:
                    results[last] += "\n\n" + r["content"]
                else:
                    break
                i += 1
            turns.append(
                Turn(
                    m.get("content"),
                    [
                        (
                            c["function"]["name"],
                            _args(c["function"]["arguments"]),
                            results.get(c["id"], '{"error":"no result recorded"}'),
                        )
                        for c in calls
                    ],
                )
            )
            if not calls:
                break
            if i < len(messages) and messages[i]["role"] == "user":
                break  # a user message after tool results with no call between: rare
        if prompt or turns:
            found.append(Segment("\n\n".join(prompt) or "(continue)", turns))
    return found


def _args(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {"_raw": text}
    return value if isinstance(value, dict) else {"_value": value}


# --- Probes: details from early tool results, to see what survives -----------------------

_VALUE = re.compile(r"\b(?:[0-9a-f]{64}|\d{30,})\b")


@dataclass(frozen=True)
class Probe:
    value: str
    query: str
    tool: str
    segment: int


def probes(found: list[Segment], *, until: float = 0.5, limit: int = 12) -> list[Probe]:
    """Values (a SHA-256, a number of 30 digits or more) from tool results in the first
    ``until`` of the session, each with a query made of the words just before it, as a
    model that remembers roughly where it saw a value would search for it."""
    picked: list[Probe] = []
    seen: set[str] = set()
    last = int(len(found) * until)
    for n, seg in enumerate(found[:last]):
        for turn in seg.turns:
            for name, _args, result in turn.calls:
                for match in _VALUE.finditer(result):
                    value = match.group(0)
                    if value in seen:
                        continue
                    seen.add(value)
                    words = re.findall(
                        r"[A-Za-z][A-Za-z_]{2,}", result[max(0, match.start() - 60) : match.start()]
                    )
                    query = " ".join(words[-3:]) or name
                    picked.append(Probe(value, query, name, n))
    if len(picked) <= limit:
        return picked
    step = len(picked) / limit
    return [picked[int(k * step)] for k in range(limit)]


# --- The scripted gateway ------------------------------------------------------------------

_IDS = re.compile(
    r"\b(?:[A-Z]-\d+|[a-z]+-[0-9a-f]{12,}|\d{4}/\d{3,5}|[\w.-]+/[\w./-]+\.\w{1,5}|r\d+)\b"
)


def standin_summary(prompt: str) -> str:
    """A structured summary in the harness's sections: the first and last text of the
    conversation, and the identifiers it names. It keeps no values."""
    start, end = prompt.find("<messages>"), prompt.rfind("</messages>")
    if 0 <= start < end:
        prompt = prompt[start + len("<messages>") : end]
    ids: list[str] = []
    for found in _IDS.findall(prompt):
        if found not in ids:
            ids.append(found)
    body = re.sub(r"\s+", " ", prompt)
    return (
        "## Intent\n"
        + body[:600]
        + "\n\n## Artifacts\n"
        + ", ".join(ids[-80:])
        + "\n\n## Current state\n"
        + body[-600:]
        + "\n\n## Next steps\nContinue the user's latest request."
    )


SUMMARIZER = "summarizer"
"""The model alias the replays give summarization, which tells its calls apart."""


def is_summary_call(body: dict[str, Any]) -> bool:
    return body.get("model") == SUMMARIZER


@dataclass
class Call:
    body: dict[str, Any]
    summary: bool


@dataclass
class Scripter:
    """Answers the fake gateway's calls from the segments, in order."""

    found: list[Segment]
    calls: list[Call] = field(default_factory=list[Call])
    results: dict[str, str] = field(default_factory=dict[str, str])
    """Each tool call ID's recorded result, for the framework's tool to return."""
    pending: list[Turn] = field(default_factory=list[Turn])
    numbered: int = 0
    schema_tokens: int = TOOL_SCHEMA_TOKENS
    """What the reported input adds for tool schemas the requests do not carry."""
    live: set[str] = field(default_factory=set[str])
    """Call IDs of live segments, whose tools really run."""
    _live: bool = False

    def start(self, segment: Segment, *, live: bool = False) -> None:
        self.pending = list(segment.turns)
        self._live = live

    def __call__(self, path: str, body: dict[str, Any]) -> Reply:
        size = tokens(json.dumps(body))
        if is_summary_call(body):
            self.calls.append(Call(body, True))
            prompt = "\n".join(str(m.get("content")) for m in body.get("messages") or [])
            return Reply(standin_summary(prompt), input_tokens=size, cost="0.001")
        self.calls.append(Call(body, False))
        turn = self.pending.pop(0) if self.pending else Turn("(the replay has no turn here)", [])
        ids: list[str] = []
        for name, _args, result in turn.calls:
            self.numbered += 1
            call_id = f"r{self.numbered}-{name}"
            ids.append(call_id)
            if self._live:
                self.live.add(call_id)
            else:
                self.results[call_id] = result
        return Reply(
            turn.text,
            [(name, args) for name, args, _r in turn.calls],
            call_ids=ids,
            input_tokens=size + self.schema_tokens,
            output_tokens=tokens((turn.text or "") + json.dumps([c[1] for c in turn.calls])),
            cost="0.001",
        )


# --- Measurement -----------------------------------------------------------------------------


def _elements(body: dict[str, Any]) -> list[str]:
    """The request as a cache sees it: the tools, then each message, in order."""
    head = json.dumps(body.get("tools") or [], sort_keys=True)
    system = body.get("system")
    items = body.get("messages") or body.get("input") or []
    return [
        head,
        *([json.dumps(system, sort_keys=True)] if system else []),
        *(json.dumps(m, sort_keys=True) for m in items),
    ]


@dataclass
class Stats:
    calls: int
    summary_calls: int
    input_tokens: int
    """Every main call's input, schemas included."""
    max_input_tokens: int
    final_input_tokens: int
    uncached_tokens: int
    """Input not in the prefix shared with the previous call: what a prefix cache misses."""
    busts: int
    """Calls whose request does not begin with the whole previous request."""
    summary_input_tokens: int
    per_call: list[tuple[int, int]]
    """(input tokens, tokens shared with the previous call's prefix) per main call."""

    @property
    def cost_units(self) -> float:
        """Input tokens priced as a prefix cache prices them: a cached token at a tenth of
        an uncached one (OpenAI and Anthropic cache reads), summaries at full price."""
        cached = self.input_tokens - self.uncached_tokens
        return self.uncached_tokens + 0.1 * cached + self.summary_input_tokens

    def line(self) -> str:
        hit = 1 - self.uncached_tokens / self.input_tokens if self.input_tokens else 0
        return (
            f"calls={self.calls} max_in={self.max_input_tokens:,} final_in={self.final_input_tokens:,} "
            f"total_in={self.input_tokens:,} uncached={self.uncached_tokens:,} "
            f"cost_units={self.cost_units:,.0f} "
            f"prefix_hit={hit:.1%} busts={self.busts} summaries={self.summary_calls} "
            f"summary_in={self.summary_input_tokens:,}"
        )


def measure(calls: list[Call]) -> Stats:
    main = [c.body for c in calls if not c.summary]
    per: list[tuple[int, int]] = []
    previous: list[str] = []
    busts = 0
    for body in main:
        elements = _elements(body)
        sizes = [tokens(e) for e in elements]
        total = sum(sizes) + TOOL_SCHEMA_TOKENS
        shared = 0
        k = 0
        while k < min(len(previous), len(elements)) and previous[k] == elements[k]:
            shared += sizes[k]
            k += 1
        if previous and k < len(previous):
            busts += 1
        if previous and k > 0:
            shared += TOOL_SCHEMA_TOKENS
        per.append((total, shared))
        previous = elements
    summary = [c.body for c in calls if c.summary]
    return Stats(
        calls=len(main),
        summary_calls=len(summary),
        input_tokens=sum(t for t, _ in per),
        max_input_tokens=max((t for t, _ in per), default=0),
        final_input_tokens=per[-1][0] if per else 0,
        uncached_tokens=sum(t - s for t, s in per),
        busts=busts,
        summary_input_tokens=sum(tokens(json.dumps(b)) for b in summary),
        per_call=per,
    )


def present(probe: Probe, body: dict[str, Any]) -> bool:
    return probe.value in json.dumps(body)


Transform = Callable[[list[Message]], list[Message]]
