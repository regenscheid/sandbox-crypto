# SPDX-License-Identifier: MIT
"""Claude through the LiteLLM proxy's ``/v1/messages`` (docs/specs/context-management.md,
CM-38, CM-38a, CM-39, CM-42): Anthropic's Messages API behind ``ChatModel``, for the
aliases the deployment names for it (``--api ALIAS=messages``). Every other alias keeps
its own client (CM-41).

The conversation stays in the chat format the loop keeps, and each request renders it:

- the first system message is the request's ``system``; tool results and the platform's
  messages between two answers make one user turn, the results first;
- an answer that came back through this API (an ``AssistantMessage`` whose native answer
  is a message) is sent as the response's content blocks when its text and calls are
  still the response's. Its thinking blocks go back unchanged only while thinking replay
  is on (``ThinkingReplay``, off by default) and the request before the answer is still
  the one it answered, by its ``prefix``. Otherwise its thinking is left out, and
  its progress notes (the text of thinking blocks under ``display: "updates"``) are sent
  as text blocks marked ``NOTE``, the same from the first time the answer goes back.
  Thinking is only ever left out from the start or the end of the run, never from the
  middle. Any other answer is sent as its text and calls (CM-42);
- calls and results are paired first (``pairing``, CM-29a);
- tools loaded during the run are declared with ``defer_loading`` and announced by a
  ``tool_addition`` where they first appeared, so the request's tool list is not
  edited, on the models that take it; elsewhere the list changes, which leaves out the
  thinking of the answers before the change, as a rebuild does;
- cache breakpoints mark the tools, the system message and the last user turn.

A turn's answer (``text``) is its text blocks, and its progress notes are ``notes``. What
the conversation holds (``content``) is both, in order, a paragraph each, so the archive
indexes the notes and another adapter sends them as the answer's text, while the user's
answer never shows one. A refusal of the thinking sent back is answered once more without
it, replay off for the run; a refusal of ``display: "updates"``, as when the gateway does
not pass the beta header on, with summaries from then on. A reasoning summary
(``display: "summarized"``, on the models without ``updates``) is archived with the
response and never sent as text. Usage is CM-28's: the input is ``input_tokens`` with
the cache's reads and writes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Final, cast

import aiohttp

from .model import (
    AssistantMessage,
    GatewayModel,
    Message,
    ModelError,
    ModelTurn,
    ToolCall,
    ToolSpec,
    gateway_said,
    prefix_digest,
    read_limited,
    reported_cost,
)
from .pairing import ABORTED, CALL, OUTPUT, Item, paired

_log = logging.getLogger(__name__)

Json = dict[str, Any]

MESSAGES: Final = "messages"
"""The native format of this client's answers: a message, as the gateway sent it."""
NOTE: Final = "[progress note] "
"""What marks a progress note sent as text (CM-38a)."""
UPDATES_BETA: Final = "thinking-display-updates-2026-08-18"
BINDING_BETA: Final = "thinking-binding-controls-2026-08-01"
TOOLS_BETA: Final = "mid-conversation-tool-changes-2026-07-01"
MAX_TOKENS: Final = 16_000
"""An answer's length when the role sets none: the API needs one, and a request that
does not stream must end within the client's timeout."""
REPLAY_DAYS: Final = 30
"""How long a check of the gateway lets thinking replay stay on (CM-39)."""
MIN_THINKING: Final = 256
"""The least replayed thinking, in tokens, whose loss a call's input can show."""
_UPDATES: Final = re.compile(r"claude-(?:fable-5(?:-1)?|mythos-5-1|opus-5-5|sonnet-5-5)(?![-.]?\d)")
"""The models whose documentation offers ``display: "updates"`` (CM-38)."""
_TOOL_CHANGES: Final = re.compile(
    r"claude-(?:opus-(?:5|5-5|4-8)|fable-5(?:-1)?|mythos-5(?:-1)?|sonnet-5-5)(?![-.]?\d)"
)
"""The models that take mid-conversation tool changes."""
_OLDER: Final = re.compile(
    r"claude-3|claude-(?:opus|sonnet|haiku)-4(?:-[015])?(?:-\d{8})?(?![-.]?\d)"
)
"""The Claude models before the 4.6 family, which take no adaptive thinking."""
_UNBOUND: Final = ("bound to a different conversation", "signature` in `thinking` block")
"""How Anthropic refuses thinking sent back whose history changed, or that it cannot
read: its documented recovery is the request again without thinking."""
_UNDISPLAYED: Final = ("thinking.display", UPDATES_BETA, "anthropic-beta")
"""How a refusal of ``display: "updates"`` reads, when the gateway does not pass the
beta header on."""
_THINKING: Final = frozenset({"thinking", "redacted_thinking"})
_FINISH: Final = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
}
_CACHED: Final = {"type": "ephemeral"}
_INFO_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class ThinkingReplay:
    """CM-39's deployment setting, which turns thinking replay on for the gateway image
    it was verified on: the image's digest as the operator deployed it, the version its
    ``x-litellm-version`` header reports, the date of the CM-F30 run that passed against
    it, and that run's ID. A call that reports another version, or that shows the
    gateway dropped replayed thinking, turns replay off for the rest of the run, and the
    setting lapses ``REPLAY_DAYS`` after its date."""

    image: str
    version: str
    verified: date
    run: str

    @classmethod
    def parse(cls, text: str) -> ThinkingReplay:
        """The setting from its JSON object; a ValueError says what is wrong with it."""
        try:
            data: object = json.loads(text)
        except ValueError:
            raise ValueError("the thinking replay setting is not JSON") from None
        names = ("image", "version", "verified", "run")
        if not isinstance(data, dict) or sorted(cast(dict[str, object], data)) != sorted(names):
            raise ValueError("the thinking replay setting names image, version, verified and run")
        values = cast(dict[str, object], data)
        for name in names:
            value = values[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"the thinking replay setting's {name} is not a short text")
        try:
            verified = date.fromisoformat(str(values["verified"]))
        except ValueError:
            raise ValueError("the thinking replay setting's verified is not a date") from None
        return cls(str(values["image"]), str(values["version"]), verified, str(values["run"]))

    def lapsed(self, today: date) -> str | None:
        """Why the setting no longer holds on ``today``, if it does not."""
        if today > self.verified + timedelta(days=REPLAY_DAYS):
            return f"the setting's check of {self.verified} is over {REPLAY_DAYS} days old"
        return None


