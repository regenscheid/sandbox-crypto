# SPDX-License-Identifier: MIT
"""A run's delegated runs' work, in its review (docs/specs/consistency-review.md, CR-2 as
the coordinator amended it for lab 41's B5 and lab 42's B3).

A delegated run has no review of its own: what it did reaches its parent as an outcome.
So once a delegation of the run ends, its review window gains, from the platform's own
records: the delegation's outcome, as context; the delegated run's actions, read from its
run record as the run's own are (``actions.action_from``), naming the delegation; its
executions with their output, bounded as the run's own are; and the result files its
outcome pinned, copied as a ledger entry's pins are. Delegations of those delegations
come in too. None of it makes a review due.

The executions are the record an outcome's summary rests on (lab 42's B3): a task printed
one value from Sage and its summary gave another, and a review that held only the
summary took it as checked. A busy run has more than a review holds, so they are chosen:
first, for each number the outcome states and its brief does not, the latest whose output
holds it, or nearly, then the latest; the outcome says how many were left out. An
execution whose output was cut short brings the file its whole output was saved to, when
the run recorded it. Each names its run, its delegation and its place among the run's tool
calls, which is where it is read from: the run's record keeps each execution whole, which
the archive's tool results, sampled for the model, do not. Each also names the archived
tool result its call came back as (CR-14 as amended).

A delegation's observations are one batch, recorded in one change with the outcome first,
which marks the delegation as recorded: a review window takes a batch whole or not at all
(``ReviewStore._feed_items``), so a review that holds an execution holds its outcome.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from itertools import chain
from typing import Any, Final, cast

from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.history.memory import RunMemory
from sandbox_science.orchestrator.history.scratch import Scratch
from sandbox_science.orchestrator.projects import ProjectStore, run_id_of

from .actions import action_from
from .archived import results
from .artifacts import pinned_artifact
from .executions import execution_projection
from .ledger import delegation_outcome
from .numbers import distance, numbers
from .store import MAX_ITEM_BYTES, Json, ReviewStore, digest, dump

MAX_DEPTH: Final = 3
"""How many levels of delegations below a run its review follows."""
MAX_DELEGATED_ACTIONS: Final = 200
"""The most actions one delegated run brings into its parent's review."""
MAX_DELEGATED_EXECUTIONS: Final = 100
"""The most execution items one delegated run brings into its parent's review."""
MAX_DELEGATED_BYTES: Final = 2 * 1024 * 1024
"""The most bytes its execution items and saved outputs take, so that a delegation's batch
fits one review's snapshot whole."""
MAX_SAVED: Final = 8
"""The most saved outputs of executions cut short one delegated run brings."""
MAX_STATED: Final = 64
"""The most numbers of an outcome its executions are matched against."""
MAX_HELD: Final = 200
"""The most distinct numbers of one execution's output matched, the last it printed."""
MAX_SCANNED: Final = 128 * 1024
"""The characters at the end of each output stream its numbers are read from."""
MAX_NEAR: Final = 2000
"""The most near comparisons (edit distances) one delegation's choice makes."""
MAX_RESULTS: Final = 8
"""The most result files one delegation brings into its parent's review."""
CHOSEN: Final = (
    "for each number this outcome states and its brief does not, the latest whose output "
    "holds it, or nearly; then the latest"
)


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
            "SELECT delegation_id,parent,run,ended,EXISTS(SELECT 1 FROM review_feed f "
            "WHERE f.project=d.project_id AND f.session=d.session_id "
            "AND f.source_key='delegated:'||d.delegation_id AND f.kind='delegation') "
            "FROM delegations d WHERE project_id=? "
            "AND session_id=? ORDER BY started_at,delegation_id LIMIT 1000",
            (project, session),
        ).fetchall()
    below: set[str] = set()
    found: list[tuple[str, str, str]] = []
    for _ in range(MAX_DEPTH):
        for delegation, parent, run, ended, held in rows:
            if run in below:
                continue
            if run_id_of(parent) == run_id or parent in below:
                below.add(run)
                child = run_id_of(run)
                if ended is not None and child and not held:
                    found.append((delegation, run, child))
    return found


