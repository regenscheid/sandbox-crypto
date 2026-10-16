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
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import aiohttp

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock

_log = logging.getLogger(__name__)

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
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    """What the call cost, as the gateway reported it (``COST_HEADER``, or the answer's
    ``usage.cost``), in its currency: LiteLLM's is USD. ``None`` when it reported none:
    it is never estimated."""


COST_HEADER: Final = "x-litellm-response-cost"
"""The header in which LiteLLM's proxy reports a call's cost."""


class ChatModel(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelTurn: ...


class ModelError(Exception):
    """The model gateway failed or answered with something unusable."""

    def __init__(
        self, message: str, *, status: int | None = None, cost: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        """The gateway's HTTP status, when it answered with an error."""
        self.cost = cost
        """What the gateway reported the call cost, when it answered 2xx with something
        unusable: a call it charged for, which its caller may count."""


class ContextLimitError(ModelError):
    """The conversation is longer than the model's context window."""


class GatewayBudgetError(ModelError):
    """The gateway refused the call because a spending budget is exhausted: its key's,
    user's or team's, which LiteLLM keeps (run-service spec RS-6d). Not tried again: a
    later attempt fails the same way until the budget is raised or resets."""


GATEWAY_BUDGET: Final = "gateway_budget"
"""How a run stops when the gateway refuses a call for its budget."""


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""The largest answer read from the gateway, by default."""
RETRIED: Final = frozenset({429, 502, 503, 504})
"""Gateway statuses that a later attempt can get past: rate limits and an unavailable
or slow upstream."""
MAX_RETRY_WAIT: Final = 60.0
"""The longest wait before a retry, whatever the gateway's ``Retry-After`` asks."""


class OpenAICompatibleModel:
    """Chat completions with function calling, as the LiteLLM proxy serves them.

    ``max_tokens``, when given, limits each answer's length. An answer larger than
    ``max_response_bytes``, a redirect (never followed: it would carry the request to
    another address), and any other status but 2xx are errors. A gateway that refuses
    the call because a spending budget is exhausted raises ``GatewayBudgetError``, never
    tried again, whatever its status. A rate limit (429) or an unavailable upstream (502,
    503, 504) is tried again up to ``retries`` times, after ``Retry-After`` or ``backoff``
    seconds doubling each time, at most a minute. A gateway that says the conversation
    exceeds the model's context raises ``ContextLimitError``. An error's message quotes
    the gateway's answer, with the key replaced if the answer holds it.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 600,
        max_tokens: int | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        retries: int = 3,
        backoff: float = 2.0,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if retries < 0 or backoff < 0:
            raise ValueError("retries and backoff are not negative")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_tokens = max_tokens
        self._max_response_bytes = max_response_bytes
        self._retries, self._backoff, self._clock = retries, backoff, clock

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        body: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
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
        wait = self._backoff
        for attempt in range(self._retries + 1):
            try:
                text, priced = await self._post(body)
                try:
                    return _parse_completion(text, priced)
                except ModelError as error:  # answered, and charged, whatever it holds
                    error.cost = _cost(priced) if priced is not None else None
                    raise
            except _RetryableError as retry:
                # A gateway that asks for a longer wait than the cap is not waited for.
                if attempt == self._retries or (retry.after or 0) > MAX_RETRY_WAIT:
                    raise retry.error from None
                delay = retry.after if retry.after is not None else min(wait, MAX_RETRY_WAIT)
                # The status only: the error, with the gateway's text, is the caller's to
                # report once if the retries end.
                _log.warning(
                    "the model gateway answered HTTP %s; trying again in %.1f s",
                    retry.error.status,
                    delay,
                )
                await self._clock.sleep(delay)
                wait *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post(self, body: dict[str, Any]) -> tuple[str, str | None]:
        """The answer's text, and its cost header if it has one."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                try:
                    data = await read_limited(response, self._max_response_bytes)
                except ModelError as error:
                    if 200 <= response.status < 300:  # charged, though too long to read
                        error.cost = _cost(response.headers.get(COST_HEADER))
                    raise
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ModelError("the model gateway's answer is not UTF-8") from exc
                if self._api_key:  # An answer, or an error about it, can quote the key.
                    text = text.replace(self._api_key, "[the gateway key]")
                if 200 <= response.status < 300:
                    return text, response.headers.get(COST_HEADER)
                status = response.status
                said = gateway_said(text, response.content_type)
                error = ModelError(f"the model gateway returned {status}: {said}", status=status)
                # Before the retries: LiteLLM sends some budget refusals as 429s.
                if 400 <= status < 500 and _budget_exceeded(text):
                    raise GatewayBudgetError(str(error), status=status)
                if status in RETRIED:
                    raise _RetryableError(error, _retry_after(response.headers.get("Retry-After")))
                if status in (400, 413) and _context_exceeded(text):
                    raise ContextLimitError(str(error), status=status)
                raise error
        except aiohttp.ClientError as exc:
            # An exception about a response holds the request, with its Authorization
            # header; only one without it is chained.
            cause = None if hasattr(exc, "request_info") else exc
            raise ModelError(
                f"cannot reach the model gateway: {type(exc).__name__}: {exc}"
            ) from cause


class ModelGateway:
    """The deployment's model gateway, for runs whose alias is chosen as they start, as
    a delegated run's is (plans-and-delegation spec, PD-28, PD-30): a model for an
    alias, and the aliases the gateway lists. The models it makes share its session,
    which ``close`` closes."""

    LISTED_BYTES: Final = 1024 * 1024
    """The largest list of models read from the gateway."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _shared(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def model(self, alias: str) -> OpenAICompatibleModel:
        """A model that calls ``alias``, through the gateway's session."""
        return OpenAICompatibleModel(self.base_url, alias, self._api_key, session=self._shared())

    async def aliases(self) -> frozenset[str] | None:
        """The model aliases the gateway lists (``GET /models``, as the LiteLLM proxy
        serves it); ``None`` when it cannot be asked or its answer is not a list, so
        that a gateway that lists nothing is not taken to serve nothing."""
        try:
            async with self._shared().get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                data = await read_limited(response, self.LISTED_BYTES)
                if not 200 <= response.status < 300:
                    _log.warning("the model gateway lists no models: HTTP %s", response.status)
                    return None
        except (aiohttp.ClientError, TimeoutError, ModelError) as exc:
            _log.warning("the model gateway lists no models: %s", type(exc).__name__)
            return None
        try:
            found = _object(json.loads(data)).get("data")
        except (ValueError, TypeError):
            return None
        if not isinstance(found, list):
            return None
        return frozenset(
            cast(str, item["id"])
            for item in cast(list[object], found)
            if isinstance(item, dict) and isinstance(cast(dict[str, object], item).get("id"), str)
        )

    async def tool_calling(self) -> dict[str, bool] | None:
        """Whether each alias the gateway describes can call tools (``GET /model/info``,
        as the LiteLLM proxy serves it, its ``supports_function_calling``): an alias it
        does not describe, or whose answer is not a yes or a no, is left out; ``None``
        when it cannot be asked (PD-30)."""
        try:
            async with self._shared().get(
                f"{self.base_url}/model/info",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                data = await read_limited(response, self.LISTED_BYTES)
                if not 200 <= response.status < 300:
                    _log.warning("the model gateway describes no models: HTTP %s", response.status)
                    return None
        except (aiohttp.ClientError, TimeoutError, ModelError) as exc:
            _log.warning("the model gateway describes no models: %s", type(exc).__name__)
            return None
        try:
            found = _object(json.loads(data)).get("data")
        except (ValueError, TypeError):
            return None
        if not isinstance(found, list):
            return None
        capable: dict[str, bool] = {}
        for item in cast(list[object], found):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            name, info = entry.get("model_name"), entry.get("model_info")
            if not isinstance(name, str) or not isinstance(info, dict):
                continue
            calls = cast(dict[str, object], info).get("supports_function_calling")
            if isinstance(calls, bool):
                # An alias several deployments serve can call tools when any of them can.
                capable[name] = capable.get(name, False) or calls
        return capable

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class _RetryableError(Exception):
    def __init__(self, error: ModelError, after: float | None) -> None:
        self.error, self.after = error, after


def _retry_after(value: str | None) -> float | None:
    """``Retry-After`` in seconds; an HTTP date or anything else is ignored."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 <= seconds < float("inf") else None


BUDGET_MARKS: Final = (
    "budget has been exceeded",
    "budgetexceedederror",
    "exceededbudget",
    "exceededtokenbudget",
    "exceeded budget",
)
"""How LiteLLM's errors name a spent budget, in lower case: ``BudgetExceededError``'s
"Budget has been exceeded! Current cost: ..., Max budget: ...", and the proxy's checks
of a key's, user's, team's or end user's budget ("ExceededBudget: User=... over
budget", "ExceededTokenBudget: ...", "... exceeded budget for model=...")."""


def _error_fields(text: str) -> dict[str, object]:
    """The error object of a gateway's answer, or nothing."""
    try:
        error = _object(json.loads(text)).get("error")
    except (ValueError, RecursionError, TypeError):
        return {}
    return cast(dict[str, object], error) if isinstance(error, dict) else {}


def _budget_exceeded(text: str) -> bool:
    """Whether a gateway's error says a spending budget is exhausted.

    The error object's ``type`` is LiteLLM's ``budget_exceeded``, or its message names a
    spent budget as LiteLLM's do (``BUDGET_MARKS``). Its status varies with LiteLLM's
    version (400, 401 or 429), so it is not consulted. Text elsewhere in the answer,
    which can quote the request, does not count."""
    fields = _error_fields(text)
    message = fields.get("message")
    said = message.lower() if isinstance(message, str) else ""
    return fields.get("type") == "budget_exceeded" or any(mark in said for mark in BUDGET_MARKS)


def _context_exceeded(text: str) -> bool:
    """Whether a gateway's error says the input is longer than the model's context.

    The error object's ``code`` is ``context_length_exceeded`` as OpenAI-compatible
    providers send it, or its message names LiteLLM's ``ContextWindowExceededError``.
    Text elsewhere in the answer, which can quote the request, does not count."""
    fields = _error_fields(text)
    message = fields.get("message")
    return fields.get("code") == "context_length_exceeded" or (
        isinstance(message, str) and message.startswith("litellm.ContextWindowExceededError")
    )


def gateway_said(text: str, content_type: str | None = None) -> str:
    """What a gateway's answer says, for an error's message: JSON or plain text cut to 300
    characters; a page of another kind, such as a proxy's HTML error page, only by its
    type and size (lab 42, B7)."""
    kind = (content_type or "").partition(";")[0].strip().lower()
    head = text.lstrip()[:1]
    if kind == "application/json" or kind.endswith("+json") or head in ("{", "["):
        return text[:300]
    if kind in ("", "text/plain") and "<" not in text[:300]:
        return text[:300]
    return f"a {kind or 'non-JSON'} page of {len(text):,} characters"


async def read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """The answer's body, read to its end, or a ModelError once it is larger than
    ``limit`` bytes: a single read returns only what has arrived (lab 42, B1)."""
    if response.content_length is not None and response.content_length > limit:
        raise ModelError(f"the model gateway's answer is larger than {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise ModelError(f"the model gateway's answer is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _cost(value: object) -> float | None:
    """A reported cost: a finite number, not negative; anything else is none."""
    if isinstance(value, bool):
        return None
    try:
        found = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return found if 0 <= found < float("inf") else None


def _parse_completion(text: str, priced: str | None = None) -> ModelTurn:
    """A completion as a turn; anything not shaped like one is a ModelError. ``priced``
    is the cost header, which the answer's ``usage.cost`` stands in for when it is
    missing."""
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
        raise ModelError(f"unexpected completion: {gateway_said(text)}") from exc
    content, finish = message.get("content"), choice.get("finish_reason")
    resolved = cast(dict[str, Any], data).get("model")
    return ModelTurn(
        text=content if isinstance(content, str) else None,
        tool_calls=tuple(calls),
        finish_reason=finish if isinstance(finish, str) else None,
        model=resolved if isinstance(resolved, str) else None,
        input_tokens=_usage(data, "prompt_tokens"),
        output_tokens=_usage(data, "completion_tokens"),
        cost=_cost(priced) if priced is not None else _cost(_usage_value(data, "cost")),
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


def _usage(data: object, key: str) -> int | None:
    value = _usage_value(data, key)
    return value if type(value) is int and value >= 0 else None


def _usage_value(data: object, key: str) -> object:
    usage = _object(data).get("usage")
    return cast(dict[str, object], usage).get(key) if isinstance(usage, dict) else None
