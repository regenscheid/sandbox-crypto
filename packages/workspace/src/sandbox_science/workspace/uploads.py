# SPDX-License-Identifier: MIT
"""Explicit authenticated uploads and fixed-path delivery to the configured worker."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import Callable
from typing import Final
from urllib.parse import quote, unquote

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
from sandbox_science.orchestrator.aio import held
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.projects import ProjectError, ensure_environment
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
from .errors import TooLargeError
from .identity import author

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/uploads"
_IDS: Final = TypeAdapter[list[Identifier]](list[Identifier])
_CHUNK: Final = 512 * 1024


class UploadMetadata(Contract):
    upload_id: Identifier
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="application/octet-stream", max_length=128)


class UploadBody(UploadMetadata):
    # Old JSON clients remain compatible without materializing 133 MiB JSON bodies.
    content_b64: str = Field(max_length=4 * ((8 * 1024 * 1024 + 2) // 3))


def add_routes(app: web.Application) -> None:
    app.router.add_post(PATH, uploaded)
    app.router.add_get(PATH, listed)
    app.router.add_get(PATH + "/{upload}/content", downloaded)
    app.router.add_post(PATH + "/{upload}/document", document)


async def uploaded(request: web.Request) -> web.Response:
    context = request.app[CONTEXT]
    try:
        async with asyncio.timeout(context.timeouts.upload), context.uploads:
            return await _uploaded(request)
    except TimeoutError:
        raise ProjectError(ErrorKind.UNAVAILABLE, "upload timed out; retry the file") from None


async def _uploaded(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    found = await _offload(session_record, context.projects, project, session)
    if found.closed_at is not None:
        raise ProjectError(ErrorKind.CONFLICT, "the session is closed")
    if request.query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "uploads take no query parameters")
    try:
        async with asyncio.timeout(context.timeouts.upload):
            if request.content_type == "application/octet-stream":
                metadata = request.headers.get("X-Upload-Metadata", "")
                if len(metadata) > 2048:
                    raise ValueError("upload metadata too large")
                body = UploadMetadata.model_validate_json(unquote(metadata, errors="strict"))
                return await _binary_upload(request, body, project, session)
            raw = await json_body(request, "upload", limit=12 * 1024 * 1024)
            legacy = UploadBody.model_validate(raw)
            body = legacy
            data = base64.b64decode(legacy.content_b64, validate=True)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("legacy JSON upload exceeds 8 MiB; use binary upload")
    except (ValidationError, ValueError):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid upload body") from None
    except TimeoutError:
        raise ProjectError(ErrorKind.UNAVAILABLE, "upload timed out; retry the file") from None
    result = await _offload(
        UserUploads(context.writer).create,
        project,
        session,
        body.upload_id,
        body.filename,
        body.media_type,
        data,
    )
    return json_answer(result.model_dump_json())


async def _binary_upload(
    request: web.Request, metadata: UploadMetadata, project: str, session: str
) -> web.Response:
    """Admit before reading, and own staged bytes through cancellation and commit."""
    if request.headers.get(hdrs.CONTENT_ENCODING, "identity") != "identity":
        raise ProjectError(ErrorKind.INVALID_REQUEST, "encoded upload bodies are unsupported")
    if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
        raise TooLargeError("upload exceeds its byte limit")
    store = UserUploads(request.app[CONTEXT].writer)
    writer, cancelled = await held(
        asyncio.to_thread(
            store.begin,
            project,
            session,
            metadata.upload_id,
            metadata.filename,
            metadata.media_type,
            expected_size=request.content_length,
        )
    )
    try:
        if cancelled:
            raise asyncio.CancelledError
        size = 0
        async for chunk in request.content.iter_chunked(_CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise TooLargeError("upload exceeds its byte limit")
            await _offload(writer.write, chunk)
        result = await _offload(writer.commit)
        return json_answer(result.model_dump_json())
    finally:
        await _offload(writer.discard)


async def listed(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    after = request.query.get("after", "0")
    if not after.isascii() or not after.isdigit() or len(after) > 18:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid upload cursor")

    def read() -> str:
        session_record(context.projects, project, session)
        return UserUploads(context.documents).list(project, session, int(after)).model_dump_json()

    return json_answer(await _offload(read))


async def downloaded(request: web.Request) -> web.StreamResponse:
    context = request.app[CONTEXT]
    project, session, upload = (
        identifier(request, key) for key in ("project", "session", "upload")
    )
    no_query(request, "upload download")
    response: web.StreamResponse | None = None
    try:
        async with asyncio.timeout(context.timeouts.upload), context.uploads:
            store = UserUploads(context.documents)
            found = await _offload(store.get, project, session, upload)
            manager = store.open_content(project, session, upload)
            stream, cancelled = await held(asyncio.to_thread(manager.__enter__))
            try:
                if cancelled:
                    raise asyncio.CancelledError
                response = web.StreamResponse(
                    headers={
                        hdrs.CONTENT_TYPE: "application/octet-stream",
                        hdrs.CONTENT_LENGTH: str(found.size),
                        hdrs.CONTENT_DISPOSITION: "attachment; filename*=UTF-8''"
                        + quote(found.filename, safe=""),
                        hdrs.CACHE_CONTROL: "no-store",
                        "X-Content-SHA256": found.sha256,
                    }
                )
                await response.prepare(request)
                if request.method != hdrs.METH_HEAD:
                    remaining = found.size
                    while remaining:
                        chunk = await _offload(stream.read, min(_CHUNK, remaining))
                        if not chunk:
                            raise OSError("upload was truncated while streaming")
                        await response.write(chunk)
                        remaining -= len(chunk)
                await response.write_eof()
            finally:
                await _offload(manager.__exit__, None, None, None)
            return response
    except (OSError, TimeoutError, asyncio.CancelledError) as error:
        if response is None or not response.prepared:
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ProjectError(
                ErrorKind.UNAVAILABLE, "download timed out; retry the file"
            ) from None
        # Once headers were sent, an error JSON response would corrupt the download.
        # Close the incomplete body so Content-Length exposes the truncation to clients.
        response.force_close()
        if request.transport is not None:
            request.transport.close()
        if isinstance(error, asyncio.CancelledError):
            raise
        return response


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
            author=author(request),
            session_id=session,
            reason=f"Imported original upload {upload}",
            operation_id=name,
        )
        return json.dumps({"document_id": result.document_id})

    return json_answer(await _offload(create))


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


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def deliver(
    context: WorkspaceContext, project: str, session: str, files: list[UserUpload]
) -> None:
    """Create copies without overwriting research edits; recheck an uncertain receipt."""
    if not files:
        return
    async with asyncio.timeout(context.timeouts.upload), context.uploads:
        await _deliver(context, project, session, files)


async def _deliver(
    context: WorkspaceContext, project: str, session: str, files: list[UserUpload]
) -> None:
    if not files:
        return
    record = await _offload(session_record, context.projects, project, session)
    if record.closed_at is not None:
        raise ProjectError(ErrorKind.CONFLICT, "the session is closed")
    worker = context.worker
    if worker is None:
        raise ProjectError(
            ErrorKind.UNAVAILABLE, "files are stored; connect a worker before sending attachments"
        )
    # A new session is lazy: no run has prepared its environment yet. Delivery
    # precedes message admission, so it must do that preparation itself.
    configured = await _offload(context.projects.get, project)
    try:
        await ensure_environment(worker, configured, record.environment_id)
    except (WorkerAPIError, TimeoutError) as error:
        if isinstance(error, WorkerAPIError) and error.kind is ErrorKind.UNSUPPORTED:
            raise ProjectError(
                ErrorKind.UNSUPPORTED,
                "file retained; update the incompatible worker before retrying the message",
            ) from None
        raise ProjectError(
            ErrorKind.UNAVAILABLE,
            "file retained; sandbox preparation is unavailable, retry the message",
        ) from None
    scope = ExecutionContext(
        project_id=project, session_id=session, environment_id=record.environment_id, actor="user"
    )
    for found in files:
        data = await _offload(
            UserUploads(context.documents).content, project, session, found.upload_id
        )
        try:
            try:
                async with asyncio.timeout(context.timeouts.upload):
                    written = await worker.write_file(
                        WriteFileRequest(
                            context=scope,
                            path=found.path,
                            content_b64=await _offload(_encoded, data),
                            create_only=True,
                        )
                    )
                size, sha256 = written.size, written.sha256
            except (WorkerAPIError, TimeoutError) as error:
                if isinstance(error, WorkerAPIError) and error.kind is ErrorKind.UNSUPPORTED:
                    raise ProjectError(
                        ErrorKind.UNSUPPORTED,
                        "file retained; this environment cannot accept this upload; "
                        "update the worker and recreate the environment before retrying",
                    ) from None
                # A lost acknowledgement or create-only conflict may already contain
                # our exact bytes. An operator client remains required by worker fences.
                async with asyncio.timeout(context.timeouts.upload):
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


async def _offload[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Keep admission until the actual thread ends, then propagate caller cancellation."""
    work = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    interrupted: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            interrupted = interrupted or error
        except BaseException:
            if interrupted is None:
                raise
            break
    if interrupted is not None:
        with contextlib.suppress(BaseException):
            work.result()
        raise interrupted
    return work.result()
