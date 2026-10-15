# SPDX-License-Identifier: MIT
"""Every file a call writes, recorded as an artifact revision by itself (research
artifacts, RA-27 to RA-35).

After a call ends, the recorder takes the files the worker reported written and
deleted, leaves out the project's ignore patterns, and records each one: its bytes up
to 8 MiB while the project's budget allows, otherwise its hash and size; a deleted
artifact gets a tombstone. One call records at most 100 files and 64 MiB of bytes; what
it leaves out, and why, is reported with the call. A project that records results only
records nothing here. The worker reported the files written while the call ran; that
is what a revision says, never that the call wrote them (RA-34).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import posixpath
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from sandbox_science.contracts import (
    Execution,
    ExecutionState,
    FileEntry,
    ListFilesRequest,
    PublishRequest,
    ReadFileRequest,
)
from sandbox_science.contracts.artifact_inventory import (
    ArtifactPublicationSource,
    ArtifactRevisionRef,
    artifact_path,
)

from .artifact_publisher import ArtifactPublisher
from .documents.errors import DocumentError
from .file_settings import IgnoreRules, ProjectFiles
from .hooks import TurnHook
from .worker_client import WorkerAPIError

_log = logging.getLogger(__name__)

MAX_FILES: Final = 100
"""Files one call records (RA-30)."""
MAX_BYTES: Final = 64 * 1024 * 1024
"""Bytes one call records (RA-30)."""
MAX_HASHED: Final = 4 * 1024**3
"""The largest file hashed; a larger one is a gap (RA-31)."""
MAX_LISTED: Final = 200
"""Recorded files a run keeps the list of (RA-36)."""
_TERMINAL: Final = {
    ExecutionState.SUCCEEDED: "succeeded",
    ExecutionState.FAILED: "failed",
    ExecutionState.TIMED_OUT: "timed_out",
    ExecutionState.CANCELLED: "cancelled",
    ExecutionState.LOST: "lost",
}


@dataclass(frozen=True)
class Written:
    """The files a call changed, as the worker reported them."""

    paths: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    incomplete: bool = False
    """The worker's lists were cut short: some files may be missing from them."""
    execution: Execution | None = None

    @classmethod
    def of(cls, execution: Execution) -> Written:
        result = execution.result
        if result is None:
            return cls(execution=execution)
        return cls(
            result.written_files,
            result.deleted_files,
            result.written_files_truncated or result.deleted_files_truncated,
            execution,
        )


@dataclass
class Recorded:
    """What one call's files became: revisions, tombstones and the files left out."""

    files: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    left_out: Counter[str] = field(default_factory=Counter[str])
    """Files not recorded, by reason."""
    incomplete: bool = False

    def summary(self) -> dict[str, Any] | None:
        """The call's report for the model and its record; ``None`` when nothing happened."""
        if not self.files and not self.left_out and not self.incomplete:
            return None
        report: dict[str, Any] = {
            "recorded": sum(1 for f in self.files if not f["deleted"]),
            "deleted": sum(1 for f in self.files if f["deleted"]),
            "without_content": sum(1 for f in self.files if not f["content_retained"]),
        }
        if self.left_out:
            report["left_out"] = dict(self.left_out)
        if self.incomplete:
            report["incomplete"] = "the worker's list of changed files was cut short"
        return report