def delegated_work(
    store: ReviewStore,
    project: str,
    session: str,
    delegation: str,
    name: str,
    run_id: str,
    reviewed: str,
) -> list[Delegated] | None:
    """What delegation ``delegation``'s run (record ``name``, ID ``run_id``) did, as
    observations of the review of run ``reviewed``, in the order they are recorded: its
    outcome, then its actions, its chosen executions with the saved outputs of those cut
    short, and its result files; None while its run record is not kept yet."""
    projects = ProjectStore(store.documents.path.parent)
    try:
        record = projects.run(project, session, name)
    except Exception:
        projects.close()
        return None
    projects.close()
    tools: Sequence[Json | None]
    if record.stored_history is not None:
        tools = RunMemory(store.documents.control, (project, session, name)).evidence_tools()
    else:
        tools = record.tools
    with store.documents.control.read() as tx:
        files = tx.sql.execute(
            "SELECT files FROM delegations WHERE delegation_id=? AND project_id=?",
            (delegation, project),
        ).fetchone()
    located = {"run": run_id, "delegation": delegation, "for_run": reviewed}
    outcome = delegation_outcome(store, project, delegation)
    unavailable = sum(1 for call in tools if call and "_evidence_unavailable" in call)
    if outcome is not None and unavailable:
        outcome = {
            **outcome,
            "tool_evidence_unavailable": unavailable,
            "tool_evidence_limit_bytes": 8 * 1024 * 1024,
        }
    executions, left_out = _chosen(store, project, tools, outcome)
    found: list[Delegated] = []
    if outcome is not None:
        count = sum(len(listed) for listed in executions.values())
        held = {"held": count, "left_out": left_out, "chosen": CHOSEN}
        found.append(
            Delegated(
                f"delegated:{delegation}",
                "delegation",
                {**located, "name": name},
                {**outcome, "executions": held},
                complete=not unavailable,
            )
        )
    actions = 0
    # Each call's result in the session's archive, which each item names (CR-14).
    returned = results(Archive(store.documents.control), project, session, name, tools)
    for number, call in enumerate(tools):
        at = {"message": number, **({"archive": returned[number]} if returned[number] else {})}
        action = action_from(call, f"{number}") if actions < MAX_DELEGATED_ACTIONS else None
        if action is not None:
            actions += 1
            found.append(
                Delegated(
                    f"delegated:{delegation}:action:{number}",
                    "action",
                    {**located, **at},
                    action,
                )
            )
        for execution, payload, complete, saved in executions.get(number, []):
            identity = f"{execution['execution_id']}:{execution.get('state')}"
            found.append(
                Delegated(
                    f"delegated:{delegation}:execution:{identity}",
                    "execution",
                    {
                        "execution": execution["execution_id"],
                        "attempt": execution.get("attempt_id"),
                        **located,
                        **at,
                    },
                    payload,
                    complete,
                )
            )
            for stream, (locator, output) in saved.items():
                output_of = {"execution": execution["execution_id"], "stream": stream}
                found.append(
                    Delegated(
                        f"delegated:{delegation}:output:{identity}:{stream}",
                        "artifact",
                        {**locator, "delegated": located, "output_of": output_of},
                        output,
                    )
                )
    found += _results(store, project, delegation, files[0] if files else None, located)
    return found


Chosen = tuple[Json, Json, bool, dict[str, tuple[Json, Json]]]
"""An execution chosen for a review: its record, its projection, whether that is complete,
and the saved outputs of its streams cut short, each its locator and copy."""


def _chosen(
    store: ReviewStore, project: str, tools: Sequence[Json | None], outcome: Json | None
) -> tuple[dict[int, list[Chosen]], int]:
    """The executions a review holds of a delegated run's tool records ``tools``, by the
    number of the record each was read from, in the order the calls saw them, and how many
    it leaves out: each in each state its calls saw it, once; as many as
    ``MAX_DELEGATED_EXECUTIONS`` within ``MAX_DELEGATED_BYTES``. First, for each number
    ``outcome`` states, the latest execution whose output holds it (``_decisive``), then
    the latest, so that a busy run's last, decisive ones are not the ones left out (the
    review of lab 42's B3)."""
    with closing(Scratch[Json]()) as executions, closing(Scratch[int]()) as positions:
        for number, call in enumerate(tools):
            for execution in _executions(call):
                identity = f"{execution['execution_id']}:{execution.get('state')}"
                if executions.append(execution, identity=identity):
                    positions.append(number)
        return _pick(store, project, tools, outcome, executions, positions)


