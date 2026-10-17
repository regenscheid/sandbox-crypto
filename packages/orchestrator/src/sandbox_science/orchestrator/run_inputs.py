# SPDX-License-Identifier: MIT
"""Immutable starting inputs, saved before model dispatch (research continuity RC-2).

These observations support later reconciliation. They confer no replay authority and
do not describe a surviving kernel. Historical conversations remain in the archive.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Final, Literal, Self

from pydantic import JsonValue, model_validator

from sandbox_science.contracts import Contract, ExecutionContext

from .aio import threaded
from .control import ControlError, ControlStore
from .control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from .hooks import Notice, Point, RunView, TurnHook
from .projects import RunName
from .prompts import PromptPins

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
    ) -> None:
        self.store, self.context, self.run = store, context, run
        self.request, self.configuration = request, configuration
        self.predecessor = predecessor

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point == "start":
            if run.prompt_provenance is None:
                raise ValueError("starting inputs need the assembled prompt pins")
            if self.predecessor is not None:
                try:
                    previous = await threaded(
                        self.store.get,
                        self.context.project_id,
                        self.context.session_id,
                        self.predecessor,
                    )
                except ControlError as exc:
                    if exc.kind != NOT_FOUND:
                        raise
                else:
                    if previous.pins != run.prompt_provenance:
                        raise ControlError(CONFLICT, "recovery prompt/tool/skill pins changed")
            await threaded(
                self.store.save,
                StartingInputs(
                    run=self.run,
                    context=self.context,
                    request=self.request,
                    configuration=self.configuration,
                    pins=run.prompt_provenance,
                ),
            )
        return ()
