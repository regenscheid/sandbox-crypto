# SPDX-License-Identifier: MIT
"""Binary MCP results kept as artifacts by control, never passed to the model (D67, D98).

The MCP proxy withholds image and audio blocks and blob resources from its answers, and
cannot keep them: it has no route to control's stores. An MCP client that reaches servers
straight through OpenShell (``mcp_direct``) gets them as the server sent them, as base64.
``Connectors`` then withholds them by the same rule as the proxy
(``sandbox_science.policy.mcp_answers``) and hands each to a ``BinarySink``, whose note
replaces it in the text the model sees.

``ArtifactBinaries`` is the harness's sink: it writes the decoded bytes into the session's
workspace through the worker, at a path named by their digest under ``mcp-results/``,
and publishes that file as an artifact with the run's publisher, so the inventory, its
retention and its budget apply as to any file the agent publishes. The note names the
artifact and the path, which the agent can read, view or cite. Anything that goes wrong
leaves a note saying the item was withheld and why, never the bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from typing import Final, Protocol

from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext, PublishRequest
from sandbox_science.contracts.worker import WriteFileRequest
from sandbox_science.policy.mcp_answers import Withheld

from .artifact_publisher import ArtifactPublisher
from .worker_client import WorkerAPIError, WorkerClient

_log = logging.getLogger(__name__)

RESULTS_DIR: Final = "mcp-results"
"""Where kept binary results go in the session's workspace."""
MAX_KEPT_BYTES: Final = 16 * 1024 * 1024
"""The largest binary item kept; a larger one is withheld only."""
_SAFE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_EXTENSIONS: Final = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}


class BinarySink(Protocol):
    """Keeps one binary item of a tool's result; returns the note the model sees instead."""

    async def keep(self, server: str, tool: str, item: Withheld) -> str: ...


def withheld(item: Withheld, why: str) -> str:
    """The note for an item that was withheld and not kept."""
    return f"[{item.mime or 'binary data'} withheld: {item.size} bytes of base64; {why}]"


class ArtifactBinaries:
    """Keeps binary results as artifacts of the run, through the worker (see the module)."""

    def __init__(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        publisher: ArtifactPublisher | None = None,
    ) -> None:
        self.client, self.context, self.publisher = client, context, publisher

    async def keep(self, server: str, tool: str, item: Withheld) -> str:
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            return withheld(item, "it is not valid base64")
        if len(data) > MAX_KEPT_BYTES:
            return withheld(item, f"larger than the {MAX_KEPT_BYTES} bytes kept")
        digest = hashlib.sha256(data).hexdigest()
        name = server if _SAFE.fullmatch(server) else "server"
        path = f"{RESULTS_DIR}/{name}/{digest[:24]}{_EXTENSIONS.get(item.mime or '', '.bin')}"
        try:
            write = WriteFileRequest(
                context=self.context, path=path, content_b64=base64.b64encode(data).decode()
            )
            await self.client.write_file(write)
            try:
                request = PublishRequest(context=self.context, path=path, media_type=item.mime)
            except ValidationError:
                request = PublishRequest(context=self.context, path=path)
            if self.publisher is not None:
                # One operation per run and content: the same bytes again are the same.
                operation = (
                    "mcp-"
                    + hashlib.sha256(f"{self.publisher.run_id}:{digest}".encode()).hexdigest()[:40]
                )
                outcome = await self.publisher.publish(
                    request, operation, tool_call=f"mcp:{tool}"[:128]
                )
                artifact = outcome.worker.artifact_id if outcome.worker is not None else None
            else:
                artifact = (await self.client.publish(request)).artifact_id
        except (WorkerAPIError, ValidationError, ValueError, OSError) as exc:
            _log.warning("a binary result of %s could not be kept: %s", tool, type(exc).__name__)
            return withheld(item, "it could not be kept as an artifact")
        kept = f"artifact {artifact}" if artifact is not None else "a file (publication held)"
        return (
            f"[{item.mime or 'binary data'} from {server}, {len(data)} bytes: not shown inline; "
            f"kept as {kept} at {path} (sha256 {digest})]"
        )
