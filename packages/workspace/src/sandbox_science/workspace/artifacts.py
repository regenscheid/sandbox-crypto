# SPDX-License-Identifier: MIT
"""A project's published artifacts, and links that open a PDF in the viewer.

Routes (docs/specs/pdf-viewer.md, PV-4):

- ``GET /api/v0/projects/{project}/artifacts?before=N&group=results|files&session=S&run=R
  &contains=TEXT`` lists the project's artifacts, each at its latest revision, the most
  recently changed first, ten at a time: published and declared results, the other
  recorded files, or both, of a session or a run, with a path that has ``TEXT`` (research
  artifacts, RA-37); and says whether the viewer is on.
- ``GET /api/v0/projects/{project}/artifacts/{artifact}/revisions?before=N`` lists one
  artifact's revisions, the newest first, twenty at a time, tombstones included (RA-37).
- ``GET /api/v0/projects/{project}/artifacts/{artifact}/revisions/{revision}/viewer
  ?sha256=HEX`` mints a capability for that exact revision of a PDF and answers the viewer's
  URL on the preview origin, which opens it for 10 minutes (PV-3).
- ``GET …/artifacts/{artifact}/revisions/{revision}/figure?sha256=HEX`` mints one for that
  exact revision of a figure file of at most 8 MiB and says how a page shows it
  (docs/specs/figures.md, FG-12 to FG-14): a PNG or a JPEG in an ``<img>`` from the preview
  origin; an SVG in the figure frame, which is given the capability; an HTML file as the
  page of a frame without scripts; a PDF in the viewer's figure mode. Any other file is
  shown as a link.
- ``GET /api/v0/projects/{project}/figures/chart?data=ARTIFACT:REVISION:SHA256`` (each data
  file a chart pins, in its order, at most 16) answers the figure frame's URL and a
  capability for each file, a CSV, TSV or JSON file of at most 8 MiB, 8 MiB in all, for
  the frame to read the chart's data (FG-15, FG-17).

All read through the orchestrator's ``ArtifactInventory``; none writes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from urllib.parse import urlencode

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactLink, ArtifactRevision
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.projects import ProjectError

from . import api, artifact_downloads
from .context import CONTEXT, identifier, json_answer
from .preview import CAPABILITY_SECONDS, DATA, HTML, IMAGES, MAX_IMAGE_BYTES, PDF, SVG, Preview

PREVIEW: Final = web.AppKey("preview", Preview)
_IDENTIFIER: Final = TypeAdapter[str](Identifier)


class ArtifactEntry(Contract):
    """An artifact at its latest revision, and who made that revision."""

    artifact_id: str
    revision: int
    path: str
    sha256: str
    size: int
    media_type: str
    created_at: datetime
    result: bool = False
    """It has a published or declared revision."""
    deleted: bool = False
    content_retained: bool = True
    content_note: str | None = None
    """Why the bytes were not kept: ``too_large`` or ``quota``."""
    kind: str = "agent_publication"
    """How the revision came: published, recorded from a call, or declared a result."""
    session_id: str
    run_id: str
    role: str = "main"
    tool_call: str


class RevisionListing(Contract):
    revisions: list[ArtifactEntry]
    next_before: int | None
    """The next page's cursor, a revision number; ``None`` at the end."""


class ArtifactListing(Contract):
    artifacts: list[ArtifactEntry]
    next_before: int | None
    """The next page's cursor; ``None`` at the end."""
    viewer: bool
    """Whether the PDF viewer's origin is on."""


class ViewerLink(Contract):
    """The viewer's URL for one PDF revision, and when it stops opening it."""

    url: str
    expires_at: datetime
    path: str
    """Where the PDF was published from, which an anchor into it names."""


class FigureLink(Contract):
    """How a page shows a pinned figure file, and when its capability stops opening it:
    ``image``, an ``<img>`` of ``url``; ``svg``, the figure frame at ``url``, posted
    ``capability``; ``html``, a frame without scripts whose page is ``url``; ``pdf``, the
    viewer's figure mode at ``url``."""

    kind: Literal["image", "svg", "html", "pdf"]
    url: str
    capability: str | None = None
    expires_at: datetime
    media_type: str


class ChartLink(Contract):
    """The figure frame's URL for a chart, and a capability for each of its data files, in
    the order asked, until ``expires_at``."""

    url: str
    capabilities: list[str]
    expires_at: datetime


MAX_CHART_FILES: Final = 16
_PINNED: Final = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,127}):([1-9][0-9]{0,8}):([0-9a-f]{64})")


