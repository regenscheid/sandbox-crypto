# SPDX-License-Identifier: MIT
"""Explicit authenticated uploads and fixed-path delivery to the configured worker."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Final
from urllib.parse import quote

from aiohttp import hdrs, web
from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    Identifier,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.uploads import MAX_UPLOAD_BYTES, UserUpload, UserUploads
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .context import (
    CONTEXT,
    WorkspaceContext,
    identifier,
    json_answer,
    json_body,
    no_query,
    session_record,
)

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/uploads"
_IDS: Final = TypeAdapter[list[Identifier]](list[Identifier])


class UploadBody(Contract):
    upload_id: Identifier
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="application/octet-stream", max_length=128)
    content_b64: str = Field(max_length=4 * ((MAX_UPLOAD_BYTES + 2) // 3))


def add_routes(app: web.Application) -> None:
    app.router.add_post(PATH, uploaded)
    app.router.add_get(PATH, listed)
    app.router.add_get(PATH + "/{upload}/content", downloaded)
    app.router.add_post(PATH + "/{upload}/document", document)


async def uploaded(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    found = await asyncio.to_thread(session_record, context.projects, project, session)
    if found.closed_at is not None:
        raise ProjectError(ErrorKind.CONFLICT, "the session is closed")
    raw = await json_body(request, "upload", limit=12 * 1024 * 1024)
    try:
        body = UploadBody.model_validate(raw)
        data = base64.b64decode(body.content_b64, validate=True)
    except (ValidationError, ValueError):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid upload body") from None
    result = await asyncio.to_thread(
        UserUploads(context.writer).create,
        project,
        session,
        body.upload_id,
        body.filename,
        body.media_type,
        data,
    )
    return json_answer(result.model_dump_json())


async def listed(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    after = request.query.get("after", "0")
    if not after.isascii() or not after.isdigit() or len(after) > 18:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid upload cursor")

    def read() -> str:
        session_record(context.projects, project, session)
        return UserUploads(context.documents).list(project, session, int(after)).model_dump_json()

    return json_answer(await asyncio.to_thread(read))


async def downloaded(request: web.Request) -> web.Response:
    context = request.app[CONTEXT]
    project, session, upload = (
        identifier(request, key) for key in ("project", "session", "upload")
    )
    no_query(request, "upload download")
    store = UserUploads(context.documents)
    found = await asyncio.to_thread(store.get, project, session, upload)
    data = await asyncio.to_thread(store.content, project, session, upload)
    return web.Response(
        body=data,
        content_type="application/octet-stream",
        headers={
            hdrs.CONTENT_DISPOSITION: "attachment; filename*=UTF-8''"
            + quote(found.filename, safe=""),
            hdrs.CACHE_CONTROL: "no-store",
        },
    )


async def document(request: web.Request) -> web.Response:
    """Import a small UTF-8 original as a living document, preserving original bytes."""
    context = request.app[CONTEXT]
    project, session, upload = (
        identifier(request, key) for key in ("project", "session", "upload")
    )
    if await json_body(request, "document import") != {}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "document import takes an empty object")

    def create() -> str:
        record = session_record(context.projects, project, session)
        if record.closed_at is not None:
            raise ProjectError(ErrorKind.CONFLICT, "the session is closed")
        store = UserUploads(context.writer)
        found = store.get(project, session, upload)
        if (
            not found.filename.lower().endswith((".md", ".markdown", ".txt", ".tex"))
            or found.size > 1024 * 1024
        ):
            raise ProjectError(
                ErrorKind.UNSUPPORTED, "document import accepts UTF-8 text up to 1 MiB"
            )
        try:
            text = store.content(project, session, upload).decode("utf-8")
        except UnicodeDecodeError:
            raise ProjectError(
                ErrorKind.INVALID_REQUEST, "the original is not UTF-8 text"
            ) from None
        # Operation and name derive from the scoped immutable upload, not a browser path.
        name = "upload-" + found.sha256[:16] + "-" + str(found.number)
        result = context.writer.create(
            project,
            name=name,
            title=found.filename[:200],
            text=text,
            author=USER,
            session_id=session,
            reason=f"Imported original upload {upload}",
            operation_id=name,
        )
        return json.dumps({"document_id": result.document_id})

    return json_answer(await asyncio.to_thread(create))


def attachments(
    context: WorkspaceContext, project: str, session: str, raw: object
) -> list[UserUpload]:
    """Resolve at most eight immutable originals within the message's own session."""
    try:
        ids = _IDS.validate_python(raw, strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid attachment IDs") from None
    if len(ids) > 8 or len(set(ids)) != len(ids):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "attach at most eight distinct files")
    return [UserUploads(context.documents).get(project, session, item) for item in ids]


def message_text(text: str, files: list[UserUpload]) -> str:
    """Durable, byte-pinned manifest in the existing message/answer/archive path."""
    if not files:
        return text
    manifest = [
        {
            "upload_id": f.upload_id,
            "filename": f.filename,
            "path": f.path,
            "sha256": f.sha256,
            "size": f.size,
        }
        for f in files
    ]
    return (
        text.rstrip()
        + "\n\nAttached files (sandbox copies; original bytes retained):\n```json\n"
        + json.dumps(manifest, ensure_ascii=False, indent=2)
        + "\n```"
    )


async def deliver(
    context: WorkspaceContext, project: str, session: str, files: list[UserUpload]
) -> None:
    """Create copies without overwriting research edits; recheck an uncertain receipt."""
    if not files:
        return
    record = await asyncio.to_thread(session_record, context.projects, project, session)
    if record.closed_at is not None:
        raise ProjectError(ErrorKind.CONFLICT, "the session is closed")
    worker = context.worker
    if worker is None:
        raise ProjectError(
            ErrorKind.UNAVAILABLE, "files are stored; connect a worker before sending attachments"
        )
    scope = ExecutionContext(
        project_id=project, session_id=session, environment_id=record.environment_id, actor="user"
    )
    for found in files:
        data = await asyncio.to_thread(
            UserUploads(context.documents).content, project, session, found.upload_id
        )
        try:
            try:
                async with asyncio.timeout(context.timeouts.call):
                    written = await worker.write_file(
                        WriteFileRequest(
                            context=scope,
                            path=found.path,
                            content_b64=base64.b64encode(data).decode(),
                            create_only=True,
                        )
                    )
                size, sha256 = written.size, written.sha256
            except (WorkerAPIError, TimeoutError):
                # A lost acknowledgement or create-only conflict may already contain
                # our exact bytes. An operator client remains required by worker fences.
                async with asyncio.timeout(context.timeouts.call):
                    existing = await worker.read_file(
                        ReadFileRequest(context=scope, path=found.path, length=0)
                    )
                size, sha256 = existing.size, existing.sha256
            if (size, sha256) != (found.size, found.sha256):
                raise DocumentError(
                    "conflict", "sandbox upload copy changed; it was not overwritten"
                )
        except (WorkerAPIError, TimeoutError):
            raise ProjectError(
                ErrorKind.UNAVAILABLE,
                "file retained; sandbox delivery is unavailable, retry the message",
            ) from None
