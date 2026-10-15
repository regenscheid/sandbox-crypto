# SPDX-License-Identifier: MIT
"""What a run did, for the reviewer to match its narration against (docs/specs/
consistency-review.md, CR-2 as the coordinator amended it for lab 40).

The review controller records each tool call a run makes whose effect class is
``effect`` as an ``action`` observation: the tool, its target as the arguments name it,
how it ended (``ok``, ``refused`` or ``error``) and what it made (a document or ledger
revision, a work record, an artifact revision, an execution, files written). Actions
are not review evidence one by one: a review's snapshot holds one derived ``actions``
item for its window, listing them by run, with the blocks each document revision in
the window changed since the revision before. A narrated action the list does not hold
has no record (rubric ``sandbox-consistency-4``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final, cast

from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.model import Message

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
)
"""The arguments that name what a call acts on, in the order they are looked for."""
_REFUSED: Final = frozenset({"policy_denied", "conflict", "not_found", "invalid_request"})


def action_of(messages: Sequence[Message], call: Json | None) -> Json | None:
    """The action the tool call ``messages`` ends with, when the run executed it and its
    effect class is ``effect`` (``call``, its ``tool_records`` entry); None otherwise."""
    if not messages:
        return None
    return action_from(call, str(messages[-1].get("tool_call_id", "")))


def action_from(call: Json | None, call_id: str) -> Json | None:
    """The action of tool record ``call``, made under ``call_id``, when its effect class
    is ``effect``: a run's own, or a delegated run's, read from its run record."""
    if call is None or call.get("effect") != "effect":
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
        "made": _made(call)[:20],
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


def _made(call: Json) -> list[str]:
    """What the call made, as the run's tool record says."""
    made: list[str] = []
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
    return made


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


def actions_item(
    actions: list[tuple[Json, Json]], documents: list[Json], start: int, end: int
) -> tuple[Json, Json]:
    """One window's ``actions`` item: its locator, and its payload, which lists the
    actions by run in order, and each document revision's changed blocks, as many as fit
    one item; the transcript holds the calls themselves."""
    listed = [
        {
            **payload,
            "run": locator.get("run"),
            **({"delegation": locator["delegation"]} if locator.get("delegation") else {}),
        }
        for locator, payload in actions
    ]
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
    while len(dump(payload).encode()) > MAX_ITEM_BYTES // 2 and payload["actions"]:
        payload["actions"] = payload["actions"][: len(payload["actions"]) // 2]
    payload["shown"] = len(payload["actions"])
    locator = {"actions": "window", "from": start, "to": end}
    return locator, payload
