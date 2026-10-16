# SPDX-License-Identifier: MIT
"""What a run did, for the reviewer to match its narration against (docs/specs/
consistency-review.md, CR-2 as the coordinator amended it for lab 40).

The review controller records each tool call a run makes that may change something, of
any effect class but ``read``, as an ``action`` observation: the tool, its target as the
arguments name it, how it ended (``ok``, ``refused`` or ``error``) and what it made (a
document or ledger revision, a work record, an artifact revision, an execution, files
written, a plan's version, status or step, a memory proposal). Plan changes are actions
too (lab 42's B2): a run that recorded its steps and finished its plan, and said so, was
otherwise held to have no record of it. Actions are not review evidence one by one: a
review's snapshot holds one derived ``actions`` item for its window, listing them by
run, with the blocks each document revision in the window changed since the revision
before; a terminal review's lists the run's actions in its earlier windows too, as
context (lab 42's B5). A narrated action the list does not hold has no record (rubric
``sandbox-consistency-6``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.session_plans import PlanAuthority, PlanScope, SessionPlans

from .store import MAX_ITEM_BYTES, Json, dump

MAX_ACTIONS: Final = 500
"""The most actions one window's item lists."""
MAX_BLOCKS: Final = 100
"""The most block IDs a document revision's change lists, of each kind."""
_TARGETS: Final = (
    "document",
    "entry",
    "path",
    "name",
    "category",
    "delegation_id",
    "artifact",
    "reference",
    "step_id",
    "job_id",
    "finding",
)
"""The arguments that name what a call acts on, in the order they are looked for."""
CHANGES: Final = frozenset({"effect", "plan", "proposal", "cancel"})
"""The effect classes of the calls that may change something, every class but ``read``
(``hooks.Effect``): a ``plan`` call changes the session plan or a track's steps, a
``proposal`` makes a memory proposal, and a ``cancel`` stops a job or a delegation."""
_REFUSED: Final = frozenset({"policy_denied", "conflict", "not_found", "invalid_request"})


def action_of(messages: Sequence[Message], call: Json | None) -> Json | None:
    """The action the tool call ``messages`` ends with, when the run executed it and it
    may change something (``call``, its ``tool_records`` entry); None otherwise."""
    if not messages:
        return None
    return action_from(call, str(messages[-1].get("tool_call_id", "")))


def action_from(call: Json | None, call_id: str) -> Json | None:
    """The action of tool record ``call``, made under ``call_id``, when its effect class
    is one of ``CHANGES``: a run's own, or a delegated run's, read from its run record."""
    if call is None or call.get("effect") not in CHANGES:
        return None
    try:
        arguments: object = json.loads(str(call.get("arguments") or "{}"))
    except ValueError:
        arguments = {}
    given = cast(Json, arguments) if isinstance(arguments, dict) else {}
    target = {
        key: str(given[key])[:200]
        for key in _TARGETS
        if isinstance(given.get(key), str | int) and not isinstance(given.get(key), bool)
    }
    return {
        "tool": str(call.get("tool", ""))[:128],
        "call": call_id[:128],
        "target": target,
        "outcome": _outcome(call),
        "made": _made(call, given)[:20],
    }


def _outcome(call: Json) -> str:
    """``refused`` when a gate, a policy or the store turned the call down, ``error`` when
    it failed, else ``ok``."""
    error = call.get("error")
    document = call.get("document")
    if isinstance(document, dict) and cast(Json, document).get("outcome") in (
        "conflict",
        "refused",
    ):
        return "refused"
    if error is None:
        return "ok"
    return "refused" if str(error) in _REFUSED else "error"


