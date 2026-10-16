# SPDX-License-Identifier: MIT
"""The preview origin: a second listener that serves published PDFs to the viewer, and
figure files to the workspace's pages.

PDFs and figures are untrusted content (docs/THREAT-MODEL.md, T6), so they are served from
an origin of their own (docs/specs/pdf-viewer.md, PV-1 to PV-3; docs/specs/figures.md,
FG-12, FG-13). This application serves only the viewer's bundle, PDF bytes and figure
files, each for a capability the workspace minted: an HMAC-SHA-256, under a key made when
the process starts and kept only in memory, over the project, the artifact, the revision,
its SHA-256 and an expiry. A figure file is served only as the type it is recorded as and
only when its first bytes are that type's, at most 8 MiB: a PNG or a JPEG of at most 50
megapixels by its header, for a page's ``<img>``; an SVG, which the figure frame
(``/figures/``) fetches and shows as an image, so it runs no script and loads nothing; and
an HTML file, the page of a frame the workspace makes without scripts, which its CSP's
``sandbox`` enforces too (FG-14). It has no API and no login, and it never reads a cookie:
the browser sends the workspace's cookie to every port on the host, and a request with it
gets exactly what one without it gets.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from aiohttp import hdrs, web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactLink, ArtifactRevisionRef
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

from .context import own_hosts

CAPABILITY_SECONDS: Final = 600
"""How long a capability opens its PDF (PV-3)."""
MAX_PDF_BYTES: Final = 64 * 1024 * 1024
"""The largest PDF the viewer is given (PV-3)."""
CONCURRENT_READS: Final = 4
"""PDFs read from the store at once; more wait, so no frame can tie up every thread."""
PDF: Final = "application/pdf"
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_IMAGE_PIXELS: Final = 50_000_000
MAX_IMAGE_SIDE: Final = 16_384
"""The largest image a page is given, in bytes and by its header's size (FG-13), and
each side at most, so no image is a sliver too long to lay out (review of #487, L3)."""
Purpose = Literal["pdf", "file"]
"""The route a capability opens, which its MAC covers (review of #487, I1): the viewer's
PDF, or a figure file."""
VIEWER_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".bcmap": "application/octet-stream",
    ".pfb": "application/octet-stream",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}
"""The only types the viewer's bundle is served as."""

_IDENTIFIER: Final = TypeAdapter[str](Identifier)
_CAPABILITY: Final = re.compile(r"[A-Za-z0-9_-]{1,2048}\.[A-Za-z0-9_-]{43}")
"""A capability's one spelling: unpadded base64url payload, then a 32-byte MAC."""
_SHA256: Final = frozenset("0123456789abcdef")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SANDBOXED: Final = web.ResponseKey("sandboxed", bool)
"""Marks a response whose CSP also says ``sandbox``: the PDF bytes and figure files."""
EMBEDDED: Final = web.ResponseKey("embedded", bool)
"""Marks an image a workspace page shows in an ``<img>``: another origin may load it."""
STYLED: Final = web.ResponseKey("styled", bool)
"""Marks a response whose CSP allows inline styles: the figure frame's page, whose
libraries write ``<style>`` elements and style attributes, and an HTML figure (FG-14)."""
SVG: Final = "image/svg+xml"
HTML: Final = "text/html"
_SPACE: Final = " \t\r\n"


def svg_begins(text: str) -> bool:
    """Whether ``text`` begins as an SVG: past whitespace, an XML declaration, comments and
    an SVG doctype without an internal subset, ``<svg``. A linear scan: each step only
    moves forward, by ``str.find`` (review of #492, H1: a regular expression with a lazy
    comment inside a repeated group backtracked for hours on 40 comments, holding the
    interpreter's lock, and froze every listener)."""
    at, end = 0, len(text)
    doctype = False
    while True:
        while at < end and text[at] in _SPACE:
            at += 1
        if text.startswith("<?xml", at):
            close = text.find("?>", at + 5)
        elif text.startswith("<!--", at):
            close = text.find("-->", at + 4)
        elif text[at : at + 9].upper() == "<!DOCTYPE" and not doctype:
            close = text.find(">", at + 9)
            declared = text[at + 9 : close] if close >= 0 else ""
            name = declared.lstrip(_SPACE)
            if "[" in declared or name[:3].lower() != "svg" or name[3:4] not in ("", *_SPACE):
                return False
            doctype = True
        else:
            break
        if close < 0:
            return False
        at = close + (2 if text.startswith("<?", at) else 3 if text.startswith("<!--", at) else 1)
    return text[at : at + 4].lower() == "<svg" and text[at + 4 : at + 5] in (*_SPACE, ">", "/")


