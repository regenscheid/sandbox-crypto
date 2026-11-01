# SPDX-License-Identifier: MIT
"""A session's run tree, as the workspace reads it (plans-and-delegation spec, PD-42).

The tree is every run of the session: those whose records are kept, and those still
running or cut off, which only their events and the session's lease name. Each run has
its role, its parent and its delegation, from its record or its ``started`` event, and
its state:

- ``ended``: its ``stopped`` event is written, or its record is kept;
- ``running``: the run that holds the session's lease is it, or, for a delegated run,
  its root (delegated runs take no lease of their own, PD-32);
- ``interrupted``: otherwise: its process died, and the next main run ends its tree
  (PD-40).

Beside the runs come the session's delegations, each with its brief (the platform's
task, the inputs, the parent's note, the check, the model chosen and the limits
granted) and, once it ended, its outcome as it is kept, with its review (PD-46); and a
reopened task's earlier results. Nothing here writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final, Literal, cast

from pydantic import Field, JsonValue

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.model import Contract

from .control import ControlStore
from .delegation.records import Delegation, DelegationRecords, DelegationResult
from .documents import DocumentStore
from .projects import ProjectStore, RunRole, run_id_of, run_started
from .runstate import RunState

TreeState = Literal["running", "ended", "interrupted"]
DEPTH: Final = 8
"""How far up a run's parents are followed to its root; deeper is not a tree the
platform makes."""


class TreeRun(Contract):
    """One run of the session's tree."""

    name: str
    """The name its record is (or will be) kept under: its start time and its ID."""
    run_id: str
    role: RunRole = "main"
    parent: str | None = None
    """For a delegated run, its parent's name."""
    delegation: Identifier | None = None
    started_at: datetime
    state: TreeState
    stopped: str | None = None
    """How it ended: its record's ``stopped``, or its ``stopped`` event's."""
    kept: bool
    """Whether its record is kept; a running run's is not yet."""
    alias: str | None = None
    """The model alias its calls named, from its record (PD-29)."""
    limits: dict[str, JsonValue] | None = None
    """The limits it ran under, from its record (PD-29)."""
    tool_mix: dict[str, JsonValue] | None = None
    """How it computed: its cells, shell calls, jobs and kernel restarts, from its
    record's summaries; older records have none (tools spec, Kernels 13)."""


class RunTree(Contract):
    """A session's runs, oldest first, and its delegations, the latest ``TREE`` of them
    oldest first, with how many there are."""

    runs: list[TreeRun]
    delegations: list[Delegation]
    delegations_total: int
    earlier: list[DelegationResult] = Field(default_factory=list[DelegationResult])
    """The listed delegations' earlier results, before each one's latest: a reopened
    task's (PD-46), at most ``EARLIER`` of each, oldest first."""


class RunTrees:
    """The run trees of one control store's sessions, which its owner closes: the store
    itself, or the one a document or project store opened."""

    def __init__(self, store: ControlStore | DocumentStore | ProjectStore) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control

    def session(self, project: str, session: str) -> RunTree:
        """The session's run tree as the control store keeps it now."""
        runs = _runs(self.control, project, session)
        holder = self._holder(project, session)
        parents = {name: run.parent for name, run in runs.items()}
        shown = [
            run.model_copy(update={"state": "running"})
            if run.state != "ended" and holder is not None and _root(parents, name) == holder
            else run
            for name, run in sorted(runs.items())
        ]
        records = DelegationRecords(self.control)
        delegations, total = records.listed(project, session)
        earlier = records.earlier(
            project, session, [d.delegation_id for d in delegations if d.results > 1]
        )
        return RunTree(
            runs=shown, delegations=delegations, delegations_total=total, earlier=earlier
        )

    def state(self, project: str, session: str, run: str) -> TreeState:
        """One run's state, as the tree gives it, read from that run and its parents
        only: the workspace asks for it with every page of the run's events (RS-15)."""
        parents: dict[str, str | None] = {}
        with self.control.read() as tx:
            db = tx.sql
            ended = db.execute(
                "SELECT 1 FROM run_events WHERE project_id = ? AND session_id = ? AND run = ? "
                "AND kind = 'stopped' UNION ALL SELECT 1 FROM runs WHERE project_id = ? "
                "AND session_id = ? AND name = ? LIMIT 1",
                (project, session, run, project, session, run),
            ).fetchone()
            if ended is not None:
                return "ended"
            name: str | None = run
            for _ in range(DEPTH):
                if name is None or name in parents:
                    break
                found = db.execute(
                    "SELECT json_extract(data, '$.parent') FROM run_events WHERE project_id = ? "
                    "AND session_id = ? AND run = ? AND kind = 'started' ORDER BY seq LIMIT 1",
                    (project, session, name),
                ).fetchone()
                if found is None:
                    found = db.execute(
                        "SELECT parent FROM runs WHERE project_id = ? AND session_id = ? "
                        "AND name = ?",
                        (project, session, name),
                    ).fetchone()
                parents[name] = found[0] if found is not None else None
                name = parents[name]
        holder = self._holder(project, session)
        return "running" if holder is not None and _root(parents, run) == holder else "interrupted"

    def _holder(self, project: str, session: str) -> str | None:
        """The run that holds the session's live lease."""
        lease = RunState(self.control).lease(project, session)
        return lease.run if lease is not None else None


