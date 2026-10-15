# SPDX-License-Identifier: MIT
"""The reviewer's reading of the project ledger (docs/specs/consistency-review.md, CR-2,
CR-9, CR-10; docs/specs/ledger.md, LG-33).

Each revision an agent of the session makes to a ledger entry becomes an observation
of the session's reviews, of kind ``ledger``: the entry at that revision, with its
statement, fields, links and evidence, which the reviewer reads as it reads other
evidence. A revision's new title and statement count toward the text trigger, and a new
claim or result is a structural block (CR-10 as amended). The flags an agent's change
raises are observations too, which make no review due, and so is an agent's clearing of
a flag, with its reason (LG-29). The user's revisions and clearings are not reviewed.
The artifact revisions an observed revision's evidence pins come with it, as artifact
observations that make no review due, once in each review window, whichever session
published them (CR-2, CR-14 as amended): the reviewer compares what the entry states with
what the computation wrote. So do the recorded sources of its quotes of references,
the tool results an agent read them in, as the platform holds them (CR-4).
The consumer reads the ledger's events after its cursor in bounded pages, and moves the
cursor and writes the observations in one change.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Final

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.ledger import EntryView, Ledger, gate

from .artifacts import pinned_artifact
from .quotes import quoted_items
from .store import MAX_ITEM_BYTES, ReviewStore, dump

Json = dict[str, Any]

_log = logging.getLogger(__name__)

LEDGER_FEED: Final = FeedCursor("ledger")
_KINDS: Final = ("ledger.entry_created", "ledger.entry_revised")
_FLAGS: Final = ("ledger.flags_raised", "ledger.flags_cleared")
_STRUCTURAL: Final = ("claim", "result")
"""The entries that are a structural block when made (CR-10: "a theorem or claim")."""

Observation = tuple[str, Json, Json, int, int, bool, bool]
"""Its source key, locator, payload, characters, structural blocks, whether it is
complete, and whether it is an effect of the run."""

MAX_PINNED: Final = 8
"""The most artifact revisions one entry revision brings into a review."""

MAX_CONTEXT: Final = 8
"""The most document revisions, delegation outcomes and entries one entry revision's
evidence brings into a review, besides its artifacts."""

MAX_TEXT: Final = 16384
"""The most characters of a delegation's report or brief that come with its outcome."""


def ingest_ledger(store: ReviewStore, project: str, session: str, *, drain: bool = False) -> Json:
    """Observe the session's agents' ledger revisions through the ledger's event head:
    one page of 200 events, or up to 50 pages when ``drain``."""
    store.scope(project, session)
    if store.documents.control.missing:
        return {"cursor": 0, "target": 0, "complete": True}
    log = EventLog(store.documents.control, "ledger_entry", store.documents.producer)
    ledger = Ledger(store.documents)
    with store.documents.control.read() as tx:
        target = log.head(tx.sql, project)
    after, pages = 0, 50 if drain else 1
    while pages:
        with store.documents.control.read() as tx:
            after = LEDGER_FEED.position(tx.sql, project, session)
            events = log.page(tx.sql, project, after, 200, through=target)
        if not events:
            break
        seen = [
            found
            for event in events
            if event.session == session and event.kind in _KINDS
            if (found := _observation(ledger, project, event.subject, event.payload)) is not None
        ]
        pinned = [item for _, view in seen for item in pinned_items(store, project, view)]
        context = [item for _, view in seen for item in context_items(store, project, view)]
        # The quotes' sources are found before the write, which only records them.
        with store.documents.control.read() as tx:
            quoted = [
                item for _, view in seen for item in quoted_items(tx.sql, project, session, view)
            ]
        observations = [observation for observation, _ in seen] + [
            _flag(event.kind, event.payload)
            for event in events
            if event.session == session and event.kind in _FLAGS
        ]
        end = events[-1].seq
        with store.documents.control.write() as tx:
            db = tx.sql
            if not LEDGER_FEED.advance(db, project, session, after, end):
                continue  # another consumer read this page; read on from its cursor
            for key, locator, payload, characters, structural, complete, effects in observations:
                store.record_observation(
                    db,
                    project,
                    session,
                    key,
                    "ledger",
                    locator,
                    payload,
                    complete=complete,
                    characters=characters,
                    structural=structural,
                    effects=effects,
                )
            watermark = db.execute(
                "SELECT coalesce(max(watermark),0) FROM review_windows "
                "WHERE project=? AND session=?",
                (project, session),
            ).fetchone()[0]
            for locator, payload in pinned:
                pin = locator["artifact"]
                key = f"pinned:{watermark}:{pin['artifact_id']}:{pin['revision']}"
                _once(store, db, project, session, key, "artifact", locator, payload)
            for locator, payload in quoted:
                by = locator["quoted_by"]
                key = f"quoted:{watermark}:{by['entry']}:{by['revision']}:{by['evidence']}"
                _once(store, db, project, session, key, "source", locator, payload)
            for kind, name, locator, payload in context:
                key = f"pinned:{watermark}:{name}"
                _once(store, db, project, session, key, kind, locator, payload)
        after, pages = end, pages - 1
        if after >= target:
            break
    return {"cursor": after, "target": target, "complete": after >= target}


