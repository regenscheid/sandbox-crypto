# SPDX-License-Identifier: MIT
"""Internal own-track progress; explicit supplied authority is never authentication.

No tool registration, launcher, model, worker, role-default or grant minting lives
here. The adapter owns its new registry descriptor and actual admitted thread
work, and borrows one already initialized canonical control store.
"""

# Narrow canonical-store views deliberately use existing transaction/read helpers.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, Field

from sandbox_science.contracts import ExecutionContext, Identifier, new_id

from .delegation_records import (
    DelegationMutation,
    DelegationRecords,
    DelegationScope,
    ParentAuthority,
)
from .delegation_records import (
    _checked as checked_delegation,
)
from .documents.errors import BUSY, CONFLICT, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .documents.store import DocumentStore
from .plan_tools import Progress
from .projects import ProjectError, ProjectStore, SessionSnapshot
from .session_plans import (
    CompactProgress,
    CompactStep,
    GoalOutcome,
    PlanAuthority,
    PlanEvidence,
    PlanHead,
    PlanModel,
    PlanProgress,
    PlanReceipt,
    PlanRef,
    PlanScope,
    PlanStep,
    PlanTrack,
    SessionPlans,
    StepStatus,
    _steps,
)

MAX_REQUEST = 96 * 1024
MAX_RESPONSE = 256 * 1024
MAX_EVENTS = 64
Pin = tuple[str, int, int, int, int]


def _checked[M: BaseModel](model: type[M], value: M, maximum: int = MAX_REQUEST) -> M:
    """Bound exact copied input before validation or aggregate serialization."""
    left, nodes = maximum, 8192

    def plain(item: object, depth: int = 0) -> object:
        nonlocal left, nodes
        nodes -= 1
        if nodes < 0 or depth > 12:
            raise DocumentError(INVALID_REQUEST, "delegated input exceeds its structural budget")
        kind = type(item)
        if any(kind is known for known in _INPUTS):
            raw_fields: object = object.__getattribute__(item, "__dict__")
            model_type = cast(type[BaseModel], kind)
            if type(raw_fields) is not dict:
                raise DocumentError(INVALID_REQUEST, "undeclared delegated input fields")
            fields = cast(dict[str, object], raw_fields)
            if (
                object.__getattribute__(item, "__pydantic_extra__") is not None
                or len(fields) != len(model_type.model_fields)
                or any(type(key) is not str for key in fields)
                or set(fields) != set(model_type.model_fields)
            ):
                raise DocumentError(INVALID_REQUEST, "undeclared delegated input fields")
            result = {key: plain(child, depth + 1) for key, child in fields.items()}
            left -= sum(len(key) + 3 for key in fields) + 2
            if left < 0:
                raise DocumentError(INVALID_REQUEST, "delegated input exceeds its byte budget")
            return result
        if item is None or kind is bool:
            left -= 6
        elif kind is int:
            if not -(2**63) <= cast(int, item) < 2**63:
                raise DocumentError(INVALID_REQUEST, "delegated integer exceeds its budget")
            left -= 22
        elif kind is str:
            text = cast(str, item)
            if len(text) > maximum:
                raise DocumentError(INVALID_REQUEST, "delegated text exceeds its budget")
            try:
                text.encode("utf-8")
            except UnicodeError:
                raise DocumentError(INVALID_REQUEST, "delegated text is not UTF-8") from None
            left -= (
                sum(
                    12
                    if ord(char) > 0xFFFF
                    else 6
                    if ord(char) < 32 or ord(char) >= 127
                    else 2
                    if char in '"\\'
                    else 1
                    for char in text
                )
                + 3
            )
        elif kind is datetime:
            date = cast(datetime, item)
            if date.utcoffset() is None:
                raise DocumentError(INVALID_REQUEST, "delegated timestamp is not aware")
            left -= 48
        elif kind is tuple or kind is list:
            children = cast(tuple[object, ...] | list[object], item)
            if len(children) > 128:
                raise DocumentError(INVALID_REQUEST, "delegated collection exceeds its budget")
            result = tuple(plain(child, depth + 1) for child in children)
            left -= len(children) + 2
            if left < 0:
                raise DocumentError(INVALID_REQUEST, "delegated input exceeds its byte budget")
            return result if kind is tuple else list(result)
        else:
            raise DocumentError(INVALID_REQUEST, "invalid delegated input type")
        if left < 0:
            raise DocumentError(INVALID_REQUEST, "delegated input exceeds its byte budget")
        return item

    if type(value) is not model:
        raise DocumentError(INVALID_REQUEST, "invalid delegated model type")
    try:
        return model.model_validate(plain(value), strict=True)
    except (ValueError, TypeError):
        raise DocumentError(INVALID_REQUEST, "invalid delegated input") from None