def _made(call: Json, given: Json) -> list[str]:
    """What the call made, as the run's tool record says; ``given``, its arguments, name
    the step and status a plan change recorded, which its receipt does not."""
    made = _plan_made(call, given)
    document = call.get("document")
    if isinstance(document, dict):
        found = cast(Json, document)
        if found.get("outcome") not in ("conflict", "refused", "unchanged"):
            made.append(f"document {found.get('document_id')} r{found.get('revision')}")
    result = call.get("result")
    if call.get("ledger") is not None and isinstance(result, dict):
        found = cast(Json, result)
        for entry in cast(list[Any], found.get("entries") or []):
            if isinstance(entry, dict):
                changed = cast(Json, entry)
                made.append(f"ledger {changed.get('handle')} r{changed.get('revision')}")
        made += [f"work record {work}" for work in cast(list[Any], found.get("work") or [])]
    receipt = call.get("artifact_inventory")
    artifact = call.get("artifact")
    if isinstance(receipt, dict) and isinstance(cast(Json, receipt).get("revision"), dict):
        revision = cast(Json, cast(Json, receipt)["revision"])
        made.append(f"artifact {revision.get('artifact_id')} r{revision.get('revision')}")
    elif isinstance(artifact, dict):
        made.append(f"artifact {cast(Json, artifact).get('artifact_id')}")
    execution = call.get("execution")
    if isinstance(execution, dict):
        found = cast(Json, execution)
        made.append(f"execution {found.get('execution_id')} ({found.get('state')})")
    files = call.get("recorded_files")
    if isinstance(files, dict):
        made += [f"file {path}" for path in cast(list[Any], cast(Json, files).get("files") or [])]
    delegation = call.get("delegation")
    if isinstance(delegation, dict) and cast(Json, delegation).get("delegation_id"):
        made.append(f"delegation {cast(Json, delegation)['delegation_id']}")
    proposal = call.get("result") if call.get("memory") == "memory_propose" else None
    if isinstance(proposal, dict) and cast(Json, proposal).get("proposal_id"):
        found = cast(Json, proposal)
        made.append(f"memory proposal {found['proposal_id']} ({found.get('state')})")
    return made


def _plan_made(call: Json, given: Json) -> list[str]:
    """The plan change a ``plan_*`` or ``track_progress`` call recorded, from its receipt:
    the plan and version, and the step and status, or the plan's status, it recorded."""
    receipt = call.get("plan_receipt") or call.get("track_progress")
    ref = cast(Json, receipt).get("ref") if isinstance(receipt, dict) else None
    if not isinstance(ref, dict):
        return []
    found, named = cast(Json, receipt), cast(Json, ref)
    plan = f"plan {named.get('plan_id')} v{named.get('version')}"
    action = found.get("action")
    if action == "progress":
        return [f"{plan}: step {given.get('step_id')} {given.get('status')}"]
    if action == "status" and call.get("tool") == "plan_activate":
        return [f"{plan}: active"]
    if action == "status":
        outcome = given.get("outcome")
        goal = cast(Json, outcome).get("status") if isinstance(outcome, dict) else None
        return [f"{plan}: {given.get('status', 'completed')}" + (f", goal {goal}" if goal else "")]
    return [plan + (" (draft)" if action == "publish" else "")]


