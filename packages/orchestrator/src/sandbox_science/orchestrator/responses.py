# SPDX-License-Identifier: MIT
"""GPT through the LiteLLM proxy's ``/v1/responses`` (docs/specs/context-management.md,
CM-40): OpenAI's Responses API behind ``ChatModel``, for the aliases a deployment setting
names (``--responses-api``). Every other alias keeps chat completions (CM-41).

The conversation stays in the chat format the loop keeps, and each request renders it
as the API's input items:

- the first system message is the request's ``instructions``; the other messages become
  messages, function calls and their outputs;
- an answer this adapter received (an ``AssistantMessage`` whose native answer is a
  response) is sent back as the output items the gateway returned, unchanged: reasoning
  with its ``encrypted_content``, messages with their ``phase``, and the item IDs as the
  gateway rewrote them. Of those items, a function call is sent while the conversation
  still holds the call, a message while it still holds the answer's text, and a
  reasoning item together with the item after it, the call or message it led to; so a
  rebuild that keeps part of an answer keeps the reasoning behind what it kept, and no
  other. An answer with no response behind it (an earlier run's, another adapter's), or
  whose text or calls are no longer its items', is sent as its text and calls, without
  reasoning (CM-42);
- calls and outputs are then paired (``pairing``, CM-29a).

Each request sets ``store: false``, asks for ``reasoning.encrypted_content`` explicitly,
and never sets ``truncation``, which would drop the oldest items silently. Functions are
declared with ``strict: false``, as chat completions takes them. A response that failed
is an error, a context limit if it says so; one cut short says ``length``. Usage is
OpenAI's, whose ``input_tokens`` already counts the cached tokens (CM-28).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from typing import Any, Final, cast

from .model import (
    AssistantMessage,
    ContextLimitError,
    GatewayModel,
    Message,
    ModelError,
    ModelTurn,
    ToolCall,
    ToolSpec,
    reported_cost,
)
from .pairing import CALL, OUTPUT, Item, paired

RESPONSES: Final = "responses"
"""The native format of this client's answers: a response, as the gateway sent it."""
INCLUDE: Final = ("reasoning.encrypted_content",)
"""What every request asks for, though ``store: false`` returns it by default."""
REASONING: Final = "reasoning"
MESSAGE: Final = "message"
_WRITTEN: Final = {"output_text": "text", "refusal": "refusal"}
"""Where each kind of a message's content holds its text."""


class ResponsesModel(GatewayModel):
    """OpenAI's Responses API with function calling, as the LiteLLM proxy serves it."""

    PATH = "/responses"
    FORMAT = RESPONSES

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        instructions, items = rendered(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "store": False,
            "include": list(INCLUDE),
        }
        if instructions is not None:
            body["instructions"] = instructions
        if self._max_tokens is not None:
            body["max_output_tokens"] = self._max_tokens
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in tools
            ]
            body["tool_choice"] = "auto"
        return await self._call(body, parse_response)


def rendered(messages: Sequence[Message]) -> tuple[str | None, list[Item]]:
    """A chat conversation as a request's ``instructions`` and input items."""
    instructions: str | None = None
    items: list[Item] = []
    for index, message in enumerate(messages):
        role, content = message.get("role"), message.get("content")
        text = content if isinstance(content, str) else ""
        if role == "system" and index == 0:
            instructions = text
        elif role in ("system", "user"):
            items.append({"type": MESSAGE, "role": role, "content": text})
        elif role == "assistant":
            items.extend(_answer(message))
        elif role == "tool":
            call = str(message.get("tool_call_id") or "")
            items.append({"type": OUTPUT, "call_id": call, "output": text})
        else:
            raise ModelError(f"a message whose role is {role!r} cannot be sent")
    return instructions, paired(items)


def _answer(message: Message) -> list[Item]:
    content = message.get("content")
    text = content if isinstance(content, str) and content else None
    calls = _chat_calls(message)
    sent = _sent(message)
    if sent is not None and _agrees(sent, text, calls):
        return _kept(sent, text is not None, {call.id for call in calls})
    plain: list[Item] = []
    if text is not None:
        plain.append({"type": MESSAGE, "role": "assistant", "content": text})
    plain.extend(
        {"type": CALL, "call_id": call.id, "name": call.name, "arguments": call.arguments}
        for call in calls
    )
    return plain


def _sent(message: Message) -> tuple[Item, ...] | None:
    """The output items of the response behind an answer, if it has one; read once, for
    all the requests that send them."""
    if not isinstance(message, AssistantMessage) or message.native_format != RESPONSES:
        return None
    if message.read is None and message.native is not None:
        message.read = _output(message.native)
    return cast(tuple[Item, ...], message.read) or None