class FileRecorder:
    """Records a run's calls' files through its artifact publisher (RA-27)."""

    def __init__(
        self,
        publisher: ArtifactPublisher,
        settings: ProjectFiles,
        *,
        role: str = "main",
        parent_run: str | None = None,
        delegation: str | None = None,
    ) -> None:
        self.publisher, self.settings = publisher, settings
        self.client, self.context = publisher.client, publisher.context
        self.role, self.parent_run, self.delegation = role, parent_run, delegation
        self.files: list[dict[str, Any]] = []
        """Up to ``MAX_LISTED`` of the run's recorded files, for its record (RA-36)."""
        self.total = 0

    async def record(self, written: Written, *, tool_call: str, model: str | None) -> Recorded:
        """Record one call's files; failures leave gaps and never fail the call."""
        recorded = Recorded(incomplete=written.incomplete)
        if not written.paths and not written.deleted:
            return recorded
        try:
            settings = self.settings.get(self.context.project_id)
        except DocumentError:
            _log.exception("the project's file settings could not be read")
            recorded.left_out["settings unavailable"] += len(written.paths) + len(written.deleted)
            return recorded
        if settings.recording == "results":
            return recorded
        rules = IgnoreRules(settings.ignore)
        execution = written.execution
        attribution: dict[str, Any] = {
            "kind": "execution_record",
            "role": self.role,
            "parent_run": self.parent_run,
            "delegation": self.delegation,
        }
        if execution is not None:
            attribution["execution_id"] = execution.execution_id
            attribution["attempt_state"] = _TERMINAL.get(execution.state)
        source = self.publisher.source(tool_call, model, **attribution)
        paths = [p for p in written.paths if not rules.ignores(p)]
        sizes = await self._sizes(paths)
        budget = MAX_BYTES
        for index, path in enumerate(paths):
            if len(recorded.files) >= MAX_FILES:
                recorded.left_out["over the 100-file limit of a call"] += len(paths) - index
                break
            size = sizes.get(path)
            if size is None:
                recorded.left_out["gone, or not a regular file, when recorded"] += 1
                continue
            if size > MAX_HASHED:
                recorded.left_out["larger than 4 GiB"] += 1
                continue
            try:
                room = await self.publisher.room(size)
                if room == "full":
                    recorded.left_out["the project's artifact storage is full"] += (
                        len(paths) - index
                    )
                    break
                if room == "content" and size > budget:
                    recorded.left_out["over the 64 MiB limit of a call"] += 1
                    continue
                limit = self.publisher.inventory.limits.artifact_bytes
                entry = await (
                    self._content(path, execution, tool_call, model, attribution)
                    if room == "content"
                    else self._metadata(source, path, "too_large" if size > limit else "quota")
                )
            except (WorkerAPIError, DocumentError, ValueError) as error:
                _log.warning("recording %s failed: %s", path, error)
                recorded.left_out["could not be recorded"] += 1
                continue
            if entry is None:
                recorded.left_out["could not be recorded"] += 1
                continue
            budget -= size if entry["content_retained"] else 0
            recorded.files.append(entry)
        for path in written.deleted:
            if rules.ignores(path):
                continue
            try:
                reference = await self.publisher.record_deletion(source, path)
            except (DocumentError, ValueError) as error:
                _log.warning("recording the deletion of %s failed: %s", path, error)
                recorded.left_out["could not be recorded"] += 1
                continue
            if reference is not None:
                recorded.files.append(_entry(path, reference, deleted=True))
        self.total += len(recorded.files)
        self.files.extend(recorded.files[: MAX_LISTED - len(self.files)])
        return recorded

    async def _sizes(self, paths: Sequence[str]) -> dict[str, int]:
        """Each regular file's size, from one listing of each directory."""
        sizes: dict[str, int] = {}
        for directory in sorted({posixpath.dirname(p) or "." for p in paths[: MAX_FILES * 2]}):
            try:
                listing = await self.client.list_files(
                    ListFilesRequest(context=self.context, path=directory)
                )
            except (WorkerAPIError, ValueError):
                continue
            entries: dict[str, FileEntry] = {e.name: e for e in listing.entries}
            for path in paths:
                if (posixpath.dirname(path) or ".") != directory:
                    continue
                entry = entries.get(posixpath.basename(path))
                if entry is not None and entry.type == "file":
                    sizes[path] = entry.size
        return sizes

    async def _content(
        self,
        path: str,
        execution: Execution | None,
        tool_call: str,
        model: str | None,
        attribution: dict[str, Any],
    ) -> dict[str, Any] | None:
        artifact_path(path)
        key = f"{execution.execution_id if execution else tool_call}\0{path}"
        operation = "rec-" + hashlib.sha256(key.encode()).hexdigest()[:40]
        outcome = await self.publisher.publish(
            PublishRequest(context=self.context, path=path),
            operation,
            tool_call=tool_call,
            model=model,
            attribution=attribution,
        )
        receipt = outcome.receipt
        if receipt.state != "recorded" or receipt.revision is None:
            _log.warning("recording %s was held: %s", path, receipt.message)
            return None
        return _entry(path, receipt.revision, reproduced=receipt.reproduced)

    async def _metadata(
        self,
        source: ArtifactPublicationSource,
        path: str,
        note: Literal["too_large", "quota"],
    ) -> dict[str, Any]:
        artifact_path(path)
        content = await self.client.read_file(
            ReadFileRequest(context=self.context, path=path, length=0)
        )
        media = mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"
        reference, reproduced = await self.publisher.record_metadata(
            source, path, sha256=content.sha256, size=content.size, media_type=media, note=note
        )
        return _entry(path, reference, reproduced=reproduced, retained=False)


def _entry(
    path: str,
    reference: ArtifactRevisionRef,
    *,
    reproduced: bool = False,
    retained: bool = True,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "path": path,
        "artifact_id": reference.artifact_id,
        "revision": reference.revision,
        "sha256": reference.sha256,
        "size": reference.size,
        "content_retained": retained and not deleted,
        "deleted": deleted,
        "reproduced": reproduced,
    }


class RecordedFiles(TurnHook):
    """Keeps the run's recorded files in its record, under ``files`` (RA-36)."""

    name = "files"

    def __init__(self, recorder: FileRecorder) -> None:
        self.recorder = recorder

    def summary(self) -> object | None:
        if not self.recorder.total:
            return None
        return {"total": self.recorder.total, "files": list(self.recorder.files)}


def with_recorded(text: str, report: dict[str, Any]) -> str:
    """A tool result with the report of its recorded files beside it."""
    import json

    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return json.dumps({**value, "recorded_files": report}, ensure_ascii=False)
    return f"{text}\n[recorded files: {json.dumps(report, ensure_ascii=False)}]"