def plan_item(
    plans: SessionPlans, db: sqlite3.Connection, project: str, session: str, run: str
) -> tuple[Json, Json] | None:
    """The session plan as recorded in the open read ``db``, as a review's ``plan`` item
    (lab 42's B2): its locator, and its payload, which gives the plan's status and goal
    outcome and each current step's title, track and latest recorded status, with who
    recorded it and when; None when the session has no plan, or it cannot be read."""
    try:
        reader = PlanAuthority(
            scope=PlanScope(project_id=project, session_id=session), role="main", run_id=run
        )
        plan = plans.compact_in(db, reader)
    except (DocumentError, ValueError):  # a plan past the read's bound, or no run to read as
        return None
    if plan is None:
        return None
    head = plan.head
    body = {step.step_id: step for phase in plan.version.body.phases for step in phase.steps}
    steps: list[Json] = []
    for step in plan.steps:
        latest = step.latest
        steps.append(
            {
                "step": step.step_id,
                "title": body[step.step_id].title,
                "track": body[step.step_id].track_id,
                "status": latest.status if latest is not None else "pending",
                **(
                    {
                        "by": latest.author.run_id or latest.author.role,
                        "at": latest.recorded_at.isoformat(),
                        "step_changed_since": not step.matches_current_step,
                    }
                    if latest is not None
                    else {}
                ),
            }
        )
    payload: Json = {
        "about": (
            "The session plan as the platform recorded it when this review was "
            "dispatched, as context: its status, its goal outcome, and each step's latest "
            "recorded status. A plan change an answer narrates is checked against it and "
            "the actions item; an earlier plan_read in the transcript shows the plan as "
            "it was then."
        ),
        "plan": head.ref.plan_id,
        "version": head.ref.version,
        "status": head.status,
        "goal": head.outcome.status if head.outcome is not None else None,
        "steps": steps,
        "total": len(steps),
    }
    while len(dump(payload).encode()) > MAX_ITEM_BYTES // 2 and payload["steps"]:
        payload["steps"] = payload["steps"][: len(payload["steps"]) // 2]
    locator: Json = {
        "plan": head.ref.plan_id,
        "version": head.ref.version,
        "status_revision": head.status_revision,
        "progress_cursor": head.progress_cursor,
    }
    return locator, payload


def changed_blocks(documents: DocumentStore, project: str, document: str, number: int) -> Json:
    """The blocks revision ``number`` of ``document`` changed since the revision before:
    those it added or whose text differs, and those it removed, by ID, as many as
    ``MAX_BLOCKS`` of each with their counts; for a first revision, its blocks."""
    try:
        after = documents.mapped(project, document, number)
        before = documents.mapped(project, document, number - 1) if number > 1 else None
    except DocumentError:
        return {}
    new = _texts(after)
    old = _texts(before) if before is not None else {}
    changed = [block for block, text in new.items() if old.get(block) != text]
    removed = [block for block in old if block not in new]
    return {
        "since": number - 1 if number > 1 else None,
        "changed": changed[:MAX_BLOCKS],
        "changed_total": len(changed),
        "removed": removed[:MAX_BLOCKS],
        "removed_total": len(removed),
    }


def _texts(mapped: Any) -> dict[str, str]:
    text = mapped.parsed.text
    return {
        block: text[part.start : part.end]
        for block, part in zip(mapped.block_ids, mapped.parsed.blocks, strict=False)
    }


def _listed(actions: list[tuple[Json, Json]]) -> list[Json]:
    return [
        {
            **payload,
            "run": locator.get("run"),
            **({"delegation": locator["delegation"]} if locator.get("delegation") else {}),
        }
        for locator, payload in actions
    ]


@dataclass(frozen=True)
class Earlier:
    """A run's actions and document revisions in its windows reviewed before, from feed
    item ``start`` to ``end``: context for a terminal review (lab 42's B5); and ``runs``,
    the actions of earlier runs' calls whose results the run's view of its session held:
    context for any review of its work (CR-2 as the context management spec amends it)."""

    actions: list[tuple[Json, Json]]
    documents: list[Json]
    start: int
    end: int
    runs: list[tuple[Json, Json]] = field(default_factory=list[tuple[Json, Json]])


def actions_item(
    actions: list[tuple[Json, Json]],
    documents: list[Json],
    start: int,
    end: int,
    earlier: Earlier | None = None,
) -> tuple[Json, Json]:
    """One window's ``actions`` item: its locator, and its payload, which lists the
    actions by run in order, and each document revision's changed blocks, as many as fit
    one item; the transcript holds the calls themselves. ``earlier`` adds, as context,
    the run's actions in its windows reviewed before, the latest kept when they do not
    all fit, before any of the window's own is left out."""
    listed = _listed(actions)
    payload: Json = {
        "about": (
            "Each run's tool calls in this window that change something, in order, as the "
            "platform recorded them, a delegated run's naming its delegation: the tool, its "
            "target, its outcome (ok, refused or error) and what it made; and the blocks "
            "each document revision changed since the revision before. A narrated action "
            "with no ok call here has no record."
        ),
        "actions": listed[:MAX_ACTIONS],
        "total": len(listed),
        "documents": documents,
    }
    locator: Json = {"actions": "window", "from": start, "to": end}
    if earlier is not None and (earlier.actions or earlier.documents):
        before = _listed(earlier.actions)
        older: Json = {
            "about": (
                "The reviewed run's tool calls that change something, and the blocks its "
                "document revisions changed, in its windows reviewed before this one, as "
                "the platform recorded them: context for an answer that says what the run "
                "did earlier, not under review here."
            ),
            "actions": before[-MAX_ACTIONS:],
            "total": len(before),
            "documents": earlier.documents[-MAX_ACTIONS:],
        }
        payload["earlier"] = older
        while len(dump(payload).encode()) > MAX_ITEM_BYTES // 2 and (
            older["actions"] or older["documents"]
        ):
            part = "actions" if older["actions"] else "documents"
            older[part] = older[part][(len(older[part]) + 1) // 2 :]
        older["shown"] = len(older["actions"])
        locator["earlier"] = {"from": earlier.start, "to": earlier.end}
    if earlier is not None and earlier.runs:
        listed_runs = _listed(earlier.runs)
        held: Json = {
            "about": (
                "Earlier runs' tool calls that change something, each naming its run, whose "
                "results the reviewed run's view of its session held, as the platform "
                "recorded them: context for an answer that says what an earlier run did, "
                "not under review here."
            ),
            "actions": listed_runs[-MAX_ACTIONS:],
            "total": len(listed_runs),
        }
        payload["earlier_runs"] = held
        while len(dump(payload).encode()) > MAX_ITEM_BYTES // 2 and held["actions"]:
            held["actions"] = held["actions"][(len(held["actions"]) + 1) // 2 :]
        held["shown"] = len(held["actions"])
    while len(dump(payload).encode()) > MAX_ITEM_BYTES // 2 and payload["actions"]:
        payload["actions"] = payload["actions"][: len(payload["actions"]) // 2]
    payload["shown"] = len(payload["actions"])
    return locator, payload
