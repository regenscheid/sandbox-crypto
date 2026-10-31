# SPDX-License-Identifier: MIT
"""File requests and the shared files' sync, for the execution manager.

``Files`` reads, writes, edits, lists and searches files in an environment through its
executor, and checks each reply like an event. A write or an edit whose context names a
delegated run's write area is made only below it or in ``shared/`` (DA-19). A request
for a path under ``shared/`` first syncs the project's shared files (D79); what a sync
finds is told with the results of the calls running there, or held for the session's
next result. The dispatcher runs the sync's steps before and after each call through
``sync``, whose failure never fails the call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, cast

from sandbox_science.contracts import (
    TERMINAL_STATES,
    EditFileRequest,
    EditFileResult,
    ErrorKind,
    ExecutionContext,
    FileContent,
    FileEntry,
    FileListing,
    GlobEntry,
    GlobListing,
    GlobRequest,
    GrepCount,
    GrepRequest,
    GrepResult,
    ListFilesRequest,
    ReadFileRequest,
    StageCheck,
    StageCheckRequest,
    StagePutRequest,
    StagePutResult,
    WriteFileRequest,
    WriteFileResult,
    outside_write_area,
)

from .channel import ExecutorChannel, Message
from .connections import EnvironmentConnections
from .errors import WorkerError
from .journal import ExecutionJournal
from .replies import Range, checked, epoch, integer
from .shared_sync import SharedSync
from .timeouts import ManagerTimeouts

_log = logging.getLogger(__name__)

MAX_HELD_NOTICES = 20
"""Sync notices held for one call's or one session's next result."""
MAX_HELD_BYTES = 16 * 1024
MAX_HELD_SESSIONS = 256
MAX_LIST = 5000
SEARCH_REPLY_TIMEOUT = 60.0
"""Seconds the worker waits for a glob or grep, beyond the executor's own bound on it."""
FILE_TOOLS = 1
"""The ``file_tools`` an executor's ``hello`` names when it edits, globs and greps."""
UNSUPPORTED_FILE_TOOLS = (
    "this environment's executor predates edit_file, glob and grep; recreate the "
    "environment to use them, or use write_file, list_files and the shell meanwhile"
)
UNSYNCED = (
    "shared/ could not be synced with the project's other environments just now; "
    "it is synced again at the next call"
)


def _range_reply(reply: Message, offset: int, length: int, *, hashed: bool) -> Range:
    """Validate an executor read, including an empty EOF range past the file's end."""
    try:
        encoded = reply["content_b64"]
        if not isinstance(encoded, str):
            raise TypeError("content_b64")
        data = base64.b64decode(encoded, validate=True)
        size = integer(reply.get("size"), 0, 2**62)
        start = integer(reply.get("offset"), 0, 2**62)
        sha256 = reply.get("sha256")
        beyond_end = size is not None and offset + len(data) > size
        expected_eof = size is not None and offset + len(data) >= size
        if (
            size is None
            or start != offset
            or len(data) > length
            or (beyond_end and bool(data))
            or reply.get("eof") is not expected_eof
        ):
            raise ValueError("a range that was not asked for")
        if hashed and not (isinstance(sha256, str) and len(sha256) == 64):
            raise ValueError("no SHA-256")
    except (KeyError, TypeError, ValueError) as exc:  # binascii.Error is a ValueError
        raise WorkerError(
            ErrorKind.INTERNAL, f"the executor sent a malformed file reply ({exc})"
        ) from exc
    return Range(
        size=size,
        sha256=sha256 if hashed else None,
        modified=epoch(reply.get("modified")),
        offset=offset,
        data=data,
        content_b64=encoded,
        eof=reply.get("eof") is True,
    )


