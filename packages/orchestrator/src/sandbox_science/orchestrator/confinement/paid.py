# SPDX-License-Identifier: MIT
"""Runtime paid dispatch: durable host intent precedes every gateway request."""

from collections.abc import Sequence
from dataclasses import replace

from sandbox_science.orchestrator.history import capture
from sandbox_science.orchestrator.model import (
    ChatModel,
    GatewayBudgetError,
    GatewayModel,
    Message,
    ModelTurn,
    ToolSpec,
)
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole
from sandbox_science.orchestrator.projects import ModelUsage

from .transport import Capability, Channel, TransportError
from .wire import ModelIntent, ModelKind, ModelObserved, ModelTicket, payload, typed


async def begin(
    channel: Channel,
    attempt: str,
    alias: str,
    kind: ModelKind,
    profile: ModelProfile | None = None,
    role: ModelRole | None = None,
    captured: capture.CapturedRequest | None = None,
) -> str:
    try:
        ticket = typed(
            ModelTicket,
            await channel.request(
                Capability.MODEL_INTENT,
                payload(
                    ModelIntent(
                        attempt=attempt,
                        alias=alias,
                        kind=kind,
                        profile=profile,
                        role=role,
                        captured=captured,
                    )
                ),
            ),
        )
    except TransportError as exc:
        if exc.kind == "denied" and str(exc).startswith("model_budget:"):
            raise GatewayBudgetError(str(exc)) from None
        raise
    return ticket.identity


async def observe(channel: Channel, attempt: str, identity: str, usage: ModelUsage) -> None:
    await channel.request(
        Capability.MODEL_OBSERVED,
        payload(ModelObserved(attempt=attempt, identity=identity, usage=usage)),
    )


def usage(turn: ModelTurn) -> ModelUsage:
    return ModelUsage(
        model=turn.model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_input_tokens=turn.cache_read_input_tokens,
        cache_creation_input_tokens=turn.cache_creation_input_tokens,
        cost=turn.cost,
        cost_source=turn.cost_source,
        gateway_version=turn.gateway_version,
        thinking_replay=(turn.thinking_replay or "")[:300] or None,
        retried=list(turn.retried[:16]),
    )


class PaidModel:
    """Main/child observations settle atomically with the canonical assistant append."""

    def __init__(
        self, channel: Channel, model: ChatModel, attempt: str, alias: str, kind: ModelKind
    ) -> None:
        self.channel, self.model = channel, model
        self.attempt, self.alias = attempt, alias
        self.kind: ModelKind = kind

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        if isinstance(self.model, GatewayModel):
            self.model.validate_profile()
        identity = await begin(
            self.channel,
            self.attempt,
            self.alias,
            self.kind,
            getattr(self.model, "requested_profile", None),
            "delegated" if self.kind == "delegated" else "main",
            capture.request(messages, tools),
        )
        turn = await self.model.complete(messages, tools)
        return replace(turn, intent_id=identity)
