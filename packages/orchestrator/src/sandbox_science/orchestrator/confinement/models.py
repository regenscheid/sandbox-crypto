# SPDX-License-Identifier: MIT
"""Host-side model capabilities and VM-side adapters, with no host model fallback."""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from typing import Self, cast

from pydantic import JsonValue, TypeAdapter

from sandbox_science.orchestrator.model import (
    ContextLimitError,
    GatewayBudgetError,
    GatewayModel,
    Limits,
    Message,
    ModelError,
    ModelGateway,
    ModelTurn,
    ToolSpec,
)

from .paid import begin, observe, usage
from .transport import Capability, Channel, TransportError
from .wire import (
    ModelInfo,
    ModelKind,
    ModelReply,
    ModelRequest,
    WireMessage,
    WireTurn,
    payload,
    typed,
)


class CallbackModel(GatewayModel):
    """An authority service's model handle; all autonomous model I/O runs in the VM.

    Copies preserve prompt-cache/tool-choice behavior without creating a host HTTP
    session. An unavailable runtime fails the call, including reviewer and fold calls.
    """

    def __init__(
        self,
        channel: Channel,
        alias: str,
        *,
        scope: Callable[[], str] | None = None,
        kind: ModelKind = "fold",
        max_output: int | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        # Deliberately do not initialize GatewayModel's key, session or PriceList.
        self.channel, self.model = channel, alias
        self.scope, self.kind = scope, kind
        self._max_tokens = max_output
        self._max_response_bytes = max_response_bytes
        self._tool_choice, self._cache_key = "auto", None

    @property
    def cache_identity(self) -> str | None:
        return self._cache_key

    def choosing(self, tool_choice: str) -> Self:
        if tool_choice not in ("auto", "none", "required"):
            raise ValueError("unsupported remote tool choice")
        other = copy.copy(self)
        other._tool_choice = tool_choice
        return other

    def cached_as(self, key: str) -> Self:
        other = copy.copy(self)
        other._cache_key = key
        return other

    async def close(self) -> None:
        """The owning control connection closes the capability, never a host adapter."""

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        if self.scope is None:
            raise ModelError("confined model capability has no attempt scope")
        request = ModelRequest.model_validate(
            {
                "attempt": self.scope(),
                "kind": self.kind,
                "model": self.model,
                "messages": [payload(WireMessage.of(message)) for message in messages],
                "tools": TypeAdapter(tuple[ToolSpec, ...]).dump_python(tuple(tools), mode="json"),
                "cache_key": self._cache_key,
                "tool_choice": self._tool_choice,
                "max_output": self.max_output,
                "max_response_bytes": self._max_response_bytes,
            }
        )
        try:
            reply = typed(
                ModelReply,
                await self.channel.request(Capability.MODEL, payload(request), timeout=1800),
            )
        except TransportError as error:
            raise ModelError("confined model runtime unavailable") from error
        if reply.error is not None:
            kind = {
                "context_limit": ContextLimitError,
                "gateway_budget": GatewayBudgetError,
                "model": ModelError,
            }[reply.error]
            raise kind(reply.message, status=reply.status, cost=reply.cost)
        if reply.turn is None:
            raise ModelError("confined model runtime returned no turn")
        return reply.turn.turn()


class CallbackGateway(ModelGateway):
    """A host catalog handle whose fixed metadata calls also execute in the runtime."""

    def __init__(self, channel: Channel, scope: Callable[[], str] | None = None) -> None:
        self.channel, self.scope = channel, scope

    def model(self, alias: str) -> CallbackModel:
        return CallbackModel(self.channel, alias, scope=self.scope)

    async def aliases(self) -> frozenset[str] | None:
        result = await self._info("aliases")
        return TypeAdapter(frozenset[str] | None).validate_python(result)

    async def tool_calling(self) -> dict[str, bool] | None:
        return TypeAdapter(dict[str, bool] | None).validate_python(await self._info("tool_calling"))

    async def image_input(self) -> dict[str, bool] | None:
        return TypeAdapter(dict[str, bool] | None).validate_python(await self._info("image_input"))

    async def input_limits(self) -> dict[str, Limits] | None:
        return TypeAdapter(dict[str, Limits] | None).validate_python(
            await self._info("input_limits")
        )

    async def _info(self, kind: str) -> JsonValue:
        return await self.channel.request(
            Capability.MODEL_INFO, payload(ModelInfo.model_validate({"kind": kind})), timeout=40
        )

    async def close(self) -> None:
        """The runtime owns catalog/price connections."""


class RuntimeModels:
    """The runtime's model capabilities, restricted to configured deployment aliases."""

    def __init__(self, gateway: ModelGateway, aliases: frozenset[str], channel: Channel) -> None:
        self.gateway, self.aliases, self.channel = gateway, aliases, channel

    async def complete(self, value: JsonValue) -> JsonValue:
        request = typed(ModelRequest, value)
        if request.model not in self.aliases:
            raise TransportError("denied", "model alias is outside this runtime's configuration")
        model = self.gateway.model(request.model).limited(
            max_output=request.max_output, max_response_bytes=request.max_response_bytes
        )
        if request.cache_key is not None:
            model = model.cached_as(request.cache_key)
        model = model.choosing(request.tool_choice)
        identity = await begin(self.channel, request.attempt, request.model, request.kind)
        try:
            turn = await model.complete(
                [message.message() for message in request.messages], request.tools
            )
        except ModelError as error:
            kind = (
                "context_limit"
                if isinstance(error, ContextLimitError)
                else "gateway_budget"
                if isinstance(error, GatewayBudgetError)
                else "model"
            )
            return payload(
                ModelReply.model_validate(
                    {
                        "error": kind,
                        "message": str(error)[:512],
                        "status": error.status,
                        "cost": error.cost,
                    }
                )
            )
        await observe(self.channel, request.attempt, identity, usage(turn))
        return payload(ModelReply(turn=WireTurn.of(turn)))

    async def info(self, value: JsonValue) -> JsonValue:
        request = typed(ModelInfo, value)
        if request.kind == "aliases":
            listed = await self.gateway.aliases()
            return cast(JsonValue, sorted(self.aliases & listed)) if listed is not None else None
        if request.kind == "tool_calling":
            result = await self.gateway.tool_calling()
        elif request.kind == "image_input":
            result = await self.gateway.image_input()
        else:
            limits = await self.gateway.input_limits()
            return cast(
                JsonValue,
                TypeAdapter(dict[str, Limits] | None).dump_python(
                    {key: item for key, item in limits.items() if key in self.aliases}
                    if limits is not None
                    else None,
                    mode="json",
                ),
            )
        return cast(
            JsonValue,
            {key: item for key, item in result.items() if key in self.aliases}
            if result
            else result,
        )