class Files:
    """File requests in each environment, and the notices its shared-files sync holds."""

    def __init__(
        self,
        connections: EnvironmentConnections,
        journal: ExecutionJournal,
        shared: SharedSync | None,
        *,
        timeouts: ManagerTimeouts,
    ) -> None:
        self.connections = connections
        self.journal = journal
        self.shared = shared
        """Keeps the project's shared files in step across its environments (D79)."""
        self.timeouts = timeouts
        self._held: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        """Notices from syncing shared files for file requests, by environment and session,
        for the session's next result there (D79)."""

    async def _read_range(
        self,
        channel: ExecutorChannel,
        request: ReadFileRequest,
        offset: int,
        length: int,
        *,
        hashed: bool,
    ) -> Range:
        reply = await channel.request(
            "read",
            session=request.context.session_id,
            path=request.path,
            offset=offset,
            length=length,
            hash=hashed,
        )
        return _range_reply(reply, offset, length, hashed=hashed)

    async def read(self, request: ReadFileRequest) -> FileContent:
        async with self.connections.using(request.context.environment_id) as channel:
            await self._sync_for(request.context, request.path, channel)
            found = await self._read_range(
                channel, request, request.offset, request.length, hashed=True
            )
            return checked(
                FileContent,
                path=request.path,
                sha256=found.sha256,
                size=found.size,
                modified_at=found.modified,
                offset=found.offset,
                content_b64=found.content_b64,
                eof=found.eof,
            )

    async def write(self, request: WriteFileRequest) -> WriteFileResult:
        context = request.context
        _in_write_area(context, request.path)
        async with self.connections.using(context.environment_id) as channel:
            fields: dict[str, Any] = {
                "path": request.path,
                "content_b64": request.content_b64,
                "expected_sha256": request.expected_sha256,
                "create_only": request.create_only,
            }
            shared = self.shared
            if shared is not None and in_shared(request.path):
                # The expected SHA-256 is checked against the project's version (D79).
                notices: list[str] = []
                try:
                    reply = await shared.write(
                        context.project_id,
                        context.environment_id,
                        context.session_id,
                        channel,
                        fields,
                        self.timeouts.shared,
                        notices,
                    )
                finally:  # what the sync found is told even when the write is refused
                    self.tell(context, notices)
            else:
                reply = await channel.request("write", session=context.session_id, **fields)
            return checked(WriteFileResult, sha256=reply.get("sha256"), size=reply.get("size"))

    async def edit(self, request: EditFileRequest) -> EditFileResult:
        """Replace a string in a text file, under the rules and the sync a write has."""
        context = request.context
        _in_write_area(context, request.path)
        async with self.connections.using(context.environment_id) as channel:
            _file_tools(channel)
            fields: dict[str, Any] = {
                "path": request.path,
                "old_string": request.old_string,
                "new_string": request.new_string,
                "replace_all": request.replace_all,
                "expected_sha256": request.expected_sha256,
            }
            shared = self.shared
            if shared is not None and in_shared(request.path):
                notices: list[str] = []
                try:
                    reply = await shared.write(
                        context.project_id,
                        context.environment_id,
                        context.session_id,
                        channel,
                        fields,
                        self.timeouts.shared,
                        notices,
                        op="edit",
                    )
                finally:
                    self.tell(context, notices)
            else:
                reply = await channel.request("edit", session=context.session_id, **fields)
            return checked(
                EditFileResult,
                sha256=reply.get("sha256"),
                size=reply.get("size"),
                replacements=reply.get("replacements"),
            )

    async def glob(self, request: GlobRequest) -> GlobListing:
        """The paths below a directory that match a pattern, newest first."""
        context = request.context
        async with self.connections.using(context.environment_id) as channel:
            _file_tools(channel)
            await self._sync_for(context, request.path, channel)
            reply = await channel.request(
                "glob",
                reply_timeout=SEARCH_REPLY_TIMEOUT,
                session=context.session_id,
                path=request.path,
                pattern=request.pattern,
                limit=request.limit,
            )
            entries = [
                checked(
                    GlobEntry,
                    path=item.get("path"),
                    type=item.get("type"),
                    size=item.get("size"),
                    modified_at=epoch(item.get("modified")),
                )
                for item in _objects(reply.get("entries"))[: request.limit]
            ]
            return checked(
                GlobListing,
                path=request.path,
                pattern=request.pattern,
                entries=entries,
                truncated=reply.get("truncated") is True,
                stopped=reply.get("stopped"),
                scanned=reply.get("scanned"),
                skipped=reply.get("skipped", []),
            )

    async def grep(self, request: GrepRequest) -> GrepResult:
        """Search text files for a regular expression, within the executor's bounds."""
        context = request.context
        async with self.connections.using(context.environment_id) as channel:
            _file_tools(channel)
            await self._sync_for(context, request.path, channel)
            reply = await channel.request(
                "grep",
                reply_timeout=SEARCH_REPLY_TIMEOUT,
                session=context.session_id,
                path=request.path,
                pattern=request.pattern,
                glob=request.glob,
                mode=request.mode,
                case_insensitive=request.case_insensitive,
                context_lines=request.context_lines,
                max_results=request.max_results,
            )
            counts = [
                checked(GrepCount, path=item.get("path"), count=item.get("count"))
                for item in _objects(reply.get("counts"))
            ]
            return checked(
                GrepResult,
                path=request.path,
                pattern=request.pattern,
                mode=request.mode,
                files=_listed(reply.get("files")),
                counts=counts,
                lines=_listed(reply.get("lines")),
                truncated=reply.get("truncated") is True,
                stopped=reply.get("stopped"),
                scanned_files=reply.get("scanned_files"),
                scanned_bytes=reply.get("scanned_bytes"),
                skipped_binary=reply.get("skipped_binary", 0),
                skipped_large=reply.get("skipped_large", 0),
                skipped_unreadable=reply.get("skipped_unreadable", 0),
                skipped=reply.get("skipped", []),
            )

    async def stage_check(self, request: StageCheckRequest) -> StageCheck:
        """Whether a skill pack is staged as expected, sealed once it is (SK-16)."""
        async with self.connections.using(request.context.environment_id) as channel:
            reply = await channel.request(
                "skills_check",
                # A fresh executor may need to scan the installed tool inventory.
                # This is bounded but can exceed the ordinary small-tool deadline
                # on a cold disk, particularly in large research images.
                reply_timeout=240.0,
                source=request.source,
                snapshot=request.snapshot,
                files=[file.model_dump(mode="json") for file in request.files],
                seal=request.seal,
                commands=list(request.commands),
                binding_names=list(request.binding_names),
            )
        return checked(
            StageCheck,
            location=reply.get("location"),
            ready=reply.get("ready"),
            sealed=reply.get("sealed", False),
            differing=reply.get("differing", []),
            commands=[c for c in _listed(reply.get("commands")) if c in request.commands],
            inventory=reply.get("inventory", {}),
        )

    async def stage_put(self, request: StagePutRequest) -> StagePutResult:
        """Write a batch of a skill pack's files where its snapshot is staged (SK-16)."""
        async with self.connections.using(request.context.environment_id) as channel:
            reply = await channel.request(
                "skills_put",
                source=request.source,
                snapshot=request.snapshot,
                reset=request.reset,
                files=[file.model_dump(mode="json") for file in request.files],
            )
        return checked(StagePutResult, written=reply.get("written"))

    async def listing(self, request: ListFilesRequest) -> FileListing:
        context = request.context
        async with self.connections.using(context.environment_id) as channel:
            await self._sync_for(context, request.path, channel)
            reply = await channel.request("list", session=context.session_id, path=request.path)
            raw = reply.get("entries")
            every = cast(list[object], raw) if isinstance(raw, list) else []
            entries = every[:MAX_LIST]
            listed: list[FileEntry] = []
            for entry in entries:
                item = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
                listed.append(
                    checked(
                        FileEntry,
                        name=item.get("name"),
                        type=item.get("type"),
                        size=item.get("size"),
                        modified_at=epoch(item.get("modified")),
                    )
                )
            return FileListing(
                path=request.path,
                entries=tuple(listed),
                truncated=reply.get("truncated") is True or len(entries) < len(every),
            )

    async def _sync_for(
        self, context: ExecutionContext, path: str, channel: ExecutorChannel
    ) -> None:
        """Bring the shared files up to date before a file request that reads them (D79)."""
        shared = self.shared
        if shared is None or not in_shared(path):
            return
        self.tell(context, await self.sync(shared.down, context, channel))

    def tell(self, context: ExecutionContext, notices: list[str]) -> None:
        """Pass on what syncing for a file request found (D79).

        The calls running in the environment get it with their results, since a change of
        theirs may be what it is about; the requesting session gets it with its next
        result there, unless one of those calls is its own.
        """
        if not notices:
            return
        told = False
        for live in self.journal.live.values():
            running = live.execution.context
            if (
                live.environment_id == context.environment_id
                and running.project_id == context.project_id  # never another project's
                and live.dispatched
                and live.final is None
                and live.execution.state not in TERMINAL_STATES
            ):
                live.notices = latest([*live.notices, *notices])
                told = told or running.session_id == context.session_id
        if not told:
            key = (context.environment_id, context.session_id)
            held = self._held.pop(key, [])
            self._held[key] = latest([*held, *notices])
            while len(self._held) > MAX_HELD_SESSIONS:
                self._held.popitem(last=False)

    async def sync(
        self,
        step: Callable[[str, str, str, ExecutorChannel], Awaitable[list[str]]],
        context: ExecutionContext,
        channel: ExecutorChannel,
    ) -> list[str]:
        """Run one step of the shared files' sync; its failure never fails the call (D79)."""
        try:
            return await asyncio.wait_for(
                step(context.project_id, context.environment_id, context.session_id, channel),
                self.timeouts.shared,
            )
        except Exception as exc:  # the call goes on with shared/ as it is
            _log.warning(
                "syncing the shared files of %s in %s failed: %s",
                context.project_id,
                context.environment_id,
                exc,
            )
            return [UNSYNCED]

    def take_held(self, context: ExecutionContext) -> list[str]:
        """The notices held for the session's next result in the environment, now told."""
        return self._held.pop((context.environment_id, context.session_id), [])


