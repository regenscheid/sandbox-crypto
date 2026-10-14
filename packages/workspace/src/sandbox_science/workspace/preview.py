# SPDX-License-Identifier: MIT
"""The preview origin: a second listener that serves published PDFs to the viewer.

PDFs are untrusted content (docs/THREAT-MODEL.md, T6), so the viewer runs on an origin of
its own (docs/specs/pdf-viewer.md, PV-1 to PV-3). This application serves only the
viewer's bundle and PDF bytes for a capability the workspace minted: an HMAC-SHA-256,
under a key made when the process starts and kept only in memory, over the project, the
artifact, the revision, its SHA-256 and an expiry. It has no API and no login, and it
never reads a cookie: the browser sends the workspace's cookie to every port on the host,
and a request with it gets exactly what one without it gets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aiohttp import hdrs, web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactLink
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

CAPABILITY_SECONDS: Final = 600
"""How long a capability opens its PDF (PV-3)."""
MAX_PDF_BYTES: Final = 64 * 1024 * 1024
"""The largest PDF the viewer is given (PV-3)."""
PDF: Final = "application/pdf"
VIEWER_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".bcmap": "application/octet-stream",
    ".pfb": "application/octet-stream",
    ".ttf": "font/ttf",
    ".json": "application/json",
}
"""The only types the viewer's bundle is served as."""

_IDENTIFIER: Final = TypeAdapter[str](Identifier)
_SHA256: Final = frozenset("0123456789abcdef")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class CapabilityError(Exception):
    """A capability that is malformed, forged or expired; the reason is for the log only."""


@dataclass(frozen=True)
class Capability:
    """What a capability names: one revision of one artifact, until ``expires``."""

    project: str
    artifact: str
    revision: int
    sha256: str
    expires: int


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Capabilities:
    """Mints and checks capabilities under a key that lives only in this process."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._key = secrets.token_bytes(32)
        self._clock = clock

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, b"pdf-viewer-1\x00" + payload, hashlib.sha256).digest()

    def mint(self, project: str, artifact: str, revision: int, sha256: str) -> str:
        """A capability for the revision, as the text the viewer's URL carries."""
        expires = int(self._clock()) + CAPABILITY_SECONDS
        payload = json.dumps(
            [project, artifact, revision, sha256, expires], separators=(",", ":")
        ).encode()
        return f"{_encode(payload)}.{_encode(self._mac(payload))}"

    def check(self, text: str) -> Capability:
        """The capability ``text`` names; ``CapabilityError`` unless genuine and current."""
        try:
            encoded, mac = text.split(".")
            payload, given = _decode(encoded), _decode(mac)
        except (ValueError, UnicodeError):
            raise CapabilityError("malformed") from None
        if not hmac.compare_digest(given, self._mac(payload)):
            raise CapabilityError("forged")
        project, artifact, revision, sha256, expires = json.loads(payload)
        if int(self._clock()) >= expires:
            raise CapabilityError("expired")
        return Capability(project, artifact, revision, sha256, expires)


@dataclass
class Preview:
    """The preview origin as the workspace knows it: the capabilities it mints, and the
    two origins, set once each listener is bound."""

    capabilities: Capabilities
    origin: str = ""
    workspace_origin: str = ""
    """The workspace's origin, the only one that may frame the viewer."""


def preview_policy(workspace_origin: str) -> str:
    """The preview origin's CSP (PV-2): its own scripts, styles, worker, fonts and
    connections only, framed only by the workspace."""
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "worker-src 'self'",
            "style-src 'self'",
            "font-src 'self' data: blob:",
            "img-src 'self' data: blob:",
            "connect-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {workspace_origin}",
        )
    )


def _segments(path: str) -> list[str] | None:
    if "\\" in path or "\x00" in path:
        return None
    segments = [part for part in path.split("/") if part]
    if any(part.startswith(".") for part in segments):
        return None
    return segments


def create_preview_app(
    documents: DocumentStore,
    capabilities: Capabilities,
    *,
    workspace_origin: Callable[[], str],
    viewer_dir: Path | None = None,
    check_host: Callable[[web.Request], bool] = lambda request: True,
) -> web.Application:
    """The preview origin's application. ``workspace_origin`` names the origin allowed to
    frame it, read when each response is prepared (it is known once the workspace listens).
    ``check_host`` refuses a request whose ``Host`` names another server."""
    inventory = ArtifactInventory(documents)
    viewer = viewer_dir.resolve() if viewer_dir is not None and viewer_dir.is_dir() else None

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not check_host(request):
            return web.Response(status=403, text="This server answers only to its own address.")
        if request.method not in (hdrs.METH_GET, hdrs.METH_HEAD):
            return web.Response(status=405, text="The preview origin only serves reads.")
        return await handler(request)

    async def secure(request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(
            {
                "Content-Security-Policy": preview_policy(workspace_origin()),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Resource-Policy": "same-origin",
                hdrs.CACHE_CONTROL: "no-store",
            }
        )

    async def pdf(request: web.Request) -> web.StreamResponse:
        try:
            found = capabilities.check(request.match_info["capability"])
            _IDENTIFIER.validate_python(found.project, strict=True)
            _IDENTIFIER.validate_python(found.artifact, strict=True)
            if len(found.sha256) != 64 or not set(found.sha256) <= _SHA256:
                raise CapabilityError("malformed hash")
        except (CapabilityError, ValidationError, TypeError, ValueError):
            return web.Response(status=403, text="This link is not valid, or has expired.")
        try:
            revision = inventory.resolve(
                found.project,
                ArtifactLink(
                    artifact_id=found.artifact,
                    project_id=found.project,
                    mode="pinned",
                    revision=found.revision,
                    sha256=found.sha256,
                ),
            )
            reference = revision.reference
            if reference.media_type != PDF:
                return web.Response(status=415, text="This artifact is not a PDF.")
            if reference.size > MAX_PDF_BYTES:
                return web.Response(status=413, text="This PDF is larger than the viewer takes.")
            bound = min(MAX_PDF_BYTES, inventory.limits.artifact_bytes)
            data = inventory.content(found.project, reference, max_bytes=bound)
        except DocumentError:
            return web.Response(status=404, text="This PDF is not available.")
        return web.Response(
            body=data,
            headers={
                hdrs.CONTENT_TYPE: PDF,
                hdrs.CONTENT_DISPOSITION: "inline",
            },
        )

    async def viewer_file(request: web.Request) -> web.StreamResponse:
        segments = _segments(request.match_info["path"])
        if viewer is None or segments is None:
            raise web.HTTPNotFound()
        path = viewer.joinpath(*segments) if segments else viewer / "index.html"
        try:
            found = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise web.HTTPNotFound() from None
        content_type = VIEWER_TYPES.get(found.suffix.lower())
        if not found.is_relative_to(viewer) or not found.is_file() or content_type is None:
            raise web.HTTPNotFound()
        return web.FileResponse(found, headers={hdrs.CONTENT_TYPE: content_type})

    app = web.Application(middlewares=[guard])
    app.on_response_prepare.append(secure)
    app.router.add_get("/pdf/{capability}", pdf)
    app.router.add_get("/viewer/{path:.*}", viewer_file)
    return app