def _pick(
    store: ReviewStore,
    project: str,
    tools: Sequence[Json | None],
    outcome: Json | None,
    every: Sequence[Json],
    positions: Sequence[int],
) -> tuple[dict[int, list[Chosen]], int]:
    first = _decisive(every, outcome)
    taken = set(first)
    rest = (index for index in reversed(range(len(every))) if index not in taken)
    order = chain(first, rest)
    picked: dict[int, tuple[Json, bool, dict[str, tuple[Json, Json]]]] = {}
    size = saved = 0
    for index in order:
        if len(picked) >= MAX_DELEGATED_EXECUTIONS:
            break
        number, execution = positions[index], every[index]
        payload, complete = execution_projection(execution)
        cost = len(dump(payload).encode())
        if size + cost > MAX_DELEGATED_BYTES:
            continue
        size += cost
        outputs: dict[str, tuple[Json, Json]] = {}
        for stream in ("stdout", "stderr"):
            if saved >= MAX_SAVED or not payload.get(f"{stream}_truncated"):
                continue
            copied = _saved_output(store, project, tools[number], stream)
            cost = len(dump(copied).encode()) if copied is not None else 0
            if copied is not None and size + cost <= MAX_DELEGATED_BYTES:
                outputs[stream] = copied
                size, saved = size + cost, saved + 1
        picked[index] = (payload, complete, outputs)
    chosen: dict[int, list[Chosen]] = {}
    for index in sorted(picked):
        number, execution = positions[index], every[index]
        chosen.setdefault(number, []).append((execution, *picked[index]))
    return chosen, len(every) - len(picked)


