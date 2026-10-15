# SPDX-License-Identifier: MIT
"""The project memory's store: agents propose, the user decides, every change is a
version (docs/specs/project-memory.md, PM-1 to PM-4, PM-7, PM-8, PM-11)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.memory import (
    GIVEN_CHARS,
    MAX_ENTRIES,
    MAX_PER_RUN,
    MAX_WAITING,
    ProjectMemory,
)

MAIN = Author(kind="agent", session_id="s", run_id="r1", role="main", alias="m", model="m-1")
CHILD = Author(kind="agent", session_id="s", run_id="r2", role="delegated", alias="m")


@pytest.fixture
def documents(tmp_path: Path) -> Iterator[DocumentStore]:
    store = DocumentStore(store_path(tmp_path))
    yield store
    store.close()


@pytest.fixture
def memory(documents: DocumentStore) -> ProjectMemory:
    return ProjectMemory(documents)


def propose(
    memory: ProjectMemory, op: str, author: Author = MAIN, **request: Any
) -> tuple[str, bool]:
    proposal, replayed = memory.propose("p", {"reason": "Learned.", **request}, author, op)
    assert proposal.state == "waiting"
    return proposal.proposal_id, replayed


def user(memory: ProjectMemory, op: str, **change: Any) -> dict[str, Any]:
    return memory.change("p", {"operation_id": op, **change}, USER).model_dump()


def texts(memory: ProjectMemory) -> list[str]:
    return [entry.text for entry in memory.memory("p").entries]


def test_agents_only_propose_and_a_retry_returns_the_first(memory: ProjectMemory) -> None:
    user(memory, "u1", action="add", text="Write finite fields as GF(p).")
    assert propose(memory, "c1", action="add", text="Use provision, not pip.") == ("prop-1", False)
    propose(memory, "c2", action="change", entry="mem-1", revision=1, text="Write GF(q).")
    propose(memory, "c3", action="remove", entry="mem-1", revision=1)
    assert propose(memory, "c1", action="add", text="Use provision, not pip.") == ("prop-1", True)
    with pytest.raises(DocumentError) as refused:
        propose(memory, "c1", action="add", text="Something else.")
    assert refused.value.kind == "conflict"
    # Proposals change no entry; the proposals wait, with the entry beside each.
    assert texts(memory) == ["Write finite fields as GF(p)."]
    assert memory.memory("p").version == 1
    waiting = memory.proposals("p")
    assert [p.proposal_id for p in waiting] == ["prop-1", "prop-2", "prop-3"]
    assert waiting[1].current is not None
    assert waiting[1].current.text.startswith("Write finite")
    assert waiting[0].proposer.run_id == "r1"
    assert waiting[0].proposer.model == "m-1"
    with pytest.raises(DocumentError) as denied:
        memory.change("p", {"operation_id": "x", "action": "add", "text": "t"}, MAIN)
    assert denied.value.kind == "policy_denied"
    with pytest.raises(DocumentError):
        memory.propose("p", {"action": "add", "text": "t", "reason": "r"}, USER, "x")


def test_accepting_applies_a_version_declining_applies_nothing(memory: ProjectMemory) -> None:
    propose(memory, "c1", action="add", text="Install with provision.")
    propose(memory, "c2", action="add", text="Always use 128-bit floats.")
    assert user(memory, "u1", action="accept", proposal="prop-1") == {
        "version": 1,
        "entry_id": "mem-1",
        "revision": 1,
        "proposal_id": "prop-1",
        "replayed": False,
    }
    assert user(memory, "u2", action="decline", proposal="prop-2")["version"] == 1
    assert user(memory, "u2", action="decline", proposal="prop-2")["replayed"] is True
    assert texts(memory) == ["Install with provision."]
    assert memory.proposals("p") == ()
    with pytest.raises(DocumentError) as twice:
        user(memory, "u3", action="accept", proposal="prop-2")
    assert twice.value.kind == "conflict"
    (version,) = memory.history("p", "mem-1").versions
    assert version.change == "added"
    assert version.author == USER
    assert version.proposal == "prop-1"
    assert version.reason == "Learned."
    assert version.proposer is not None
    assert version.proposer.role == "main"


def test_an_edit_meanwhile_shows_the_current_text_first(memory: ProjectMemory) -> None:
    """PM-F6: the user edits mem-1, then accepts a proposal made against revision 1."""
    user(memory, "u1", action="add", text="Install with provision.")
    propose(memory, "c1", action="change", entry="mem-1", revision=1, text="Use conda-forge.")
    user(memory, "u2", action="edit", entry="mem-1", expected=1, text="Install with provision -c.")
    (waiting,) = memory.proposals("p")
    assert waiting.base == 1
    assert waiting.current is not None
    assert waiting.current.revision == 2
    with pytest.raises(DocumentError) as stale:
        user(memory, "u3", action="accept", proposal="prop-1", expected=1)
    assert stale.value.kind == "conflict"
    assert stale.value.details == {"revision": 2}
    with pytest.raises(DocumentError):
        user(memory, "u3", action="accept", proposal="prop-1")  # names no revision
    assert user(memory, "u4", action="accept", proposal="prop-1", expected=2)["revision"] == 3
    history = memory.history("p", "mem-1").versions
    assert [(v.revision, v.change, v.proposer is not None) for v in history] == [
        (3, "edited", True),
        (2, "edited", False),
        (1, "added", False),
    ]
    assert texts(memory) == ["Use conda-forge."]


def test_a_stale_proposal_or_edit_is_refused(memory: ProjectMemory) -> None:
    user(memory, "u1", action="add", text="One.")
    user(memory, "u2", action="edit", entry="mem-1", expected=1, text="Two.")
    with pytest.raises(DocumentError) as stale:
        propose(memory, "c1", action="change", entry="mem-1", revision=1, text="Three.")
    assert "revision 2" in stale.value.message
    with pytest.raises(DocumentError) as missing:
        propose(memory, "c2", action="remove", entry="mem-9", revision=1)
    assert missing.value.kind == "not_found"
    with pytest.raises(DocumentError):
        user(memory, "u3", action="edit", entry="mem-1", expected=1, text="Three.")
    with pytest.raises(DocumentError) as same:
        user(memory, "u3", action="edit", entry="mem-1", expected=2, text="Two.")
    assert same.value.kind == "invalid_request"
    user(memory, "u4", action="remove", entry="mem-1", expected=2)
    with pytest.raises(DocumentError) as removed:
        propose(memory, "c3", action="remove", entry="mem-1", revision=3)
    assert "removed" in removed.value.message
    assert memory.history("p", "mem-1").removed is True
    assert texts(memory) == []


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ({"action": "add", "text": "x" * 1001}, "1 to 1000"),
        ({"action": "add", "text": "   "}, "1 to 1000"),
        ({"action": "add", "text": "a\x00b"}, "control"),
        ({"action": "add", "text": "evil \u202egnp.exe"}, "bidirectional"),
        ({"action": "add", "text": "ok", "reason": "y" * 501}, "1 to 500"),
        ({"action": "add", "entry": "mem-1", "revision": 1, "text": "x"}, "add names no entry"),
        ({"action": "change", "entry": "mem-1", "text": "x"}, "revision you read"),
        ({"action": "remove", "entry": "mem-1", "revision": 1, "text": "x"}, "remove gives none"),
        ({"action": "add", "text": "x", "author": "user"}, "Extra inputs"),
        ({"action": "change", "entry": "1", "revision": 1, "text": "x"}, "entry"),
    ],
)
def test_a_proposal_is_checked(
    memory: ProjectMemory, request_: dict[str, Any], message: str
) -> None:
    with pytest.raises(DocumentError) as refused:
        memory.propose("p", {"reason": "Learned.", **request_}, MAIN, "c1")
    assert refused.value.kind == "invalid_request"
    assert message in refused.value.message


def test_a_run_and_a_project_have_their_limits(memory: ProjectMemory) -> None:
    for index in range(MAX_PER_RUN):
        propose(memory, f"a{index}", action="add", text=f"Note {index}.")
    with pytest.raises(DocumentError) as run:
        propose(memory, "a-more", action="add", text="One more.")
    assert "has not caught up" in run.value.message
    runs = [Author(kind="agent", session_id="s", run_id=f"r{n}", role="main") for n in range(2, 7)]
    for author in runs[:4]:
        for index in range(MAX_PER_RUN):
            propose(memory, f"b{index}", author, action="add", text=f"{author.run_id} {index}.")
    assert len(memory.proposals("p")) == MAX_WAITING
    with pytest.raises(DocumentError) as project:
        propose(memory, "c", runs[4], action="add", text="Another.")
    assert "50 proposals already wait" in project.value.message
    user(memory, "u", action="decline", proposal="prop-1")
    propose(memory, "c", runs[4], action="add", text="Another.")


def test_the_memory_holds_its_most_entries(memory: ProjectMemory) -> None:
    for index in range(MAX_ENTRIES):
        user(memory, f"u{index}", action="add", text=f"Note {index}.")
    with pytest.raises(DocumentError) as full:
        user(memory, "more", action="add", text="One more.")
    assert "remove or merge" in full.value.message
    propose(memory, "c1", action="add", text="One more.")
    with pytest.raises(DocumentError):
        user(memory, "accept", action="accept", proposal="prop-1")


def test_the_user_moves_entries(memory: ProjectMemory) -> None:
    for index, text in enumerate(["A.", "B.", "C."]):
        user(memory, f"u{index}", action="add", text=text)
    user(memory, "m1", action="move", entry="mem-3", expected=1, direction="up")
    assert texts(memory) == ["A.", "C.", "B."]
    with pytest.raises(DocumentError) as first:
        user(memory, "m2", action="move", entry="mem-1", expected=1, direction="up")
    assert "first already" in first.value.message
    with pytest.raises(DocumentError) as last:
        user(memory, "m3", action="move", entry="mem-2", expected=2, direction="down")
    assert "last already" in last.value.message
    user(memory, "m4", action="move", entry="mem-1", expected=1, direction="down")
    assert texts(memory) == ["C.", "A.", "B."]
    # Both entries of a move get a version, at one version of the memory.
    assert memory.memory("p").version == 5
    moved = [memory.history("p", e).versions[0] for e in ("mem-1", "mem-2", "mem-3")]
    assert [(v.revision, v.version, v.change) for v in moved] == [
        (2, 5, "moved"),
        (2, 4, "moved"),
        (3, 5, "moved"),
    ]


def test_an_agent_receives_whole_entries_up_to_the_bound(memory: ProjectMemory) -> None:
    for index in range(20):
        user(memory, f"u{index}", action="add", text=f"{index:02d}" + "x" * 998)
    shown = memory.memory("p")
    assert shown.characters == 20_000
    assert shown.given == GIVEN_CHARS // 1000 == 16


def test_each_change_commits_its_events(documents: DocumentStore, memory: ProjectMemory) -> None:
    propose(memory, "c1", action="add", text="Install with provision.")
    user(memory, "u1", action="accept", proposal="prop-1")
    user(memory, "u2", action="edit", entry="mem-1", expected=1, text="Install with provision!")
    log = EventLog(documents.control, "memory", "test")
    with documents.control.read() as tx:
        events = log.page(tx.sql, "p", 0, 10)
    assert [(e.kind, e.subject, e.session) for e in events] == [
        ("memory.proposed", "prop-1", "s"),
        ("memory.changed", "mem-1", None),
        ("memory.decided", "prop-1", "s"),
        ("memory.changed", "mem-1", None),
    ]
    assert events[2].payload == {"proposal": "prop-1", "decision": "accept", "version": 1}


def test_versions_and_decisions_are_written_once(documents: DocumentStore) -> None:
    memory = ProjectMemory(documents)
    propose(memory, "c1", action="add", text="Install with provision.")
    user(memory, "u1", action="accept", proposal="prop-1")
    with documents.control.write() as tx:
        for statement in (
            "UPDATE memory_versions SET text='other'",
            "DELETE FROM memory_versions",
            "UPDATE memory_proposals SET state='declined'",
            "UPDATE memory_proposals SET reason='other'",
            "DELETE FROM memory_entries",
            "UPDATE memory_entries SET head=5",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                tx.sql.execute(statement)
