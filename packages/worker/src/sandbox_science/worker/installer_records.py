# SPDX-License-Identifier: MIT
"""Bounded host-generated installer diagnostics; arbitrary output never becomes an artifact.

These artifacts observe an existing original step. They neither dispatch/retry an
installer nor grant access. Blob threads remain owned through cancellation; SQLite
reference publication stays on the original provisioner event-loop owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, cast

from pydantic import BaseModel

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ArtifactRef,
    Execution,
    ExecutionContext,
    ExecutionState,
    ProvisionOperation,
    ProvisionRequest,
)
from sandbox_science.contracts.provisioning import InstallerLabel, InstallerStep
from sandbox_science.contracts.worker import Result, StreamOutput

from .artifacts import Blobs
from .store import Store

MAX_LOG_BYTES: Final = 64 * 1024
MAX_SCAN_CHARS: Final = 64 * 1024
MAX_LINES: Final = 256
MAX_ACTIVE: Final = 4
MEDIA_TYPE: Final = "application/vnd.sandbox-science.installer-log+json"
LABELS: Final[dict[str, str]] = {
    "version-conda": "micromamba",
    "version-pip": "pip",
    "resolve-conda": "micromamba",
    "resolve-pip": "pip",
    "install-conda": "micromamba",
    "install-pip": "pip",
}
# Full literal lines only. Unknown content, including URLs/paths/JSON/tracebacks,
# is omitted rather than guessing whether a credential detector found every secret.
_MESSAGES: Final = frozenset(
    {
        "Transaction starting",
        "Transaction finished",
        "Preparing transaction: done",
        "Verifying transaction: done",
        "Executing transaction: done",
    }
)
_VERSION: Final = r"[0-9][0-9A-Za-z.!+_-]{0,63}"
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class InstallerLogError(Exception):
    """Fixed local metadata refusal; never retain raw output or exception text."""


def _fields(value: object, kind: type[BaseModel]) -> None:
    if type(value) is not kind:
        raise InstallerLogError("invalid original metadata type")
    data = cast(dict[object, object], object.__getattribute__(value, "__dict__"))
    if (
        type(data) is not dict
        or len(data) != len(kind.model_fields)
        or any(type(key) is not str for key in data)
        or set(data) != set(kind.model_fields)
        or object.__getattribute__(value, "__pydantic_extra__") is not None
    ):
        raise InstallerLogError("invalid original metadata fields")


def _id(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise InstallerLogError("invalid original identity")
    return value


def _date(value: datetime | None) -> str | None:
    if value is None:
        return None
    if type(value) is not datetime or value.utcoffset() is None:
        raise InstallerLogError("invalid original timestamp")
    return value.isoformat()


def _stream(value: StreamOutput | None, name: str) -> dict[str, object]:
    if value is None:
        return {"present": False, "total_bytes": None, "source_truncated": None}
    _fields(value, StreamOutput)
    if (
        type(value.name) is not str
        or value.name != name
        or type(value.text) is not str
        or type(value.total_bytes) is not int
        or not 0 <= value.total_bytes < 2**62
        or type(value.truncated) is not bool
    ):
        raise InstallerLogError("invalid stream metadata")
    head = value.text[:MAX_SCAN_CHARS]
    messages: list[str] = []
    omitted = scanned = 0
    # Avoid splitting an arbitrarily large retained execution output. The bounded
    # prefix and line count are explicit; no full-output hash is computed.
    position = 0
    while position < len(head) and scanned < MAX_LINES:
        end = head.find("\n", position)
        if end < 0:
            end = len(head)
        line = head[position:end]
        position = min(end + 1, len(head))
        scanned += 1
        if line in _MESSAGES:
            messages.append(line)
        else:
            omitted += 1
    return {
        "present": True,
        "total_bytes": value.total_bytes,
        "source_truncated": value.truncated,
        "scanned_characters": position,
        "unscanned_characters": len(value.text) - position,
        "scanned_lines": scanned,
        "omitted_scanned_lines": omitted,
        "fixed_messages": messages,
        "arbitrary_text_retained": False,
    }


def observed_version(execution: Execution, label: str) -> str | None:
    if not label.startswith("version-") or execution.state is not ExecutionState.SUCCEEDED:
        return None
    result = execution.result
    if result is None or result.stdout is None:
        return None
    text = result.stdout.text
    if result.stdout.truncated or len(text) > 4096:
        return None
    # Only the bounded version token is retained, never pip's installation path.
    pattern = (
        _VERSION
        if label == "version-conda"
        else rf"pip ({_VERSION}) from [^\r\n]{{1,2048}} \(python [0-9.]{{1,16}}\)"
    )
    match = re.fullmatch(pattern, text.strip(" \r\n"))
    return (match.group(0) if label == "version-conda" else match.group(1)) if match else None


@dataclass(frozen=True)
class PreparedLog:
    step: InstallerStep
    artifact: ArtifactRef
    raw: bytes
    source_operation: ProvisionOperation = field(repr=False)
    source_execution: Execution = field(repr=False)
    source_label: str = field(repr=False)


def prepare(operation: ProvisionOperation, execution: Execution, label: str) -> PreparedLog:
    if type(label) is not str or label not in LABELS:
        raise InstallerLogError("invalid original step label")
    _fields(operation, ProvisionOperation)
    _fields(operation.request, ProvisionRequest)
    _fields(operation.request.context, ExecutionContext)
    _fields(execution, Execution)
    _fields(execution.context, ExecutionContext)
    provision_id = _id(operation.provision_id)
    for context in (operation.request.context, execution.context):
        for name in ("project_id", "session_id", "environment_id"):
            _id(getattr(context, name))
    if (
        execution.context != operation.request.context
        or type(execution.kind) is not str
        or execution.kind != "shell"
        or _id(execution.origin) != provision_id
        or _id(execution.operation_id) != f"{provision_id}.{label}"
        or type(execution.state) is not ExecutionState
        or execution.state not in TERMINAL_STATES
    ):
        raise InstallerLogError("original execution scope/state differs")
    execution_id, attempt_id = _id(execution.execution_id), _id(execution.attempt_id)
    if type(operation.executions) is not tuple or len(operation.executions) > 128:
        raise InstallerLogError("invalid original execution index")
    for retained in cast(tuple[object, ...], operation.executions):
        _id(retained)
    if execution_id not in operation.executions:
        raise InstallerLogError("execution lacks original operation admission")
    steps = operation.installer_steps
    if type(steps) is not tuple or len(steps) > 8:
        raise InstallerLogError("legacy or invalid original installer intent")
    found: list[InstallerStep] = []
    for step in cast(tuple[InstallerStep, ...], steps):
        _fields(step, InstallerStep)
        if type(step.label) is not str:
            raise InstallerLogError("invalid original installer label")
        if step.label == label:
            _id(step.operation_id)
            _id(step.execution_id)
            _id(step.attempt_id)
            if type(step.log_status) is not str:
                raise InstallerLogError("invalid original installer log status")
            found.append(step)
    if len(found) != 1 or (
        found[0].operation_id != execution.operation_id
        or found[0].execution_id != execution_id
        or found[0].attempt_id != attempt_id
        or found[0].log_status not in ("pending", "stored")
    ):
        raise InstallerLogError("execution differs from original admitted installer step")
    result = execution.result
    if result is not None:
        _fields(result, Result)
        for output in (result.stdout, result.stderr):
            if output is not None:
                _fields(output, StreamOutput)
        if (
            result.exit_code is not None
            and (type(result.exit_code) is not int or not -(2**31) <= result.exit_code < 2**31)
        ) or (
            result.signal is not None
            and (type(result.signal) is not int or not 0 <= result.signal < 2**31)
        ):
            raise InstallerLogError("invalid terminal numeric metadata")
        if (
            (type(result.elapsed_seconds) is not int and type(result.elapsed_seconds) is not float)
            or not math.isfinite(result.elapsed_seconds)
            or not 0 <= result.elapsed_seconds < 2**31
        ):
            raise InstallerLogError("invalid terminal timing")
    # A version observation is useful only as its exact bounded token. Even fixed
    # diagnostic lines are omitted for that command to avoid copying its raw path.
    streams = {
        name: _stream(getattr(result, name) if result is not None else None, name)
        for name in ("stdout", "stderr")
    }
    version = observed_version(execution, label)
    if type(execution.submitted_at) is not datetime:
        raise InstallerLogError("invalid original submission time")
    value = {
        "format": "sandbox-science-installer-log",
        "version": 1,
        "provision_id": provision_id,
        "request_operation_id": _id(operation.request.operation_id),
        "step_operation_id": execution.operation_id,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "project_id": operation.request.context.project_id,
        "session_id": operation.request.context.session_id,
        "environment_id": operation.request.context.environment_id,
        "installer": LABELS[label],
        "label": label,
        "observed_version": version,
        "execution_state": execution.state.value,
        "submitted_at": _date(execution.submitted_at),
        "started_at": _date(execution.started_at),
        "ended_at": _date(execution.ended_at),
        "exit_code": result.exit_code if result else None,
        "signal": result.signal if result else None,
        "elapsed_seconds": result.elapsed_seconds if result else None,
        "streams": streams,
        "sanitization": "fixed-literal-messages-only-v1",
        "complete_raw_log": False,
        "raw_output_hashed": False,
    }
    raw = (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    if len(raw) > MAX_LOG_BYTES:
        raise InstallerLogError("generated log exceeds bound")
    digest = hashlib.sha256(raw).hexdigest()
    artifact = ArtifactRef(
        artifact_id="art-inst-"
        + hashlib.sha256((provision_id + "\0" + execution_id).encode("ascii")).hexdigest()[:32],
        project_id=operation.request.context.project_id,
        sha256=digest,
        size=len(raw),
        media_type=MEDIA_TYPE,
        source_path=f"provisioning/{provision_id}/{label}.json",
        produced_by=None,
        created_at=execution.ended_at or execution.started_at or execution.submitted_at,
    )
    return PreparedLog(
        InstallerStep(
            label=cast(InstallerLabel, label),
            installer="micromamba" if label.endswith("conda") else "pip",
            operation_id=execution.operation_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            execution_state=execution.state,
            observed_version=version,
            log_status="stored",
            log=artifact,
        ),
        artifact,
        raw,
        operation,
        execution,
        label,
    )


def checked_prepared(value: PreparedLog) -> PreparedLog:
    """Reform an independently owned safe projection before any publication await.

    A PreparedLog is host metadata, not an authentication token. Its caller cannot
    substitute arbitrary bytes/fields and still use this artifact producer.
    """
    if type(value) is not PreparedLog or type(value.raw) is not bytes:
        raise InstallerLogError("invalid prepared log type")
    if not 0 < len(value.raw) <= MAX_LOG_BYTES:
        raise InstallerLogError("prepared log exceeds bounds")
    fresh = prepare(value.source_operation, value.source_execution, value.source_label)
    _fields(value.artifact, ArtifactRef)
    artifact = value.artifact
    if (
        any(
            type(item) is not str or len(item) > 256
            for item in (
                artifact.artifact_id,
                artifact.project_id,
                artifact.sha256,
                artifact.media_type,
                artifact.source_path,
            )
        )
        or type(artifact.size) is not int
        or not 0 < artifact.size <= MAX_LOG_BYTES
        or artifact.produced_by is not None
        or type(artifact.created_at) is not datetime
    ):
        raise InstallerLogError("invalid prepared artifact fields")
    _date(artifact.created_at)
    _fields(value.step, InstallerStep)
    step = value.step
    if (
        any(
            type(item) is not str or len(item) > 128
            for item in (
                step.label,
                step.installer,
                step.operation_id,
                step.execution_id,
                step.attempt_id,
                step.log_status,
            )
        )
        or type(step.execution_state) is not ExecutionState
        or (
            step.observed_version is not None
            and (type(step.observed_version) is not str or len(step.observed_version) > 64)
        )
        or step.unavailable_reason is not None
        or step.log is not artifact
    ):
        raise InstallerLogError("invalid prepared step fields")
    if value.raw != fresh.raw or artifact != fresh.artifact or step != fresh.step:
        raise InstallerLogError("prepared log differs from safe original projection")
    return fresh


async def owned[T](
    work: asyncio.Future[T] | asyncio.Task[T],
) -> tuple[T, asyncio.CancelledError | None]:
    first = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            first = first or error
        except BaseException:
            if first is not None:
                raise first from None
            raise
    if first is not None and work.cancelled():
        raise first
    try:
        return work.result(), first
    except BaseException:
        if first is not None:
            raise first from None
        raise


class InstallerArtifacts:
    """Four actual blob owners at most; the provisioner owns SQLite references."""

    def __init__(self, store: Store, blobs: Blobs, max_artifact_size: int) -> None:
        self.store, self.blobs = store, blobs
        self.limit = min(MAX_LOG_BYTES, max_artifact_size)
        self._work: set[asyncio.Task[InstallerStep]] = set()
        self._closing = False

    def _blob(self, prepared: PreparedLog, existing: bool) -> None:
        if existing:
            fd = os.open(
                self.blobs.path(prepared.artifact.sha256),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != len(prepared.raw):
                    raise InstallerLogError("retained log blob is unavailable")
                raw = os.pread(fd, MAX_LOG_BYTES + 1, 0)
                if raw != prepared.raw:
                    raise InstallerLogError("retained log blob changed")
            finally:
                os.close(fd)
            return
        writer = self.blobs.writer()
        try:
            writer.write(prepared.raw)
            if not writer.commit(prepared.artifact.sha256):
                raise InstallerLogError("generated blob commit refused")
        finally:
            writer.discard()

    def _existing(self, prepared: PreparedLog) -> ArtifactRef | None:
        try:
            return self.store.installer_artifact(prepared.artifact)
        except (ValueError, RuntimeError, sqlite3.Error):
            raise InstallerLogError("retained artifact reference unavailable") from None

    async def _publish(self, prepared: PreparedLog) -> InstallerStep:
        old = self._existing(prepared)
        thread = asyncio.create_task(asyncio.to_thread(self._blob, prepared, old is not None))
        try:
            _, interrupted = await owned(thread)
            # This whole owner includes SQLite reference publication. A caller may
            # finish its separate operation update only after this reference is durable.
            self.store.installer_artifact(prepared.artifact, insert=True)
            if interrupted is not None:
                raise interrupted
            return prepared.step
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise InstallerLogError("artifact retention unavailable") from None

    async def publish(
        self, prepared: PreparedLog
    ) -> tuple[InstallerStep, asyncio.CancelledError | None]:
        if self._closing or len(self._work) >= MAX_ACTIVE:
            raise InstallerLogError("log size/admission bound exceeded")
        prepared = checked_prepared(prepared)
        if len(prepared.raw) > self.limit:
            raise InstallerLogError("log size/admission bound exceeded")
        task = asyncio.create_task(self._publish(prepared))
        self._work.add(task)
        try:
            return await owned(task)
        finally:
            self._work.discard(task)

    async def close(self) -> None:
        self._closing = True
        if self._work:
            _, interrupted = await owned(
                asyncio.ensure_future(asyncio.gather(*tuple(self._work), return_exceptions=True))
            )
            if interrupted is not None:
                raise interrupted