def _output(native: bytes) -> tuple[Item, ...]:
    """A response's output items; none if it is not shaped like a response."""
    try:
        output = _object(json.loads(native)).get("output")
    except (ValueError, TypeError, RecursionError):
        return ()
    items = cast(list[object], output) if isinstance(output, list) else []
    if not all(isinstance(item, dict) for item in items):
        return ()
    return tuple(cast(list[Item], items))


def _agrees(sent: Sequence[Item], text: str | None, calls: Sequence[ToolCall]) -> bool:
    """Whether an answer's text and calls are its items': a rebuild may leave some out,
    but an answer changed otherwise is no longer the response."""
    made = {call.id: call for call in _calls(sent)}
    if any(made.get(call.id) != call for call in calls):
        return False
    return text is None or text == _text(sent)


def _kept(sent: Sequence[Item], text: bool, calls: Collection[str]) -> list[Item]:
    """The items still in the conversation, each reasoning item with the one after it."""
    kept: list[Item] = []
    follows = False
    for item in reversed(sent):
        kind = item.get("type")
        if kind == REASONING:
            keep = follows
        elif kind == CALL:
            keep = item.get("call_id") in calls
        else:
            keep = kind == MESSAGE and text
        if keep:
            kept.append(item)
        follows = keep
    kept.reverse()
    return kept


def _chat_calls(message: Message) -> list[ToolCall]:
    found: list[ToolCall] = []
    for raw in cast(list[object], message.get("tool_calls") or []):
        call = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        function = call.get("function")
        function = cast(dict[str, Any], function) if isinstance(function, dict) else {}
        found.append(
            ToolCall(
                str(call.get("id", "")),
                str(function.get("name", "")),
                str(function.get("arguments", "{}")),
            )
        )
    return found


def parse_response(text: str, priced: str | None = None) -> ModelTurn:
    """A response as a turn; anything not shaped like one is a ModelError. ``priced`` is
    the cost header, which the response's ``usage.cost`` stands in for when it is
    missing."""
    try:
        data = _object(json.loads(text))
        output = data["output"]
        if not isinstance(output, list):
            raise TypeError("output is not a list")
        items = [_object(item) for item in cast(list[object], output)]
        calls = _calls(items)
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ModelError(f"unexpected response: {text[:300]}") from exc
    status = data.get("status")
    if status == "failed":
        error = data.get("error")
        fields = cast(dict[str, Any], error) if isinstance(error, dict) else {}
        said = f"the model's response failed: {str(fields.get('message', ''))[:300]}"
        if fields.get("code") == "context_length_exceeded":
            raise ContextLimitError(said)
        raise ModelError(said)
    if status == "incomplete":
        details = data.get("incomplete_details")
        reason = cast(dict[str, Any], details).get("reason") if isinstance(details, dict) else None
        finish = "length" if reason == "max_output_tokens" else str(reason or "incomplete")
    else:
        finish = "tool_calls" if calls else "stop"
    usage = data.get("usage")
    usage = cast(dict[str, Any], usage) if isinstance(usage, dict) else {}
    resolved = data.get("model")
    return ModelTurn(
        text=_text(items),
        tool_calls=tuple(calls),
        finish_reason=finish,
        model=resolved if isinstance(resolved, str) else None,
        input_tokens=_tokens(usage.get("input_tokens")),
        output_tokens=_tokens(usage.get("output_tokens")),
        cost=reported_cost(priced if priced is not None else usage.get("cost")),
    )


def _calls(items: Sequence[Item]) -> list[ToolCall]:
    found: list[ToolCall] = []
    for item in items:
        if item.get("type") == CALL:
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            found.append(
                ToolCall(str(item.get("call_id", "")), str(item.get("name", "")), arguments)
            )
    return found


def _text(items: Sequence[Item]) -> str | None:
    """The text of a response's messages, a paragraph each; a refusal counts as text."""
    texts: list[str] = []
    for item in items:
        content = item.get("content") if item.get("type") == MESSAGE else None
        written: list[str] = []
        for raw in cast(list[object], content) if isinstance(content, list) else []:
            part = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            field = _WRITTEN.get(str(part.get("type")))
            said = part.get(field) if field is not None else None
            if isinstance(said, str):
                written.append(said)
        if "".join(written):
            texts.append("".join(written))
    return "\n\n".join(texts) if texts else None


def _tokens(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, not {type(value).__name__}")
    return cast(dict[str, Any], value)
