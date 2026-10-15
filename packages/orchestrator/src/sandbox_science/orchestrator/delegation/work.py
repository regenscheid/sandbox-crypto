# SPDX-License-Identifier: MIT
"""A delegation's outcome as work records on the entry it worked on (ledger spec, LG-6,
LG-F8; research artifacts' attribution, RA-34).

When a delegation's outcome is written, the platform records it as the delegated run's
work, citing the delegation, whose outcome stays as returned in its own record (PD-17).
A check's is on the entry it checked: ``assessment`` when it returned a result, its
note giving the verdict and status, or ``limit``, naming what ran out. A task's is on
the issue its brief names, if any (the coordinator, 2026-10-15):

- ``produced`` when its run created entries, naming them;
- ``limit`` when it was partial, blocked or stopped, naming what ran out;
- ``limit`` with ``ideas`` when it completed and created nothing: a finished
  investigation that found nothing ran out of ideas, and leaves the issue open (LG-7).

Each line of work the task dismissed is a ``limit`` record, and each obstruction it
claims an ``obstruction_claimed`` record citing the delegation with its evidence as the
note; both are ``part_of`` the outcome's record. The records' operation ID is the
delegation's, so writing them again replays them. A check changes no entry itself
(PD-34): the records are the platform's account of the run's work, attributed to it with
the delegation as provenance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, cast

from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.projects import run_id_of

from .records import Delegation

Json = dict[str, Any]

NOTE_CHARS: Final = 1000
"""The most of a summary or evidence a work record's note quotes."""
LIMITS: Final = {"context_limit": "context"}
"""What ran out, by how the run stopped; a turn or budget limit is ``time``, and a
failure, a cancel or an interruption ``tools``."""
_TIME: Final = frozenset({"turn_limit", "run_budget", "budget", "gateway_budget", "cost_limit"})
OUTCOME: Final = "outcome"
"""The key of the outcome's record, which the others are part of."""


def work_record(
    delegation: Delegation, session: str, created: Sequence[str] = ()
) -> tuple[Json, Author] | None:
    """The mutation that records ``delegation``'s outcome as work, and its author, the
    delegated run; ``None`` for a delegation still running, or one with no entry to
    record it on (a task that names no issue). ``created`` names the entries its run
    created, by ID."""
    run_id = run_id_of(delegation.run)
    if delegation.ended is None or run_id is None:
        return None
    records = _task(delegation, created) if delegation.kind == "task" else _check(delegation)
    if not records:
        return None
    mutation: Json = {
        "operation_id": f"work-{delegation.delegation_id}",
        "reason": f"The outcome of delegation {delegation.delegation_id}, as it ended.",
        "work": records,
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


def _check(delegation: Delegation) -> list[Json]:
    check = delegation.brief.get("check")
    if not isinstance(check, dict):
        return []
    entry, handle, revision = check.get("entry"), check.get("handle"), check.get("revision")
    if not isinstance(entry, str):
        return []
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
        record["limit"] = _ran_out(delegation.stopped)
        note = f"{delegation.ended}, {delegation.stopped or 'stopped'}: no result"
    record["references"] = [_cited(delegation, note)]
    return [record]


def _task(delegation: Delegation, created: Sequence[str]) -> list[Json]:
    issue = delegation.brief.get("issue")
    entry = issue.get("entry") if isinstance(issue, dict) else None
    if not isinstance(entry, str):
        return []
    task = str(delegation.brief.get("task") or "")
    tried = _clipped(f"A delegated task: {task}", 8000)
    result = delegation.result
    record: Json = {"key": OUTCOME, "entry": entry, "tried": tried}
    status = result.get("status") if result is not None else None
    if created:
        record["ending"] = "produced"
        record["produced"] = list(created)[:64]
    elif result is None or status in ("partial", "blocked"):
        record["ending"] = "limit"
        record["limit"] = "tools" if status == "blocked" else _ran_out(delegation.stopped)
    else:
        record["ending"] = "limit"
        record["limit"] = "ideas"
    if result is not None:
        note = f"{status}: {result.get('summary') or ''}"
    else:
        note = f"{delegation.ended}, {delegation.stopped or 'stopped'}: no result"
    record["references"] = [_cited(delegation, note)]
    records = [record]
    if result is not None:
        for item in _items(result.get("dismissals")):
            records.append(
                {
                    "entry": entry,
                    "tried": _clipped(str(item.get("what") or "A line of work."), 8000),
                    "ending": "limit",
                    "limit": item.get("limit"),
                    "part_of": f"new:{OUTCOME}",
                    "references": [_cited(delegation, "dismissed by the delegated run")],
                }
            )
        for item in _items(result.get("obstructions")):
            records.append(
                {
                    "entry": entry,
                    "tried": _clipped(str(item.get("claim") or "An obstruction."), 8000),
                    "ending": "obstruction_claimed",
                    "part_of": f"new:{OUTCOME}",
                    "references": [_cited(delegation, str(item.get("evidence") or ""))],
                }
            )
    return records[:32]


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        cast(dict[str, Any], item) for item in cast(list[object], value) if isinstance(item, dict)
    ]


def _ran_out(stopped: str | None) -> str:
    found = stopped or ""
    return LIMITS.get(found, "time" if found in _TIME else "tools")


def _cited(delegation: Delegation, note: str) -> Json:
    reference: Json = {"delegation": delegation.delegation_id}
    if note.strip():
        reference["note"] = _clipped(note.strip(), NOTE_CHARS)
    return reference


def _clipped(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"
