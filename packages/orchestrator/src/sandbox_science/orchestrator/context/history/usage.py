# SPDX-License-Identifier: MIT
"""Observed model usage committed with its assistant response (RC-10/RC-13)."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Final

from pydantic import AwareDatetime, Field

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST
from sandbox_science.orchestrator.projects import ModelUsage

USAGE_BYTES: Final = 16 * 1024


class AnsweredUsage(Contract):
    """One answered response's index, observation time and unmodified usage."""

    ordinal: Annotated[int, Field(strict=True, ge=1, le=999999999)]
    observed_at: AwareDatetime
    usage: ModelUsage
    intent_id: str | None = None


def save_usage(
    db: sqlite3.Connection, project: str, message_id: str, response: AnsweredUsage
) -> None:
    """Join the archive transaction; derive scope and role from that assistant row."""
    encoded = response.usage.model_dump_json()
    if len(encoded.encode()) > USAGE_BYTES:
        raise ControlError(INVALID_REQUEST, "model usage exceeds its 16 KiB storage bound")
    try:
        written = db.execute(
            "INSERT INTO archive_model_usage "
            "(project,message_id,session,run,ordinal,role,observed,usage) "
            "SELECT project,message_id,session,run,?, "
            "CASE WHEN parent IS NULL THEN 'main' ELSE 'delegated' END,?,? "
            "FROM archive_messages WHERE project=? AND message_id=? "
            "AND role='assistant' AND kind='assistant'",
            (response.ordinal, response.observed_at.timestamp(), encoded, project, message_id),
        )
    except sqlite3.IntegrityError:
        raise ControlError(
            CONFLICT, "model usage for this response is already recorded or invalid"
        ) from None
    if written.rowcount != 1:
        raise ControlError(INVALID_REQUEST, "model usage requires its scoped assistant response")
    if response.intent_id is not None:
        from sandbox_science.orchestrator.continuity.models import observed

        session, run = db.execute(
            "SELECT session,run FROM archive_messages WHERE project=? AND message_id=?",
            (project, message_id),
        ).fetchone()
        observed(db, project, session, run, response.intent_id, response.usage)
