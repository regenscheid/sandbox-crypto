# SPDX-License-Identifier: MIT
"""A run's delegated runs' work, in its review (docs/specs/consistency-review.md, CR-2 as
the coordinator amended it for lab 41's B5).

A delegated run has no review of its own: what it did reaches its parent as an outcome.
So once a delegation of the run ends, its review window gains, from the platform's own
records: the delegated run's actions, read from its run record as the run's own are
(``actions.action_from``), naming the delegation; its executions with their output, each
in each state its calls saw it, bounded as the run's own are; the delegation's outcome,
as context; and the result files its outcome pinned, copied as a ledger entry's pins are.
Delegations of those delegations come in too. None of it makes a review due.

The executions are the record an outcome's summary rests on (lab 42's B3): a task printed
one value from Sage and its summary gave another, and a review that held only the
summary took it as checked. Each names its run, its delegation and its place among the
run's tool calls, which is where it is read from.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final, cast

from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.projects import run_id_of

from .actions import action_from
from .artifacts import pinned_artifact
from .executions import execution_projection
from .ledger import delegation_outcome
from .store import Json, ReviewStore

_log = logging.getLogger(__name__)

MAX_DEPTH: Final = 3
"""How many levels of delegations below a run its review follows."""
MAX_DELEGATED_ACTIONS: Final = 200
"""The most actions one delegated run brings into its parent's review."""
MAX_DELEGATED_EXECUTIONS: Final = 100
"""The most execution items one delegated run brings into its parent's review."""
MAX_RESULTS: Final = 8
"""The most result files one delegation brings into its parent's review."""


@dataclass(frozen=True)
class Delegated:
    """One observation a delegation brings: its key, kind, locator and payload."""

    key: str
    kind: str
    locator: Json
    payload: Json
    complete: bool = True


def ended_delegations(
    store: ReviewStore, project: str, session: str, run_id: str
) -> list[tuple[str, str, str]]:
    """The ended delegations below run ``run_id`` in the session, to ``MAX_DEPTH`` levels,
    whose outcome the session's feed does not hold yet: each its ID, its run's record
    name and the run's ID."""
    with store.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT delegation_id,parent,run,ended FROM delegations WHERE project_id=? "
            "AND session_id=? ORDER BY started_at,delegation_id LIMIT 1000",
            (project, session),
        ).fetchall()
        held = {
            key
            for (key,) in tx.sql.execute(
                "SELECT source_key FROM review_feed WHERE project=? AND session=? "
                "AND source_key GLOB 'delegated:*' AND kind='delegation'",
                (project, session),
            )
        }
    below: set[str] = set()
    found: list[tuple[str, str, str]] = []
    for _ in range(MAX_DEPTH):
        for delegation, parent, run, ended in rows:
            if run in below:
                continue
            if run_id_of(parent) == run_id or parent in below:
                below.add(run)
                child = run_id_of(run)
                if ended is not None and child and f"delegated:{delegation}" not in held:
                    found.append((delegation, run, child))
    return found


def delegated_work(
    store: ReviewStore, project: str, session: str, delegation: str, name: str, run_id: str
) -> list[Delegated] | None:
    """What delegation ``delegation``'s run (record ``name``) did, as observations of its
    parent's review: its actions, its result files and, last, its outcome; None while its
    run record is not kept yet."""
    with store.documents.control.read() as tx:
        row = tx.sql.execute(
            "SELECT json_extract(record,'$.tools') FROM runs WHERE project_id=? "
            "AND session_id=? AND name=?",
            (project, session, name),
        ).fetchone()
        files = tx.sql.execute(
            "SELECT files FROM delegations WHERE delegation_id=? AND project_id=?",
            (delegation, project),
        ).fetchone()
    if row is None:
        return None
    tools = cast(list[Any], json.loads(row[0])) if row[0] else []
    found: list[Delegated] = []
    located = {"run": run_id, "delegation": delegation}
    actions = executions = 0
    states: set[str] = set()  # an execution in a state once, as the run's own are
    for number, raw in enumerate(tools):
        call = cast(Json, raw) if isinstance(raw, dict) else None
        action = action_from(call, f"{number}") if actions < MAX_DELEGATED_ACTIONS else None
        if action is not None:
            actions += 1
            found.append(
                Delegated(
                    f"delegated:{delegation}:action:{number}",
                    "action",
                    {**located, "message": number},
                    action,
                )
            )
        for execution in _executions(call):
            identity = f"{execution['execution_id']}:{execution.get('state')}"
            if executions >= MAX_DELEGATED_EXECUTIONS or identity in states:
                continue
            executions += 1
            states.add(identity)
            payload, complete = execution_projection(execution)
            found.append(
                Delegated(
                    f"delegated:{delegation}:execution:{identity}",
                    "execution",
                    {
                        "execution": execution["execution_id"],
                        "attempt": execution.get("attempt_id"),
                        **located,
                        "message": number,
                    },
                    payload,
                    complete,
                )
            )
    recorded = cast(Json, json.loads(files[0])) if files and files[0] else {}
    results = [
        cast(Json, item)
        for part in ("results", "files")
        for item in cast(list[Any], recorded.get(part) or [])
        if isinstance(item, dict)
    ]
    seen: set[tuple[str, int]] = set()
    for item in results:
        if len(seen) >= MAX_RESULTS:
            break
        artifact, revision = item.get("artifact_id"), item.get("revision")
        if not isinstance(artifact, str) or not isinstance(revision, int):
            continue
        if (artifact, revision) in seen or item.get("content_retained") is not True:
            continue
        seen.add((artifact, revision))
        sha256 = item.get("sha256")
        copied = pinned_artifact(
            store, project, artifact, revision, sha256 if isinstance(sha256, str) else None
        )
        if copied is not None:
            locator, payload = copied
            found.append(
                Delegated(
                    f"delegated:{delegation}:file:{artifact}:{revision}",
                    "artifact",
                    {**locator, "delegated": located},
                    payload,
                )
            )
    outcome = delegation_outcome(store, project, delegation)
    if outcome is not None:
        found.append(
            Delegated(f"delegated:{delegation}", "delegation", {**located, "name": name}, outcome)
        )
    return found


def _executions(call: Json | None) -> list[Json]:
    """The executions a tool record holds, in the states its call saw them: a run's, a
    job's or a restart's, and the jobs a wait saw end (lab 41's B7)."""
    if call is None:
        return []
    ended = call.get("executions")
    found = [call.get("execution"), *(cast(list[Any], ended) if isinstance(ended, list) else [])]
    return [
        cast(Json, item)
        for item in found
        if isinstance(item, dict) and isinstance(cast(Json, item).get("execution_id"), str)
    ]


def observe_delegated(store: ReviewStore, project: str, session: str, run_id: str) -> int:
    """Record the work of each ended delegation below run ``run_id`` the session's feed
    does not hold yet; how many delegations it recorded. A record the feed refuses is
    left out, and logged."""
    recorded = 0
    for delegation, name, child in ended_delegations(store, project, session, run_id):
        found = delegated_work(store, project, session, delegation, name, child)
        if found is None:
            continue  # its run record comes later
        for item in found:
            try:
                store.observe(
                    project,
                    session,
                    item.key,
                    cast(Any, item.kind),
                    item.locator,
                    item.payload,
                    complete=item.complete,
                )
            except DocumentError:
                _log.warning("left out %s for %s/%s", item.key, project, session)
        recorded += 1
    return recorded