def _once(
    store: ReviewStore,
    db: sqlite3.Connection,
    project: str,
    session: str,
    key: str,
    kind: str,
    locator: Json,
    payload: Json,
) -> None:
    """Record what an observed entry revision brings with it, once under ``key``: a
    record already there is kept, and one the feed refuses is left out, so the ledger's
    cursor still moves on (the review of CR-4)."""
    if db.execute(
        "SELECT 1 FROM review_feed WHERE project=? AND session=? AND source_key=?",
        (project, session, key),
    ).fetchone():
        return
    try:
        store.record_observation(db, project, session, key, kind, locator, payload)
    except DocumentError:
        _log.warning("left out %s for %s/%s: the review feed refused it", key, project, session)


def _fitted(payload: Json) -> tuple[Json, bool]:
    """The entry within one review item, and whether all of it is there. The fields,
    statement and every link and evidence link are kept; when they do not fit, the
    links' notes and the evidence's scopes are shortened, and the item says it is
    incomplete, so that it covers nothing in the gate (LG-17)."""
    if len(dump(payload).encode()) <= MAX_ITEM_BYTES:
        return payload, True
    for keep in (1000, 200, 0):
        shortened = {
            **payload,
            "links": [_shortened(link, "note", keep) for link in payload["links"]],
            "evidence": [_shortened(link, "scope", keep) for link in payload["evidence"]],
            "shortened": True,
        }
        if len(dump(shortened).encode()) <= MAX_ITEM_BYTES:
            return shortened, False
    return {**payload, "fields": {}, "links": [], "evidence": [], "clipped": True}, False


def _shortened(link: Json, key: str, keep: int) -> Json:
    text = link.get(key)
    if not isinstance(text, str) or len(text) <= keep:
        return link
    return {**link, key: text[:keep] + "…" if keep else None}


def entry_item(view: EntryView) -> tuple[Json, Json, bool]:
    """An entry revision as review evidence: its locator, the entry as it reads at that
    revision, and whether all of it fits one item."""
    payload: Json = view.model_dump(
        mode="json",
        exclude={"incoming", "incoming_total", "head", "findings", "flags", "open_flags"},
    )
    payload, complete = _fitted(payload)
    locator = {
        "entry": view.entry_id,
        "handle": view.handle,
        "revision": view.revision,
        "run": view.author.get("run_id"),
    }
    return locator, payload, complete


def pinned_items(store: ReviewStore, project: str, view: EntryView) -> list[tuple[Json, Json]]:
    """The artifact revisions ``view``'s evidence pins, as many as ``MAX_PINNED``, each
    copied whole as review evidence with the entry revision that pins it; one whose text
    cannot be copied whole is left to the entry's evidence, which names it."""
    found: list[tuple[Json, Json]] = []
    seen: set[tuple[str, int]] = set()
    for link in view.evidence:
        if link.target != "artifact" or link.revision is None:
            continue
        if (link.target_id, link.revision) in seen or len(found) >= MAX_PINNED:
            continue
        seen.add((link.target_id, link.revision))
        copied = pinned_artifact(store, project, link.target_id, link.revision, link.sha256)
        if copied is not None:
            locator, payload = copied
            by = {"entry": view.entry_id, "handle": view.handle, "revision": view.revision}
            found.append(({**locator, "pinned_by": by}, payload))
    return found


