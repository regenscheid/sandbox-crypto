# SPDX-License-Identifier: MIT
"""The agent-facing service. Run this entrypoint inside its restricted OpenShell VM."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path

import aiohttp
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.control.migrations import VERSION
from sandbox_science.orchestrator.messages import ThinkingReplay
from sandbox_science.orchestrator.model import ModelGateway
from sandbox_science.orchestrator.model_profiles import Effort, ModelRole
from sandbox_science.orchestrator.skills.embeddings import Embedder

from .embeddings import RuntimeEmbeddings
from .endpoints import client_tls, control_endpoint, control_trace
from .models import RuntimeModels
from .runtime_loop import RuntimeLoops
from .scheduling import Scheduling
from .server import private_credential, private_token
from .transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    MAX_FRAME_BYTES,
    Capability,
    Channel,
    TransportError,
)
from .wire import MAX_MODEL_RESPONSE_BYTES, Hello, ModelAlias, Ready, payload, typed


class RuntimeSettings(Contract):
    control_url: str = Field(min_length=1, max_length=2048)
    control_ca_file: Path | None = None
    token_file: Path
    model_url: str = Field(min_length=1, max_length=2048)
    model_key_file: Path
    models: tuple[ModelAlias, ...] = Field(min_length=1, max_length=128)
    reasoning_effort: dict[ModelRole, Effort] = Field(default_factory=dict[ModelRole, Effort])
    apis: dict[str, str] = Field(default_factory=dict[str, str])
    options: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict[str, dict[str, JsonValue]]
    )
    scratch: Path = Path("/sandbox/runtime/scratch")
    image_record: Path = Path("/usr/share/sandbox-science/image.json")
    embedding: ModelAlias | None = None
    thinking_replay: ThinkingReplay | None = None
    generation: Identifier

    @field_validator("control_url")
    @classmethod
    def valid_control_url(cls, value: str) -> str:
        return control_endpoint(value)

    @model_validator(mode="after")
    def ca_requires_tls(self) -> RuntimeSettings:
        if self.control_ca_file is not None and not self.control_url.startswith("https://"):
            raise ValueError("a control CA requires an HTTPS control endpoint")
        return self


async def serve(settings: RuntimeSettings, *, clock: Clock = SYSTEM_CLOCK) -> None:
    """Select queued work and own root/child loop tasks; disconnect never falls back."""
    record = TypeAdapter(dict[str, JsonValue]).validate_json(settings.image_record.read_bytes())
    if record.get("profile") != "orchestrator":
        raise ValueError("runtime requires the orchestrator image's build record")
    revision = record.get("revision")
    if not isinstance(revision, str) or not revision or revision == "unknown":
        raise ValueError("runtime image has no qualified source revision")
    settings.scratch.mkdir(parents=True, exist_ok=True)
    embedder = (
        Embedder(
            settings.model_url,
            settings.embedding,
            private_credential(settings.model_key_file),
            max_response_bytes=MAX_MODEL_RESPONSE_BYTES,
        )
        if settings.embedding
        else None
    )
    gateway = ModelGateway(
        settings.model_url,
        private_credential(settings.model_key_file),
        apis=settings.apis,
        reasoning_effort=settings.reasoning_effort,
        options={
            **settings.options,
            **(
                {"messages": {"replay": settings.thinking_replay}}
                if settings.thinking_replay
                else {}
            ),
        },
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(sock_connect=10), trace_configs=[control_trace()]
    )
    loops: RuntimeLoops | None = None
    try:
        # The endpoint and egress are fixed by trusted launcher configuration;
        # no caller or agent tool supplies a connection URL.
        async with session.ws_connect(
            settings.control_url,
            headers={
                "Authorization": "Bearer " + private_token(settings.token_file),
                CONTROL_HEADER: str(CONTROL_PROTOCOL),
            },
            max_msg_size=MAX_FRAME_BYTES,
            ssl=client_tls(settings.control_ca_file),
            heartbeat=15,
            autoping=True,
        ) as socket:

            async def handle(kind: Capability, value: JsonValue) -> JsonValue:
                if kind is Capability.MODEL:
                    return await models.complete(value)
                if kind is Capability.EMBEDDING:
                    return await embedding.embed(value)
                if kind is Capability.MODEL_INFO:
                    return await models.info(value)
                if loops is None:
                    raise TransportError("unavailable", "runtime is not initialized")
                if kind is Capability.RUN:
                    return await loops.run(value)
                if kind is Capability.CANCEL:
                    return await loops.cancel(value)
                raise TransportError("denied", "host requested an unavailable runtime capability")

            channel = Channel(socket.send_str, handle)
            models = RuntimeModels(gateway, frozenset(settings.models), channel)
            embedding = RuntimeEmbeddings(embedder, channel)
            loops = RuntimeLoops(channel, gateway, settings.scratch)

            async def read() -> None:
                try:
                    async for frame in socket:
                        if frame.type is aiohttp.WSMsgType.TEXT:
                            await channel.receive(frame.data)
                        elif frame.type is aiohttp.WSMsgType.BINARY:
                            raise TransportError("invalid_request", "binary control frame refused")
                        elif frame.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                finally:
                    # Wake reverse-call waiters first, then interrupt local loops.
                    await asyncio.gather(channel.close(), loops.close())

            reader = asyncio.create_task(read())
            scheduling: Scheduling | None = None
            try:
                ready = typed(
                    Ready,
                    await channel.request(
                        Capability.HANDSHAKE, payload(Hello(image_revision=revision)), timeout=30
                    ),
                )
                if ready.control_schema != VERSION or ready.generation != settings.generation:
                    raise TransportError("conflict", "runtime state schema or generation mismatch")
                scheduling = Scheduling(
                    channel,
                    frozenset(ready.projects),
                    lambda: len(loops.active) < 64,
                )
                print("confined agent runtime ready", flush=True)
                while not reader.done():
                    await scheduling.poll()
                    await clock.sleep(2)
                await reader
                raise TransportError("unavailable", "control connection ended")
            finally:
                if scheduling is not None:
                    await scheduling.close()
                await asyncio.gather(channel.close(), loops.close())
                await socket.close()
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
    finally:
        await session.close()
        await gateway.close()
        if embedder is not None:
            await embedder.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    settings = RuntimeSettings.model_validate_json(args.settings.read_text(encoding="utf-8"))
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
