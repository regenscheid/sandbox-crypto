# SPDX-License-Identifier: MIT
"""Small observational provisioning records; no permission or effect authority.

Only metadata from saved operation transitions crosses this writer boundary.
Two databases cannot provide atomic coverage here: an absent event stays unknown,
and a failed/lost append is never retried by this producer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, cast, get_args

from pydantic import BaseModel

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    ExecutionContext,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
)
from sandbox_science.contracts.provisioning import Reason, Stage
from sandbox_science.event_store import format_time

from .installer_records import owned

MAX_BYTES: Final = 4096
MAX_ACTIVE: Final = 8
KINDS: Final = frozenset(
    {"provision.requested", "provision.awaiting_permission", "provision.finished"}
)
_STAGES: Final = frozenset(get_args(Stage))
_REASONS: Final = frozenset(get_args(Reason))
_STATES: Final = frozenset(x.value for x in ProvisionState)
_OUTCOMES: Final = frozenset(x.value for x in ProvisionOutcome)
_ERRORS: Final = frozenset(x.value for x in ErrorKind)
_STAGE: Final = {
    ProvisionState.QUEUED: "queue",
    ProvisionState.CHECKING: "check",
    ProvisionState.RESOLVING: "resolve",
    ProvisionState.AWAITING_PERMISSION: "permission",
    ProvisionState.INSTALLING: "link",
    ProvisionState.PROBING: "probe",
}
_KEYS: Final = frozenset(
    {
        "format",
        "version",
        "event_id",
        "provision_id",
        "request_operation_id",
        "context",
        "operation_seq",
        "state",
        "stage",
        "outcome",
        "reason",
        "error_kind",
        "permission_request",
        "revision_planned",
        "revision_before",
        "revision_after",
        "execution_count",
        "file_count",
        "probe_count",
        "submitted_at",
        "started_at",
        "ended_at",
        "trigger",
        "authority",
    }
)
_CONTEXT: Final = frozenset({"project_id", "session_id", "environment_id"})


class ObservationError(ValueError):
    """Fixed invalid metadata refusal; no original text in diagnostics."""


def _fields(value: object, kind: type[BaseModel]) -> None:
    if type(value) is not kind:
        raise ObservationError("invalid observation source type")
    fields = cast(dict[object, object], object.__getattribute__(value, "__dict__"))
    if (
        type(fields) is not dict
        or len(fields) != len(kind.model_fields)
        or any(type(k) is not str for k in fields)
        or set(fields) != set(kind.model_fields)
        or object.__getattribute__(value, "__pydantic_extra__") is not None
    ):
        raise ObservationError("invalid observation source fields")


def _id(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise ObservationError("invalid observation identity")
    return value


def _integer(value: object, maximum: int = 2**63 - 1, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ObservationError("invalid observation integer")
    return value


def _choice(value: object, choices: frozenset[str]) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 64 or value not in choices:
        raise ObservationError("invalid observation enum")
    return value


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if type(value) is not datetime or value.utcoffset() is None:
        raise ObservationError("invalid observation source time")
    return format_time(value)


def _event_id(kind: str, provision_id: str, sequence: int) -> str:
    return hashlib.sha256(
        f"sandbox-provision-observation-1\0{kind}\0{provision_id}\0{sequence}".encode("ascii")
    ).hexdigest()


def checked_payload(kind: str, value: object) -> dict[str, object]:
    """Copy only exact bounded primitives; no generic/unbounded JSON serialization."""
    if type(kind) is not str or kind not in KINDS:
        raise ObservationError("unsupported observation kind")
    if type(value) is not dict:
        raise ObservationError("invalid observation payload type")
    payload = cast(dict[str, object], value)
    if (
        len(payload) != len(_KEYS)
        or any(type(k) is not str for k in payload)
        or set(payload) != set(_KEYS)
    ):
        raise ObservationError("invalid observation payload fields")
    if (
        type(payload["version"]) is not int
        or payload["version"] != 1
        or type(payload["format"]) is not str
        or payload["format"] != "sandbox-provision-observation"
        or type(payload["authority"]) is not str
        or payload["authority"] != "observation-only"
    ):
        raise ObservationError("unsupported observation format")
    context = cast(dict[str, object], payload["context"])
    if (
        type(context) is not dict
        or len(context) != 3
        or any(type(k) is not str for k in context)
        or set(context) != set(_CONTEXT)
    ):
        raise ObservationError("invalid observation context")
    copied_context = {key: _id(context[key]) for key in _CONTEXT}
    provision = _id(payload["provision_id"])
    _id(payload["request_operation_id"])
    sequence = _integer(payload["operation_seq"])
    if (
        type(payload["event_id"]) is not str
        or len(payload["event_id"]) != 64
        or payload["event_id"] != _event_id(kind, provision, sequence)
    ):
        raise ObservationError("invalid observation event identity")
    for key, values in (
        ("state", _STATES),
        ("stage", _STAGES),
        ("outcome", _OUTCOMES),
        ("reason", _REASONS),
        ("error_kind", _ERRORS),
    ):
        _choice(payload[key], values)
    if payload["state"] is None:
        raise ObservationError("missing observation state")
    for name in ("revision_planned", "revision_before", "revision_after", "permission_request"):
        if payload[name] is not None:
            _integer(payload[name], minimum=1 if name == "permission_request" else 0)
    for name, limit in (("execution_count", 128), ("file_count", 2000), ("probe_count", 64)):
        _integer(payload[name], limit)
    for name in ("submitted_at", "started_at", "ended_at"):
        value = payload[name]
        if value is not None:
            if (
                type(value) is not str
                or re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value
                )
                is None
            ):
                raise ObservationError("invalid observation time")
            try:
                datetime.fromisoformat(value)
            except ValueError:
                raise ObservationError("invalid observation time") from None
    if payload["submitted_at"] is None:
        raise ObservationError("missing original submission time")
    trigger = _choice(payload["trigger"], frozenset({"transition", "startup_classification"}))
    if trigger is None or (trigger == "startup_classification" and kind != "provision.finished"):
        raise ObservationError("invalid observation trigger")
    expected = {
        "provision.requested": "queued",
        "provision.awaiting_permission": "awaiting_permission",
        "provision.finished": "finished",
    }[kind]
    if payload["state"] != expected:
        raise ObservationError("observation differs from source transition")
    if kind == "provision.awaiting_permission" and (
        payload["outcome"] != "awaiting_permission" or payload["permission_request"] is None
    ):
        raise ObservationError("missing original permission observation")
    if kind == "provision.finished" and payload["outcome"] in (None, "awaiting_permission"):
        raise ObservationError("missing original terminal observation")
    result = dict(payload)
    result["context"] = copied_context
    if (
        len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        > MAX_BYTES
    ):
        raise ObservationError("observation exceeds byte bound")
    return result


def project(record: ProvisionOperation, kind: str, *, startup: bool = False) -> dict[str, object]:
    """Project already-saved source facts; never serialize the full operation."""
    if type(startup) is not bool:
        raise ObservationError("invalid observation trigger type")
    _fields(record, ProvisionOperation)
    _fields(record.request, ProvisionRequest)
    _fields(record.request.context, ExecutionContext)
    if type(record.state) is not ProvisionState or (
        record.outcome is not None and type(record.outcome) is not ProvisionOutcome
    ):
        raise ObservationError("invalid source state/outcome")
    error_kind = None
    if record.error is not None:
        _fields(record.error, ErrorInfo)
        if type(record.error.kind) is not ErrorKind:
            raise ObservationError("invalid source error kind")
        error_kind = record.error.kind.value
    for value, maximum in ((record.executions, 128), (record.files, 2000), (record.probes, 64)):
        if type(value) is not tuple or len(value) > maximum:
            raise ObservationError("invalid observation source count")
    provision = _id(record.provision_id)
    sequence = _integer(record.seq)
    if type(kind) is not str or kind not in KINDS:
        raise ObservationError("unsupported observation kind")
    return checked_payload(
        kind,
        {
            "format": "sandbox-provision-observation",
            "version": 1,
            "event_id": _event_id(kind, provision, sequence),
            "provision_id": provision,
            "request_operation_id": _id(record.request.operation_id),
            "context": {key: _id(getattr(record.request.context, key)) for key in _CONTEXT},
            "operation_seq": sequence,
            "state": record.state.value,
            "stage": record.stage if record.stage is not None else _STAGE.get(record.state),
            "outcome": record.outcome.value if record.outcome is not None else None,
            "reason": record.reason,
            "error_kind": error_kind,
            "permission_request": record.permission_request,
            "revision_planned": record.planned_revision,
            "revision_before": record.revision_before,
            "revision_after": record.revision_after,
            "execution_count": len(record.executions),
            "file_count": len(record.files),
            "probe_count": len(record.probes),
            "submitted_at": _time(record.submitted_at),
            "started_at": _time(record.started_at),
            "ended_at": _time(record.ended_at),
            "trigger": "startup_classification" if startup else "transition",
            "authority": "observation-only",
        },
    )


class ObservationWriter(Protocol):
    def observation(self, kind: str, payload: Mapping[str, object]) -> int: ...


@dataclass(frozen=True)
class Delivery:
    status: Literal["stored", "unknown", "unavailable", "disabled"]
    sequence: int | None = None


class ProvisionEvents:
    """Own bounded append threads; borrow the configured writer without closing it."""

    def __init__(self, writer: ObservationWriter | None) -> None:
        self.writer = writer
        self._work: set[asyncio.Task[Delivery]] = set()
        self._closing = False

    def _append(self, kind: str, payload: dict[str, object]) -> Delivery:
        if self.writer is None:
            return Delivery("disabled")
        try:
            sequence = self.writer.observation(kind, payload)
            return Delivery("stored", _integer(sequence, minimum=1))
        except Exception:
            # An append can have committed before a lost ACK. No retry/inference.
            return Delivery("unknown")

    async def emit(
        self, record: ProvisionOperation, kind: str, *, startup: bool = False
    ) -> tuple[Delivery, asyncio.CancelledError | None]:
        if self.writer is None:
            return Delivery("disabled"), None
        if self._closing or len(self._work) >= MAX_ACTIVE:
            return Delivery("unavailable"), None
        try:
            payload = project(record, kind, startup=startup)
        except Exception:
            return Delivery("unavailable"), None
        task = asyncio.create_task(asyncio.to_thread(self._append, kind, payload))
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
