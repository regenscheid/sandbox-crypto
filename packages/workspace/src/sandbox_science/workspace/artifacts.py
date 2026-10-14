# SPDX-License-Identifier: MIT
"""A project's published artifacts, and links that open a PDF in the viewer.

Routes (docs/specs/pdf-viewer.md, PV-4):

- ``GET /api/v0/projects/{project}/artifacts?after=ID`` lists the project's published
  artifacts, each at its latest revision, ten at a time, and says whether the viewer is on.
- ``GET /api/v0/projects/{project}/artifacts/{artifact}/revisions/{revision}/viewer
  ?sha256=HEX`` mints a capability for that exact revision of a PDF and answers the viewer's
  URL on the preview origin, which opens it for 10 minutes (PV-3).

Both read through the orchestrator's ``ArtifactInventory``; neither writes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlencode

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactLink
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.projects import ProjectError

from . import api
from .context import CONTEXT, identifier, json_answer
from .preview import CAPABILITY_SECONDS, PDF, Preview

PREVIEW: Final = web.AppKey("preview", Preview)
_IDENTIFIER: Final = TypeAdapter[str](Identifier)


class ArtifactEntry(Contract):
    """A published artifact at its latest revision."""

    artifact_id: str
    revision: int
    path: str
    sha256: str
    size: int
    media_type: str
    created_at: datetime


class ArtifactListing(Contract):
    artifacts: list[ArtifactEntry]
    next_after: str | None
    viewer: bool
    """Whether the PDF viewer's origin is on."""


class ViewerLink(Contract):
    """The viewer's URL for one PDF revision, and when it stops opening it."""

    url: str
    expires_at: datetime
    path: str
    """Where the PDF was published from, which an anchor into it names."""


def add_routes(app: web.Application) -> None:
    base = "/api/v0/projects/{project}/artifacts"
    app.router.add_get(base, listing)
    app.router.add_get(base + "/{artifact}/revisions/{revision}/viewer", viewer)


async def listing(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    after = request.query.get("after", "")
    if after:
        try:
            _IDENTIFIER.validate_python(after, strict=True)
        except ValidationError:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid artifact cursor") from None
    context = request.app[CONTEXT]
    on = request.app.get(PREVIEW) is not None

    def read() -> str:
        context.projects.get(project)
        page = ArtifactInventory(context.documents).list(project, after=after)
        entries = [
            ArtifactEntry(
                artifact_id=found.reference.artifact_id,
                revision=found.reference.revision,
                path=found.path,
                sha256=found.reference.sha256,
                size=found.reference.size,
                media_type=found.reference.media_type,
                created_at=found.created_at,
            )
            for found in page.entries
        ]
        answer = ArtifactListing(artifacts=entries, next_after=page.next_after, viewer=on)
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


async def viewer(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    artifact = identifier(request, "artifact")
    raw_revision, sha256 = request.match_info["revision"], request.query.get("sha256", "")
    if not (raw_revision.isascii() and raw_revision.isdigit()) or not (
        1 <= int(raw_revision) <= 999999999
    ):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid artifact revision")
    if len(sha256) != 64 or not set(sha256) <= set("0123456789abcdef"):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid sha256")
    revision = int(raw_revision)
    preview = request.app.get(PREVIEW)
    if preview is None or not preview.origin:
        raise ProjectError(ErrorKind.UNAVAILABLE, "the PDF viewer is off on this server")
    context = request.app[CONTEXT]

    def mint() -> str:
        context.projects.get(project)
        link = ArtifactLink(
            artifact_id=artifact,
            project_id=project,
            mode="pinned",
            revision=revision,
            sha256=sha256,
        )
        found = ArtifactInventory(context.documents).resolve(project, link)
        if found.reference.media_type != PDF:
            raise ProjectError(ErrorKind.UNSUPPORTED, "the viewer opens PDFs only")
        # The capability counts its ten minutes on a monotonic clock; the time of day is
        # read first, so the time shown is never later than the real expiry.
        expires = datetime.now(UTC) + timedelta(seconds=CAPABILITY_SECONDS)
        capability = preview.capabilities.mint(project, artifact, revision, sha256)
        # The fragment never reaches a server. The workspace's origin is the only window
        # the viewer posts anchors to (docs/specs/pdf-viewer.md, PV-6).
        fragment = urlencode({"c": capability, "w": preview.workspace_origin})
        url = f"{preview.origin}/viewer/#{fragment}"
        answer = ViewerLink(url=url, expires_at=expires, path=found.path)
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(mint))