def _decisive(executions: Sequence[Json], outcome: Json | None) -> list[int]:
    """For each number ``outcome`` states, the latest of ``executions`` whose output
    holds it, or, when none does, the latest that holds one within the edits a number
    copied by hand is off by (``numbers.near``'s bound, its last two digits included: p
    and p + 2 are the pair a misstated summary makes); their indices, once each, in the
    order the numbers come.

    The numbers are read from the outcome's result and report themselves, as many as
    ``MAX_STATED``, leaving out those its brief states: an input every call prints, such
    as the modulus, would match them all (the review of #449). An output's numbers are a
    set, matched exactly first, and at most ``MAX_NEAR`` near comparisons are made in
    all, so that a delegation with many numbers cannot stall its parent's safe point."""
    if outcome is None:
        return []
    given = set(_stated(outcome.get("brief")))
    told = _stated([outcome.get("result"), outcome.get("report")])
    stated = [number for number in dict.fromkeys(told) if number not in given][:MAX_STATED]
    if not stated:
        return []
    found: dict[str, int] = {}
    for index, execution in enumerate(executions):
        lengths = _held(execution)
        for number in stated:
            if number in lengths.get(len(number), ()):
                found[number] = index  # the latest wins
    near = MAX_NEAR
    for number in (number for number in stated if number not in found):
        if near <= 0:
            break
        bound = max(3, len(number) * 5 // 100)
        lengths = range(len(number) - bound, len(number) + bound + 1)
        for index in reversed(range(len(executions))):
            held = _held(executions[index])
            values = [value for length in lengths for value in held.get(length, ())]
            compared = values[: max(near, 0)]
            near -= len(compared)
            if any(distance(number, value, bound) is not None for value in compared):
                found[number] = index
                break
            if near <= 0:
                break
    return list(dict.fromkeys(found[number] for number in stated if number in found))


def _stated(value: object) -> list[str]:
    """The numbers of 10 or more digits the strings in ``value`` state, in order, read
    through the separators that group digits; a whole number given as a number counts."""
    if isinstance(value, str):
        return numbers(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return numbers(str(value))
    if isinstance(value, dict):
        return [found for item in cast(Json, value).values() for found in _stated(item)]
    if isinstance(value, list):
        return [found for item in cast(list[Any], value) for found in _stated(item)]
    return []


def _held(execution: Json) -> dict[int, set[str]]:
    """The distinct numbers an execution's output holds, by their length, read from the
    last ``MAX_SCANNED`` characters of each stream, the last ``MAX_HELD`` it printed."""
    result = cast(Json, execution.get("result") or {})
    text = "\n".join(
        str(cast(Json, result.get(stream) or {}).get("text", ""))[-MAX_SCANNED:]
        for stream in ("stdout", "stderr")
    )
    lengths: dict[int, set[str]] = {}
    for number in list(dict.fromkeys(numbers(text)))[-MAX_HELD:]:
        lengths.setdefault(len(number), set()).add(number)
    return lengths


def _saved_output(
    store: ReviewStore, project: str, call: Json | None, stream: str
) -> tuple[Json, Json] | None:
    """The file the harness saved ``stream``'s whole output of ``call`` to (tools spec
    Shell 5), as the run recorded it, copied as a ledger entry's pins are, so at most
    64 KiB of text; None when there is none, it was not recorded with its content, or it
    cannot be copied whole."""
    if call is None:
        return None
    path = cast(Json, call.get("saved_output") or {}).get(stream)
    listed = cast(Json, call.get("recorded_files") or {}).get("files")
    if not isinstance(path, str) or not isinstance(listed, list):
        return None
    for raw in cast(list[Any], listed):
        entry = cast(Json, raw) if isinstance(raw, dict) else {}
        named = entry.get("path")
        if not isinstance(named, str) or not (named == path or named.endswith(f"/{path}")):
            continue
        artifact, revision, sha256 = (entry.get(k) for k in ("artifact_id", "revision", "sha256"))
        if (
            entry.get("content_retained") is not True
            or not isinstance(artifact, str)
            or not isinstance(revision, int)
            or not isinstance(sha256, str)
        ):
            return None
        return pinned_artifact(store, project, artifact, revision, sha256)
    return None


def _results(
    store: ReviewStore, project: str, delegation: str, files: str | None, located: Json
) -> list[Delegated]:
    """The result files a delegation's outcome pinned (``files``, as its record keeps
    them), as many as ``MAX_RESULTS``, copied as a ledger entry's pins are."""
    recorded = cast(Json, json.loads(files)) if files else {}
    results = [
        cast(Json, item)
        for part in ("results", "files")
        for item in cast(list[Any], recorded.get(part) or [])
        if isinstance(item, dict)
    ]
    found: list[Delegated] = []
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
    does not hold yet; how many delegations it recorded. Each delegation's observations
    are recorded in one change, its outcome first, so the feed holds its batch whole or
    not at all. Oversized observations become explicit incomplete markers."""
    recorded = 0
    for delegation, name, child in ended_delegations(store, project, session, run_id):
        found = delegated_work(store, project, session, delegation, name, child, run_id)
        if found is None:
            continue  # its run record comes later
        bounded: list[Delegated] = []
        omitted = 0
        for item in found:
            payload, locator = dump(item.payload), dump(item.locator)
            if len(payload.encode()) > MAX_ITEM_BYTES or len(locator.encode()) > 4096:
                omitted += 1
                marker = {
                    "omitted": "delegated observation exceeds review bound",
                    "kind": item.kind,
                    "sha256": digest(payload),
                }
                bounded.append(
                    Delegated(
                        item.key,
                        item.kind,
                        {"delegation": delegation, "for_run": run_id, "key": item.key},
                        marker,
                        complete=False,
                    )
                )
            else:
                bounded.append(item)
        if omitted and bounded and bounded[0].kind == "delegation":
            bounded[0] = replace(
                bounded[0],
                payload={**bounded[0].payload, "review_observations_omitted": omitted},
                complete=False,
            )
            if len(dump(bounded[0].payload).encode()) > MAX_ITEM_BYTES:
                bounded[0] = Delegated(
                    bounded[0].key,
                    "delegation",
                    {"delegation": delegation, "for_run": run_id},
                    {
                        "omitted": "delegated outcome exceeds review bound",
                        "review_observations_omitted": omitted,
                        "sha256": digest(dump(found[0].payload)),
                    },
                    complete=False,
                )
        store.scope(project, session)
        with store.documents.control.write() as tx:
            for item in bounded:
                store.record_observation(
                    tx.sql,
                    project,
                    session,
                    item.key,
                    item.kind,
                    item.locator,
                    item.payload,
                    complete=item.complete,
                )
        recorded += 1
    return recorded