@dataclass(frozen=True)
class Kept:
    """What this adapter keeps of a call for the requests that send its answer back
    (the answer's ``adapter_state``, archived with it): the request's tools by name, and,
    when the call turned thinking replay off for the rest of the run, why (CM-39). An
    answer without it, from before it was kept, sends the tools as they are now."""

    tools: tuple[str, ...]
    off: str | None = None

    def state(self) -> dict[str, Any]:
        return {"tools": list(self.tools), "off": self.off}

    @classmethod
    def of(cls, state: Mapping[str, Any] | None) -> Kept | None:
        """The kept state an answer carries, if it is shaped as ``state`` makes it."""
        tools, off = (state or {}).get("tools"), (state or {}).get("off")
        listed = cast(list[object], tools) if isinstance(tools, list) else None
        if listed is None or not all(isinstance(name, str) for name in listed):
            return None
        return cls(tuple(map(str, listed)), off if isinstance(off, str) else None)


@dataclass(frozen=True)
class _Read:
    """An answer of this API as an adapter reads it once: its response's content
    blocks, and what its call kept, if it is known."""

    blocks: tuple[Json, ...]
    kept: Kept | None


@dataclass(frozen=True)
class _Served:
    """What the model behind the alias takes, as the gateway's model information says."""

    updates: bool
    tool_changes: bool
    adaptive: bool = True


@dataclass(frozen=True)
class _Prior:
    """The answer before a request, when its thinking was sent back: its call's input
    and output tokens, and about how many of those were thinking."""

    input: int
    output: int
    thinking: int


@dataclass
class _View:
    body: Json
    prefix: str
    prior: _Prior | None
    added: int
    """The characters sent after the prior answer."""