def _listed(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _objects(value: object) -> list[dict[str, Any]]:
    """A reply's list of objects; anything else in it is an empty object, which the
    contract then refuses."""
    return [cast(dict[str, Any], item) if isinstance(item, dict) else {} for item in _listed(value)]


def _in_write_area(context: ExecutionContext, path: str) -> None:
    """Refuse a delegated run's write outside its write area and ``shared/`` (DA-19),
    before anything reaches the environment."""
    refusal = outside_write_area(context, path)
    if refusal is not None:
        raise WorkerError(ErrorKind.POLICY_DENIED, refusal)


def _file_tools(channel: ExecutorChannel) -> None:
    """Refuse an edit or a search that an older executor cannot run, before sending it."""
    found = channel.hello.get("file_tools")
    if type(found) is not int or found != FILE_TOOLS:
        raise WorkerError(ErrorKind.UNSUPPORTED, UNSUPPORTED_FILE_TOOLS)


def latest(notices: list[str]) -> list[str]:
    """The latest notices, within ``MAX_HELD_NOTICES`` and ``MAX_HELD_BYTES``."""
    kept: list[str] = []
    size = 0
    for notice in reversed(notices[-MAX_HELD_NOTICES:]):
        size += len(notice)
        if size > MAX_HELD_BYTES:
            break
        kept.append(notice)
    return kept[::-1]


def in_shared(path: str) -> bool:
    """Whether a file request's path is in the project's shared area (D79)."""
    normal = posixpath.normpath(path)
    return normal == "shared" or normal.startswith("shared/")
