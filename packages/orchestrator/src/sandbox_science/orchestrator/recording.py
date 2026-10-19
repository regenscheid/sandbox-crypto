# SPDX-License-Identifier: MIT
"""Every file a call writes, recorded as an artifact revision by itself (research
artifacts, RA-27 to RA-35).

After a call ends, the recorder takes the files the worker reported written and
deleted, leaves out the project's ignore patterns, and records each one: its bytes up
to 1 GiB while the project's budget allows, otherwise its hash and size; a deleted
artifact gets a tombstone. One call records at most 100 files and 2 GiB of bytes; what
it leaves out, and why, is reported with the call. A project that records results only
records nothing here. The worker reported the files written while the call ran; that
is what a revision says, never that the call wrote them (RA-34).
"""

from __future__ import annotations

import asyncio
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
    ArtifactReceipt,
    ArtifactRevisionRef,
    artifact_path,
)

from .artifact_publisher import ArtifactPublisher
from .artifacts import ExecutionReplay
from .documents.errors import DocumentError
from .file_settings import IgnoreRules, ProjectFiles
from .hooks import TurnHook
from .worker_client import WorkerAPIError

_log = logging.getLogger(__name__)

MAX_FILES: Final = 100
"""Files one call records (RA-30)."""
MAX_BYTES: Final = 2 * 1024**3
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
    failed: str | None = None
    """Why recording stopped part way; the files recorded before stay."""

    def summary(self) -> dict[str, Any] | None:
        """The call's report for the model and its record; ``None`` when nothing happened."""
        if not self.files and not self.left_out and not self.incomplete and not self.failed:
            return None
        report: dict[str, Any] = {
            "recorded": sum(1 for f in self.files if not f["deleted"]),
            "deleted": sum(1 for f in self.files if f["deleted"]),
            "without_content": sum(
                1 for f in self.files if not f["deleted"] and not f["content_retained"]
            ),
        }
        if self.left_out:
            report["left_out"] = dict(self.left_out)
        if self.incomplete:
            report["incomplete"] = "the worker's list of changed files was cut short"
        if self.failed:
            report["failed"] = self.failed
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
        self.gaps: Counter[str] = Counter()
        """The calls' gaps, by reason: files left out, lists the worker cut short
        (``incomplete``) and recordings that stopped part way (``failed``)."""

    async def record(
        self,
        written: Written,
        *,
        tool_call: str,
        model: str | None,
        operation: str | None = None,
    ) -> Recorded:
        """Record one call's files; failures leave gaps and never fail the call, and the
        files recorded before a failure stay recorded and reported. ``operation`` keys a
        call without an execution, such as ``write_file`` (the harness's operation ID)."""
        recorded = Recorded(incomplete=written.incomplete)
        if not written.paths and not written.deleted:
            return recorded
        try:
            await self._record(written, recorded, tool_call, model, operation)
        except Exception as error:  # recording never costs the call its result
            _log.exception("recording the files of call %s stopped", tool_call)
            recorded.failed = f"recording stopped part way ({type(error).__name__})"
        finally:
            self.total += len(recorded.files)
            self.files.extend(recorded.files[: max(0, MAX_LISTED - len(self.files))])
            self.gaps.update(recorded.left_out)
            self.gaps.update(
                ["incomplete"] * recorded.incomplete + ["failed"] * bool(recorded.failed)
            )
        return recorded

    async def _record(
        self,
        written: Written,
        recorded: Recorded,
        tool_call: str,
        model: str | None,
        operation: str | None,
    ) -> None:
        try:
            settings = self.settings.get(self.context.project_id)
        except DocumentError:
            _log.exception("the project's file settings could not be read")
            recorded.left_out["settings unavailable"] += len(written.paths) + len(written.deleted)
            return
        if settings.recording == "results":
            return
        rules = IgnoreRules(settings.ignore)
        # Matching is bounded, but a thousand paths are still kept off the event loop.
        paths, unusable = await asyncio.to_thread(_kept, rules, written.paths)
        gone, unusable_gone = await asyncio.to_thread(_kept, rules, written.deleted)
        if unusable + unusable_gone:
            recorded.left_out["a name no artifact can have"] += unusable + unusable_gone
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
        key = execution.execution_id if execution is not None else operation or tool_call
        prior: dict[str, ExecutionReplay] = {}
        if execution is not None:
            for path in paths[:MAX_FILES]:
                operation_id = self._content_operation(key, path)
                try:
                    receipt = await self.publisher.prior_execution(operation_id, path, source)
                except (DocumentError, ValueError) as error:
                    _log.warning("recording %s could not reuse its capture: %s", path, error)
                    receipt = ExecutionReplay(
                        ArtifactReceipt(
                            state="held",
                            operation_id=operation_id,
                            message="existing execution capture has a different identity",
                        )
                    )
                if receipt is not None:
                    prior[path] = receipt
        sizes, unlisted = await self._sizes([path for path in paths if path not in prior])
        budget = MAX_BYTES
        limit = self.publisher.inventory.limits.artifact_bytes
        attempts = 0
        for index, path in enumerate(paths):
            if attempts >= MAX_FILES:
                recorded.left_out["over the 100-file limit of a call"] += len(paths) - index
                break
            attempts += 1  # every file tried counts, recorded or not
            replay = prior.get(path)
            if replay is not None:
                if replay.resume_original_transfer:
                    try:
                        entry = await self._content(
                            path, key, tool_call, model, attribution, budget
                        )
                    except (WorkerAPIError, DocumentError, ValueError) as error:
                        _log.warning("resuming recorded transfer %s failed: %s", path, error)
                        recorded.left_out["could not be recorded"] += 1
                        continue
                    if entry is None:
                        recorded.left_out["original transfer remains held"] += 1
                    else:
                        if entry["content_retained"]:
                            budget -= entry["size"]
                        recorded.files.append(entry)
                    continue
                receipt = replay.receipt
                if receipt.state == "recorded" and receipt.revision is not None:
                    entry = _entry(
                        path,
                        receipt.revision,
                        reproduced=receipt.reproduced,
                        retained=replay.content_retained,
                        deleted=replay.deleted,
                    )
                    if entry["content_retained"]:
                        budget = max(0, budget - entry["size"])
                    recorded.files.append(entry)
                elif receipt.state != "failed":
                    recorded.left_out[receipt.message or "execution capture unavailable"] += 1
                continue
            size = sizes.get(path)
            if size is None:
                recorded.left_out[unlisted.get(path, "gone, or not a regular file")] += 1
                continue
            if size > MAX_HASHED:
                recorded.left_out["larger than 4 GiB"] += 1
                continue
            try:
                room = await self.publisher.room(size, requires_operation=execution is not None)
                if room == "full":
                    recorded.left_out["the project's artifact storage is full"] += (
                        len(paths) - index
                    )
                    break
                if room == "content" and size > budget:
                    room = "metadata"
                entry = await (
                    self._content(path, key, tool_call, model, attribution, budget)
                    if room == "content"
                    else self._metadata(source, key, path, "too_large" if size > limit else "quota")
                )
            except (WorkerAPIError, DocumentError, ValueError) as error:
                _log.warning("recording %s failed: %s", path, error)
                recorded.left_out["could not be recorded"] += 1
                continue
            if entry is None:
                recorded.left_out["could not be recorded"] += 1
                continue
            if entry["content_retained"]:
                budget -= entry["size"]  # the bytes kept, which the transfer bounded
            recorded.files.append(entry)
        for index, path in enumerate(gone):
            if attempts >= MAX_FILES:
                recorded.left_out["over the 100-file limit of a call"] += len(gone) - index
                break
            attempts += 1
            try:
                entry = await self._deletion(source, key, path)
            except (DocumentError, ValueError) as error:
                _log.warning("recording the deletion of %s failed: %s", path, error)
                recorded.left_out["could not be recorded"] += 1
                continue
            if entry is not None:
                recorded.files.append(entry)

    async def _sizes(self, paths: Sequence[str]) -> tuple[dict[str, int], dict[str, str]]:
        """Each regular file's size, from one listing of each directory, and why the
        others have none."""
        sizes: dict[str, int] = {}
        unlisted: dict[str, str] = {}
        for directory in sorted({posixpath.dirname(p) or "." for p in paths[: MAX_FILES * 2]}):
            here = [p for p in paths if (posixpath.dirname(p) or ".") == directory]
            try:
                listing = await self.client.list_files(
                    ListFilesRequest(context=self.context, path=directory)
                )
            except (WorkerAPIError, ValueError) as error:
                _log.warning("listing %s failed: %s", directory, error)
                unlisted.update(dict.fromkeys(here, "its directory could not be listed"))
                continue
            entries: dict[str, FileEntry] = {e.name: e for e in listing.entries}
            for path in here:
                entry = entries.get(posixpath.basename(path))
                if entry is not None and entry.type == "file":
                    sizes[path] = entry.size
                elif entry is None and listing.truncated:
                    unlisted[path] = "its directory's listing was cut short"
        return sizes, unlisted

    async def _content(
        self,
        path: str,
        key: str,
        tool_call: str,
        model: str | None,
        attribution: dict[str, Any],
        budget: int,
    ) -> dict[str, Any] | None:
        artifact_path(path)
        operation = self._content_operation(key, path)
        outcome = await self.publisher.publish(
            PublishRequest(context=self.context, path=path),
            operation,
            tool_call=tool_call,
            model=model,
            attribution=attribution,
            max_bytes=budget,  # a grown file cannot pass the call's limit
        )
        receipt = outcome.receipt
        if receipt.state != "recorded" and "publisher binding" in receipt.message:
            source = self.publisher.source(tool_call, model, **attribution)
            replay = await self.publisher.prior_execution(operation, path, source)
            if replay is not None:
                receipt = replay.receipt
        if receipt.state != "recorded" or receipt.revision is None:
            _log.warning("recording %s was held: %s", path, receipt.message)
            return None
        return _entry(
            path,
            receipt.revision,
            reproduced=receipt.reproduced,
            retained=receipt.worker_snapshot is not None,
        )

    @staticmethod
    def _content_operation(key: str, path: str) -> str:
        return "rec-" + hashlib.sha256(f"{key}\0{path}".encode()).hexdigest()[:40]

    async def _metadata(
        self,
        source: ArtifactPublicationSource,
        key: str,
        path: str,
        note: Literal["too_large", "quota"],
    ) -> dict[str, Any] | None:
        artifact_path(path)
        operation = self._content_operation(key, path)
        media = mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"
        if source.execution_id is not None:
            try:
                state = await self.publisher.begin_execution_record(source, operation, path, media)
            except DocumentError:
                # Another recorder may have completed the same execution while we
                # were checking capacity. The lookup verifies the saved identity.
                replay = await self.publisher.prior_execution(operation, path, source)
                if replay is None:
                    raise
                return _replayed_entry(path, replay)
            if not state.fresh:
                replay = await self.publisher.prior_execution(operation, path, source)
                if replay is not None and replay.receipt.state == "recorded":
                    return _replayed_entry(path, replay)
                _log.warning("recording %s was held: original metadata intent is unfinished", path)
                return None
        content = await self.client.read_file(
            ReadFileRequest(context=self.context, path=path, length=0)
        )
        if source.execution_id is None:
            reference, reproduced = await self.publisher.record_metadata(
                source, path, sha256=content.sha256, size=content.size, media_type=media, note=note
            )
            return _entry(path, reference, reproduced=reproduced, retained=False)
        receipt = await self.publisher.finish_execution_metadata(
            source,
            operation,
            path,
            sha256=content.sha256,
            size=content.size,
            media_type=media,
            note=note,
        )
        assert receipt.revision is not None  # noqa: S101 - finish commits a revision
        return _entry(path, receipt.revision, reproduced=receipt.reproduced, retained=False)

    async def _deletion(
        self, source: ArtifactPublicationSource, key: str, path: str
    ) -> dict[str, Any] | None:
        artifact_path(path)
        if source.execution_id is None:
            reference = await self.publisher.record_deletion(source, path)
            return None if reference is None else _entry(path, reference, deleted=True)
        operation = self._content_operation(key, path)
        replay = await self.publisher.prior_execution(operation, path, source)
        if replay is not None:
            receipt = replay.receipt
            if receipt.state == "recorded" and receipt.revision is not None:
                return _entry(
                    path,
                    receipt.revision,
                    reproduced=receipt.reproduced,
                    retained=replay.content_retained,
                    deleted=replay.deleted,
                )
            if receipt.state == "failed":
                return None
            _log.warning("recording deletion %s was held: %s", path, receipt.message)
            return None
        try:
            state = await self.publisher.begin_execution_record(source, operation, path, None)
        except DocumentError:
            replay = await self.publisher.prior_execution(operation, path, source)
            if replay is None:
                raise
            return _replayed_entry(path, replay)
        if not state.fresh:
            replay = await self.publisher.prior_execution(operation, path, source)
            return None if replay is None else _replayed_entry(path, replay)
        receipt = await self.publisher.finish_execution_deletion(source, operation, path)
        return None if receipt.revision is None else _entry(path, receipt.revision, deleted=True)


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


def _replayed_entry(path: str, replay: ExecutionReplay) -> dict[str, Any] | None:
    receipt = replay.receipt
    if receipt.state != "recorded" or receipt.revision is None:
        return None
    return _entry(
        path,
        receipt.revision,
        reproduced=receipt.reproduced,
        retained=replay.content_retained,
        deleted=replay.deleted,
    )


class RecordedFiles(TurnHook):
    """Keeps the run's recorded files in its record, under ``files`` (RA-36)."""

    name = "files"

    def __init__(self, recorder: FileRecorder) -> None:
        self.recorder = recorder

    def summary(self) -> object | None:
        recorder = self.recorder
        if not recorder.total and not recorder.gaps:
            return None
        found: dict[str, Any] = {"total": recorder.total, "files": list(recorder.files)}
        if recorder.gaps:
            found["gaps"] = dict(recorder.gaps)
        return found


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


def _kept(rules: IgnoreRules, paths: Sequence[str]) -> tuple[list[str], int]:
    """The paths the patterns keep, and how many have a name nothing can match."""
    kept: list[str] = []
    unusable = 0
    for path in paths:
        try:
            if not rules.ignores(path):
                kept.append(path)
        except ValueError:
            unusable += 1
    return kept, unusable
