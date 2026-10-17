# SPDX-License-Identifier: MIT
"""The agent-facing service. Run this entrypoint inside its restricted OpenShell VM."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path

import aiohttp
from pydantic import Field, JsonValue, TypeAdapter

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.control.migrations import VERSION
from sandbox_science.orchestrator.messages import ThinkingReplay
from sandbox_science.orchestrator.model import ModelGateway
from sandbox_science.orchestrator.skills.embeddings import Embedder

from .embeddings import RuntimeEmbeddings
from .models import RuntimeModels
from .runtime_loop import RuntimeLoops
from .server import private_token
from .transport import (
    CONTROL_HEADER,
    CONTROL_PROTOCOL,
    MAX_FRAME_BYTES,
    Capability,
    Channel,
    TransportError,
)
from .wire import Hello, ModelAlias, Queue, Ready, payload, typed


class RuntimeSettings(Contract):
    control_url: str = Field(pattern=r"^http://[A-Za-z0-9.-]+:[0-9]{1,5}/control$")
    token_file: Path
    model_url: str = Field(min_length=1, max_length=2048)
    model_key_file: Path
    models: tuple[ModelAlias, ...] = Field(min_length=1, max_length=128)
    apis: dict[str, str] = Field(default_factory=dict[str, str])
    options: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict[str, dict[str, JsonValue]]
    )
    scratch: Path = Path("/sandbox/runtime/scratch")
    image_record: Path = Path("/usr/share/sandbox-science/image.json")
    embedding: ModelAlias | None = None
    thinking_replay: ThinkingReplay | None = None
    generation: Identifier


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
        Embedder(settings.model_url, settings.embedding, private_token(settings.model_key_file))
        if settings.embedding
        else None
    )
    embedding = RuntimeEmbeddings(embedder)
    gateway = ModelGateway(
        settings.model_url,
        private_token(settings.model_key_file),
        apis=settings.apis,
        options={
            **settings.options,
            **(
                {"messages": {"replay": settings.thinking_replay}}
                if settings.thinking_replay
                else {}
            ),
        },
    )
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=10))
    loops: RuntimeLoops | None = None
    try:
        # The one endpoint is fixed by trusted launcher configuration. No redirect
        # may forward a runtime capability or gateway key to another host.
        async with session.ws_connect(
            settings.control_url,
            headers={
                "Authorization": "Bearer " + private_token(settings.token_file),
                CONTROL_HEADER: str(CONTROL_PROTOCOL),
            },
            max_msg_size=MAX_FRAME_BYTES,
            heartbeat=15,
            autoping=True,
        ) as socket:
            models = RuntimeModels(gateway, frozenset(settings.models))

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
            try:
                ready = typed(
                    Ready,
                    await channel.request(
                        Capability.HANDSHAKE, payload(Hello(image_revision=revision)), timeout=30
                    ),
                )
                if ready.control_schema != VERSION or ready.generation != settings.generation:
                    raise TransportError("conflict", "runtime state schema or generation mismatch")
                print("confined agent runtime ready", flush=True)
                while not reader.done():
                    queue = typed(Queue, await channel.request(Capability.QUEUE, None, timeout=30))
                    for entry in queue.entries:
                        if entry.project not in ready.projects:
                            raise TransportError(
                                "denied", "control returned work outside its scope"
                            )
                        if len(loops.active) >= 64:
                            break
                        await channel.request(Capability.ADMIT, payload(entry), timeout=30)
                    await clock.sleep(2)
                await reader
                raise TransportError("unavailable", "control connection ended")
            finally:
                await loops.close()
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
