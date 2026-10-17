# SPDX-License-Identifier: MIT
"""Explicit operator decisions about unknown outcomes; never dispatch or infer success."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Annotated, Literal, Self, cast

from pydantic import Field, model_validator

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract
from sandbox_science.contracts.model import Revision
from sandbox_science.orchestrator.control import ControlError, ControlStore, Tx
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import READ_BYTES, ToolRecord
from sandbox_science.orchestrator.projects import ModelUsage, RunName
from sandbox_science.orchestrator.runstate import LEASE_SECONDS

from .models import observed

Kind = Literal[
    "model_usage", "model_reservation", "fold_estimate_link", "operation", "kernel_recompute"
]


class Resolution(Contract):
    work: RunName
    current: RunName
    epoch: Revision
    run: RunName
    kind: Kind
    identity: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2000)
    cost: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] | None = None

    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    source: Literal["collection", "summary", "event"] | None = None
    position: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    event_seq: int | None = Field(default=None, strict=True, ge=1, le=999_999_999)
    estimate_json: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _explicit(self) -> Self:
        if not self.reason.strip():
            raise ValueError("operator reason must contain text")
        if (self.kind in {"model_usage", "model_reservation"}) != (self.cost is not None):
            raise ValueError("model decisions require explicit cost; other kinds have no cost")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("input and output tokens must both be supplied or both unknown")
        if self.kind not in {"model_usage", "model_reservation"} and self.input_tokens is not None:
            raise ValueError("only model decisions accept tokens")
        if self.kind == "model_reservation" and (
            self.cost is None
            or self.cost <= 0
            or self.input_tokens is None
            or self.output_tokens is None
            or self.input_tokens <= 0
            or self.output_tokens <= 0
        ):
            raise ValueError("reservation requires positive cost and input/output upper bounds")
        if self.kind == "kernel_recompute" and (
            re.fullmatch(r"[0-9a-f]{64}", self.identity) is None or self.run != self.current
        ):
            raise ValueError("kernel recompute requires the saved snapshot digest and current run")
        if self.kind == "fold_estimate_link":
            if (
                self.source is None
                or self.position is None
                or self.estimate_json is None
                or (self.source == "event") != (self.event_seq is not None)
            ):
                raise ValueError("fold estimate link requires an exact source, position and copy")
            try:
                raw: object = json.loads(self.estimate_json)
            except ValueError:
                raise ValueError("fold estimate link copy is not JSON") from None
            if not isinstance(raw, dict):
                raise ValueError("fold estimate link copy is not an object")
            copied = cast(dict[str, object], raw)
            input_tokens = copied.get("input_tokens")
            if (
                copied.get("cancelled") is not True
                or copied.get("estimated") is not True
                or copied.get("cost") is not None
                or copied.get("intent_id") is not None
                or type(input_tokens) is not int
                or input_tokens <= 0
            ):
                raise ValueError("fold estimate link copy is not an unlinked cancelled estimate")
        elif any(
            value is not None
            for value in (self.source, self.position, self.event_seq, self.estimate_json)
        ):
            raise ValueError("only fold estimate links name a stored copy")
        return self


class Decision(Contract):
    resolution: Resolution
    message_id: str
    operator: str
    observed: float


class RecoveryControls:
    def __init__(
        self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def resolve(
        self, project: str, session: str, request: Resolution, *, operator: str
    ) -> Decision:
        """One atomic decision, exact intent settlement and visible archive message."""
        # Public callers may pass a model_copy/constructed value; retain the same
        # bounds as the authenticated JSON endpoint before settling uncertainty.
        request = Resolution.model_validate(request.model_dump())
        if not operator or len(operator) > 128:
            raise ValueError("operator identity is required")
        now = self.clock.now().timestamp()
        identity = hashlib.sha256(json.dumps([request.run, request.identity]).encode()).hexdigest()
        key = (project, session, request.work, "resolution:" + request.kind, identity)
        with self.control.write() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT w.current,w.state,w.parent,s.epoch FROM research_work w "
                "JOIN research_sessions s USING(project,session) "
                "WHERE w.project=? AND w.session=? AND w.work=?",
                (project, session, request.work),
            ).fetchone()
            if row is None:
                raise ControlError(NOT_FOUND, "no research work in this scope")
            current, state, parent, epoch = row
            if parent is not None:
                original_parent = db.execute(
                    "SELECT parent FROM delegations WHERE project_id=? AND session_id=? AND run=?",
                    (project, session, request.run),
                ).fetchone()
                if original_parent is None:
                    raise ControlError(CONFLICT, "child attempt has no original parent")
                parent = original_parent[0]
            if current != request.current or epoch != request.epoch:
                raise ControlError(CONFLICT, "research attempt or epoch changed")
            if state not in {"paused", "blocked", "waiting"}:
                raise ControlError(
                    CONFLICT, "only inactive unfinished research work can be resolved"
                )
            if db.execute(
                "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? AND renewed_at>? "
                "UNION ALL SELECT 1 FROM research_work WHERE project=? AND session=? "
                "AND state='running' "
                "UNION ALL SELECT 1 FROM research_resumes WHERE project=? AND session=? "
                "AND status IN ('queued','running') LIMIT 1",
                (project, session, now - LEASE_SECONDS, project, session, project, session),
            ).fetchone():
                raise ControlError(CONFLICT, "research is active or Resume is pending")
            attempt = db.execute(
                "SELECT 1 FROM research_attempts WHERE project=? AND session=? "
                "AND work=? AND run=?",
                (project, session, request.work, request.run),
            ).fetchone()
            if attempt is None:
                raise ControlError(NOT_FOUND, "original attempt is outside this work")
            existing = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind=? AND identity=?",
                key,
            ).fetchone()
            if existing:
                decision = Decision.model_validate_json(existing[0])
                if decision.resolution != request or decision.operator != operator:
                    raise ControlError(CONFLICT, "this identity has another operator decision")
                return decision
            if (
                request.kind in {"model_usage", "model_reservation"}
                and db.execute(
                    "SELECT 1 FROM research_observations WHERE project=? AND session=? AND work=? "
                    "AND kind IN ('resolution:model_usage','resolution:model_reservation') "
                    "AND json_extract(detail,'$.resolution.run')=? "
                    "AND json_extract(detail,'$.resolution.identity')=?",
                    (project, session, request.work, request.run, request.identity),
                ).fetchone()
            ):
                raise ControlError(CONFLICT, "this model identity has another operator decision")
            text = f"Operator uncertainty resolution ({request.kind}): {request.reason}"
            entry = Entry("user", "notice", text, source="operator_recovery")
            tool_record = None
            if request.kind == "model_usage":
                self._model(tx, project, session, request)
                text += f"\nOriginal model intent {request.identity}; supplied cost {request.cost}."
                if request.input_tokens is not None:
                    text += (
                        f" Observed input tokens {request.input_tokens}; "
                        f"output tokens {request.output_tokens}."
                    )
                entry = Entry("user", "notice", text, source="operator_recovery")
            elif request.kind == "model_reservation":
                if not db.execute(
                    "SELECT 1 FROM research_model_calls WHERE project=? AND session=? "
                    "AND run=? AND identity=? AND usage IS NULL",
                    (project, session, request.run, request.identity),
                ).fetchone():
                    raise ControlError(CONFLICT, "model intent is not pending in this attempt")
                entry = Entry(
                    "user",
                    "notice",
                    text
                    + f"\nOriginal model intent {request.identity}: actual usage and charge remain "
                    "UNKNOWN. Conservative budget reservation only: "
                    f"cost upper bound {request.cost}; "
                    f"input token upper bound {request.input_tokens}; output token upper bound "
                    f"{request.output_tokens}. No response was recovered or synthesized.",
                    source="operator_recovery",
                )
            elif request.kind == "fold_estimate_link":
                self._fold_estimate_link(db, project, session, request)
                entry = Entry(
                    "user",
                    "notice",
                    text + f"\nLegacy cancelled fold estimate at {request.source} position "
                    f"{request.position} (event {request.event_seq or 0}) is explicitly "
                    f"linked to reserved model intent {request.identity}. Actual usage and "
                    "charge remain UNKNOWN; the existing reservation is counted once. "
                    "No response was recovered or synthesized.",
                    source="operator_recovery",
                )
            elif request.kind == "operation":
                entry, tool_record = self._operation(tx, project, session, request, text)
            else:
                saved = db.execute(
                    "SELECT detail FROM research_observations WHERE project=? AND session=? "
                    "AND work=? AND kind='kernels' AND identity='latest'",
                    (project, session, request.work),
                ).fetchone()
                if saved is None:
                    raise ControlError(CONFLICT, "no saved kernel state to acknowledge")
                if hashlib.sha256(saved[0].encode()).hexdigest() != request.identity:
                    raise ControlError(CONFLICT, "saved kernel observation changed")
                db.execute(
                    "DELETE FROM research_observations WHERE project=? AND session=? AND work=? "
                    "AND kind='kernels' AND identity='latest'",
                    (project, session, request.work),
                )
                entry = Entry(
                    "user",
                    "notice",
                    text + "\nLost kernel state acknowledged; recomputation is "
                    "required. No code was executed.\nAcknowledged saved observation: " + saved[0],
                    source="operator_recovery",
                )
            archived = Archive(self.control, clock=self.clock).append(
                project,
                session,
                request.run,
                entry,
                parent=parent,
                tool_record=tool_record,
                transaction=tx,
            )
            decision = Decision(
                resolution=request, message_id=archived.message_id, operator=operator, observed=now
            )
            detail = decision.model_dump_json()
            if len(detail.encode()) > 16384:
                raise ControlError(INVALID_REQUEST, "operator decision exceeds 16 KiB")
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,?,?,?,?)", (*key, now, detail)
            )
            return decision

    @staticmethod
    def _fold_estimate_link(
        db: sqlite3.Connection, project: str, session: str, request: Resolution
    ) -> None:
        """Bind one immutable legacy estimate to one already reserved fold intent."""
        assert request.source is not None  # noqa: S101 - validated by Resolution
        assert request.position is not None  # noqa: S101 - validated by Resolution
        assert request.estimate_json is not None  # noqa: S101 - validated by Resolution
        reservation = db.execute(
            "SELECT json_extract(o.detail,'$.resolution.input_tokens') "
            "FROM research_model_calls m JOIN research_observations o "
            "ON o.project=m.project AND o.session=m.session AND o.work=? "
            "AND o.kind='resolution:model_reservation' "
            "AND json_extract(o.detail,'$.resolution.run')=m.run "
            "AND json_extract(o.detail,'$.resolution.identity')=m.identity "
            "JOIN research_attempts a ON a.project=m.project AND a.session=m.session "
            "AND a.work=o.work AND a.run=m.run "
            "WHERE m.project=? AND m.session=? AND m.run=? AND m.identity=? "
            "AND m.kind='fold' AND m.usage IS NULL",
            (request.work, project, session, request.run, request.identity),
        ).fetchone()
        if reservation is None:
            raise ControlError(CONFLICT, "fold intent has no unresolved reserved charge")
        estimate = cast(dict[str, int], json.loads(request.estimate_json))
        if reservation[0] < estimate["input_tokens"]:
            raise ControlError(CONFLICT, "fold estimate exceeds reserved input bound")
        if request.source == "collection":
            row = db.execute(
                "SELECT item FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection='fold_calls' AND position=?",
                (project, session, request.run, request.position),
            ).fetchone()
        elif request.source == "summary":
            if db.execute(
                "SELECT 1 FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection='fold_calls' LIMIT 1",
                (project, session, request.run),
            ).fetchone():
                raise ControlError(CONFLICT, "fold collection supersedes saved summary")
            row = db.execute(
                "SELECT u.value FROM runs r,json_each(r.record,'$.summaries.folds.calls') u "
                "WHERE r.project_id=? AND r.session_id=? AND r.name=? AND u.key=?",
                (project, session, request.run, request.position),
            ).fetchone()
        else:
            if db.execute(
                "SELECT 1 FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection='fold_calls' UNION ALL SELECT 1 FROM runs "
                "WHERE project_id=? AND session_id=? AND name=? LIMIT 1",
                (project, session, request.run, project, session, request.run),
            ).fetchone():
                raise ControlError(CONFLICT, "saved fold history supersedes event fallback")
            row = db.execute(
                "SELECT u.value FROM run_events e,json_each(e.data,'$.calls') u "
                "WHERE e.project_id=? AND e.session_id=? AND e.run=? AND e.seq=? "
                "AND e.kind='fold' AND u.key=?",
                (project, session, request.run, request.event_seq, request.position),
            ).fetchone()
        if row is None or row[0] != request.estimate_json:
            raise ControlError(CONFLICT, "fold estimate copy changed or was not found")
        if db.execute(
            "SELECT 1 FROM research_observations WHERE project=? AND session=? "
            "AND kind='resolution:fold_estimate_link' "
            "AND json_extract(detail,'$.resolution.run')=? "
            "AND json_extract(detail,'$.resolution.source')=? "
            "AND json_extract(detail,'$.resolution.position')=? "
            "AND coalesce(json_extract(detail,'$.resolution.event_seq'),0)=? LIMIT 1",
            (
                project,
                session,
                request.run,
                request.source,
                request.position,
                request.event_seq or 0,
            ),
        ).fetchone():
            raise ControlError(CONFLICT, "fold estimate already linked to another intent")

    @staticmethod
    def _model(tx: Tx, project: str, session: str, request: Resolution) -> None:
        bounded = tx.sql.execute(
            "SELECT 1 FROM research_work w JOIN research_blueprints b "
            "USING(project,session,work) WHERE w.project=? AND w.session=? AND w.work=? "
            "AND w.parent IS NOT NULL AND json_extract(b.snapshot,'$.request.tokens') IS NOT NULL",
            (project, session, request.work),
        ).fetchone()
        if bounded and request.input_tokens is None:
            raise ControlError(
                INVALID_REQUEST, "token-bounded child work requires observed input/output tokens"
            )
        usage = ModelUsage(
            cost=request.cost,
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
        )
        observed(tx.sql, project, session, request.run, request.identity, usage)

    @staticmethod
    def _operation(
        tx: Tx, project: str, session: str, request: Resolution, text: str
    ) -> tuple[Entry, ToolRecord]:
        key = (project, session, request.run, request.identity)
        size = tx.sql.execute(
            "SELECT body_bytes FROM tool_intents WHERE project=? AND session=? AND run=? "
            "AND operation_id=?",
            key,
        ).fetchone()
        if size is None:
            raise ControlError(NOT_FOUND, "no original tool intent in this work")
        if size[0] > READ_BYTES:
            raise ControlError(INVALID_REQUEST, "original intent exceeds 1 MiB; not resolved")
        if tx.sql.execute(
            "SELECT 1 FROM archive_tool_records WHERE project=? AND session=? AND run=? "
            "AND operation_id=?",
            key,
        ).fetchone():
            raise ControlError(CONFLICT, "original operation already has a result")
        call, tool, arguments = tx.sql.execute(
            "SELECT call_id,tool,arguments FROM tool_intents WHERE project=? AND session=? "
            "AND run=? AND operation_id=?",
            key,
        ).fetchone()
        text += (
            "\nOriginal operation result unavailable. Its effects remain unknown. The operator "
            "acknowledged this uncertainty; this is not a success result. "
            "No operation was repeated."
        )
        entry = Entry("tool", "tool_result", text, source=tool, call_id=call)
        record = ToolRecord(
            {
                "tool": tool,
                "call_id": call,
                "arguments": arguments,
                "operator_uncertainty": {
                    "operation_id": request.identity,
                    "reason": request.reason,
                    "outcome": "unavailable_result_acknowledged",
                },
            },
            request.identity,
        )
        return entry, record