def context_items(
    store: ReviewStore, project: str, view: EntryView
) -> list[tuple[str, str, Json, Json]]:
    """What the evidence that bears on ``view``, or checks it, pins besides artifacts,
    brought into its review as context, as many as ``MAX_CONTEXT`` (CR-14 as amended for
    lab 40's B3): the document revision a block is in, naming the blocks, which the
    reviewer reads by block; a delegation's outcome, its texts bounded; and an entry at
    the revision pinned. Each is its kind, its name for the window's key, its locator,
    which names the entry revision that pins it (``pinned_by``), and its payload. They
    make no review due, and they are not under review: a pinned entry is an ``entry``
    item, never a ``ledger`` one, so no review covers it by holding it. One that cannot be
    read, or does not fit one item, is left to the entry's evidence, which names it."""
    by = {"entry": view.entry_id, "handle": view.handle, "revision": view.revision}
    links = [
        link for link in view.evidence if gate.bearing(link) or link.role == "independent_check"
    ]
    blocks: dict[tuple[str, int], list[str]] = {}
    for link in links:
        if link.target == "block" and link.revision is not None and link.block is not None:
            named = blocks.setdefault((link.target_id, link.revision), [])
            if link.block not in named:
                named.append(link.block)
    found: list[tuple[str, str, Json, Json]] = []
    for (document, revision), named in blocks.items():
        try:
            info = store.documents.revision(project, document, revision).info
        except DocumentError:
            continue
        common: Json = {"document": document, "revision": revision, "sha256": info.sha256}
        found.append(
            (
                "document",
                f"document:{document}:{revision}",
                {**common, "blocks": named, "pinned_by": by},
                {
                    **common,
                    "author": info.author.model_dump(mode="json"),
                    "reason": info.reason,
                    "tombstone": info.tombstone,
                },
            )
        )
    seen: set[str] = set()
    for link in links:
        if link.target == "delegation" and link.target_id not in seen:
            seen.add(link.target_id)
            outcome = delegation_outcome(store, project, link.target_id)
            if outcome is not None:
                locator = {"delegation": link.target_id, "pinned_by": by}
                found.append(("delegation", f"delegation:{link.target_id}", locator, outcome))
        elif link.target == "entry" and link.revision is not None:
            name = f"entry:{link.target_id}:{link.revision}"
            if name in seen:
                continue
            seen.add(name)
            try:
                target = Ledger(store.documents).entry(project, link.target_id, link.revision)
            except DocumentError:
                continue
            located, payload, complete = entry_item(target)
            if complete:
                located.pop("run", None)
                found.append(("entry", name, {**located, "pinned_by": by}, payload))
    return found[:MAX_CONTEXT]


def delegation_outcome(store: ReviewStore, project: str, delegation: str) -> Json | None:
    """A delegation's outcome as review evidence: what it was asked, how it ended, the
    result the child returned with its verdict and coverage, what the platform saw it
    read, and its report, the long texts bounded; None when it has not ended."""
    with store.documents.control.read() as tx:
        row = tx.sql.execute(
            "SELECT kind,brief,ended,stopped,result,report,refused,read_entry,read_proofs,"
            "alias,models FROM delegations WHERE delegation_id=? AND project_id=?",
            (delegation, project),
        ).fetchone()
    if row is None or row[2] is None:
        return None
    kind, brief, ended, stopped, result, report, refused, read, proofs, alias, models = row
    brief_text = brief if len(brief) <= MAX_TEXT else brief[:MAX_TEXT] + "…"
    outcome: Json = {
        "delegation": delegation,
        "kind": kind,
        "brief": json.loads(brief) if len(brief) <= MAX_TEXT else {"clipped": brief_text},
        "ended": ended,
        "stopped": stopped,
        "result": None if result is None else json.loads(result),
        "read_entry": None if read is None else bool(read),
        "read_proofs": None if proofs is None else bool(proofs),
        "refused": refused,
        "alias": alias,
        "models": None if models is None else json.loads(models),
        "report": None if report is None else report[:MAX_TEXT],
        "report_clipped": report is not None and len(report) > MAX_TEXT,
    }
    return outcome if len(dump(outcome).encode()) <= MAX_ITEM_BYTES else None


def _observation(
    ledger: Ledger, project: str, entry: str, event: Json
) -> tuple[Observation, EntryView] | None:
    """The observation of an agent's revision of ``entry``, and the entry as it reads at
    that revision; None for the user's."""
    view = ledger.entry(project, entry, event["revision"])
    if view.author.get("kind") not in ("agent", "worker"):
        return None
    locator, payload, complete = entry_item(view)
    created = "changed" not in event
    changed = set(event.get("changed", ()))
    characters = (len(view.title) if created or "title" in changed else 0) + (
        len(view.statement) if created or "statement" in changed else 0
    )
    return (
        f"ledger:{entry}:{view.revision}",
        locator,
        payload,
        characters,
        int(created and view.type in _STRUCTURAL),
        complete,
        True,
    ), view


def _flag(kind: str, event: Json) -> Observation:
    """The flags one agent's change raised, which are not the run's effect and make no
    review due, or those it cleared, with its reason: one item, which lists as many as
    the event does and counts them all."""
    cleared = kind == "ledger.flags_cleared"
    action = "cleared" if cleared else "raised"
    locator = {
        "flags": action,
        "operation_id": event["operation_id"],
        "total": event["total"],
        "run": event.get("run"),
    }
    return (f"ledger:flags:{event['operation_id']}:{action}", locator, event, 0, 0, True, cleared)