class GrantedStep(PlanModel):
    step: PlanStep
    current: CompactStep
    status: StepStatus
    historical_assertion: bool


def _granted_step(step: PlanStep, projection: CompactStep) -> GrantedStep:
    latest = projection.latest
    return GrantedStep(
        step=step,
        current=projection,
        status=latest.status
        if projection.matches_current_step and latest is not None
        else "pending",
        historical_assertion=latest is not None and not projection.matches_current_step,
    )


class GrantedRead(PlanModel):
    """Complete own-track input plus a host-only successful-delivery correlation."""

    delivery_id: Identifier
    scope: PlanScope
    environment_id: Identifier
    delegation_id: Identifier
    child_run_id: Identifier
    event_cursor: int = Field(strict=True, ge=1)
    head: PlanHead
    track: PlanTrack
    steps: tuple[GrantedStep, ...] = Field(min_length=1, max_length=128)
    observed_at: str


class GrantedHistory(PlanModel):
    scope: PlanScope
    ref: PlanRef
    track_id: Identifier
    step_id: Identifier
    after: int = Field(strict=True, ge=0)
    target: int = Field(strict=True, ge=0)
    more: bool
    items: tuple[PlanProgress, ...] = Field(max_length=8)


_INPUTS = frozenset(
    {
        ExecutionContext,
        PlanAuthority,
        PlanScope,
        PlanRef,
        PlanEvidence,
        Progress,
        GrantedRead,
        GrantedStep,
        PlanHead,
        PlanTrack,
        PlanStep,
        CompactStep,
        CompactProgress,
        GoalOutcome,
    }
)


def _read_hash(value: GrantedRead) -> str:
    checked = _checked(GrantedRead, value, MAX_RESPONSE)
    return hashlib.sha256(checked.model_dump_json().encode("utf-8")).hexdigest()


def _bounded(value: PlanModel) -> None:
    if len(value.model_dump_json().encode("utf-8")) > MAX_RESPONSE:
        raise DocumentError(INVALID_REQUEST, "complete delegated response exceeds its byte budget")


class _BorrowedStore(DocumentStore):
    """No constructor/open/schema/close ownership; guard the owner's real transaction."""

    def __init__(self, owner: DocumentStore, validate: Callable[[sqlite3.Connection, bool], None]):
        self.owner, self.validate = owner, validate
        self.readonly = owner.readonly

    @contextmanager
    def _read(self) -> Generator[sqlite3.Connection | None]:
        with self.owner._read() as db:
            if db is None:
                raise DocumentError(CONFLICT, "canonical delegated history is absent")
            self.validate(db, False)
            yield db
            self.validate(db, False)

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        with self.owner._write() as db:
            self.validate(db, True)  # after BEGIN IMMEDIATE, including writer wait
            yield db
            self.validate(db, True)  # refusal rolls back before owner's COMMIT

    def close(self) -> None:
        raise RuntimeError("a delegated view cannot close its borrowed store")


class _PlanView(SessionPlans):
    def __init__(self, base: SessionPlans, validate: Callable[[sqlite3.Connection, bool], None]):
        # Calling writable SessionPlans.__init__ would initialize schema/triggers.
        # The existing canonical methods/limits are borrowed without that call.
        self.documents = _BorrowedStore(base.documents, validate)
        self.limits = base.limits


async def _settle[T](task: asyncio.Task[T], withdraw: Callable[[], None]) -> T:
    first: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if first is None:
                first = error
            withdraw()
        except BaseException:
            if first is not None:
                raise first from None
            raise
    if first is not None:
        with suppress(BaseException):
            task.result()
        raise first
    return task.result()