class MessagesModel(GatewayModel):
    """Anthropic's Messages API with tool use, as the LiteLLM proxy serves it; thinking
    replay on only under ``replay`` (CM-39)."""

    PATH = "/messages"
    FORMAT = MESSAGES
    PREFIX = ("system", "tools", "messages")

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        replay: ThinkingReplay | None = None,
        **options: Any,
    ) -> None:
        super().__init__(base_url, model, api_key, **options)
        self.replay = replay
        self._served: _Served | None = None

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        served = await self._serving()
        why_not = self._why_not(messages)
        off: str | None = None
        try:
            turn, view, reading = await self._ask(messages, tools, served, why_not is None)
        except ModelError as error:
            said = str(error)
            if error.status != 400:
                raise
            if why_not is None and any(mark in said for mark in _UNBOUND):
                # Anthropic's recovery: the request again with no thinking from the
                # history, and replay off for the rest of the run (CM-39).
                why_not = off = "the API refused the thinking sent back as bound elsewhere"
                _log.warning("thinking replay is off for the rest of the run: %s", said)
            elif served.updates and any(mark in said for mark in _UNDISPLAYED):
                # The gateway did not pass the beta on: summaries from now on (CM-F31).
                self._served = served = replace(served, updates=False)
                _log.warning(
                    "%s gets no progress notes, and summaries instead: %s", self.model, said
                )
            else:
                raise
            turn, view, reading = await self._ask(messages, tools, served, why_not is None)
        if why_not is None and self.replay is not None:
            off = _turned_off(self.replay, turn, reading, view)
        return replace(
            turn,
            prefix=view.prefix,
            adapter_state=Kept(tuple(tool.name for tool in tools), off).state(),
            thinking_replay="on" if why_not is None else f"off: {why_not}",
        )

    async def _ask(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        served: _Served,
        replay: bool,
    ) -> tuple[ModelTurn, _View, _Reading]:
        view = _render(messages, tools, served, replay)
        view.body["model"] = self.model
        view.body["max_tokens"] = self._max_tokens or MAX_TOKENS
        reading = _Reading(served.updates)
        return await self._call(view.body, reading.parse), view, reading

    def _headers(self, body: Mapping[str, Any]) -> dict[str, str]:
        """The beta flags the request's features need: progress notes, the binding
        check, and tools added during the run."""
        thinking = cast(Json, body.get("thinking") or {})
        betas = [UPDATES_BETA] if thinking.get("display") == "updates" else []
        if "block_binding" in thinking:
            betas.append(BINDING_BETA)
        if any(message.get("role") == "system" for message in body.get("messages", [])):
            betas.append(TOOLS_BETA)
        return {"anthropic-beta": ",".join(betas)} if betas else {}

    def _why_not(self, messages: Sequence[Message]) -> str | None:
        """Why this call sends no thinking back, if it does not."""
        if self.replay is None:
            return "no thinking replay setting"
        records = (_read(message).kept for message in messages)
        turned = next((kept.off for kept in records if kept is not None and kept.off), None)
        return turned or self.replay.lapsed(self._clock.now().date())

    async def _serving(self) -> _Served:
        """What the alias's model takes, asked of the gateway once it has answered."""
        if self._served is not None:
            return self._served
        models = await self._models()
        if models is None:
            return _Served(False, False)
        if not models:
            _log.warning("the gateway does not say which model %s is", self.model)
        served = _Served(
            bool(models) and all(_UPDATES.search(model) for model in models),
            bool(models) and all(_TOOL_CHANGES.search(model) for model in models),
            not models or not all(_OLDER.search(model) for model in models),
        )
        self._served = served
        return served

    async def _models(self) -> list[str] | None:
        """The provider models the gateway serves the alias by (``GET /model/info``, its
        ``litellm_params.model``); ``None`` when the gateway could not be asked."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                f"{self.base_url}/model/info",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                data = await read_limited(response, _INFO_BYTES)
                if not 200 <= response.status < 300:
                    _log.warning("the gateway describes no models: HTTP %s", response.status)
                    return []
        except (aiohttp.ClientError, TimeoutError, ModelError) as error:
            _log.warning("the gateway describes no models: %s", type(error).__name__)
            return None
        try:
            found = _object(json.loads(data)).get("data")
        except (ValueError, TypeError, RecursionError):
            return []
        models: list[str] = []
        for entry in cast(list[object], found) if isinstance(found, list) else []:
            fields = cast(Json, entry) if isinstance(entry, dict) else {}
            params = fields.get("litellm_params")
            named = cast(Json, params).get("model") if isinstance(params, dict) else None
            if fields.get("model_name") == self.model and isinstance(named, str):
                models.append(named.lower())
        return models


class _Reading:
    """Reads a response as a turn, noting whether the API said it dropped thinking."""

    def __init__(self, notes: bool) -> None:
        self.notes = notes
        self.dropped = False

    def parse(self, text: str, priced: str | None = None) -> ModelTurn:
        try:
            data = _object(json.loads(text))
            content = data["content"]
            if not isinstance(content, list):
                raise TypeError("content is not a list")
            blocks = [_object(block) for block in cast(list[object], content)]
            said, calls = _answer(blocks, self.notes)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise ModelError(f"unexpected message: {gateway_said(text)}") from exc
        changed = data.get("input_transformations")
        if isinstance(changed, list) and changed:
            # Logged, as CM-38 asks; a model switch's dropped blocks are expected.
            _log.warning("the API changed the request's thinking: %s", json.dumps(changed)[:500])
            self.dropped = any(
                isinstance(entry, dict) and cast(Json, entry).get("type") == "thinking_dropped"
                for entry in cast(list[object], changed)
            )
        usage = _usage(data)
        stop, resolved = data.get("stop_reason"), data.get("model")
        # The answer is its text blocks; its notes go in what the conversation holds.
        answer = _answer(blocks, False)[0]
        notes = [block.get("thinking") for block in blocks if _note(block) is not None]
        return ModelTurn(
            text=answer,
            content=said if said != answer else None,
            notes=tuple(str(note) for note in notes) if self.notes else (),
            tool_calls=calls,
            finish_reason=_FINISH.get(stop, stop) if isinstance(stop, str) else None,
            model=resolved if isinstance(resolved, str) else None,
            input_tokens=_input(usage),
            output_tokens=_tokens(usage.get("output_tokens")),
            cost=reported_cost(priced if priced is not None else usage.get("cost")),
        )


def _turned_off(
    replay: ThinkingReplay, turn: ModelTurn, reading: _Reading, view: _View
) -> str | None:
    """Why replay is off for the rest of the run after a replaying call, if it is:
    another version than the setting's, or an input short by about the thinking the
    call sent back, which the API did not say it dropped (CM-39)."""
    why = None
    if turn.gateway_version != replay.version:
        got = turn.gateway_version or "no version"
        why = f"the gateway reported {got}, the setting names {replay.version}"
    elif (prior := view.prior) is not None and turn.input_tokens is not None:
        expected = prior.input + prior.output + view.added // 4
        short = expected - turn.input_tokens
        # The newest answer's thinking at least is missing when any is; the tolerance
        # covers the estimate of what was added since.
        tolerance = prior.thinking // 2 + view.added // 16
        if not reading.dropped and prior.thinking >= MIN_THINKING and short >= tolerance:
            why = (
                f"the gateway dropped thinking: {turn.input_tokens:,} input tokens "
                f"reported, about {expected:,} expected"
            )
    if why is not None:
        _log.warning("thinking replay is off for the rest of the run: %s", why)
    return why


def _render(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    served: _Served,
    replay: bool,
) -> _View:
    """The request's body, without its model and length, and what the checks need."""
    conversation = _paired(messages)
    system: list[Json] = []
    if conversation and conversation[0].get("role") == "system":
        first = _text(conversation.pop(0))
        system = [{"type": "text", "text": first}] if first else []
    by_name = {tool.name: tool for tool in tools}
    reads = {id(m): _read(m) for m in conversation if m.get("role") == "assistant"}
    named = [read.kept.tools for read in reads.values() if read.kept is not None]
    base = named[0] if named else tuple(by_name)
    # Tools loaded during the run are deferred and announced, on the models that can,
    # while every tool the run has had is still there.
    had = {name for names in named for name in names}
    deferring = served.tool_changes and bool(base) and had <= by_name.keys()
    declared = [_tool(by_name[name]) for name in base] if deferring else list(map(_tool, tools))
    seen = set(base if deferring else by_name)
    deferred: list[Json] = []
    out: list[Json] = []
    clean: list[Json] = []
    results: list[Json] = []
    texts: list[Json] = []

    def digest() -> str:
        """What the binding of thinking covers so far: the system message, the tools
        but the deferred ones, and the messages without their thinking."""
        return prefix_digest({"system": system, "tools": declared, "messages": clean})

    def add(message: Json) -> None:
        out.append(message)
        clean.append(_unthought(message))

    def flush() -> None:
        if results or texts:
            add({"role": "user", "content": [*results, *texts]})
            results.clear()
            texts.clear()

    def announce(names: Sequence[str]) -> None:
        new = [name for name in names if name not in seen]
        if new:
            seen.update(new)
            deferred.extend(_tool(by_name[name], deferred=True) for name in new)
            add({"role": "system", "content": [
                {"type": "tool_addition", "tool": {"type": "tool_reference", "name": name}}
                for name in new
            ]})  # fmt: skip

    sent = stopped = False
    prior: _Prior | None = None
    after = 0
    for message in conversation:
        role = message.get("role")
        if role == "tool":
            call = _scrubbed(message.get("tool_call_id"))
            results.append({"type": "tool_result", "tool_use_id": call, "content": _text(message)})
        elif role in ("user", "system"):
            if text := _text(message):
                texts.append({"type": "text", "text": text})
        elif role == "assistant":
            flush()
            read = reads[id(message)]
            if deferring and read.kept is not None:
                announce(read.kept.tools)
            notes = _notes(message, read.blocks)
            thought = any(block.get("type") in _THINKING for block in read.blocks)
            # Thinking goes back while the request before it is the one it answered,
            # and is left out only from the run's start or its end, never between.
            send = (
                replay and notes is not None and thought and not stopped
                and isinstance(message, AssistantMessage) and message.prefix == digest()
            )  # fmt: skip
            sent, stopped = sent or send, stopped or (thought and sent and not send)
            whole = read.blocks if notes is not None else None
            if content := _assistant(message, whole, bool(notes), send):
                add({"role": "assistant", "content": content})
            prior = _prior(message) if send and isinstance(message, AssistantMessage) else None
            after = len(out)
        else:
            raise ModelError(f"a message whose role is {role!r} cannot be sent")
    flush()
    if deferring:
        announce(list(by_name))
    prefix = digest()
    body: Json = {"messages": _cached(out)}
    if system:
        body["system"] = [*system[:-1], {**system[-1], "cache_control": _CACHED}]
    if declared or deferred:
        if declared:
            declared[-1] = {**declared[-1], "cache_control": _CACHED}
        body["tools"] = [*declared, *deferred]
    # Claude models before the 4.6 family take no adaptive thinking: they run without.
    if served.adaptive:
        display = "updates" if served.updates else "summarized"
        thinking: Json = {"type": "adaptive", "display": display}
        if replay:
            thinking["block_binding"] = {"prefix_mismatch_behavior": "error"}
        body["thinking"] = thinking
    added = len(_canonical(out[after:])) if prior is not None else 0
    return _View(body, prefix, prior, added)