def add_routes(app: web.Application) -> None:
    artifact_downloads.add_routes(app)
    base = "/api/v0/projects/{project}/artifacts"
    app.router.add_get(base, listing)
    app.router.add_get(base + "/{artifact}/revisions", revisions)
    app.router.add_get(base + "/{artifact}/revisions/{revision}/viewer", viewer)
    app.router.add_get(base + "/{artifact}/revisions/{revision}/figure", figure)
    app.router.add_get("/api/v0/projects/{project}/figures/chart", chart)


async def listing(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    group = request.query.get("group")
    if group not in (None, "results", "files"):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "group is results or files")
    raw_before = request.query.get("before", "")
    if raw_before and not (raw_before.isascii() and raw_before.isdigit() and len(raw_before) < 19):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid artifact cursor")
    before = int(raw_before) if raw_before else None
    contains = request.query.get("contains") or None
    if contains is not None and (
        len(contains) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in contains)
    ):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid path filter")
    scope: dict[str, str | None] = {}
    for name in ("session", "run"):
        value = request.query.get(name)
        if value:
            try:
                _IDENTIFIER.validate_python(value, strict=True)
            except ValidationError:
                raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {name}") from None
        scope[name] = value or None
    context = request.app[CONTEXT]
    on = request.app.get(PREVIEW) is not None

    def read() -> str:
        context.projects.get(project)
        inventory = ArtifactInventory(context.documents)
        found_entries, next_before = inventory.recent(
            project,
            before=before,
            group=group,
            session=scope["session"],
            run=scope["run"],
            contains=contains,
        )
        results = inventory.results(project, [e.reference.artifact_id for e in found_entries])
        entries = [_entry(found, found.reference.artifact_id in results) for found in found_entries]
        answer = ArtifactListing(artifacts=entries, next_before=next_before, viewer=on)
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


def _entry(found: ArtifactRevision, result: bool) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=found.reference.artifact_id,
        revision=found.reference.revision,
        path=found.path,
        sha256=found.reference.sha256,
        size=found.reference.size,
        media_type=found.reference.media_type,
        created_at=found.created_at,
        result=result,
        deleted=found.deleted,
        content_retained=found.content_retained,
        content_note=found.content_note,
        kind=found.publication.kind,
        session_id=found.publication.context.session_id,
        run_id=found.publication.run_id,
        role=found.publication.role,
        tool_call=found.publication.tool_call,
    )


async def revisions(request: web.Request) -> web.Response:
    """One artifact's revisions, the newest first, twenty at a time (RA-37)."""
    project = identifier(request, "project")
    artifact = identifier(request, "artifact")
    raw_before = request.query.get("before", "")
    if raw_before and not (raw_before.isascii() and raw_before.isdigit() and len(raw_before) < 10):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid revision cursor")
    before = int(raw_before) if raw_before else None
    context = request.app[CONTEXT]

    def read() -> str:
        context.projects.get(project)
        inventory = ArtifactInventory(context.documents)
        found, next_before = inventory.history(project, artifact, before=before)
        result = artifact in inventory.results(project, [artifact])
        answer = RevisionListing(
            revisions=[_entry(revision, result) for revision in found], next_before=next_before
        )
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


def _pinned(request: web.Request, off: str) -> tuple[str, str, int, str, Preview]:
    """The project, artifact, revision and SHA-256 a minting route names, and the preview
    origin, which must be on (``off`` says what is off without it)."""
    project = identifier(request, "project")
    artifact = identifier(request, "artifact")
    raw_revision, sha256 = request.match_info["revision"], request.query.get("sha256", "")
    if not (raw_revision.isascii() and raw_revision.isdigit()) or not (
        1 <= int(raw_revision) <= 999999999
    ):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid artifact revision")
    if len(sha256) != 64 or not set(sha256) <= set("0123456789abcdef"):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid sha256")
    preview = request.app.get(PREVIEW)
    if preview is None or not preview.origin:
        raise ProjectError(ErrorKind.UNAVAILABLE, f"{off} is off on this server")
    return project, artifact, int(raw_revision), sha256, preview


def _resolve(request: web.Request, project: str, artifact: str, revision: int, sha256: str):
    context = request.app[CONTEXT]
    context.projects.get(project)
    link = ArtifactLink(
        artifact_id=artifact, project_id=project, mode="pinned", revision=revision, sha256=sha256
    )
    return ArtifactInventory(context.documents).resolve(project, link)