_SOF: Final = frozenset(
    {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), 0xCD, 0xCE, 0xCF}
)
"""JPEG's start-of-frame markers, which give the image's size."""


def png_size(data: bytes) -> tuple[int, int] | None:
    """A PNG's width and height from its header; ``None`` unless it starts as a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or len(data) < 24:
        return None
    return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """A JPEG's width and height from its first frame header; ``None`` unless it starts
    as a JPEG and has one."""
    if data[:3] != b"\xff\xd8\xff":
        return None
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:  # fill
            at += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
            at += 2
            continue
        length = int.from_bytes(data[at + 2 : at + 4])
        if marker in _SOF:
            if length < 7 or at + 9 > len(data):
                return None
            return int.from_bytes(data[at + 7 : at + 9]), int.from_bytes(data[at + 5 : at + 7])
        if marker == 0xDA or length < 2:  # the scan begins before any frame header
            return None
        at += 2 + length
    return None


IMAGES: Final = {"image/png": png_size, "image/jpeg": jpeg_size}
"""The figure files a page shows as images, each with how its header is read (FG-12)."""
FIGURE_FILES: Final = frozenset({*IMAGES, SVG, HTML})
"""Every type the preview origin serves as a figure file."""


NOT_ITS_TYPE: Final = (415, "This file's bytes are not the type it is recorded as.")


def text_problem(media_type: str, data: bytes) -> tuple[int, str] | None:
    """Why an SVG or an HTML file is not served; ``None`` when it is (FG-13): it is not
    UTF-8 text without a NUL, or an SVG does not begin, past its prolog, with ``<svg``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return NOT_ITS_TYPE
    if "\x00" in text:
        return NOT_ITS_TYPE
    if media_type == SVG and not svg_begins(text.removeprefix("\ufeff")):
        return NOT_ITS_TYPE
    return None


def image_problem(media_type: str, data: bytes) -> tuple[int, str] | None:
    """Why an image is not served, as a status and a reason; ``None`` when it is (FG-13):
    its first bytes are not its type's, or it passes the size limits."""
    size = IMAGES[media_type](data)
    if size is None:
        return NOT_ITS_TYPE
    width, height = size
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        return 413, "This image is larger than a page shows."
    if max(width, height) > MAX_IMAGE_SIDE:
        return 413, "This image is larger than a page shows."
    return None


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

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # The key lives as long as the process, so expiry needs only a clock that never
        # steps back, not the time of day.
        self._key = secrets.token_bytes(32)
        self._clock = clock

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, b"pdf-viewer-1\x00" + payload, hashlib.sha256).digest()

    def mint(
        self, project: str, artifact: str, revision: int, sha256: str, purpose: Purpose
    ) -> str:
        """A capability for the revision on one route, as the text a URL carries."""
        expires = int(self._clock()) + CAPABILITY_SECONDS
        payload = json.dumps(
            [project, artifact, revision, sha256, expires, purpose], separators=(",", ":")
        ).encode()
        return f"{_encode(payload)}.{_encode(self._mac(payload))}"

    def check(self, text: str, purpose: Purpose) -> Capability:
        """The capability ``text`` names for the route ``purpose`` names; ``CapabilityError``
        unless genuine, current and minted for that route."""
        if not _CAPABILITY.fullmatch(text):
            raise CapabilityError("malformed")
        encoded, mac = text.split(".")
        try:
            payload, given = _decode(encoded), _decode(mac)
        except (ValueError, UnicodeError):
            raise CapabilityError("malformed") from None
        if _encode(payload) != encoded or _encode(given) != mac:
            raise CapabilityError("not canonical")  # unused trailing bits set
        if not hmac.compare_digest(given, self._mac(payload)):
            raise CapabilityError("forged")
        project, artifact, revision, sha256, expires, minted = json.loads(payload)
        if minted != purpose:
            raise CapabilityError("minted for another route")
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