def _paired(messages: Sequence[Message]) -> list[Message]:
    """The conversation with every call answered and every result's call present, by
    ``pairing``'s rules: an unanswered call gets a result saying it was aborted."""
    items: list[Item] = []
    for message in messages:
        if message.get("role") == "tool":
            items.append({"type": OUTPUT, "call_id": message.get("tool_call_id"), "m": message})
            continue
        items.append({"type": "message", "m": message})
        if message.get("role") == "assistant":
            items.extend({"type": CALL, "call_id": call.id} for call in _chat_calls(message))
    kept: list[Message] = []
    for item in paired(items):
        if item["type"] != CALL:
            synthetic = {"role": "tool", "tool_call_id": item.get("call_id"), "content": ABORTED}
            kept.append(cast(Message, item.get("m", synthetic)))
    return kept


def _assistant(
    message: Message, blocks: Sequence[Json] | None, notes: bool, send: bool
) -> list[Json]:
    """An answer's content: its response's blocks, with or without their thinking, the
    progress notes of those without as text (with ``notes``), or else its text and
    calls."""
    if blocks is not None:
        if send:
            return list(blocks)
        content: list[Json] = []
        for block in blocks:
            if block.get("type") not in _THINKING:
                content.append(block)
            elif notes and (note := _note(block)) is not None:
                content.append({"type": "text", "text": note})
        return content
    content = [{"type": "text", "text": text}] if (text := _text(message)) else []
    for call in _chat_calls(message):
        try:
            given: object = json.loads(call.arguments)
        except ValueError:
            given = None
        content.append(
            {
                "type": "tool_use",
                "id": _scrubbed(call.id),
                "name": call.name,
                "input": given if isinstance(given, dict) else {},
            }
        )
    return content


