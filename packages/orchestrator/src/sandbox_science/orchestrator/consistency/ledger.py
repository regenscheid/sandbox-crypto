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

import logging
import sqlite3
from typing import Any, Final

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.ledger import EntryView, Ledger

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
        # The quotes' sources are found before the write, which only records them.
        with store.documents.control.read() as tx:
            quoted = [item for _, view in seen for item in quoted_items(tx.sql, project, view)]
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
