# SPDX-License-Identifier: MIT
"""GPT through the LiteLLM proxy's ``/v1/responses`` (docs/specs/context-management.md,
CM-40): OpenAI's Responses API behind ``ChatModel``, for the aliases the deployment names
for it (``--api ALIAS=responses``). Every other alias keeps chat completions (CM-41).

The conversation stays in the chat format the loop keeps, and each request renders it
as the API's input items:

- the first system message is the request's ``instructions``; the other messages become
  messages, function calls and their outputs;
- an answer that came back through this API (an ``AssistantMessage`` whose native
  answer is a response), on this client's alias and from the model the alias answers
  as now, is sent back as the output items the gateway returned, unchanged: reasoning
  with its ``encrypted_content``, messages with their ``phase``, and the item IDs as the
  gateway rewrote them, which name its deployment. The model the alias answers as now is
  the one its last answer through this client named, or before there is one, the one
  the conversation's newest answer on the alias names: OpenAI carries reasoning only
  within a model family. A reasoning item goes with the item after it, the call or
  message it led to, so one that led to nothing, as at the end of a response cut short,
  is left out;
- any other answer, from another alias or model, another adapter, or no call at all
  (RS-10's earlier conversation), or whose text or calls are no longer its response's,
  is sent as its text and calls, without reasoning (CM-42);
- calls and outputs are then paired (``pairing``, CM-29a).

Each request sets ``store: false``, asks for ``reasoning.encrypted_content`` explicitly,
and never sets ``truncation``, which would drop the oldest items silently. Functions are
declared with ``strict: false``, as chat completions takes them. A response that failed
is an error, a context limit if it says so, and tried again when it says the upstream is
overloaded; one cut short says ``length``. Usage is OpenAI's, whose ``input_tokens``
already counts the cached tokens (CM-28). LiteLLM prices no call on this route (lab 43,
B2): its cost comes from the gateway's model information (``pricing``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from .model import (
    AssistantMessage,
    ContextLimitError,
    GatewayModel,
    Message,
    ModelError,
    ModelTurn,
    OverloadedError,
    ToolCall,
    ToolSpec,
    overloaded,
    reported_cost,
)
from .pairing import CALL, OUTPUT, Item, paired
from .pricing import Tokens, responses_tokens

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
    PREFIX = ("instructions", "tools", "input")
    resolved: str | None = None
    """The model this client's alias last answered as."""

    def _tokens(self, usage: Mapping[str, object]) -> Tokens | None:
        return responses_tokens(usage)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        model = self.resolved or newest(messages, self.model)
        instructions, items = rendered(messages, alias=self.model, model=model)
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
            body["tool_choice"] = self._tool_choice
        turn = await self._call(body, parse_response)
        if turn.model is not None:
            self.resolved = turn.model
        return turn


def newest(messages: Sequence[Message], alias: str) -> str | None:
    """The model of the conversation's newest answer through this API on ``alias``."""
    for message in reversed(messages):
        if (
            isinstance(message, AssistantMessage)
            and message.native_format == RESPONSES
            and message.alias == alias
        ):
            return message.model
    return None


def rendered(
    messages: Sequence[Message], *, alias: str, model: str | None
) -> tuple[str | None, list[Item]]:
    """A chat conversation as a request's ``instructions`` and input items, sending back
    the native items of the answers made on ``alias`` by ``model``."""
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
            items.extend(_answer(message, alias, model))
        elif role == "tool":
            call = str(message.get("tool_call_id") or "")
            items.append({"type": OUTPUT, "call_id": call, "output": text})
        else:
            raise ModelError(f"a message whose role is {role!r} cannot be sent")
    return instructions, paired(items)


def _answer(message: Message, alias: str, model: str | None) -> list[Item]:
    content = message.get("content")
    text = content if isinstance(content, str) and content else None
    calls = _chat_calls(message)
    sent = _sent(message, alias, model)
    if sent is not None and _agrees(sent, text, calls):
        return _kept(sent)
    plain: list[Item] = []
    if text is not None:
        plain.append({"type": MESSAGE, "role": "assistant", "content": text})
    plain.extend(
        {"type": CALL, "call_id": call.id, "name": call.name, "arguments": call.arguments}
        for call in calls
    )
    return plain


def _sent(message: Message, alias: str, model: str | None) -> tuple[Item, ...] | None:
    """The output items of the response behind an answer, if it has one and was made on
    ``alias`` by ``model``; read once, for all the requests that send them."""
    if not (
        isinstance(message, AssistantMessage)
        and message.native_format == RESPONSES
        and message.alias == alias
        and message.model == model
    ):
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
    """Whether an answer's text and calls are still its response's: one changed since is
    no longer the response."""
    return _calls(sent) == list(calls) and text == _text(sent)


def _kept(sent: Sequence[Item]) -> list[Item]:
    """A response's items, each reasoning item with the item after it: one that led to
    nothing is left out."""
    kept: list[Item] = []
    follows = False
    for item in reversed(sent):
        keep = follows if item.get("type") == REASONING else True
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
    except (ValueError, TypeError, RecursionError) as exc:
        raise ModelError(f"unexpected response: {text[:300]}") from exc
    status = data.get("status")
    if status == "failed":  # with or without its output
        error = data.get("error")
        fields = cast(dict[str, Any], error) if isinstance(error, dict) else {}
        said = f"the model's response failed: {str(fields.get('message', ''))[:300]}"
        if fields.get("code") == "context_length_exceeded":
            raise ContextLimitError(said)
        if overloaded(fields):  # tried again, as a 529 is (lab 43, B3)
            raise OverloadedError(said)
        raise ModelError(said)
    try:
        output = data["output"]
        if not isinstance(output, list):
            raise TypeError("output is not a list")
        items = [_object(item) for item in cast(list[object], output)]
        calls = _calls(items)
    except (KeyError, TypeError) as exc:
        raise ModelError(f"unexpected response: {text[:300]}") from exc
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
