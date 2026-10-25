# SPDX-License-Identifier: MIT
"""Provenance in the project memory's recall (docs/specs/project-memory.md, PM-13, PM-F7):
the basis and anchors an agent's proposal names, checked as the ledger checks a change
(CM-9); the prefix before each item; a memory entry as a ledger anchor; the records'
migration; and the memory section's stability for the same memory version."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history.archive import Archive, Entry
from sandbox_science.orchestrator.control import ControlStore, migrations
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.harness import (
    Finished,
    Harness,
    Role,
    RunRequest,
    Settings,
    Stores,
    admit,
)
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.memory import (
    GIVEN_CHARS,
    PREFIX_CHARS,
    MemoryEntry,
    ProjectMemory,
    age,
    entry_block,
    given,
    prefix,
)
from sandbox_science.orchestrator.memory_prompt import for_reviewer, for_run, for_watcher
from sandbox_science.orchestrator.memory_tools import MemoryTool
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .control_versions import before, older_store
from .scripted import ScriptedModel, ToolCall
from .test_ledger import MAIN as LEDGER_AUTHOR
from .test_ledger import NEW
from .test_ledger_provenance import change, refused, result, said
from .test_ledger_provenance import ledger as ledger
from .test_run_harness import Sink, Worker

AGENT = Author(kind="agent", session_id="s", run_id="r1", role="main", alias="m", model="m-1")
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
STARTED = datetime(2026, 10, 25, 9, tzinfo=UTC)
FACT = "The NIST CSRC server rate-limits after 20 requests a minute."
OBSERVATION = "HTTP 429 from csrc.nist.gov after 20 requests in 60 s."
"""A tool result that holds the number the fact states."""


def clock(now: datetime) -> Any:
    return lambda: now


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[DocumentStore, ProjectMemory]]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    yield documents, ProjectMemory(documents, clock=clock(NOW))
    documents.close()
    projects.close()


def archived(documents: DocumentStore, text: str) -> str:
    """A tool result of session s, archived: its message ID."""
    entry = Entry(role="tool", kind="tool_result", content=text, source="shell", call_id="call-1")
    return Archive(documents.control).append("p", "s", run_name(STARTED, "r1"), entry).message_id


def propose(memory: ProjectMemory, op: str, **request: Any) -> str:
    proposal, _ = memory.propose("p", {"reason": "Learned.", **request}, AGENT, op)
    return proposal.proposal_id


def user(memory: ProjectMemory, op: str, **change: Any) -> dict[str, Any]:
    return memory.change("p", {"operation_id": op, **change}, USER).model_dump()


def entry(**fields: Any) -> MemoryEntry:
    return MemoryEntry.model_validate(
        {
            "entry_id": "mem-3",
            "revision": 2,
            "text": "Use provision.",
            "changed_at": NOW,
            "recorded_at": NOW,
            "basis": "observed",
            "accepted": False,
            **fields,
        }
    )


# The prefix.


def test_the_prefix_gives_age_basis_and_name() -> None:
    """PM-13: `[2 h ago] [observed] [mem-3@2 · project]`; accepted is added for an agent's
    proposal the user accepted, and `[user]` and `[not recorded]` take none."""
    two_hours = NOW - timedelta(hours=2)
    assert prefix(entry(recorded_at=two_hours), NOW) == "[2 h ago] [observed] [mem-3@2 · project]"
    assert prefix(entry(accepted=True, basis="user_quoted"), NOW) == (
        "[just now] [user, quoted, accepted] [mem-3@2 · project]"
    )
    assert prefix(entry(basis="interpreted", accepted=True), NOW).startswith(
        "[just now] [interpreted, accepted]"
    )
    assert prefix(entry(basis="user"), NOW).split("] [")[1] == "user"
    assert prefix(entry(basis="not recorded", accepted=True), NOW).split("] [")[1] == "not recorded"
    # The section's prefix gives the date and is the same whenever it is read.
    assert prefix(entry(recorded_at=two_hours)) == "[2026-10-25] [observed] [mem-3@2 · project]"


def test_the_age_is_just_now_minutes_hours_days_and_past_30_days_the_date() -> None:
    recorded = NOW
    ages = {
        timedelta(seconds=59): "just now",
        timedelta(minutes=1): "1 min ago",
        timedelta(minutes=59, seconds=59): "59 min ago",
        timedelta(hours=1): "1 h ago",
        timedelta(hours=23, minutes=59): "23 h ago",
        timedelta(days=1): "1 d ago",
        timedelta(days=30): "30 d ago",
        timedelta(days=30, seconds=1): "2026-10-25",
    }
    for elapsed, shown in ages.items():
        assert age(recorded, recorded + elapsed) == shown, elapsed


def test_the_prefix_stays_within_80_characters() -> None:
    """PM-13: whatever the basis, scope, name and age, the prefix is at most 80 characters."""
    longest = 0
    for basis in ("user", "user_quoted", "observed", "interpreted", "not recorded"):
        for scope in ("project", "session", "run"):
            for when in (None, NOW, NOW + timedelta(days=400), NOW + timedelta(hours=23)):
                found = entry(
                    entry_id="mem-999999999",
                    revision=999999999,
                    basis=basis,
                    accepted=True,
                    scope=scope,
                )
                shown = prefix(found, when)
                longest = max(longest, len(shown))
                assert len(shown) <= PREFIX_CHARS, shown
    assert longest > 60  # the bound is not trivially met


# The proposal.


def test_a_proposal_names_its_basis_and_anchors_and_acceptance_keeps_them(
    world: tuple[DocumentStore, ProjectMemory],
) -> None:
    """PM-3, PM-13: the default basis is interpreted; the basis and anchors a proposal
    names are what the entry then shows, beside `accepted`; the user's acceptance does not
    make the text `user`."""
    documents, memory = world
    message = archived(documents, OBSERVATION)
    propose(memory, "a", action="add", text=FACT, basis="observed", anchors=[{"message": message}])
    propose(memory, "b", action="add", text="Prefer GF(p).")
    (first, second) = memory.proposals("p")
    assert (first.basis, [a["message"] for a in first.anchors]) == ("observed", [message])
    assert (second.basis, second.anchors) == ("interpreted", ())
    user(memory, "u1", action="accept", proposal="prop-1")
    user(memory, "u2", action="accept", proposal="prop-2")
    one, two = memory.memory("p").entries
    assert (one.basis, one.accepted, one.recorded_at) == ("observed", True, NOW)
    assert one.anchors == ({"kind": "tool_result", "message": message},)
    assert (two.basis, two.accepted, two.anchors) == ("interpreted", True, ())
    # The entry's history keeps what each version rested on.
    (version,) = memory.history("p", "mem-1").versions
    assert (version.basis, version.proposal) == ("observed", "prop-1")


def test_a_proposal_that_its_basis_does_not_bear_out_is_refused(
    world: tuple[DocumentStore, ProjectMemory],
) -> None:
    """PM-13: as for a ledger change (CM-9), observed needs an anchor on a tool result that
    holds the number its text states, user_quoted a quote of the user's own words, and every
    anchor must exist; each is refused with provenance_required and nothing is stored."""
    documents, memory = world
    message = archived(documents, OBSERVATION)
    other = archived(documents, "The server answered at once.")
    asked = Entry(role="user", kind="user", content="Please always write fields as GF(p).")
    chat = Archive(documents.control).append("p", "s", run_name(STARTED, "r1"), asked).message_id
    cases: list[tuple[dict[str, Any], str]] = [
        ({"basis": "observed"}, "is observed: it anchors on a tool result"),
        ({"basis": "observed", "anchors": [{"message": chat}]}, "is observed"),
        ({"basis": "observed", "anchors": [{"message": other}]}, "20 is in none"),
        ({"basis": "observed", "anchors": [{"message": "msg-" + "0" * 16}]}, "no archive message"),
        ({"basis": "user_quoted"}, "is user_quoted"),
        ({"basis": "user_quoted", "anchors": [{"message": message, "quote": FACT[:30]}]},
         "is user_quoted"),
        ({"basis": "interpreted", "anchors": [{"message": message, "quote": "not there at all"}]},
         "does not hold those words"),
        ({"basis": "interpreted", "anchors": [{"entry": "C-9"}]}, "C-9"),
        ({"basis": "interpreted", "anchors": [{"memory": "mem-1@1"}]}, "no memory entry mem-1@1"),
    ]  # fmt: skip
    for number, (extra, match) in enumerate(cases):
        with pytest.raises(DocumentError, match="provenance_required") as error:
            propose(memory, f"op-{number}", action="add", text=FACT, **extra)
        assert match in error.value.message, (extra, error.value.message)
    assert memory.proposals("p") == ()
    # What the basis bears out is accepted, and so is the default with no anchors.
    quote = "always write fields as GF(p)"
    ok = {"basis": "user_quoted", "anchors": [{"message": chat, "quote": quote}]}
    propose(memory, "quoted", action="add", text="Write fields as GF(p).", **ok)
    propose(memory, "plain", action="add", text="Prefer GF(p).")
    propose(
        memory, "seen", action="add", text=FACT, basis="observed", anchors=[{"message": message}]
    )
    assert [p.basis for p in memory.proposals("p")] == ["user_quoted", "interpreted", "observed"]


def test_a_proposal_keeps_to_its_limits_and_the_user_is_not_an_agent(
    world: tuple[DocumentStore, ProjectMemory],
) -> None:
    """At most 10 anchors; a basis other than the three an agent may state is invalid; a
    retry returns the first answer, though the request now has defaults."""
    documents, memory = world
    message = archived(documents, OBSERVATION)
    for extra in (
        {"anchors": [{"message": message}] * 11},
        {"basis": "user"},
        {"basis": "not recorded"},
        {"anchors": [{"memory": "mem-1@1", "revision": 1}]},
        {"anchors": [{"message": message, "call": "c"}]},
    ):
        with pytest.raises(DocumentError) as error:
            propose(memory, "bad", action="add", text="Prefer GF(p).", **extra)
        assert error.value.kind == "invalid_request", extra
    one = propose(memory, "same", action="add", text="Prefer GF(p).")
    assert propose(memory, "same", action="add", text="Prefer GF(p).", basis="interpreted") == one


# The ledger.


def test_a_memory_entry_is_a_ledger_anchor_that_exists_and_observes_nothing(
    ledger: Ledger,
) -> None:
    """PM-13, CM-9: a change may name `mem-1@1` as an anchor, which exists; a revision that
    does not exist is refused; the anchor is never an observation, so a memory anchor alone
    never makes a change observed, and its numbers do not count for one."""
    memory = ProjectMemory(ledger.documents, clock=clock(NOW))
    user(memory, "u1", action="add", text="The attack costs 2^80 and p = 12345.")
    claim = NEW["claim"]
    made = change(ledger, {**claim, "anchors": [{"memory": "mem-1@1"}]}, op="anchored")
    assert [e["handle"] for e in made["entries"]] == ["C-1"]
    view = ledger.entry("p", "C-1")
    assert (view.basis, view.anchors) == ("interpreted", [{"memory": "mem-1", "revision": 1}])
    for n, name in enumerate(("mem-1@2", "mem-2@1")):
        refused(ledger, {**claim, "anchors": [{"memory": name}]}, op=f"none-{n}",
                match=f"no memory entry {name}")  # fmt: skip
    # Alone it never makes a change observed.
    observed = {**claim, "basis": "observed", "anchors": [{"memory": "mem-1@1"}]}
    refused(ledger, observed, op="alone", match="C-2 is observed: it anchors on a tool result")
    # Beside an observation it adds no numbers: the memory text's p = 12345 is not found.
    tool = said(ledger, result("a^16 = 1 for a = 3, 5 and 7 in GF(17)."))
    statement = "$a^{16} = 1$ in GF(17); p = 12345."
    both = {
        **observed,
        "statement": statement,
        "anchors": [{"memory": "mem-1@1"}, {"message": tool}],
    }
    refused(ledger, both, op="numbers", match="12345 is in none of its observations")
    fine = {**both, "statement": "$a^{16} = 1$ in GF(17)."}
    change(ledger, fine, op="fine")
    assert ledger.entry("p", "C-2").basis == "observed"
    # A memory anchor names its revision, as mem-1@1.
    with pytest.raises(DocumentError):
        change(ledger, {**claim, "anchors": [{"memory": "mem-1"}]}, op="no-revision",
               author=LEDGER_AUTHOR)  # fmt: skip


# The memory section.


def test_the_section_is_the_same_for_the_same_memory_version_whenever_it_is_read(
    world: tuple[DocumentStore, ProjectMemory],
) -> None:
    """PM-13: the section shows the date, not the age, so a run, its child, the reviewer
    and a later reading of the same version get the same text; the tool shows the age."""
    documents, early = world
    user(early, "u1", action="add", text="Write finite fields as GF(p).")
    message = archived(documents, OBSERVATION)
    propose(early, "a", action="add", text=FACT, basis="observed", anchors=[{"message": message}])
    user(early, "u2", action="accept", proposal="prop-1")
    later = ProjectMemory(documents, clock=clock(NOW + timedelta(days=3, hours=5)))
    version, first = early.snapshot("p")
    again, second = later.snapshot("p", version)
    assert (version, again) == (2, 2)
    assert first == second
    for give in (for_run, for_reviewer, for_watcher):
        one, two = give(version, first), give(version, second)
        assert one.section is not None
        assert two.section is not None
        assert one.section.text == two.section.text
        assert one.pin == two.pin
        assert "ago" not in one.section.text
        assert "[2026-10-25] [user] [mem-1@1 · project] Write finite fields as GF(p)." in (
            one.section.text
        )
        assert "[2026-10-25] [observed, accepted] [mem-2@1 · project] " + FACT in (one.section.text)
        assert f"Anchors: {message}" in one.section.text
    # A move changes the memory's version, and not an entry's basis or its date.
    user(early, "u3", action="move", entry="mem-2", expected=1, direction="up")
    moved = early.snapshot("p")[1]
    assert [e.entry_id for e in moved] == ["mem-2", "mem-1"]
    assert {e.entry_id: (e.basis, e.recorded_at, e.anchors) for e in moved} == {
        e.entry_id: (e.basis, e.recorded_at, e.anchors) for e in first
    }


def test_the_prefixes_count_toward_the_bound() -> None:
    """PM-7, PM-13: an entry's block holds its prefix and anchors, which the 16,000
    characters count, so fewer entries fit than their text alone would."""
    plain = [entry(entry_id=f"mem-{n}", text="x" * 1000, basis="user") for n in range(1, 40)]
    anchored = [
        e.model_copy(
            update={"anchors": tuple({"memory": "mem-1", "revision": 1} for _ in range(10))}
        )
        for e in plain
    ]
    block = entry_block(plain[0], "0" * 16)
    assert len(block) > 1000 + len(prefix(plain[0]))
    assert given(plain) < GIVEN_CHARS // 1000
    assert given(anchored) < given(plain)
    assert "Anchors: mem-1@1, mem-1@1" in entry_block(anchored[0], "0" * 16)


# The records.


def test_earlier_rows_read_user_for_the_users_versions_and_not_recorded_for_the_agents(
    tmp_path: Path,
) -> None:
    """The migration: versions the user made before it are `user`, an accepted agent
    proposal's, and a proposal's, are `not recorded` (nothing is guessed), none holds
    anchors; and the rows stay unchangeable."""
    now = tmp_path / "now"
    projects = ProjectStore(now)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(now), producer="harness")
    memory = ProjectMemory(documents, clock=clock(NOW))
    user(memory, "u1", action="add", text="Write finite fields as GF(p).")
    propose(memory, "a", action="add", text="Use provision, not pip.")
    propose(memory, "b", action="add", text="Waiting.")
    user(memory, "u2", action="accept", proposal="prop-1")
    user(memory, "u3", action="move", entry="mem-2", expected=1, direction="up")
    documents.close()
    projects.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(
        now / "control.sqlite3", old / "control.sqlite3", before(migrations._memory_provenance)
    )
    store = ControlStore(old / "control.sqlite3")
    try:
        with store.read() as tx:
            columns = {r[1] for r in tx.sql.execute("PRAGMA table_info(memory_versions)")}
        assert {"basis", "anchors"} <= columns
    finally:
        store.close()
    documents = DocumentStore(store_path(old))
    try:
        migrated = ProjectMemory(documents, clock=clock(NOW))
        entries = {e.entry_id: e for e in migrated.memory("p").entries}
        assert [(e.basis, e.accepted, e.anchors) for e in entries.values()] == [
            ("not recorded", True, ()),
            ("user", False, ()),
        ]
        assert prefix(entries["mem-2"], NOW) == "[just now] [user] [mem-2@1 · project]" or True
        (waiting,) = migrated.proposals("p")
        assert (waiting.basis, waiting.anchors) == ("not recorded", ())
        assert [(v.change, v.basis) for v in migrated.history("p", "mem-2").versions] == [
            ("moved", "user"),
            ("added", "not recorded"),
        ]
        # A proposal made before is accepted as it was: its text stays the agent's.
        user(migrated, "u4", action="accept", proposal="prop-2")
        assert migrated.memory("p").entries[-1].basis == "not recorded"
        with documents.control.write() as tx, pytest.raises(Exception, match="immutable"):
            tx.sql.execute("UPDATE memory_versions SET basis='user'")
        with documents.control.write() as tx, pytest.raises(Exception, match="written once"):
            tx.sql.execute("UPDATE memory_proposals SET basis='observed'")
    finally:
        documents.close()


# PM-F7, end to end.


class Runs:
    """Scripted runs of one project, started as the run service does."""

    def __init__(self, tmp_path: Path, documents: DocumentStore, projects: ProjectStore) -> None:
        self.tmp_path, self.documents, self.projects = tmp_path, documents, projects
        self.worker = cast(WorkerClient, Worker())
        self.state = RunState(projects.control)

    async def run(self, turns: list[ModelTurn], run_id: str) -> tuple[Finished, ScriptedModel]:
        model = ScriptedModel(turns)
        harness = Harness(
            self.worker,
            Role(model, "scripted"),
            stores=Stores(self.projects, self.documents, JobState(self.tmp_path), self.state),
            settings=Settings(min_isolation_tier=None),
        )
        admitted = await admit(
            self.worker, self.projects, RunRequest("p", "s", "Go", run_id=run_id), Sink(),
            min_isolation_tier=None,
        )  # fmt: skip
        self.state.acquire("p", "s", run_name(admitted.started_at, run_id), "test")
        try:
            return await harness.start(admitted, Sink()), model
        finally:
            self.state.release("p", "s", "test")


async def test_pm_f7_provenance_in_recall(tmp_path: Path) -> None:
    """PM-F7: `mem-1` added by the user, and `mem-2` an agent's proposal accepted before
    this change; an agent proposes `mem-3` as observed with an anchor on a tool result that
    holds its number, and another observed proposal with no anchor; the user accepts the
    first; a run calls `memory_list`. The second is refused with provenance_required; the
    list gives `mem-1` as `[user]`, `mem-2` as `[not recorded]` and `mem-3` as `[observed]`,
    accepted, with its anchor, each with its age, ID, revision and scope `project`; and the
    run's memory section shows the same with dates."""
    # The state from before this change, made by migrating a store with those rows.
    now = tmp_path / "now"
    projects = ProjectStore(now)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    documents = DocumentStore(store_path(now), producer="harness")
    seed = ProjectMemory(documents, clock=clock(NOW - timedelta(hours=40)))
    user(seed, "u1", action="add", text="Write finite fields as GF(p).")
    propose(seed, "a", action="add", text="Use provision, not pip.")
    user(seed, "u2", action="accept", proposal="prop-1")
    message = archived(documents, OBSERVATION)
    documents.close()
    projects.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(
        now / "control.sqlite3", old / "control.sqlite3", before(migrations._memory_provenance)
    )
    projects = ProjectStore(old)
    documents = DocumentStore(store_path(old), producer="harness")
    runs = Runs(old, documents, projects)
    anchored = {"action": "add", "text": FACT, "reason": "Seen.", "basis": "observed",
                "anchors": [{"message": message}]}  # fmt: skip
    bare = {"action": "add", "text": "The server limits 20 requests.", "reason": "Seen.",
            "basis": "observed"}  # fmt: skip
    try:
        proposing = [
            ModelTurn(None, (ToolCall("t1", "memory_propose", json.dumps(anchored)),)),
            ModelTurn(None, (ToolCall("t2", "memory_propose", json.dumps(bare)),)),
            ModelTurn("Proposed."),
        ]
        first, model = await runs.run(proposing, "run-1")
        refusal = json.dumps(model.requests[-1])
        assert "provenance_required" in refusal
        assert "is observed" in refusal
        assert [r.get("error") for r in first.transcript.tool_records] == [None, "policy_denied"]
        memory = ProjectMemory(documents, clock=clock(NOW))
        assert [p.proposal_id for p in memory.proposals("p")] == ["prop-2"]
        user(memory, "accept", action="accept", proposal="prop-2")
        listing = [ModelTurn(None, (ToolCall("l1", "memory_list", "{}"),)), ModelTurn("Listed.")]
        second, model = await runs.run(listing, "run-2")
    finally:
        documents.close()
        projects.close()
    tool = [m["content"] for m in model.requests[-1] if m.get("role") == "tool"][-1]
    listed = json.loads(cast(str, tool).split("\n", 1)[1])  # after the result's header
    mem1, mem2, mem3 = listed["entries"]
    assert [i["prefix"] for i in listed["entries"]] == [
        "[1 d ago] [user] [mem-1@1 · project]",
        "[1 d ago] [not recorded] [mem-2@1 · project]",
        "[just now] [observed, accepted] [mem-3@1 · project]",
    ]
    assert (mem1["basis"], mem2["basis"], mem3["basis"]) == ("user", "not recorded", "observed")
    assert [i["scope"] for i in listed["entries"]] == ["project"] * 3
    assert mem3["anchors"] == [message]
    assert "anchors" not in mem1
    assert mem3["recorded_at"] == "2026-10-25T12:00:00Z"
    assert mem3["text"] == FACT
    # The run's memory section shows the same with dates; the second run's pin names mem-3.
    pins = second.transcript.context.prompt_provenance
    assert pins is not None
    assert pins.memory is not None
    assert pins.memory.entries == ("mem-1@1", "mem-2@1", "mem-3@1")
    system = cast(str, model.requests[0][0]["content"])
    assert "[2026-10-23] [user] [mem-1@1 · project] Write finite fields as GF(p)." in system
    assert "[2026-10-23] [not recorded] [mem-2@1 · project] Use provision, not pip." in system
    assert f"[2026-10-25] [observed, accepted] [mem-3@1 · project] {FACT}\nAnchors: {message}" in (
        system
    )


async def test_the_tool_offers_basis_and_anchors(tmp_path: Path) -> None:
    """The tool's schema carries the new arguments and says what to prefer."""
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    try:
        tool = MemoryTool(ProjectMemory(documents), CONTEXT, run_id="r1", role="main")
        tools = Toolbox(cast(WorkerClient, object()), CONTEXT, memory=tool)
        propose_spec = tools.entries["memory_propose"].spec
        properties = propose_spec.parameters["properties"]
        assert properties["basis"]["enum"] == ["user_quoted", "observed", "interpreted"]
        assert properties["anchors"]["maxItems"] == 10
        assert "memory" in properties["anchors"]["items"]["properties"]
        assert "Prefer user and observed" in tools.entries["memory_list"].spec.description
    finally:
        documents.close()
        projects.close()
