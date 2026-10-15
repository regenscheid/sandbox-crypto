# SPDX-License-Identifier: MIT
"""The reviewer's reading of the project ledger (docs/specs/consistency-review.md, CR-2,
CR-9, CR-10; docs/specs/ledger.md, LG-33).

Each revision an agent of the session makes to a ledger entry becomes an observation
of the session's reviews, of kind ``ledger``: the entry at that revision, with its
statement, fields, links and evidence, which the reviewer reads as it reads other
evidence. Its new title and statement count toward the text trigger, and a new claim
or result is a structural block (CR-10 as amended). The user's revisions are not
reviewed. The consumer reads the ledger's events after its cursor in bounded pages, and
moves the cursor and writes the observations in one change.
"""

from __future__ import annotations

from typing import Any, Final

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.ledger import Ledger

from .store import MAX_ITEM_BYTES, ReviewStore, dump

Json = dict[str, Any]

LEDGER_FEED: Final = FeedCursor("ledger")
_KINDS: Final = ("ledger.entry_created", "ledger.entry_revised")
_STRUCTURAL: Final = ("claim", "result")
"""The entries that are a structural block when made (CR-10: "a theorem or claim")."""

Observation = tuple[str, Json, Json, int, int, bool]


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
        observations = [
            found
            for event in events
            if event.session == session and event.kind in _KINDS
            if (found := _observation(ledger, project, event.subject, event.payload)) is not None
        ]
        end = events[-1].seq
        with store.documents.control.write() as tx:
            db = tx.sql
            if not LEDGER_FEED.advance(db, project, session, after, end):
                continue  # another consumer read this page; read on from its cursor
            for key, locator, payload, characters, structural, complete in observations:
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
                    effects=True,
                )
        after, pages = end, pages - 1
        if after >= target:
            break
    return {"cursor": after, "target": target, "complete": after >= target}


def _observation(ledger: Ledger, project: str, entry: str, event: Json) -> Observation | None:
    """The observation of an agent's revision of ``entry``; None for the user's."""
    view = ledger.entry(project, entry, event["revision"])
    if view.author.get("kind") not in ("agent", "worker"):
        return None
    payload: Json = view.model_dump(mode="json", exclude={"incoming", "incoming_total", "head"})
    complete = True
    if len(dump(payload).encode()) > MAX_ITEM_BYTES:
        # Fields, links and evidence are bounded but can together exceed an item; the
        # statement is kept, and the observation says it is incomplete.
        payload = {**payload, "fields": {}, "links": [], "evidence": [], "clipped": True}
        complete = False
    created = "changed" not in event
    changed = set(event.get("changed", ()))
    characters = (len(view.title) if created or "title" in changed else 0) + (
        len(view.statement) if created or "statement" in changed else 0
    )
    locator = {
        "entry": entry,
        "handle": view.handle,
        "revision": view.revision,
        "run": view.author.get("run_id"),
    }
    return (
        f"ledger:{entry}:{view.revision}",
        locator,
        payload,
        characters,
        int(created and view.type in _STRUCTURAL),
        complete,
    )