def _object(text: str | None) -> dict[str, JsonValue] | None:
    """A JSON object the record holds, or ``None`` for anything else."""
    if text is None:
        return None
    try:
        found: object = json.loads(text)
    except ValueError:
        return None
    return cast(dict[str, JsonValue], found) if isinstance(found, dict) else None


def _root(parents: dict[str, str | None], name: str) -> str:
    """The run at the top of ``name``'s parents, as far as they are known."""
    for _ in range(DEPTH):
        parent = parents.get(name)
        if parent is None:
            return name
        name = parent
    return name


def _runs(control: ControlStore, project: str, session: str) -> dict[str, TreeRun]:
    found: dict[str, TreeRun] = {}
    with control.read() as tx:
        db = tx.sql
        events = db.execute(
            "SELECT run, kind, data FROM run_events WHERE project_id = ? AND session_id = ? "
            "AND kind IN ('started', 'stopped') ORDER BY run, seq",
            (project, session),
        ).fetchall()
        kept = db.execute(
            "SELECT name, run_id, started_at, role, parent, delegation, "
            "json_extract(record, '$.stopped'), json_extract(record, '$.alias'), "
            "json_extract(record, '$.limits'), json_extract(record, '$.summaries.tool_mix') "
            "FROM runs WHERE project_id = ? "
            "AND session_id = ?",
            (project, session),
        ).fetchall()
        held = db.execute(
            "SELECT run FROM run_leases WHERE project_id = ? AND session_id = ?",
            (project, session),
        ).fetchone()
    started: dict[str, dict[str, JsonValue]] = {}
    stopped: dict[str, str | None] = {}
    for run, kind, data in events:
        value = cast(dict[str, JsonValue], json.loads(data))
        if kind == "started":
            started.setdefault(run, value)
        else:
            stopped[run] = _text(value.get("stopped"))
    for name, run_id, began, role, parent, delegation, ended, alias, limits, mix in kept:
        found[name] = TreeRun(
            name=name,
            run_id=run_id,
            role=role,
            parent=parent,
            delegation=delegation,
            started_at=datetime.fromisoformat(began),
            state="ended",
            stopped=ended,
            kept=True,
            alias=alias,
            limits=json.loads(limits) if limits is not None else None,
            tool_mix=_object(mix),
        )
    names: set[str] = {*started, *stopped, *([str(held[0])] if held is not None else [])}
    for name in sorted(names - set(found)):
        data = started.get(name, {})
        role = data.get("role")
        found[name] = TreeRun(
            name=name,
            run_id=str(data.get("run_id") or run_id_of(name) or name),
            role="delegated" if role == "delegated" else "main",
            parent=_text(data.get("parent")),
            delegation=_text(data.get("delegation")),
            started_at=run_started(name) or datetime.min.replace(tzinfo=UTC),
            state="ended" if name in stopped else "interrupted",
            stopped=stopped.get(name),
            kept=False,
        )
    return found


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None
