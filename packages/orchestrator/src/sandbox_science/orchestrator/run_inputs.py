# SPDX-License-Identifier: MIT
"""Immutable starting inputs, saved before model dispatch (research continuity RC-2).

These observations support later reconciliation. They confer no replay authority and
do not describe a surviving kernel. Historical conversations remain in the archive.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import Final, Literal, Self

from pydantic import JsonValue, model_validator

from sandbox_science.contracts import Contract, ExecutionContext

from .aio import threaded
from .control import ControlError, ControlStore
from .control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from .hooks import Notice, Point, RunView, TurnHook
from .projects import RunName
from .prompts import PromptPins

_log = logging.getLogger(__name__)

INPUT_BYTES: Final = 1024 * 1024


class StartingInputs(Contract):
    """Versioned observations; recovery must validate them against current authority."""

    format: Literal["run-inputs-v1"] = "run-inputs-v1"
    run: RunName
    context: ExecutionContext
    request: dict[str, JsonValue]
    configuration: dict[str, JsonValue]
    pins: PromptPins

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if (
            self.request.get("project") != self.context.project_id
            or self.request.get("session") != self.context.session_id
        ):
            raise ValueError("starting inputs must name their own project and session")
        if "history" in self.request or "stop_by" in self.request:
            raise ValueError("starting inputs exclude transcript and process-only state")
        parent, delegation = self.request.get("parent"), self.request.get("delegation")
        if (parent is None) != (delegation is None):
            raise ValueError("delegated starting inputs name both parent and delegation")
        role = "main" if parent is None else "delegated"
        if self.pins.role != role:
            raise ValueError("starting inputs must preserve the observed prompt role")
        return self


class RunInputs:
    """One immutable snapshot per scoped run. A duplicate identity is a conflict."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def save(self, inputs: StartingInputs) -> None:
        text = inputs.model_dump_json()
        if len(text.encode()) > INPUT_BYTES:
            raise ControlError(INVALID_REQUEST, "starting inputs exceed the 1 MiB bound")
        context = inputs.context
        with self.control.write() as tx:
            key = context.project_id, context.session_id, inputs.run
            if tx.sql.execute(
                "SELECT 1 FROM run_inputs WHERE project=? AND session=? AND run=?", key
            ).fetchone():
                raise ControlError(CONFLICT, "starting inputs already exist; reconcile this run")
            try:
                tx.sql.execute("INSERT INTO run_inputs VALUES (?,?,?,?)", (*key, text))
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"starting inputs refused: {error}") from None

    def get(self, project: str, session: str, run: str) -> StartingInputs:
        """Read only this scope; legacy runs have no invented starting inputs."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT snapshot FROM run_inputs WHERE project=? AND session=? AND run=?",
                (project, session, run),
            ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, "this run has no saved starting inputs")
        return StartingInputs.model_validate_json(row[0])

    def previous(
        self, project: str, session: str, run: str, *, active_run: str | None = None
    ) -> StartingInputs | None:
        """Nearest saved ancestor in this work; warn on missing historical pins."""
        try:
            return self.get(project, session, run)
        except ControlError as error:
            if error.kind != NOT_FOUND:
                raise
        with self.control.read() as tx:
            row = tx.sql.execute(
                "WITH RECURSIVE chain(run,predecessor,work,depth) AS ("
                "SELECT run,predecessor,work,0 FROM research_attempts "
                "WHERE project=? AND session=? AND run=? UNION ALL "
                "SELECT a.run,a.predecessor,a.work,c.depth+1 FROM research_attempts a "
                "JOIN chain c ON a.run=c.predecessor AND a.work=c.work "
                "WHERE a.project=? AND a.session=? AND c.depth<1000) "
                "SELECT (SELECT i.snapshot FROM chain c JOIN run_inputs i ON i.run=c.run "
                "WHERE i.project=? AND i.session=? ORDER BY c.depth LIMIT 1), "
                "(SELECT max(depth) FROM chain)",
                (project, session, run, project, session, project, session),
            ).fetchone()
        if row is None or row[0] is None:
            if row is not None and row[1] is not None and row[1] >= 1000:
                raise ControlError(CONFLICT, "recovery predecessor chain exceeds its lookup bound")
            with self.control.read() as tx:
                work = tx.sql.execute(
                    "SELECT a.work,w.current FROM research_attempts a JOIN research_work w "
                    "ON w.project=a.project AND w.session=a.session AND w.work=a.work "
                    "WHERE a.project=? AND a.session=? AND a.run=?",
                    (project, session, run),
                ).fetchone()
                if work is not None:
                    if active_run is not None and work[1] != active_run:
                        raise ControlError(CONFLICT, "recovery attempt is not the current work")
                    # The new attempt has already archived its initial task/state and
                    # started event. Those are not evidence of a prior dispatch.
                    observed = tx.sql.execute(
                        "WITH attempts AS (SELECT run FROM research_attempts WHERE "
                        "project=:project AND session=:session AND work=:work) "
                        "SELECT 1 FROM research_model_calls WHERE project=:project "
                        "AND session=:session AND run IN attempts UNION ALL "
                        "SELECT 1 FROM archive_messages WHERE project=:project "
                        "AND session=:session AND run IN attempts "
                        "AND (run<>:current OR role='assistant' OR kind='tool_result') UNION ALL "
                        "SELECT 1 FROM run_events WHERE project_id=:project "
                        "AND session_id=:session "
                        "AND run IN attempts "
                        "AND (run<>:current OR kind IN ('model','assistant','tool')) "
                        "UNION ALL SELECT 1 FROM runs WHERE project_id=:project "
                        "AND session_id=:session AND name IN attempts UNION ALL "
                        "SELECT 1 FROM run_inputs WHERE project=:project AND session=:session "
                        "AND run IN attempts LIMIT 1",
                        {
                            "project": project,
                            "session": session,
                            "work": work[0],
                            "current": active_run
                            if work[1] == active_run and active_run != run
                            else "",
                        },
                    ).fetchone()
                    if observed is None:
                        return None
                    _log.warning(
                        "prior research activity has no saved starting inputs; "
                        "continuing with newly recorded prompt pins"
                    )
                    return None
            raise ControlError(CONFLICT, "recovery has no saved ancestor starting inputs")
        return StartingInputs.model_validate_json(row[0])


class RecordInputs(TurnHook):
    """Mandatory start barrier: a persistence failure stops the loop before any call."""

    name = "inputs"

    def __init__(
        self,
        store: RunInputs,
        context: ExecutionContext,
        run: str,
        request: dict[str, JsonValue],
        configuration: dict[str, JsonValue],
        *,
        predecessor: str | None = None,
        accept_changes: bool = False,
        changed: Callable[[str], None] | None = None,
        accepted_by: str = "the user's Resume",
    ) -> None:
        self.store, self.context, self.run = store, context, run
        self.request, self.configuration = request, configuration
        self.predecessor = predecessor
        self.accept_changes, self.changed = accept_changes, changed
        self.accepted_by = accepted_by
        """Who went on past the change: the user's Resume, or a parent reopening its
        returned task (PD-46)."""
        """A continuation whose prompt, tools or skills changed since the attempt it
        continues stops, unless the user's own Resume started it (``accept_changes``): it
        then goes on, and the change is recorded with the run's starting inputs and said
        (``changed``), never presented as an exact continuation (RC-13). An upgrade of the
        platform's tools, say, would otherwise block every interrupted session."""

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point == "start":
            if run.context.prompt_provenance is None:
                raise ValueError("starting inputs need the assembled prompt pins")
            if self.predecessor is not None:
                previous = await threaded(
                    self.store.previous,
                    self.context.project_id,
                    self.context.session_id,
                    self.predecessor,
                    active_run=self.run,
                )
                if previous is not None and previous.pins != run.context.prompt_provenance:
                    if not self.accept_changes:
                        raise ControlError(CONFLICT, "recovery prompt/tool/skill pins changed")
                    if self.changed is not None:
                        self.changed(
                            "this continuation runs with other prompt, tool or skill pins than "
                            f"the attempt it continues, {self.predecessor}; "
                            f"{self.accepted_by} accepted the change"
                        )
            await threaded(
                self.store.save,
                StartingInputs(
                    run=self.run,
                    context=self.context,
                    request=self.request,
                    configuration=self.configuration,
                    pins=run.context.prompt_provenance,
                ),
            )
        return ()
