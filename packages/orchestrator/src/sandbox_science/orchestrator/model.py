# SPDX-License-Identifier: MIT
"""Model access: a provider-neutral interface and its adapters (D61).

The orchestrator reaches models only through the LiteLLM proxy, whose
OpenAI-compatible endpoint is the first adapter. Messages use the OpenAI chat
format internally for now; the Anthropic Messages adapter will translate.
``ScriptedModel`` replays fixed turns, for tests and for exercising the
worker boundary without a model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import aiohttp

Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    """A JSON schema for the arguments."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    """The arguments as the model wrote them: JSON text, not yet validated."""


@dataclass(frozen=True, slots=True)
class ModelTurn:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    model: str | None = None
    """The model that answered, as the gateway names it: the resolved model."""


class ChatModel(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelTurn: ...


class ModelError(Exception):
    """The model gateway failed or answered with something unusable."""


class OpenAICompatibleModel:
    """Chat completions with function calling, as the LiteLLM proxy serves them."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        body: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            body["tool_choice"] = "auto"
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise ModelError(f"the model gateway returned {response.status}: {text[:300]}")
        except aiohttp.ClientError as exc:
            raise ModelError(f"cannot reach the model gateway: {exc}") from exc
        return _parse_completion(text)


def _parse_completion(text: str) -> ModelTurn:
    """A completion as a turn; anything not shaped like one is a ModelError."""
    try:
        data: object = json.loads(text)
        choice = _object(_object(data)["choices"][0])
        message = _object(choice["message"])
        raw_calls: object = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise TypeError("tool_calls is not a list")
        calls: list[ToolCall] = []
        for raw in cast(list[object], raw_calls):
            call = _object(raw)
            function = _object(call.get("function") or {})
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            calls.append(
                ToolCall(str(call.get("id", "")), str(function.get("name", "")), arguments)
            )
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
        raise ModelError(f"unexpected completion: {text[:300]}") from exc
    content, finish = message.get("content"), choice.get("finish_reason")
    resolved = cast(dict[str, Any], data).get("model")
    return ModelTurn(
        text=content if isinstance(content, str) else None,
        tool_calls=tuple(calls),
        finish_reason=finish if isinstance(finish, str) else None,
        model=resolved if isinstance(resolved, str) else None,
    )


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, not {type(value).__name__}")
    return cast(dict[str, Any], value)


class ScriptedModel:
    """Replays a fixed list of turns, then answers that the script has ended."""

    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[list[Message]] = []

    @classmethod
    def from_json(cls, data: object) -> ScriptedModel:
        """Turns as ``[{"text": …, "tool_calls": [{"name": …, "arguments": {…}}]}, …]``."""
        if not isinstance(data, list):
            raise ValueError("a script is a list of turns")
        turns: list[ModelTurn] = []
        number = 0
        for raw_turn in cast(list[object], data):
            if not isinstance(raw_turn, dict):
                raise ValueError("each turn is an object")
            turn = cast(dict[str, Any], raw_turn)
            calls: list[ToolCall] = []
            for raw_call in cast(list[dict[str, Any]], turn.get("tool_calls") or []):
                number += 1
                calls.append(
                    ToolCall(
                        f"call-{number}",
                        str(raw_call["name"]),
                        json.dumps(raw_call.get("arguments", {})),
                    )
                )
            turns.append(
                ModelTurn(text=turn.get("text"), tool_calls=tuple(calls), model="scripted")
            )
        return cls(turns)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.requests.append(list(messages))
        if not self._turns:
            return ModelTurn(text="(the script has ended)", model="scripted")
        return self._turns.pop(0)