def _read(message: Message) -> _Read:
    """An answer of this API, read once: its response's content blocks, and what its
    call kept (its ``adapter_state``); nothing for any other message."""
    if not isinstance(message, AssistantMessage) or message.native_format != MESSAGES:
        return _Read((), None)
    if not isinstance(message.read, _Read):
        try:
            data = _object(json.loads(message.native or b""))
        except (ValueError, TypeError, RecursionError):
            data = {}
        content = data.get("content")
        listed = cast(list[object], content) if isinstance(content, list) else []
        blocks = tuple(cast(Json, b) for b in listed if isinstance(b, dict))
        message.read = _Read(blocks, Kept.of(message.adapter_state))
    return message.read


def _notes(message: Message, blocks: Sequence[Json]) -> bool | None:
    """Whether an answer's thinking text is progress notes, which its text holds, or
    not (a summary, which it does not); ``None`` when its text and calls are no longer
    its response's: a rebuild or an edit made it another answer, sent as its text and
    calls."""
    if not blocks:
        return None
    text, calls = _text(message) or None, tuple(_chat_calls(message))
    return next((notes for notes in (True, False) if _answer(blocks, notes) == (text, calls)), None)


def _answer(blocks: Sequence[Json], notes: bool) -> tuple[str | None, tuple[ToolCall, ...]]:
    """A response's text, its text blocks and (with ``notes``) its progress notes in
    order, a paragraph each, and its calls."""
    said: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text" and isinstance(text := block.get("text"), str) and text:
            said.append(text)
        elif kind == "thinking" and notes and (note := _note(block)) is not None:
            said.append(note)
        elif kind == "tool_use":
            arguments = json.dumps(block.get("input", {}))
            calls.append(ToolCall(str(block.get("id", "")), str(block.get("name", "")), arguments))
    return ("\n\n".join(said) or None), tuple(calls)


