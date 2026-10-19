# SPDX-License-Identifier: MIT
"""Tool adapters whose original outcome the Resume reconciler understands."""

import sqlite3
from typing import Final

from pydantic import ValidationError

from sandbox_science.orchestrator.tools import PDF_SPECS

from .resources import ResourceDecision, ResourceDecisionRequest

OBSERVATION_TOOLS: Final = frozenset(
    {"wait_delegations", "delegation_status", "wait_agent_message", "wait_jobs"}
)
WORKER_TOOLS: Final = frozenset(
    {"shell", "python", "sage", "start_job", *(spec.name for spec in PDF_SPECS)}
)
RECONCILABLE_TOOLS: Final = WORKER_TOOLS | OBSERVATION_TOOLS | {"ask_user", "delegate"}


def resource_decision_receipt(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    operation: str,
    arguments: str,
) -> ResourceDecision | None:
    """Read an exact committed agent decision; never execute or infer its effect."""
    try:
        request = ResourceDecisionRequest.model_validate_json(arguments)
    except ValidationError:
        return None
    rows = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? "
        "AND kind='resource_decision' AND identity=? LIMIT 2",
        (project, session, request.request_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    decision = ResourceDecision.model_validate_json(rows[0][0])
    if (
        decision.request != request
        or decision.parent_run != run
        or decision.operation != operation
        or decision.operator is not None
    ):
        return None
    return decision


def tool_reconcilable(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    operation: str,
    name: str,
    arguments: str,
) -> bool:
    """Automatic admission requires original evidence for effectful decisions."""
    if name == "resource_decide":
        return (
            resource_decision_receipt(db, project, session, run, operation, arguments) is not None
        )
    return name in RECONCILABLE_TOOLS
