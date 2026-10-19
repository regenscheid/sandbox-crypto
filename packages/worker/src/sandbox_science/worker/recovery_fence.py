# SPDX-License-Identifier: MIT
"""Durable session epochs and host-only capabilities at actual executor admission."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.contracts.recovery import RecoveryFence

from .errors import ExecutorRefusalError, WorkerError

OPERATOR: ContextVar[bool] = ContextVar("worker_authenticated_operator", default=False)


@contextmanager
def operator_scope(granted: bool) -> Generator[None]:
    token = OPERATOR.set(granted)
    try:
        yield
    finally:
        OPERATOR.reset(token)


@dataclass(frozen=True, slots=True)
class Admission:
    fences: RecoveryFences
    context: ExecutionContext
    operator: bool

    def check(self) -> None:
        self.fences.check(self.context, operator=self.operator)


@dataclass(frozen=True, slots=True)
class LegacyAdmission:
    fences: RecoveryFences
    project: str

    def check(self) -> None:
        if self.fences.project_fenced(self.project):
            raise WorkerError(ErrorKind.POLICY_DENIED, "host admission requires current context")


_CURRENT: ContextVar[Admission | LegacyAdmission | None] = ContextVar(
    "worker_effect_admission", default=None
)
_PASSIVE = frozenset(
    {
        "hello",
        "probe",
        "active",
        "latest",
        "replay",
        "kernels",
        "usage",
        "read",
        "list",
        "job_status",
        "lockfile",
        "capture_cancel",
    }
)


def check_dispatch(operation: str | None = None) -> None:
    """Called after the channel write-lock await and directly before its synchronous write."""
    if operation in _PASSIVE:
        return  # observation/targeted capture cleanup of effects admitted before supersession
    admission = _CURRENT.get()
    if admission is not None:
        try:
            admission.check()
        except WorkerError as error:
            # Dispatch journaling precedes the write. This definitive local refusal
            # must not be mistaken for an uncertain write and close a shared channel.
            raise ExecutorRefusalError(error.kind, error.message) from None


def control_context(
    target: ExecutionContext, supplied: ExecutionContext | None
) -> ExecutionContext:
    """Internal work inherits its initiating owner; external legacy calls gain none."""
    admission = _CURRENT.get()
    context = supplied or (admission.context if isinstance(admission, Admission) else None)
    if context is None:
        return target.model_copy(update={"authority": None})
    if (context.project_id, context.session_id, context.environment_id) != (
        target.project_id,
        target.session_id,
        target.environment_id,
    ):
        raise WorkerError(ErrorKind.NOT_FOUND, "effect is outside the supplied context")
    return context


class RecoveryFences:
    """Uses the worker store and its single serving event loop (StateLock)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def install(self, fence: RecoveryFence) -> RecoveryFence:
        self.db.execute(
            "INSERT INTO recovery_fences(project_id,session_id,epoch) VALUES (?,?,?) "
            "ON CONFLICT(project_id,session_id) DO UPDATE SET epoch=excluded.epoch "
            "WHERE excluded.epoch>=recovery_fences.epoch",
            (fence.project_id, fence.session_id, fence.epoch),
        )
        if self.epoch(fence.project_id, fence.session_id) != fence.epoch:
            raise WorkerError(ErrorKind.CONFLICT, "recovery epoch cannot decrease")
        return fence

    def epoch(self, project: str, session: str) -> int | None:
        row = self.db.execute(
            "SELECT epoch FROM recovery_fences WHERE project_id=? AND session_id=?",
            (project, session),
        ).fetchone()
        return int(row[0]) if row else None

    def project_fenced(self, project: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM recovery_fences WHERE project_id=? LIMIT 1", (project,)
            ).fetchone()
            is not None
        )

    def check(self, context: ExecutionContext, *, operator: bool = False) -> None:
        epoch = self.epoch(context.project_id, context.session_id)
        if operator:
            return
        authority = context.authority
        if epoch is None:
            if authority is not None:
                raise WorkerError(
                    ErrorKind.POLICY_DENIED, "session recovery epoch is not installed"
                )
            return
        if authority is None or authority.epoch != epoch:
            raise WorkerError(ErrorKind.POLICY_DENIED, "missing or superseded research authority")

    @contextmanager
    def bind_project(self, project: str) -> Generator[None]:
        """Legacy admission expires as soon as any project root is fenced."""
        admission = LegacyAdmission(self, project)
        admission.check()
        token = _CURRENT.set(admission)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    @contextmanager
    def bind(self, context: ExecutionContext) -> Generator[None]:
        admission = Admission(self, context, OPERATOR.get())
        admission.check()
        token = _CURRENT.set(admission)
        try:
            yield
        finally:
            _CURRENT.reset(token)