def thoughts(native: bytes) -> list[str]:
    """The text of a response's thinking blocks, progress notes or summaries, for the
    archive's indexes (CM-5, CM-38); none if it is not shaped like a message."""
    try:
        content = _object(json.loads(native)).get("content")
    except (ValueError, TypeError, RecursionError):
        return []
    blocks = cast(list[object], content) if isinstance(content, list) else []
    return [
        text
        for block in blocks
        if isinstance(block, dict) and cast(Json, block).get("type") == "thinking"
        if isinstance(text := cast(Json, block).get("thinking"), str) and text.strip()
    ]


def _note(block: Json) -> str | None:
    text = block.get("thinking")
    return NOTE + text if isinstance(text, str) and text.strip() else None


def _prior(message: AssistantMessage) -> _Prior | None:
    """An answer's call's tokens, from its response's usage; what its text and calls do
    not account for, at four characters a token, is taken for thinking."""
    try:
        usage = _usage(_object(json.loads(message.native or b"")))
    except (ValueError, TypeError, RecursionError):
        return None
    total, output = _input(usage), _tokens(usage.get("output_tokens"))
    if total is None or output is None:
        return None
    shown = len(_text(message)) + sum(len(c.arguments) for c in _chat_calls(message))
    return _Prior(total, output, max(0, output - shown // 4))


def _usage(data: Json) -> Json:
    usage = data.get("usage")
    return cast(Json, usage) if isinstance(usage, dict) else {}


def _input(usage: Json) -> int | None:
    """A call's total input (CM-28): its uncached tokens, and the cache's reads and
    writes, which Anthropic counts apart."""
    fresh = _tokens(usage.get("input_tokens"))
    if fresh is None:
        return None
    cached = ("cache_read_input_tokens", "cache_creation_input_tokens")
    return fresh + sum(_tokens(usage.get(key)) or 0 for key in cached)


def _cached(messages: list[Json]) -> list[Json]:
    """The messages with a cache breakpoint on the last user turn's last block."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "user":
            *head, last = messages[index]["content"]
            turn = {"role": "user", "content": [*head, {**last, "cache_control": _CACHED}]}
            return [*messages[:index], turn, *messages[index + 1 :]]
    return messages


def _unthought(message: Json) -> Json:
    """A message as the digest takes it: without its thinking blocks."""
    content = [b for b in message["content"] if b.get("type") not in _THINKING]
    return {**message, "content": content}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _tool(tool: ToolSpec, *, deferred: bool = False) -> Json:
    declared: Json = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }
    if deferred:
        declared["defer_loading"] = True
    return declared


def _chat_calls(message: Message) -> list[ToolCall]:
    found: list[ToolCall] = []
    for raw in cast(list[object], message.get("tool_calls") or []):
        call = cast(Json, raw) if isinstance(raw, dict) else {}
        function = call.get("function")
        function = cast(Json, function) if isinstance(function, dict) else {}
        found.append(
            ToolCall(
                str(call.get("id", "")),
                str(function.get("name", "")),
                str(function.get("arguments", "{}")),
            )
        )
    return found


def _scrubbed(value: object) -> str:
    """A call ID as Anthropic takes it: letters, digits, ``_`` and ``-`` only, as
    OpenCode scrubs another provider's IDs."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "")) or "call"


def _text(message: Message) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _tokens(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _object(value: object) -> Json:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, not {type(value).__name__}")
    return cast(Json, value)
