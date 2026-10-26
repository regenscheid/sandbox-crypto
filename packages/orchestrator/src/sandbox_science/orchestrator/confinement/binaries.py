# SPDX-License-Identifier: MIT
"""Binary results of the confined runtime's direct-path connector calls, kept by the host.

In the confined runtime the direct MCP client (``mcp_direct.DirectClient``) runs in the
agent runtime's sandbox, where OpenShell's proxy and the provider's placeholders are
(D98). A ``tools/call`` answer's images, audio and blobs reach it there, as base64, while
the run's artifact publisher is the host's. The runtime withholds them by the proxy's
rule (``Connectors`` with a ``BinarySink``) and sends each over the attempt's
``publish_mcp_binary`` capability (``ControlBinaries``): the model's call ID, the media
type, the SHA-256 of the decoded bytes and the bytes. The host (``CallBinaries``) keeps
the item exactly as ``mcp_binaries.ArtifactBinaries`` keeps a proxy-path item: written
under ``mcp-results/`` in the session's workspace through the worker and published with
the run's publisher. It answers the artifact, revision and SHA-256, and the note that
replaces the item in the result the model sees.

The host takes nothing from the runtime but the call ID and the bytes. The call must be
the attempt's pending tool intent (logged with ``log_tool``, not yet executed by the host
and not yet answered), and a connector tool's, whose server the host's own index names.
It refuses a digest that does not match, an item or a call over its bounds (8 MiB
decoded, the harness's bound on a whole answer, and 100 items, the recorder's bound on one
call's files), a media type other than ``image/*``, ``audio/*`` or ``application/*``, and
a call ID it did not see. The same item of the same call again answers what it answered
the first time without keeping it again. The items a call kept are recorded with its
tool result (``mcp_binaries``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import JsonValue

from sandbox_science.orchestrator.connectors import Connectors
from sandbox_science.orchestrator.mcp_binaries import ArtifactBinaries, withheld
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.policy.mcp_answers import Withheld

from .transport import Capability, Channel, TransportError
from .wire import (
    MAX_BINARY_BYTES,
    MAX_BINARY_ITEMS,
    BinaryKept,
    BinaryResult,
    payload,
    typed,
)

BINARY_TYPES: Final = frozenset({"image", "audio", "application"})
"""The top-level media types a kept item may have."""
PUBLISH_SECONDS: Final = 300.0
"""How long the runtime waits for the host to keep one item: a worker write and a
publication of at most 8 MiB."""


def binary(mime: str) -> bool:
    """Whether ``mime`` names binary content, as ``publish_mcp_binary`` takes it."""
    return mime.partition("/")[0].lower() in BINARY_TYPES


class ControlBinaries:
    """The runtime's ``BinarySink`` for one connector call: each item goes to the host over
    the attempt's ``publish_mcp_binary``, and the host's note replaces it. An item that
    cannot be sent, or that the host refuses, is withheld and said, never passed on."""

    def __init__(self, channel: Channel, attempt: str, call: str) -> None:
        self._channel, self._attempt, self._call = channel, attempt, call

    async def keep(self, server: str, tool: str, item: Withheld) -> str:
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            return withheld(item, "it is not valid base64")
        if len(data) > MAX_BINARY_BYTES:
            return withheld(item, f"larger than the {MAX_BINARY_BYTES} bytes kept")
        mime = item.mime or "application/octet-stream"
        if not binary(mime):
            return withheld(item, "its media type is not a binary one")
        try:
            asked = BinaryResult(
                attempt=self._attempt,
                call=self._call,
                mime=mime,
                sha256=hashlib.sha256(data).hexdigest(),
                data_b64=base64.b64encode(data).decode(),
            )
            reply = typed(
                BinaryKept,
                await self._channel.request(
                    Capability.PUBLISH_MCP_BINARY, payload(asked), timeout=PUBLISH_SECONDS
                ),
            )
        except (TransportError, ValueError):
            return withheld(item, "the control side did not keep it")
        return reply.note


@dataclass
class _Kept:
    items: dict[str, tuple[BinaryKept, str]] = field(
        default_factory=dict[str, tuple[BinaryKept, str]]
    )
    size: int = 0


class CallBinaries:
    """The host's side of ``publish_mcp_binary`` for one attempt (see the module)."""

    def __init__(self) -> None:
        self._calls: dict[str, _Kept] = {}

    async def publish(
        self, value: JsonValue, pending: ToolCall | None, connectors: Connectors | None
    ) -> JsonValue:
        """Keep one item of ``pending``'s result, the attempt's unexecuted tool intent, with
        the attempt's ``connectors``, whose index names the tool's server."""
        asked = typed(BinaryResult, value)
        try:
            data = base64.b64decode(asked.data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise TransportError("invalid_request", "binary result is not base64") from None
        if len(data) > MAX_BINARY_BYTES:
            raise TransportError("invalid_request", "binary result exceeds its byte bound")
        if hashlib.sha256(data).hexdigest() != asked.sha256:
            raise TransportError("conflict", "binary result does not match its SHA-256")
        if not binary(asked.mime):
            raise TransportError("denied", "media type is not binary")
        known = self._calls.get(asked.call)
        if known is not None and asked.sha256 in known.items:
            # A retry: the same answer, nothing kept again.
            return payload(known.items[asked.sha256][0])
        if pending is None or pending.id != asked.call:
            raise TransportError("denied", "no pending tool call of this attempt has this ID")
        tool = connectors.tools.get(pending.name) if connectors is not None else None
        if connectors is None or tool is None:
            raise TransportError("denied", "the call is not a connector tool's")
        sink = connectors.binaries
        if not isinstance(sink, ArtifactBinaries):
            raise TransportError("denied", "this attempt keeps no binary results")
        known = known if known is not None else _Kept()
        if len(known.items) >= MAX_BINARY_ITEMS or known.size + len(data) > MAX_BINARY_BYTES:
            raise TransportError("invalid_request", "the call's binary results exceed their bound")
        kept = await sink.keep_bytes(tool.server, pending.name, data, asked.mime)
        reply = BinaryKept(
            sha256=kept.sha256,
            size=kept.size,
            artifact=kept.artifact,
            revision=kept.revision,
            path=kept.path,
            note=kept.note,
        )
        if kept.path is not None:
            # Only what was kept is answered again: an item the worker could not take may
            # be sent again and tried again.
            known.items[asked.sha256] = reply, asked.mime
            known.size += len(data)
            self._calls[asked.call] = known
        return payload(reply)

    def recorded(self, call: str) -> list[dict[str, Any]]:
        """The items ``call`` kept, for its tool record; they are forgotten here."""
        known = self._calls.pop(call, None)
        if known is None:
            return []
        return [
            {
                "sha256": reply.sha256,
                "size": reply.size,
                "media_type": mime,
                "artifact": reply.artifact,
                "revision": reply.revision,
            }
            for reply, mime in known.items.values()
        ]