class DelegatedPlanProgress:
    """One explicit grant, one in-flight owned thread, no public model tool surface."""

    _plans: SessionPlans
    _records: DelegationRecords
    _projects: ProjectStore
    _context: ExecutionContext
    _authority: PlanAuthority
    _prepared: DelegationMutation
    _parent: ParentAuthority
    _admitted: Callable[[], bool]
    _snapshot: SessionSnapshot | None
    _work: asyncio.Task[Any] | None
    _close_task: asyncio.Task[None] | None
    _closing: bool
    _invalid: bool
    _withdrawn: threading.Event
    _requires_read: bool
    _cursor: int
    _event_budget: int
    _scan_held: bool
    _pending: tuple[GrantedRead, Pin, str] | None
    _view: _PlanView
    _step_pins: dict[str, str]
    _track_id: str

    def __init__(self) -> None:
        raise TypeError("use the asynchronous open factory")

    @classmethod
    async def open(
        cls,
        plans: SessionPlans,
        records: DelegationRecords,
        projects: ProjectStore,
        context: ExecutionContext,
        *,
        authority: PlanAuthority,
        prepared: DelegationMutation,
        original_session: SessionSnapshot,
        admitted: Callable[[], bool],
    ) -> DelegatedPlanProgress:
        authority, context = _checked(PlanAuthority, authority), _checked(ExecutionContext, context)
        prepared = checked_delegation(DelegationMutation, prepared)
        brief = prepared.brief
        if (
            authority.role != "delegated"
            or authority.grant is None
            or prepared.action != "prepare"
            or brief is None
            or brief.track is None
            or authority.run_id != prepared.child_run_id
            or (authority.scope.project_id, authority.scope.session_id, context.environment_id)
            != (prepared.scope.project_id, prepared.scope.session_id, prepared.scope.environment_id)
            or (context.project_id, context.session_id)
            != (authority.scope.project_id, authority.scope.session_id)
            or authority.grant != PlanRef(plan_id=brief.track.plan_id, version=brief.track.version)
            or authority.track_id != brief.track.track_id
            or plans.documents is not records.documents
            or plans.documents.readonly
            or type(original_session) is not SessionSnapshot
            or not callable(admitted)
        ):
            raise DocumentError(
                POLICY_DENIED, "explicit delegated grant and original child disagree"
            )
        self = object.__new__(cls)
        self._plans, self._records, self._projects = plans, records, projects
        self._context, self._authority, self._prepared = context, authority, prepared
        self._parent = ParentAuthority(
            scope=DelegationScope.model_validate(prepared.scope.model_dump(mode="python")),
            parent_run_id=prepared.parent_run_id,
        )
        self._admitted = admitted
        self._snapshot = None
        self._work = None
        self._close_task = None
        self._closing = False
        self._invalid = False
        self._withdrawn = threading.Event()
        self._requires_read = True
        self._cursor = 0
        self._event_budget = MAX_EVENTS
        self._scan_held = True
        self._pending = None
        self._view = _PlanView(plans, self._validate)
        self._step_pins = {step.step_id: step.sha256 for step in brief.track.steps}
        self._track_id = brief.track.track_id

        def initialize() -> None:
            current = projects.session_snapshot(context.project_id, context.session_id)
            self._snapshot = current
            try:
                matches = original_session.matches(current)
            except OSError:
                matches = False
            if not matches:
                raise DocumentError(POLICY_DENIED, "original delegated session changed")
            self._guarded(lambda: self._view.summary(self._authority))

        try:
            await self._thread(initialize)
        except BaseException as error:
            try:
                await self.close()
            except BaseException:
                if isinstance(error, asyncio.CancelledError):
                    raise error from None
                raise
            raise
        return self

    def _controller(self) -> None:
        if self._closing or self._invalid or self._withdrawn.is_set():
            self._requires_read = True
            raise DocumentError(POLICY_DENIED, "delegated adapter is held or closing")
        try:
            allowed = self._admitted()
        except Exception:
            self._requires_read = True
            raise DocumentError(POLICY_DENIED, "fresh delegated admission is unavailable") from None
        if allowed is not True:
            self._requires_read = True
            raise DocumentError(POLICY_DENIED, "fresh delegated admission was refused")

    def _session(self) -> None:
        original = self._snapshot
        if original is None:
            raise DocumentError(POLICY_DENIED, "original delegated session is unavailable")
        try:
            current = self._projects.session_snapshot(
                self._context.project_id, self._context.session_id
            )
            try:
                root = os.stat(self._projects.root, follow_symlinks=False)
                held_root = os.fstat(current._root)
                named = os.stat(
                    self._context.project_id + ".json", dir_fd=current._root, follow_symlinks=False
                )
                held = os.fstat(current._descriptor)

                def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
                    return (
                        item.st_dev,
                        item.st_ino,
                        item.st_size,
                        item.st_mtime_ns,
                        item.st_ctime_ns,
                    )

                if (
                    not original.matches(current)
                    or not stat.S_ISDIR(root.st_mode)
                    or (root.st_dev, root.st_ino) != (held_root.st_dev, held_root.st_ino)
                    or not stat.S_ISREG(named.st_mode)
                    or identity(named) != identity(held)
                    or (
                        current.context.project_id,
                        current.context.session_id,
                        current.context.environment_id,
                    )
                    != (
                        self._context.project_id,
                        self._context.session_id,
                        self._context.environment_id,
                    )
                ):
                    raise DocumentError(POLICY_DENIED, "original delegated session changed")
            finally:
                current.close()
        except (ProjectError, OSError, DocumentError):
            self._invalid = True
            raise DocumentError(
                POLICY_DENIED, "original open delegated session is unavailable"
            ) from None

    def _guarded[T](self, action: Callable[[], T]) -> T:
        self._controller()
        with self._projects._locked(self._context.project_id):
            self._session()
            self._controller()
            result = action()
            self._controller()
            self._session()
            return result

    def _validate(self, db: sqlite3.Connection, write: bool) -> None:
        self._controller()
        self._session()
        self._records._schema(db)
        self._records._parent(db, self._parent)
        saved = self._records._snapshot(db, self._parent, self._prepared.delegation_id)
        if saved.prepared != self._prepared or saved.outcome is not None:
            raise DocumentError(POLICY_DENIED, "canonical prepared child changed or ended")
        authority = self._authority
        pointer = self._plans._current(db, authority.scope)
        if pointer is None or pointer.ref != authority.grant:
            self._requires_read = True
            raise DocumentError(CONFLICT, "the original granted plan version is no longer current")
        head = self._plans._head(db, authority.scope, pointer.ref)
        version = self._plans._version(db, authority.scope, pointer.ref)
        actual = {
            key: fingerprint
            for key, (step, fingerprint) in _steps(version.body).items()
            if step.track_id == authority.track_id
        }
        if actual != self._step_pins:
            self._requires_read = True
            raise DocumentError(CONFLICT, "the original granted current track intent changed")
        end = min(pointer.event_cursor, self._cursor + self._event_budget)
        for number in range(self._cursor + 1, end + 1):
            event = self._plans._event(db, authority.scope, number)
            if event.author.role != "delegated" or event.author.run_id != authority.run_id:
                self._requires_read = True
            self._event_budget -= 1
            self._cursor = number
        self._scan_held = self._cursor < pointer.event_cursor
        if self._scan_held:
            self._requires_read = True
        if write and (head.status != "active" or self._requires_read or self._scan_held):
            raise DocumentError(
                CONFLICT, "read and successfully deliver the current active granted plan"
            )
        self._controller()
        self._session()

    async def _thread[T](self, action: Callable[[], T]) -> T:
        self._controller()
        if self._work is not None:
            raise DocumentError(BUSY, "a delegated operation still owns its actual thread")
        self._event_budget = MAX_EVENTS
        cursor = self._cursor
        task = asyncio.create_task(asyncio.to_thread(action))
        self._work = task
        try:
            return await _settle(task, self._withdrawn.set)
        except BaseException:
            # Validation may have visited an event written in the same transaction
            # before the owner rolls it back. Never let an uncommitted cursor hide
            # a later USER event at that sequence, including a lost/canceled return.
            self._cursor = cursor
            self._pending = None
            self._requires_read = True
            self._scan_held = True
            raise
        finally:
            if self._work is task:
                self._work = None

    async def read_current(self) -> GrantedRead:
        def read() -> GrantedRead:
            current = self._view.compact_current(self._authority)
            if current is None:
                raise DocumentError(CONFLICT, "original granted plan is absent")
            body = current.version.body
            track = next(
                track for track in body.tracks if track.track_id == self._authority.track_id
            )
            all_steps = _steps(body)
            projections = {step.step_id: step for step in current.steps}
            result = GrantedRead(
                delivery_id=new_id("read"),
                scope=self._authority.scope,
                environment_id=self._context.environment_id,
                delegation_id=self._prepared.delegation_id,
                child_run_id=self._prepared.child_run_id,
                event_cursor=current.event_cursor,
                head=current.head,
                track=track,
                steps=tuple(
                    _granted_step(all_steps[key][0], projections[key]) for key in self._step_pins
                ),
                observed_at=datetime.now(UTC).isoformat(),
            )
            _bounded(result)
            self._pending = (
                result,
                (
                    current.head.ref.plan_id,
                    current.head.ref.version,
                    current.head.status_revision,
                    current.head.progress_cursor,
                    current.event_cursor,
                ),
                _read_hash(result),
            )
            return result

        return await self._thread(lambda: self._guarded(read))

    async def model_delivered(self, read: GrantedRead) -> None:
        """Trusted host hook ONLY after a successful call contained this exact read.

        The correlation is not a public/model-callable permission or proof that a
        model understood the data. Historical or copied results cannot clear it.
        """

        def delivered() -> None:
            pending, self._pending = self._pending, None
            if pending is None or read is not pending[0]:
                raise DocumentError(CONFLICT, "no exact current granted read was delivered")
            if _read_hash(read) != pending[2]:
                self._requires_read = True
                raise DocumentError(CONFLICT, "delivered granted read content changed")
            summary = self._view.summary(self._authority)
            if (
                summary is None
                or (
                    summary.head.plan_id,
                    summary.head.version,
                    summary.status_revision,
                    summary.progress_cursor,
                    summary.event_cursor,
                )
                != pending[1]
            ):
                self._requires_read = True
                raise DocumentError(CONFLICT, "granted plan changed before successful delivery")
            if self._scan_held:
                self._requires_read = True
                raise DocumentError(
                    CONFLICT, "delegated event scan is held; continue bounded safe-point reads"
                )
            self._requires_read = False

        await self._thread(lambda: self._guarded(delivered))

    async def safe_point(self) -> dict[str, object]:
        def refresh() -> dict[str, object]:
            summary = self._view.summary(self._authority)
            return {
                "planning": "granted_track",
                "summary": summary.model_dump(mode="json") if summary else None,
                "reread_required": self._requires_read,
                "event_scan_held": self._scan_held,
                "observed_at": datetime.now(UTC).isoformat(),
            }

        try:
            return await self._thread(lambda: self._guarded(refresh))
        except DocumentError as error:
            if error.kind == BUSY:
                raise
            self._requires_read = True
            return {
                "planning": "held",
                "reread_required": True,
                "observed_at": datetime.now(UTC).isoformat(),
            }

    async def progress(self, request: Progress, *, resolved_model: str | None) -> PlanReceipt:
        request = _checked(Progress, request)
        authority = _checked(
            PlanAuthority, self._authority.model_copy(update={"resolved_model": resolved_model})
        )
        if request.base != authority.grant or request.step_id not in self._step_pins:
            raise DocumentError(POLICY_DENIED, "progress is outside the exact original track grant")

        def change() -> PlanReceipt:
            receipt = self._view.progress(
                authority,
                request.operation_id,
                request.base,
                request.step_id,
                request.expected_step_cursor,
                request.status,
                request.note,
                evidence=request.evidence,
                expected_status_revision=request.expected_status_revision,
            )
            _bounded(receipt)
            return receipt

        return await self._thread(lambda: self._guarded(change))

    async def history(
        self,
        step_id: str,
        *,
        after: int = 0,
        target: int | None = None,
        limit: int = 8,
    ) -> GrantedHistory:
        if (
            type(step_id) is not str
            or step_id not in self._step_pins
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            raise DocumentError(
                POLICY_DENIED, "history is outside the original bounded track grant"
            )

        def read() -> GrantedHistory:
            grant = self._authority.grant
            if grant is None:
                raise DocumentError(POLICY_DENIED, "original grant is absent")
            with self._view.documents._read() as db:
                if db is None:
                    raise DocumentError(CONFLICT, "canonical delegated history is absent")
                scope = self._authority.scope
                head = self._plans._head(db, scope, grant)
                page = self._plans._page(
                    db,
                    scope,
                    "progress",
                    grant.plan_id,
                    PlanProgress,
                    head.progress_cursor,
                    after,
                    target,
                    step_id,
                    limit,
                )
                for item in page.items:
                    if (
                        item.step_id != step_id
                        or item.scope != scope
                        or item.author.scope != scope
                        or item.ref.plan_id != grant.plan_id
                        or item.ref.version > grant.version
                    ):
                        raise DocumentError(CONFLICT, "retained granted history has wrong scope")
                    version = self._plans._version(db, scope, item.ref)
                    entry = _steps(version.body).get(step_id)
                    if (
                        entry is None
                        or entry[0].track_id != self._track_id
                        or entry[1] != item.step_sha256
                    ):
                        raise DocumentError(CONFLICT, "retained granted history has wrong intent")
                items = page.items
            while True:
                cursor = items[-1].sequence if items else after
                result = GrantedHistory(
                    scope=self._authority.scope,
                    ref=grant,
                    track_id=self._track_id,
                    step_id=step_id,
                    after=cursor,
                    target=page.target,
                    more=page.more or len(items) != len(page.items),
                    items=items,
                )
                try:
                    _bounded(result)
                    return result
                except DocumentError:
                    if len(items) <= 1:
                        raise
                    items = items[:-1]

        return await self._thread(lambda: self._guarded(read))

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True

            async def drain() -> None:
                if self._work is not None:
                    # Actual thread settles before descriptor close; its caller owns failures.
                    with suppress(BaseException):
                        await asyncio.shield(self._work)
                if self._snapshot is not None:
                    self._snapshot.close()
                    self._snapshot = None

            self._close_task = asyncio.create_task(drain())
        await _settle(self._close_task, self._withdrawn.set)
