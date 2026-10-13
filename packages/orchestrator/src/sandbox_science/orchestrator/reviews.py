# SPDX-License-Identifier: MIT
"""When a checkpoint review is due, from the events of a session's document changes (CR-10).

The live reviewer is implemented in ``consistency/``. This historical trigger helper
describes the document-change window: CR-10, as amended for LD-40, counts a revision by its text
and its structural blocks, never as an artifact revision, so a few small edits do not
dispatch a review by themselves. A window gathers the session's agent-written document
revisions since the last dispatch (its watermark); a review is due once the minimum
interval has passed and the agents wrote at least ``text_characters`` characters or added
a structural block. Its snapshot is the revisions in the window.

The window reads only what the events report: the platform computed the characters and
structural blocks from block maps and authorship (LD-40), and agent narration never
supplies them (CR-9). The user's own changes are not the agents' work, so they count
toward no review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from .documents.models import EventRecord

MIN_INTERVAL: Final = timedelta(seconds=120)
TEXT_CHARACTERS: Final = 2000


@dataclass(frozen=True)
class Snapshot:
    """What a dispatched review covers: document revisions, as (document, revision)."""

    revisions: tuple[tuple[str, int], ...]
    characters: int
    structural: tuple[str, ...]


@dataclass
class CheckpointWindow:
    """One session's document work since its last review dispatch."""

    session_id: str
    last_dispatch: datetime | None = None
    min_interval: timedelta = MIN_INTERVAL
    text_characters: int = TEXT_CHARACTERS
    characters: int = 0
    structural: list[str] = field(default_factory=list[str])
    revisions: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    watermark: int = 0
    """The sequence number of the last event the window took in."""

    def observe(self, event: EventRecord) -> None:
        """Take in one event of the project's, after the watermark."""
        if event.seq <= self.watermark:
            return
        self.watermark = event.seq
        payload = event.payload
        if event.kind != "document.revision_created" or event.session_id != self.session_id:
            return
        if payload.get("author_kind") != "agent":
            return
        revision = payload.get("revision")
        if isinstance(revision, int):
            self.revisions.append((event.document_id, revision))
        characters = payload.get("agent_characters")
        if isinstance(characters, int):
            self.characters += characters
        added = payload.get("structural_added")
        if isinstance(added, list):
            for entry in added:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    self.structural.append(f"{event.document_id}:{entry['id']}")

    def due(self, now: datetime) -> bool:
        """Whether the document predicates of CR-10 call for a checkpoint review now."""
        if self.last_dispatch is not None and now - self.last_dispatch < self.min_interval:
            return False
        return self.characters >= self.text_characters or bool(self.structural)

    def dispatch(self, now: datetime) -> Snapshot:
        """The review's snapshot; the window starts again after it."""
        snapshot = Snapshot(tuple(self.revisions), self.characters, tuple(self.structural))
        self.last_dispatch = now
        self.characters = 0
        self.structural = []
        self.revisions = []
        return snapshot
