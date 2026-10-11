# SPDX-License-Identifier: MIT
"""The document predicates of CR-10, as the checkpoint window reads them from events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sandbox_science.orchestrator.documents.models import EventRecord
from sandbox_science.orchestrator.reviews import CheckpointWindow

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _event(seq: int, *, session: str | None = "s1", **payload: Any) -> EventRecord:
    fields: dict[str, Any] = {
        "revision": seq,
        "author_kind": "agent",
        "agent_characters": 0,
        "structural_added": [],
        **payload,
    }
    return EventRecord(
        seq=seq,
        time=NOW,
        kind="document.revision_created",
        producer="harness",
        project_id="p1",
        session_id=session,
        document_id="doc-1",
        payload=fields,
    )


def test_text_adds_up_until_a_review_is_due() -> None:
    window = CheckpointWindow("s1", last_dispatch=NOW - timedelta(minutes=5))
    window.observe(_event(1, agent_characters=1500))
    assert not window.due(NOW)
    window.observe(_event(2, agent_characters=500))
    assert window.due(NOW)
    snapshot = window.dispatch(NOW)
    assert snapshot.revisions == (("doc-1", 1), ("doc-1", 2))
    assert snapshot.characters == 2000
    assert not window.due(NOW)


def test_a_structural_block_is_enough_once_the_interval_has_passed() -> None:
    window = CheckpointWindow("s1", last_dispatch=NOW - timedelta(seconds=60))
    window.observe(_event(1, structural_added=[{"id": "b9", "kind": "table"}]))
    assert not window.due(NOW)
    assert window.due(NOW + timedelta(seconds=60))
    assert window.dispatch(NOW).structural == ("doc-1:b9",)


def test_other_sessions_the_user_and_repeated_events_count_for_nothing() -> None:
    window = CheckpointWindow("s1")
    window.observe(_event(1, session="s2", agent_characters=5000))
    window.observe(_event(2, session=None, author_kind="user", agent_characters=0,
                          structural_added=[{"id": "b1", "kind": "heading"}]))  # fmt: skip
    window.observe(_event(3, agent_characters=1999))
    window.observe(_event(3, agent_characters=1999))
    assert not window.due(NOW)
    assert window.revisions == [("doc-1", 3)]
    other = EventRecord(
        seq=4,
        time=NOW,
        kind="document.patch_conflicted",
        producer="harness",
        project_id="p1",
        session_id="s1",
        document_id="doc-1",
        payload={"agent_characters": 9999},
    )
    window.observe(other)
    assert not window.due(NOW)
