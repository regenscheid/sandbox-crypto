# SPDX-License-Identifier: MIT
"""Model access: a provider-neutral interface and its adapters (D61).

The orchestrator reaches models only through the LiteLLM proxy. Its chat-completions
endpoint is the default adapter (``OpenAICompatibleModel``); its ``/v1/responses``
endpoint is GPT's (``responses.ResponsesModel``, docs/specs/context-management.md,
CM-40), chosen for each alias by a deployment setting. Messages use the OpenAI chat
format internally; an adapter for another API renders them. ``ScriptedModel`` replays
fixed turns, for tests and for exercising the worker boundary without a model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Final, Protocol, cast

import aiohttp

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock

from .pricing import GATEWAY, MODEL_INFO, CostSource, PriceList, Tokens, chat_tokens
from .pricing import cost as priced_cost
from .tokens import total_input

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
    """What the call cost, in the gateway's currency (LiteLLM's is USD): as the gateway
    reported it (``COST_HEADER``, or the answer's ``usage.cost``), or else priced from
    its model information (``pricing``); ``None`` when neither gives it. It is never
    estimated, and never 0 for a call that used tokens and was not priced."""
    cost_source: CostSource | None = None
    """Where ``cost`` came from: ``gateway`` (reported) or ``model_info`` (priced from
    the gateway's model information)."""
    native: bytes | None = None
    """The gateway's answer as it sent it, which the session's archive keeps
    (docs/specs/context-management.md, CM-5); ``native_format`` says what it is."""
    native_format: str | None = None
    gateway_version: str | None = None
    """The gateway's version, as ``VERSION_HEADER`` named it (CM-39)."""
    alias: str | None = None
    """The alias the call was made on, as the deployment names it."""
    prefix: str | None = None
    """The digest of what the model was given, as its adapter rendered the request
    (``prefix_digest``)."""
    adapter_state: Mapping[str, Any] | None = None
    """What the adapter that made the answer keeps of the call for itself, as JSON values
    (such as the tools it declared): carried by the answer and archived with it, so a
    rebuild has it again. Only the adapter whose ``FORMAT`` is the answer's
    ``native_format`` reads it."""
    thinking_replay: str | None = None
    """Whether the call sent Claude's thinking back: ``on``, or ``off:`` and why (CM-39);
    ``None`` from an adapter without it."""
    notes: tuple[str, ...] = ()
    """Claude's progress notes, in order (CM-38a): the agent's account of its work
    between calls, which ``text``, the answer, does not hold."""
    content: str | None = None
    """The answer as the conversation holds it, when that is not ``text``: Claude's
    text with its progress notes in place, which the archive indexes and other adapters
    send (``AssistantMessage.of``)."""


COST_HEADER: Final = "x-litellm-response-cost"
"""The header in which LiteLLM's proxy reports a call's cost."""
VERSION_HEADER: Final = "x-litellm-version"
"""The header in which LiteLLM's proxy names its version."""
CHAT_COMPLETIONS: Final = "chat_completions"
"""The native format of this client's answers: a chat completion, as the gateway sent it."""


class ChatModel(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelTurn: ...


class AssistantMessage(dict[str, Any]):
    """The model's answer as the conversation holds it: a chat message, whose keys are
    all that any adapter sends, with what it came from beside them: the gateway's answer
    as it sent it (``native``, in ``native_format``), the alias it was asked on and the
    model that answered, the gateway's version, and ``prefix``, the digest of what the
    model was given. The adapter that made it may send its native answer back as it was
    (CM-40) while the alias and the model are its own; any other adapter sees a plain
    chat message (CM-42). ``dict()`` of it, or its JSON, is a plain message too;
    ``copy.copy`` and ``copy.deepcopy`` keep what it carries.

    ``of`` makes one from a turn, as the loop adds it; the archive makes one from what it
    kept (``history.Archived.chat``). ``adapter_state`` is the turn's, which only the
    adapter that made the answer reads. ``read`` is an adapter's reading of ``native``,
    kept so that it reads it once."""

    __slots__ = (
        "adapter_state",
        "alias",
        "gateway_version",
        "model",
        "native",
        "native_format",
        "prefix",
        "read",
    )

    def __init__(
        self,
        content: str | None,
        tool_calls: Sequence[Message] = (),
        *,
        native: bytes | None = None,
        native_format: str | None = None,
        alias: str | None = None,
        model: str | None = None,
        gateway_version: str | None = None,
        prefix: str | None = None,
        adapter_state: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(role="assistant", content=content)
        if tool_calls:
            self["tool_calls"] = [dict(call) for call in tool_calls]
        self.native, self.native_format = native, native_format
        self.alias, self.model, self.gateway_version = alias, model, gateway_version
        self.prefix, self.adapter_state = prefix, adapter_state
        self.read: object = None

    @classmethod
    def of(cls, reply: ModelTurn) -> AssistantMessage:
        """A turn's answer, its calls in the chat format."""
        calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in reply.tool_calls
        ]
        return cls(
            reply.text if reply.content is None else reply.content,
            calls,
            native=reply.native,
            native_format=reply.native_format,
            alias=reply.alias,
            model=reply.model,
            gateway_version=reply.gateway_version,
            prefix=reply.prefix,
            adapter_state=reply.adapter_state,
        )


def prefix_digest(parts: Mapping[str, object]) -> str:
    """The SHA-256, in hexadecimal, of a request's parts that hold what the model was
    given, as JSON with sorted keys: the same for the same content, however its objects
    were built. Claude's binding of thinking to what came before it compares these."""
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


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


class OverloadedError(ModelError):
    """The model's answer says its upstream is overloaded, as a failed response does: a
    later attempt can get past it, so it is tried again as a 529 is (lab 43, B3)."""


class GatewayBudgetError(ModelError):
    """The gateway refused the call because a spending budget is exhausted: its key's,
    user's or team's, which LiteLLM keeps (run-service spec RS-6d). Not tried again: a
    later attempt fails the same way until the budget is raised or resets."""


GATEWAY_BUDGET: Final = "gateway_budget"
"""How a run stops when the gateway refuses a call for its budget."""


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""The largest answer read from the gateway, by default."""
RETRIED: Final = frozenset({429, 502, 503, 504, 529})
"""Gateway statuses that a later attempt can get past: rate limits and an unavailable,
slow or overloaded upstream (Anthropic's 529). Another 5xx is tried again when its
error says the upstream is overloaded (``overloaded``)."""
OVERLOADED: Final = frozenset({"overloaded", "overloaded_error", "server_error"})
"""The error types and codes that say the upstream is overloaded or failed for a moment:
Anthropic's ``overloaded_error``, and OpenAI's ``server_error`` with which a response
fails ("Our servers are currently overloaded", lab 43, B3)."""
MAX_RETRY_WAIT: Final = 60.0
"""The longest wait before a retry, whatever the gateway's ``Retry-After`` asks."""


class GatewayModel(ABC):
    """A model the LiteLLM proxy serves, through one of its endpoints (``PATH``): what
    its adapters share.

    ``max_tokens``, when given, limits each answer's length. An answer larger than
    ``max_response_bytes``, a redirect (never followed: it would carry the request to
    another address), and any other status but 2xx are errors. A gateway that refuses
    the call because a spending budget is exhausted raises ``GatewayBudgetError``, never
    tried again, whatever its status. A rate limit (429) or an unavailable upstream (502,
    503, 504) is tried again up to ``retries`` times, after ``Retry-After`` or ``backoff``
    seconds doubling each time, at most a minute, and so is another 5xx, or an answer
    whose response failed (``OverloadedError``), that says the upstream is overloaded. A
    gateway that says the conversation exceeds the model's context raises
    ``ContextLimitError``. An error's message quotes
    the gateway's answer, with the key replaced if the answer holds it. Each turn keeps
    the answer as the gateway sent it (``native``, in ``FORMAT``), the gateway's version,
    the alias, and the digest of the request's ``PREFIX`` fields; its cost is
    ``COST_HEADER``'s, or the answer's own without the header, or else the gateway's
    model information's price of its tokens (``pricing``, from ``prices``, which models
    made by one gateway share). An adapter may add headers to a request (``_headers``),
    but not the key's.
    """

    PATH: ClassVar[str]
    """The endpoint, below the gateway's address."""
    FORMAT: ClassVar[str]
    """The native format of its answers, as the archive names it."""
    PREFIX: ClassVar[tuple[str, ...]]
    """The request's fields that hold what the model is given, which ``prefix_digest``
    covers: its instructions, tools and conversation."""

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
        prices: PriceList | None = None,
    ) -> None:
        if retries < 0 or backoff < 0:
            raise ValueError("retries and backoff are not negative")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_tokens = max_tokens
        self._max_response_bytes = max_response_bytes
        self._retries, self._backoff, self._clock = retries, backoff, clock
        self._prices = prices or PriceList(self.base_url, api_key, clock=clock)

    @property
    def max_output(self) -> int | None:
        """The most tokens an answer may take, as the calls ask for, if they ask."""
        return self._max_tokens

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @abstractmethod
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelTurn: ...

    def _headers(self, body: Mapping[str, Any]) -> dict[str, str]:
        """The headers this adapter adds to a request, such as a beta flag."""
        return {}

    def _tokens(self, usage: Mapping[str, object]) -> Tokens | None:
        """An answer's tokens as they are priced, from its usage object."""
        return chat_tokens(usage)

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _priced(self, turn: ModelTurn, text: str) -> ModelTurn:
        """``turn`` with its cost: the gateway's, unless it reported none, or 0 for a call
        that used tokens; then the gateway's model information's price of its tokens, or
        none (lab 43, B2)."""
        used = bool(turn.input_tokens or turn.output_tokens)
        if turn.cost is not None and (turn.cost > 0 or not used):
            return replace(turn, cost_source=GATEWAY)
        try:
            usage = _usage_object(json.loads(text))
        except (ValueError, TypeError, RecursionError):
            usage = {}
        tokens = self._tokens(usage)
        prices = await self._prices.prices(self.model, self._client) if tokens else None
        found = priced_cost(prices, tokens) if prices is not None and tokens else None
        if found is None:
            return replace(turn, cost=None, cost_source=None)
        return replace(turn, cost=found, cost_source=MODEL_INFO)

    async def _call(
        self, body: dict[str, Any], parse: Callable[[str, str | None], ModelTurn]
    ) -> ModelTurn:
        """Send ``body``, trying again as the class says; ``parse`` reads the answer's
        text, with its cost header if it has one."""
        prefix = prefix_digest({name: body[name] for name in self.PREFIX if name in body})
        wait = self._backoff
        for attempt in range(self._retries + 1):
            try:
                text, priced, version = await self._post(body)
                try:
                    turn = parse(text, priced)
                except ModelError as error:  # answered, and charged, whatever it holds
                    error.cost = reported_cost(priced) if priced is not None else None
                    if isinstance(error, OverloadedError):
                        raise _RetryableError(error, None) from None
                    raise
                return replace(
                    await self._priced(turn, text),
                    native=text.encode(),
                    native_format=self.FORMAT,
                    gateway_version=version,
                    alias=self.model,
                    prefix=prefix,
                )
            except _RetryableError as retry:
                # A gateway that asks for a longer wait than the cap is not waited for.
                if attempt == self._retries or (retry.after or 0) > MAX_RETRY_WAIT:
                    raise retry.error from None
                delay = retry.after if retry.after is not None else min(wait, MAX_RETRY_WAIT)
                # The status only: the error, with the gateway's text, is the caller's to
                # report once if the retries end.
                _log.warning(
                    "the model gateway answered %s; trying again in %.1f s",
                    "a failed response"
                    if retry.error.status is None
                    else f"HTTP {retry.error.status}",
                    delay,
                )
                await self._clock.sleep(delay)
                wait *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post(self, body: dict[str, Any]) -> tuple[str, str | None, str | None]:
        """The answer's text, and its cost and version headers if it has them."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        added = {k: v for k, v in self._headers(body).items() if k.lower() != "authorization"}
        try:
            async with self._session.post(
                f"{self.base_url}{self.PATH}",
                json=body,
                headers={**added, "Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                try:
                    data = await read_limited(response, self._max_response_bytes)
                except ModelError as error:
                    if 200 <= response.status < 300:  # charged, though too long to read
                        error.cost = reported_cost(response.headers.get(COST_HEADER))
                    raise
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ModelError("the model gateway's answer is not UTF-8") from exc
                if self._api_key:  # An answer, or an error about it, can quote the key.
                    text = text.replace(self._api_key, "[the gateway key]")
                if 200 <= response.status < 300:
                    version = response.headers.get(VERSION_HEADER, "").strip()[:128] or None
                    return text, response.headers.get(COST_HEADER), version
                status = response.status
                said = gateway_said(text, response.content_type)
                error = ModelError(f"the model gateway returned {status}: {said}", status=status)
                # Before the retries: LiteLLM sends some budget refusals as 429s.
                if 400 <= status < 500 and _budget_exceeded(text):
                    raise GatewayBudgetError(str(error), status=status)
                if status in RETRIED or (status >= 500 and overloaded(_error_fields(text))):
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


class OpenAICompatibleModel(GatewayModel):
    """Chat completions with function calling, as the LiteLLM proxy serves them: the
    default adapter (CM-41)."""

    PATH = "/chat/completions"
    FORMAT = CHAT_COMPLETIONS
    PREFIX = ("messages", "tools")

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
        return await self._call(body, _parse_completion)


API_KINDS: Final = ("chat", "responses", "messages")
"""The adapters an alias may be called through, as the deployment names them (CM-41):
``chat`` completions, every alias's unless the deployment says otherwise, GPT's
``responses`` (CM-40) and Claude's ``messages`` (CM-38)."""


def gateway_model(
    base_url: str,
    alias: str,
    api_key: str,
    *,
    api: str = "chat",
    options: Mapping[str, Any] | None = None,
    session: aiohttp.ClientSession | None = None,
    max_tokens: int | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    prices: PriceList | None = None,
) -> GatewayModel:
    """The client for ``alias``, through the adapter ``api`` names (``API_KINDS``), with
    that adapter's own ``options``, pricing from ``prices`` the calls the gateway does not
    price. The one place an adapter is chosen."""
    kind: type[GatewayModel]
    if api == "chat":
        kind = OpenAICompatibleModel
    elif api == "responses":
        from .responses import ResponsesModel  # which builds on this module

        kind = ResponsesModel
    elif api == "messages":
        from .messages import MessagesModel  # which builds on this module

        kind = MessagesModel
    else:
        raise ValueError(f"no adapter {api!r}: one of {', '.join(API_KINDS)}")
    return kind(
        base_url,
        alias,
        api_key,
        session=session,
        max_tokens=max_tokens,
        max_response_bytes=max_response_bytes,
        prices=prices,
        **dict(options or {}),
    )


@dataclass(frozen=True)
class Limits:
    """A model's limits as the gateway describes them: ``input``, its
    ``max_input_tokens``; ``output``, its ``max_output_tokens``; and its provider."""

    input: int
    output: int | None
    provider: str | None
    model: str | None = None
    """The model the gateway calls for it (``litellm_params.model``), which names its
    family, as ``bedrock/anthropic.claude-...`` does."""


class ModelGateway:
    """The deployment's model gateway, for runs whose alias is chosen as they start, as
    a delegated run's is (plans-and-delegation spec, PD-28, PD-30): a model for an
    alias, and the aliases the gateway lists. The models it makes share its session,
    which ``close`` closes."""

    LISTED_BYTES: Final = 1024 * 1024
    """The largest list of models read from the gateway."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30,
        apis: Mapping[str, str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self.apis = dict(apis or {})
        """The adapter of each alias the deployment names one for (``API_KINDS``); any
        other alias's is ``chat``."""
        self.options = dict(options or {})
        """Each adapter's own options, by its kind, such as Claude's thinking replay."""
        self.prices = PriceList(self.base_url, api_key, timeout=timeout)
        """The gateway's prices, which the models it makes share."""

    def _shared(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def model(self, alias: str) -> GatewayModel:
        """A model that calls ``alias``, through its adapter and the gateway's session."""
        api = self.apis.get(alias, "chat")
        return gateway_model(
            self.base_url,
            alias,
            self._api_key,
            api=api,
            options=self.options.get(api),
            session=self._shared(),
            prices=self.prices,
        )

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
        described = await self._described()
        if described is None:
            return None
        capable: dict[str, bool] = {}
        for name, info in described:
            calls = info.get("supports_function_calling")
            if isinstance(calls, bool):
                # An alias several deployments serve can call tools when any of them can.
                capable[name] = capable.get(name, False) or calls
        return capable

    async def input_limits(self) -> dict[str, Limits] | None:
        """Each described alias's limits (docs/specs/context-management.md, CM-27): its
        ``max_input_tokens``, the least of its deployments', its ``max_output_tokens``,
        and its provider, which says whether the input limit is a window that input and
        output share; ``None`` when the gateway cannot be asked."""
        described = await self._described()
        if described is None:
            return None
        limits: dict[str, Limits] = {}
        for name, info in described:
            found, output = info.get("max_input_tokens"), info.get("max_output_tokens")
            if type(found) is not int or found <= 0:
                continue
            known = limits.get(name)
            if known is None or found < known.input:
                provider, model = info.get("litellm_provider"), info.get("litellm_model")
                limits[name] = Limits(
                    found,
                    output if type(output) is int and output > 0 else None,
                    provider if isinstance(provider, str) else None,
                    model if isinstance(model, str) else None,
                )
        return limits

    async def _described(self) -> list[tuple[str, dict[str, object]]] | None:
        """Each model the gateway describes (``GET /model/info``): its alias and its
        information; ``None`` when it cannot be asked."""
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
        described: list[tuple[str, dict[str, object]]] = []
        for item in cast(list[object], found):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            name, info = entry.get("model_name"), entry.get("model_info")
            if isinstance(name, str) and isinstance(info, dict):
                params = entry.get("litellm_params")
                called = (
                    cast(dict[str, object], params).get("model")
                    if isinstance(params, dict)
                    else None
                )
                described.append((name, {**cast(dict[str, object], info), "litellm_model": called}))
        return described

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


def overloaded(fields: Mapping[str, object]) -> bool:
    """Whether an error object says the upstream is overloaded: its ``type`` or ``code``
    is one of ``OVERLOADED``, or its message says so."""
    message = fields.get("message")
    said = message.lower() if isinstance(message, str) else ""
    named = {value for value in (fields.get("type"), fields.get("code")) if isinstance(value, str)}
    return bool(named & OVERLOADED) or "overloaded" in said


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


CONTEXT_MARKS: Final = ("prompt is too long", "exceed context limit")
"""How Anthropic's errors say the input is too long for the model, in lower case, as
LiteLLM's ``/v1/messages`` relays them, alone or quoted inside its own message: "prompt
is too long: 1050000 tokens > 1000000 maximum", and "input length and `max_tokens`
exceed context limit: ..."."""


def _context_exceeded(text: str) -> bool:
    """Whether a gateway's error says the input is longer than the model's context.

    The error object's ``code`` is ``context_length_exceeded`` as OpenAI-compatible
    providers send it, or its message names LiteLLM's ``ContextWindowExceededError`` or
    says so as Anthropic's do (``CONTEXT_MARKS``). Text elsewhere in the answer, which
    can quote the request, does not count."""
    fields = _error_fields(text)
    message = fields.get("message")
    said = message.lower() if isinstance(message, str) else ""
    return (
        fields.get("code") == "context_length_exceeded"
        or said.startswith("litellm.contextwindowexceedederror")
        or any(mark in said for mark in CONTEXT_MARKS)
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


def reported_cost(value: object) -> float | None:
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
        input_tokens=total_input(_usage_object(data), "chat_completions"),
        output_tokens=_usage(data, "completion_tokens"),
        cost=reported_cost(priced if priced is not None else _usage_value(data, "cost")),
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


def _usage_object(data: object) -> dict[str, object]:
    usage = _object(data).get("usage")
    return cast(dict[str, object], usage) if isinstance(usage, dict) else {}


def _usage(data: object, key: str) -> int | None:
    value = _usage_value(data, key)
    return value if type(value) is int and value >= 0 else None


def _usage_value(data: object, key: str) -> object:
    usage = _object(data).get("usage")
    return cast(dict[str, object], usage).get(key) if isinstance(usage, dict) else None