async def figure(request: web.Request) -> web.Response:
    """How a page shows a figure file (FG-12 to FG-14), within the size a page shows; the
    preview origin checks its first bytes and, for an image, its header."""
    project, artifact, revision, sha256, preview = _pinned(request, "the preview origin")

    def mint() -> str:
        found = _resolve(request, project, artifact, revision, sha256)
        media = found.reference.media_type
        if media not in (*IMAGES, SVG, HTML, PDF):
            raise ProjectError(ErrorKind.UNSUPPORTED, "this file is shown as a link")
        if found.reference.size > MAX_IMAGE_BYTES:
            raise ProjectError(ErrorKind.UNSUPPORTED, "this file is larger than a page shows")
        expires = datetime.now(UTC) + timedelta(seconds=CAPABILITY_SECONDS)
        purpose = "pdf" if media == PDF else "file"
        capability = preview.capabilities.mint(project, artifact, revision, sha256, purpose)
        file = f"{preview.origin}/file/{capability}"
        given: str | None = None
        if media in IMAGES:
            kind, url = "image", file
        elif media == HTML:
            kind, url = "html", file
        elif media == SVG:
            # The frame takes the capability by message, from the workspace's window only.
            kind, given = "svg", capability
            url = f"{preview.origin}/figures/#{urlencode({'w': preview.workspace_origin})}"
        else:
            fragment = urlencode({"c": capability, "w": preview.workspace_origin, "m": "figure"})
            kind, url = "pdf", f"{preview.origin}/viewer/#{fragment}"
        answer = FigureLink(
            kind=kind, url=url, capability=given, expires_at=expires, media_type=media
        )
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(mint))


async def chart(request: web.Request) -> web.Response:
    """The figure frame for a chart and its data files' capabilities (FG-15, FG-17)."""
    project = identifier(request, "project")
    named = request.query.getall("data", [])
    if set(request.query) - {"data"} or len(named) > MAX_CHART_FILES:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, f"a chart names at most {MAX_CHART_FILES} data files"
        )
    pinned = [_PINNED.fullmatch(item) for item in named]
    if any(found is None for found in pinned):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "data is ARTIFACT:REVISION:SHA256")
    preview = request.app.get(PREVIEW)
    if preview is None or not preview.origin:
        raise ProjectError(ErrorKind.UNAVAILABLE, "the preview origin is off on this server")

    def mint() -> str:
        context = request.app[CONTEXT]
        context.projects.get(project)
        inventory = ArtifactInventory(context.documents)
        total = 0
        capabilities: list[str] = []
        for found in pinned:
            assert found is not None  # noqa: S101 - checked above
            artifact, revision, sha256 = found.group(1), int(found.group(2)), found.group(3)
            link = ArtifactLink(
                artifact_id=artifact, project_id=project, mode="pinned", revision=revision,
                sha256=sha256,
            )  # fmt: skip
            reference = inventory.resolve(project, link).reference
            if reference.media_type not in DATA:
                raise ProjectError(ErrorKind.UNSUPPORTED, "a chart reads CSV, TSV or JSON files")
            total += reference.size
            if total > MAX_IMAGE_BYTES:
                raise ProjectError(ErrorKind.UNSUPPORTED, "the chart's data pass 8 MiB")
            capabilities.append(
                preview.capabilities.mint(project, artifact, revision, sha256, "file")
            )
        expires = datetime.now(UTC) + timedelta(seconds=CAPABILITY_SECONDS)
        url = f"{preview.origin}/figures/#{urlencode({'w': preview.workspace_origin})}"
        answer = ChartLink(url=url, capabilities=capabilities, expires_at=expires)
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(mint))


async def viewer(request: web.Request) -> web.Response:
    project, artifact, revision, sha256, preview = _pinned(request, "the PDF viewer")

    def mint() -> str:
        found = _resolve(request, project, artifact, revision, sha256)
        if found.reference.media_type != PDF:
            raise ProjectError(ErrorKind.UNSUPPORTED, "the viewer opens PDFs only")
        # The capability counts its ten minutes on a monotonic clock; the time of day is
        # read first, so the time shown is never later than the real expiry.
        expires = datetime.now(UTC) + timedelta(seconds=CAPABILITY_SECONDS)
        capability = preview.capabilities.mint(project, artifact, revision, sha256, "pdf")
        # The fragment never reaches a server. The workspace's origin is the only window
        # the viewer posts anchors to (docs/specs/pdf-viewer.md, PV-6).
        fragment = urlencode({"c": capability, "w": preview.workspace_origin})
        url = f"{preview.origin}/viewer/#{fragment}"
        answer = ViewerLink(url=url, expires_at=expires, path=found.path)
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(mint))