def preview_policy(workspace_origin: str, *, styled: bool = False) -> str:
    """The preview origin's CSP (PV-2): its own scripts, styles, worker, fonts and
    connections only, framed only by the workspace. ``styled`` also allows inline styles,
    for the figure frame and an HTML figure (FG-14); never ``'unsafe-eval'``."""
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "worker-src 'self'",
            "style-src 'self' 'unsafe-inline'" if styled else "style-src 'self'",
            "font-src 'self'",
            "img-src 'self' data: blob:",
            "connect-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {workspace_origin}",
        )
    )


def own_host(request: web.Request) -> bool:
    """Whether a request names this server by its own address; false when that cannot be
    told, so a host check fails closed."""
    try:
        return request.headers.get(hdrs.HOST, "").lower() in own_hosts(request)
    except AttributeError:
        return False


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
    figures_dir: Path | None = None,
    check_host: Callable[[web.Request], bool] = own_host,
) -> web.Application:
    """The preview origin's application. ``workspace_origin`` names the origin allowed to
    frame it, read when each response is prepared (it is known once the workspace listens).
    ``check_host`` refuses a request whose ``Host`` names another server; only a listener
    off loopback, whose names are not known, accepts every ``Host``."""
    inventory = ArtifactInventory(documents)
    reads = asyncio.Semaphore(CONCURRENT_READS)
    viewer = viewer_dir.resolve() if viewer_dir is not None and viewer_dir.is_dir() else None
    figures = figures_dir.resolve() if figures_dir is not None and figures_dir.is_dir() else None

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not check_host(request):
            return web.Response(status=403, text="This server answers only to its own address.")
        if request.method not in (hdrs.METH_GET, hdrs.METH_HEAD):
            return web.Response(status=405, text="The preview origin only serves reads.")
        return await handler(request)

    async def secure(request: web.Request, response: web.StreamResponse) -> None:
        policy = preview_policy(workspace_origin(), styled=STYLED in response)
        if SANDBOXED in response:
            policy += "; sandbox"
        response.headers.update(
            {
                "Content-Security-Policy": policy,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                # An image the workspace's page loads comes from this other origin; only
                # a capability, which the workspace minted for that page, opens it.
                "Cross-Origin-Resource-Policy": "cross-origin"
                if EMBEDDED in response
                else "same-origin",
                hdrs.CACHE_CONTROL: "no-store",
            }
        )

    def checked(request: web.Request, purpose: Purpose) -> Capability | None:
        try:
            found = capabilities.check(request.match_info["capability"], purpose)
            _IDENTIFIER.validate_python(found.project, strict=True)
            _IDENTIFIER.validate_python(found.artifact, strict=True)
            if len(found.sha256) != 64 or not set(found.sha256) <= _SHA256:
                raise CapabilityError("malformed hash")
        except (CapabilityError, ValidationError, TypeError, ValueError):
            return None
        return found

    def reference_of(found: Capability) -> ArtifactRevisionRef:
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
        return revision.reference

    async def pdf(request: web.Request) -> web.StreamResponse:
        if (found := checked(request, "pdf")) is None:
            return web.Response(status=403, text="This link is not valid, or has expired.")

        def read() -> web.Response:
            reference = reference_of(found)
            if reference.media_type != PDF:
                return web.Response(status=415, text="This artifact is not a PDF.")
            if reference.size > MAX_PDF_BYTES:
                return web.Response(status=413, text="This PDF is larger than the viewer takes.")
            bound = min(MAX_PDF_BYTES, inventory.limits.artifact_bytes)
            data = inventory.content(found.project, reference, max_bytes=bound)
            # PDF.js fetches the bytes; nothing navigates to them. Opened directly, they
            # download, and a browser that renders them anyway does so in a sandbox.
            return web.Response(
                body=data,
                headers={hdrs.CONTENT_TYPE: PDF, hdrs.CONTENT_DISPOSITION: "attachment"},
            )

        # The store is read, and the bytes hashed, off the event loop the workspace shares.
        try:
            async with reads:
                answer = await asyncio.to_thread(read)
        except DocumentError:
            return web.Response(status=404, text="This PDF is not available.")
        answer[SANDBOXED] = True
        return answer

    async def figure_file(request: web.Request) -> web.StreamResponse:
        """A pinned figure file (FG-12 to FG-14): as the type it is recorded as, only when
        its first bytes are that type's, within the limits. A PNG or a JPEG is for a page's
        ``<img>``; an SVG for the figure frame, which fetches it; an HTML file is the page
        of a frame without scripts, which its CSP's ``sandbox`` keeps too."""
        if (found := checked(request, "file")) is None:
            return web.Response(status=403, text="This link is not valid, or has expired.")

        def read() -> tuple[web.Response, str]:
            reference = reference_of(found)
            media = reference.media_type
            return answer_of(reference, media), media

        def answer_of(reference: ArtifactRevisionRef, media: str) -> web.Response:
            if media not in FIGURE_FILES:
                return web.Response(status=415, text="This file is not shown as a figure.")
            if reference.size > MAX_IMAGE_BYTES:
                return web.Response(status=413, text="This file is larger than a page shows.")
            bound = min(MAX_IMAGE_BYTES, inventory.limits.artifact_bytes)
            data = inventory.content(found.project, reference, max_bytes=bound)
            checks = image_problem if media in IMAGES else text_problem
            if (problem := checks(media, data)) is not None:
                return web.Response(status=problem[0], text=problem[1])
            # An HTML figure is a frame's page; anything else, opened directly, downloads.
            shown = "inline" if media == HTML else "attachment"
            kind = f"{media}; charset=utf-8" if media in (HTML, SVG) else media
            return web.Response(
                body=data, headers={hdrs.CONTENT_TYPE: kind, hdrs.CONTENT_DISPOSITION: shown}
            )

        try:
            async with reads:
                answer, media = await asyncio.to_thread(read)
        except DocumentError:
            return web.Response(status=404, text="This file is not available.")
        answer[SANDBOXED] = True
        if answer.status == 200 and media in IMAGES:
            answer[EMBEDDED] = True
        if answer.status == 200 and media == HTML:
            answer[STYLED] = True
        return answer

    async def figures_file(request: web.Request) -> web.StreamResponse:
        """The figure frame's bundle (FG-14), served like the viewer's, its page allowed
        inline styles."""
        answer = bundle_file(figures, request)
        answer[STYLED] = True
        return answer

    async def viewer_file(request: web.Request) -> web.StreamResponse:
        return bundle_file(viewer, request)

    def bundle_file(bundle: Path | None, request: web.Request) -> web.FileResponse:
        segments = _segments(request.match_info["path"])
        if bundle is None or segments is None:
            raise web.HTTPNotFound()
        path = bundle.joinpath(*segments) if segments else bundle / "index.html"
        try:
            found = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise web.HTTPNotFound() from None
        content_type = VIEWER_TYPES.get(found.suffix.lower())
        if not found.is_relative_to(bundle) or not found.is_file() or content_type is None:
            raise web.HTTPNotFound()
        return web.FileResponse(found, headers={hdrs.CONTENT_TYPE: content_type})

    app = web.Application(middlewares=[guard])
    app.on_response_prepare.append(secure)
    app.router.add_get("/pdf/{capability:.+}", pdf)  # any spelling gets the same refusal
    app.router.add_get("/file/{capability:.+}", figure_file)
    app.router.add_get("/viewer/{path:.*}", viewer_file)
    app.router.add_get("/figures/{path:.*}", figures_file)
    return app
