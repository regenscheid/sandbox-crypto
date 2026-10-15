# SPDX-License-Identifier: MIT
"""A delegation's outcome as a work record on the entry it worked on (ledger spec, LG-6,
LG-F8; research artifacts' attribution, RA-34).

When a check's outcome is written, the platform records it on the entry it checked, as
the delegated run's work: what was tried (an independent check of the entry at its
revision), how it ended, and a reference to the delegation, whose outcome stays as
returned in its own record (PD-17). A check that returned a result ends
``assessment``, its note giving the verdict and status; one that returned none ends
``limit``, naming what ran out. The record's operation ID is the delegation's, so
writing it again replays it. A check changes no entry itself (PD-34): the record is the
platform's account of its work, attributed to it with the delegation as provenance.
"""

from __future__ import annotations

from typing import Any, Final

from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.projects import run_id_of

from .records import Delegation

Json = dict[str, Any]

NOTE_CHARS: Final = 1000
"""The most of a check's summary its work record's note quotes."""
LIMITS: Final = {"context_limit": "context"}
"""What ran out, by how the run stopped; a turn or budget limit is ``time``, and a
failure, a cancel or an interruption ``tools``."""
_TIME: Final = frozenset({"turn_limit", "run_budget", "budget", "gateway_budget"})


def work_record(delegation: Delegation, session: str) -> tuple[Json, Author] | None:
    """The mutation that records ``delegation``'s outcome on the entry it checked, and its
    author, the delegated run; ``None`` for a delegation still running, or one whose brief
    names no entry."""
    check = delegation.brief.get("check")
    run_id = run_id_of(delegation.run)
    if delegation.ended is None or not isinstance(check, dict) or run_id is None:
        return None
    entry, handle, revision = check.get("entry"), check.get("handle"), check.get("revision")
    if not isinstance(entry, str):
        return None
    name = handle if isinstance(handle, str) else entry
    tried = (
        f"An independent check of {name} at revision {revision}, by a delegated run with a "
        "fresh context."
    )
    record: Json = {"entry": entry, "tried": tried}
    result = delegation.result
    if result is not None:
        record["ending"] = "assessment"
        said = f"{result.get('verdict')}, {result.get('status')}"
        summary = str(result.get("summary") or "")
        note = f"{said}: {summary}" if summary else said
    else:
        record["ending"] = "limit"
        stopped = delegation.stopped or ""
        record["limit"] = LIMITS.get(stopped, "time" if stopped in _TIME else "tools")
        note = f"{delegation.ended}, {stopped or 'stopped'}: no result"
    record["references"] = [{"delegation": delegation.delegation_id, "note": _clipped(note)}]
    mutation: Json = {
        "operation_id": f"work-{delegation.delegation_id}",
        "reason": f"The outcome of delegation {delegation.delegation_id}, as it ended.",
        "work": [record],
    }
    author = Author(
        kind="agent",
        session_id=session,
        run_id=run_id,
        role="delegated",
        alias=delegation.alias,
        model=delegation.models[0] if delegation.models else None,
    )
    return mutation, author


def _clipped(text: str) -> str:
    return text if len(text) <= NOTE_CHARS else text[: NOTE_CHARS - 1] + "…"
