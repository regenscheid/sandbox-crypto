# SPDX-License-Identifier: MIT
"""A task's own track of the session plan (plans-and-delegation spec, PD-9, PD-12).

A task that names a track of the session's active plan works under a grant: that plan
version and that track. Its run reads the track's steps with ``track_read`` and records
progress on them with ``track_progress``, and on no other step; the plan store holds the
grant (``PlanAuthority``). A plan revised since the delegation started refuses its
progress, as the fence says (``ChildFence``): the parent delegates the track again
(the coordinator, 2026-10-15). The tool reads each step's cursor and the plan's status
revision as it records: only this delegation records on its track while it runs.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import Field, ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanRef,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError

Json = dict[str, Any]

SPECS: Final = (
    ToolSpec(
        "track_read",
        "Read your track of the session's plan: its steps, each with its title, what it "
        "asks for, the evidence it needs, and its status.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        "track_progress",
        "Record your progress on one step of your track: its status and a note of what "
        "you did and found. Only your track's steps, and only while the plan is the "
        "version you started under.",
        {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "completed", "blocked", "skipped"],
                },
                "note": {"type": "string", "maxLength": 4000},
            },
            "required": ["step_id", "status", "note"],
            "additionalProperties": False,
        },
    ),
)
EFFECTS: Final[dict[str, Effect]] = {"track_read": "read", "track_progress": "plan"}


class Progress(Contract):
    step_id: Identifier
    status: Literal["in_progress", "completed", "blocked", "skipped"]
    note: str = Field(min_length=1, max_length=4000)


def steps_of(body: PlanBody, track: str) -> list[Json]:
    """The steps of ``track`` in ``body``, in order, as ``track_read`` shows them."""
    return [
        {
            "step_id": step.step_id,
            "title": step.title,
            "description": step.description,
            "evidence_standard": step.evidence_standard,
        }
        for phase in body.phases
        for step in phase.steps
        if step.track_id == track
    ]


def grant(plans: SessionPlans, context: ExecutionContext, track: str) -> PlanRef:
    """The plan version a task on ``track`` works under: the session's active plan,
    which must hold the track. Call it in a thread."""
    authority = PlanAuthority(
        scope=PlanScope(project_id=context.project_id, session_id=context.session_id),
        role="main",
        run_id="track-grant",
    )
    current = plans.current(authority)
    if current is None or current.head.status != "active":
        raise WorkerAPIError(
            ErrorKind.INVALID_REQUEST, "a track is of the session's active plan: there is none"
        )
    body = current.version.body
    if track not in {t.track_id for t in body.tracks}:
        raise WorkerAPIError(ErrorKind.NOT_FOUND, f"the session's plan has no track {track!r}")
    return current.head.ref


class TrackTools:
    """``track_read`` and ``track_progress`` for a task's run, under its grant."""

    specs = SPECS

    def __init__(
        self,
        plans: SessionPlans,
        projects: ProjectStore,
        context: ExecutionContext,
        *,
        run_id: str,
        alias: str,
        granted: PlanRef,
        track: str,
    ) -> None:
        """Pins the session's opening, as the main run's plan tools do; call it in a
        thread."""
        snapshot = projects.session_snapshot(context.project_id, context.session_id)
        opening = snapshot.session.kernel_notice_lifetime
        if opening is None:
            raise DocumentError(CONFLICT, "a track needs an open recorded session")
        self.plans = plans.pinned(opening)
        self.authority = PlanAuthority(
            scope=PlanScope(project_id=context.project_id, session_id=context.session_id),
            role="delegated",
            run_id=run_id,
            alias=alias,
            grant=granted,
            track_id=track,
        )
        self.granted, self.track = granted, track

    def call(
        self, name: str, args: dict[str, Any], *, operation: str, model: str | None
    ) -> tuple[str, Json]:
        """One call, in a thread."""
        if name == "track_read":
            if args:
                raise DocumentError(INVALID_REQUEST, "track_read takes no arguments")
            shown = self._read()
            return json.dumps(shown, ensure_ascii=False), {"track_read": shown["track"]}
        try:
            request = Progress.model_validate(args)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise DocumentError(INVALID_REQUEST, f"{where}: {first['msg']}") from None
        current = self.plans.compact_current(self.authority)
        summary = self.plans.summary(self.authority)
        if current is None or summary is None or current.head.ref != self.granted:
            raise DocumentError(
                CONFLICT,
                "plan_changed: the session's plan was revised since this delegation "
                "started; your progress on its track is refused",
            )
        if request.step_id not in {
            s["step_id"] for s in steps_of(current.version.body, self.track)
        }:
            raise DocumentError(NOT_FOUND, f"your track has no step {request.step_id!r}")
        latest = next(s.latest for s in current.steps if s.step_id == request.step_id)
        authority = self.authority.model_copy(update={"resolved_model": model})
        receipt = self.plans.progress(
            authority,
            operation,
            self.granted,
            request.step_id,
            latest.sequence if latest is not None else 0,
            request.status,
            request.note,
            expected_status_revision=summary.status_revision,
        )
        value = receipt.model_dump(mode="json")
        return json.dumps({"receipt": value}, ensure_ascii=False), {"track_progress": value}

    def _read(self) -> Json:
        current = self.plans.compact_current(self.authority)
        if current is None or current.head.ref != self.granted:
            raise DocumentError(
                CONFLICT,
                "plan_changed: the session's plan was revised since this delegation started",
            )
        status = {
            s.step_id: (s.latest.status if s.latest is not None else "pending")
            for s in current.steps
        }
        steps = [
            {**step, "status": status.get(step["step_id"], "pending")}
            for step in steps_of(current.version.body, self.track)
        ]
        return {
            "plan": self.granted.model_dump(mode="json"),
            "track": self.track,
            "steps": steps,
        }
