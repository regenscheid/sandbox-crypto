# SPDX-License-Identifier: MIT
"""Skill embeddings execute in the runtime; the host persists their bounded receipts."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import JsonValue

from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.skills.embeddings import (
    BATCH,
    Embedded,
    Embedder,
    EmbeddingError,
    Paid,
    PaidCall,
    Space,
    paid_values,
)

from .paid import begin, observe
from .transport import Capability, Channel, TransportError
from .wire import EmbeddingReply, EmbeddingRequest, payload, typed


class CallbackEmbedder(Embedder):
    def __init__(self, channel: Channel, alias: str, scope: Callable[[], str]) -> None:
        self.channel, self.alias, self.scope = channel, alias, scope

    async def close(self) -> None:
        """The runtime owns the actual embedding client."""

    async def embed(self, texts: Sequence[str], paid: Paid | None = None) -> Embedded:
        vectors: list[list[float]] = []
        space: Space | None = None
        costs: list[float] = []
        for start in range(0, len(texts), BATCH):
            try:
                reply = typed(
                    EmbeddingReply,
                    await self.channel.request(
                        Capability.EMBEDDING,
                        payload(
                            EmbeddingRequest(
                                attempt=self.scope(),
                                alias=self.alias,
                                texts=tuple(texts[start : start + BATCH]),
                            )
                        ),
                        timeout=120,
                    ),
                )
            except TransportError as error:
                raise EmbeddingError("confined embedding runtime unavailable") from error
            if paid is not None:
                if reply.paid_identities:
                    if len(reply.paid_identities) != len(reply.paid):
                        raise EmbeddingError("embedding charge identities do not match receipts")
                    paid.extend(
                        PaidCall(*call, identity)
                        for call, identity in zip(reply.paid, reply.paid_identities, strict=True)
                    )
                else:
                    paid.extend(reply.paid)
            if reply.error is not None:
                raise EmbeddingError(reply.error)
            if reply.model is None or len(reply.vectors) != len(texts[start : start + BATCH]):
                raise EmbeddingError("confined embeddings have an invalid shape")
            observed = Space(reply.model, reply.dimensions)
            if space is not None and space != observed:
                raise EmbeddingError("confined embedding space changed")
            space = observed
            vectors.extend([list(vector) for vector in reply.vectors])
            if reply.cost is not None:
                costs.append(reply.cost)
        if space is None:
            raise EmbeddingError("nothing to embed")
        return Embedded(space, vectors, sum(costs) if costs else None)


class RuntimeEmbeddings:
    def __init__(self, embedder: Embedder | None, channel: Channel) -> None:
        self.embedder, self.channel = embedder, channel

    async def embed(self, value: JsonValue) -> JsonValue:
        request = typed(EmbeddingRequest, value)
        if self.embedder is None or request.alias != self.embedder.alias:
            raise TransportError("denied", "embedding alias is outside runtime configuration")
        identity = await begin(self.channel, request.attempt, request.alias, "embedding")
        paid: Paid = []
        try:
            result = await self.embedder.embed(request.texts, paid)
        except EmbeddingError as error:
            if paid:
                await self._observe(request, identity, paid)
            return payload(
                EmbeddingReply(
                    error=str(error)[:512],
                    paid=tuple(map(paid_values, paid)),
                    paid_identities=(identity,) if paid else (),
                )
            )
        await observe(
            self.channel,
            request.attempt,
            identity,
            ModelUsage(
                model=result.space.model,
                cost=result.cost,
                cost_source="gateway" if result.cost is not None else None,
            ),
        )
        return payload(
            EmbeddingReply(
                model=result.space.model,
                dimensions=result.space.dimensions,
                vectors=tuple(tuple(vector) for vector in result.vectors),
                cost=result.cost,
                paid=tuple(map(paid_values, paid)),
                paid_identities=(identity,),
            )
        )

    async def _observe(self, request: EmbeddingRequest, identity: str, paid: Paid) -> None:
        # One request contains at most one embedder batch/HTTP charge.
        if len(paid) != 1 or isinstance(paid[0], PaidCall):
            raise TransportError("invalid_request", "embedding charge receipt is not one batch")
        model, _, cost = paid[0]
        await observe(
            self.channel,
            request.attempt,
            identity,
            ModelUsage(model=model, cost=cost, cost_source="gateway" if cost is not None else None),
        )
