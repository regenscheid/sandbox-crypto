# SPDX-License-Identifier: MIT
"""Explicit operator decisions about unknown outcomes; never dispatch or infer success."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, Self

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

Kind = Literal["model_usage", "operation", "kernel_recompute"]


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

    @model_validator(mode="after")
    def _explicit(self) -> Self:
        if not self.reason.strip():
            raise ValueError("operator reason must contain text")
        if (self.kind == "model_usage") != (self.cost is not None):
            raise ValueError("model_usage requires an explicit cost; other kinds have no cost")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("input and output tokens must both be supplied or both unknown")
        if self.kind != "model_usage" and self.input_tokens is not None:
            raise ValueError("only model_usage accepts token observations")
        if self.kind == "kernel_recompute" and (
            re.fullmatch(r"[0-9a-f]{64}", self.identity) is None or self.run != self.current
        ):
            raise ValueError("kernel recompute requires the saved snapshot digest and current run")
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
