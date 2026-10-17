# SPDX-License-Identifier: MIT
"""Inputs are durable before a child coroutine can be scheduled."""

from __future__ import annotations

import sqlite3

from pydantic import JsonValue

from sandbox_science.contracts import Contract, ExecutionContext
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, NOT_FOUND
from sandbox_science.orchestrator.projects import run_id_of

from .records import CURRENT, Ownership, check_owner


class Blueprint(Contract):
    context: ExecutionContext
    request: dict[str, JsonValue]


def keep(
    db: sqlite3.Connection, project: str, session: str, work: str, blueprint: Blueprint
) -> None:
    encoded = blueprint.model_dump_json()
    if len(encoded.encode()) > 1048576:
        raise ValueError("recovery blueprint exceeds 1 MiB")
    if (blueprint.context.project_id, blueprint.context.session_id) != (project, session):
        raise ValueError("recovery blueprint scope differs")
    if (blueprint.request.get("project"), blueprint.request.get("session")) != (project, session):
        raise ValueError("recovery request scope differs")
    db.execute(
        "INSERT OR IGNORE INTO research_blueprints VALUES (?,?,?,?)",
        (project, session, work, encoded),
    )


def reserve_child(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    parent: str,
    blueprint: Blueprint,
) -> None:
    owner = CURRENT.get()
    if owner is None or (owner.project, owner.session, owner.run) != (project, session, parent):
        raise ControlError(CONFLICT, "child blueprint requires its parent's current authority")
    check_owner(db, owner)
    root = db.execute(
        "SELECT root FROM research_work WHERE project=? AND session=? "
        "AND work=? AND state='running'",
        (project, session, owner.work),
    ).fetchone()
    if root is None:
        raise ControlError(CONFLICT, "parent work is no longer running")
    db.execute(
        "INSERT INTO research_work VALUES (?,?,?,?,?,?, 'delegated',?, 'waiting',NULL)",
        (project, session, run, root[0], owner.work, run, run_id_of(run)),
    )
    db.execute(
        "INSERT INTO research_attempts VALUES (?,?,?,?,NULL,?,NULL)",
        (project, session, run, run, owner.epoch),
    )
    keep(db, project, session, run, blueprint)


class Blueprints:
    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def save(self, owner: Ownership, blueprint: Blueprint) -> None:
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            keep(tx.sql, owner.project, owner.session, owner.work, blueprint)

    def get(self, project: str, session: str, work: str) -> Blueprint:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT snapshot FROM research_blueprints WHERE project=? AND session=? AND work=?",
                (project, session, work),
            ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, "work has no durable recovery blueprint")
        return Blueprint.model_validate_json(row[0])
