# SPDX-License-Identifier: MIT
"""Operator upper-bound reservations never replace unknown provider usage."""

from __future__ import annotations

import json
import sqlite3

from pydantic import Field

from sandbox_science.contracts import Contract

# Fixed SQL fragments use the caller's model-intent alias m. Every match includes
# original attempt membership; an observation from another work cannot settle it.
UNRESERVED = (
    "NOT EXISTS (SELECT 1 FROM research_observations reservation "
    "JOIN research_attempts original ON original.project=reservation.project "
    "AND original.session=reservation.session AND original.work=reservation.work "
    "WHERE reservation.project=m.project AND reservation.session=m.session "
    "AND original.run=m.run AND reservation.kind='resolution:model_reservation' "
    "AND json_extract(reservation.detail,'$.resolution.run')=m.run "
    "AND json_extract(reservation.detail,'$.resolution.identity')=m.identity)"
)

RESERVATIONS = (
    "SELECT m.project,m.session,a.work,m.run,m.identity,m.created, "
    "CASE m.kind WHEN 'reviewer' THEN 'reviewer' WHEN 'watcher' THEN 'watcher' "
    "WHEN 'embedding' THEN 'embeddings' WHEN 'skills' THEN 'metadata' ELSE w.role END AS role, "
    "json_extract(o.detail,'$.resolution.cost') AS cost, "
    "json_extract(o.detail,'$.resolution.input_tokens') AS input_tokens, "
    "json_extract(o.detail,'$.resolution.output_tokens') AS output_tokens "
    "FROM research_model_calls m JOIN research_attempts a USING(project,session,run) "
    "JOIN research_work w USING(project,session,work) "
    "JOIN research_observations o ON o.project=m.project AND o.session=m.session "
    "AND o.work=a.work AND o.kind='resolution:model_reservation' "
    "AND json_extract(o.detail,'$.resolution.run')=m.run "
    "AND json_extract(o.detail,'$.resolution.identity')=m.identity WHERE m.usage IS NULL"
)


class ModelReservation(Contract):
    """Explicit bounds and their operator evidence; actual usage remains unknown."""

    cost: float = Field(gt=0, allow_inf_nan=False)
    input_tokens: int = Field(gt=0, strict=True)
    output_tokens: int = Field(gt=0, strict=True)
    operator: str
    reason: str
    message_id: str


def reservation(
    db: sqlite3.Connection,
    project: str,
    session: str,
    work: str,
    run: str,
    identity: str,
) -> ModelReservation | None:
    """The exact original intent's recorded reservation, without claiming observation."""
    row = db.execute(
        "SELECT o.detail FROM research_observations o JOIN research_attempts a "
        "USING(project,session,work) WHERE o.project=? AND o.session=? AND o.work=? "
        "AND a.run=? AND o.kind='resolution:model_reservation' "
        "AND json_extract(o.detail,'$.resolution.run')=? "
        "AND json_extract(o.detail,'$.resolution.identity')=?",
        (project, session, work, run, run, identity),
    ).fetchone()
    if row is None:
        return None
    decision = json.loads(row[0])
    value = decision["resolution"]
    return ModelReservation(
        cost=value["cost"],
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        operator=decision["operator"],
        reason=value["reason"],
        message_id=decision["message_id"],
    )
