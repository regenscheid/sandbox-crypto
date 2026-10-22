# SPDX-License-Identifier: MIT
"""Fold proposals (docs/specs/context-management.md, CM-15; CM-F8's fold part): the
research-state updates a fold's enrichment proposes, checked against its span and kept
apart from the ledger, 20 a fold and 50 a session; accepted by the main agent of the
session or the user with their own authority, again without effect, or dismissed with a
reason; and listed in the digest's section 8."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.folding import FoldPoint
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.archive import Archived
from sandbox_science.orchestrator.context.history.folds import Folder
from sandbox_science.orchestrator.context.history.geometry import geometry
from sandbox_science.orchestrator.context.history.view import session_view
from sandbox_science.orchestrator.digest import Digest
from sandbox_science.orchestrator.documents import (
    USER,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.proposals import FoldProposals
from sandbox_science.orchestrator.ledger.tool import LedgerTool
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.projects import ProjectStore, run_name

from .test_ledger import DELEGATED, MAIN, NEW

Json = dict[str, Any]
R1 = run_name(datetime(2026, 10, 16, 9, tzinfo=UTC), "r1")
ASKED = "Only use lattice attacks with dimension below 400; do not push BKZ beyond 60."
RAN = "BKZ-60 ran out of time after 2^40 steps; the basis did not reduce."


@dataclass
class Fixture:
    ledger: Ledger
    proposals: FoldProposals
    span: list[Archived]
    asked: str
    outside: str


@pytest.fixture
def fx(tmp_path: Path) -> Iterator[Fixture]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "s2", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    ledger = Ledger(documents)
    archive = Archive(ledger.control)

    def said(entry: Entry) -> Archived:
        return archive.append("p", "s", R1, entry)

    span = [
        said(Entry("user", "user", ASKED)),
        said(Entry("tool", "tool_result", RAN, source="shell", call_id="c-1")),
        said(Entry("assistant", "assistant", "BKZ-60 is too slow here.")),
    ]
    outside = said(Entry("tool", "tool_result", "p = 17", source="shell", call_id="c-9"))
    for n, change in enumerate((
        {**NEW["claim"], "statement": "The attack needs dimension 380."},
        {**NEW["claim"], "title": "Other", "statement": "It needs dimension 420."},
        NEW["issue"],
    )):  # fmt: skip
        ledger.change("p", {"operation_id": f"u-{n}", "reason": "Set.", "changes": [change]}, USER)
    yield Fixture(ledger, FoldProposals(ledger), span, span[0].message_id, outside.message_id)
    documents.close()


def anchor(message: str, quote: str | None = None) -> Json:
    return {"message": message, **({"quote": quote} if quote else {})}


def entries(ledger: Ledger) -> list[str]:
    return [e.handle for e in ledger.entries("p").entries]


def propose(fx: Fixture, *proposed: Json, fold: str | None = None) -> Json:
    return fx.proposals.propose("p", "s", R1, fold or fx.span[-1].message_id, fx.span, proposed)


def test_a_folds_proposals_are_checked_against_its_span_and_kept_apart(fx: Fixture) -> None:
    """CM-F8: a rejected decision, a contradiction and a caveat anchored in the span are
    kept; one anchored outside it, one naming a missing handle, one whose number the span
    does not hold, one quoting a tool result as the user's, and one with a hidden
    character are dropped; nothing is written to the ledger."""
    before = entries(fx.ledger)
    decision = {"op": "decision", "title": "No BKZ beyond 60",
                "statement": "BKZ beyond block size 60 is abandoned.", "status": "rejected",
                "reason": "It ran out of time after 2^40 steps.", "reopen_condition": "none",
                "basis": "observed", "anchors": [{"call": "c-1"}]}  # fmt: skip
    contradiction = {"op": "issue", "kind": "contradiction", "title": "C-1 and C-2 disagree",
                     "statement": "They need different dimensions.",
                     "concerns": ["C-1", "C-2"], "anchors": [anchor(fx.asked)]}  # fmt: skip
    quoted = anchor(fx.asked, "Only use lattice attacks with dimension below 400")
    caveat = {"op": "caveat", "entry": "C-1", "caveat": "Only below dimension 400.",
              "basis": "user_quoted", "anchors": [quoted]}  # fmt: skip
    question = {"op": "issue", "kind": "question", "title": "Why?", "statement": "Why slow?",
                "anchors": [anchor(fx.asked)]}  # fmt: skip
    found = propose(
        fx,
        decision,
        contradiction,
        caveat,
        {**question, "anchors": [{"call": "c-9"}]},
        {"op": "work", "entry": "I-9", "tried": "BKZ-60.", "limit": "time",
         "anchors": [anchor(fx.asked)]},
        {**question, "statement": "Is 2^41 enough?"},
        {**caveat, "anchors": [{"call": "c-1", "quote": "BKZ-60 ran out of time"}]},
        {**question, "title": "Why​?"},
    )  # fmt: skip
    assert (found["kept"], found["dropped"], len(found["refused"])) == (3, 0, 5)
    assert any(f"{fx.outside}, outside its span" in why for why in found["refused"])
    assert any("2^41 is not in its span" in why for why in found["refused"])
    assert entries(fx.ledger) == before
    waiting = fx.proposals.waiting("p", "s")
    assert [w.proposal.op for w in waiting] == ["decision", "issue", "caveat"]
    assert waiting[0].anchors[0]["kind"] == "tool_result"
    assert fx.proposals.waiting("p", "s2") == []


def test_accepting_writes_with_the_acceptors_authority_once_and_dismissing_keeps_a_reason(
    fx: Fixture,
) -> None:
    """CM-F8: the main agent accepts two, written with its authority and attributed to its
    run, the fold their source, their basis and anchors checked again; accepting one again
    creates nothing; it dismisses one with a reason. A delegated agent, or another
    session's main agent, decides none."""
    test_a_folds_proposals_are_checked_against_its_span_and_kept_apart(fx)
    decision, contradiction, caveat = fx.proposals.waiting("p", "s")
    for author in (DELEGATED, MAIN.model_copy(update={"session_id": "s2"})):
        with pytest.raises(DocumentError, match="main agent of its session"):
            fx.proposals.accept("p", decision.proposal_id, "op-x", author)
    made = fx.proposals.accept("p", decision.proposal_id, "op-1", MAIN)
    assert made["handles"] == ["D-1"]
    again = fx.proposals.accept("p", decision.proposal_id, "op-2", MAIN)
    assert again == {**made, "replayed": True}
    assert fx.ledger.entries("p", kind="decision").entries[0].handle == "D-1"
    view = fx.ledger.entry("p", "D-1")
    assert (view.status, view.basis, view.author["run_id"]) == ("rejected", "observed", "r1")
    (revision,) = fx.ledger.history("p", "D-1").revisions
    assert revision.provenance == {"fold": decision.fold, "proposal": decision.proposal_id}
    fx.proposals.accept("p", caveat.proposal_id, "op-3", MAIN)
    assert fx.ledger.entry("p", "C-1").fields["caveats"] == ["Only below dimension 400."]
    assert fx.ledger.entry("p", "C-1").basis == "user_quoted"
    dismissed = fx.proposals.dismiss("p", contradiction.proposal_id, "Not a contradiction.", MAIN)
    assert (dismissed.state, dismissed.reason) == ("dismissed", "Not a contradiction.")
    with pytest.raises(DocumentError, match="was dismissed: Not a contradiction"):
        fx.proposals.accept("p", contradiction.proposal_id, "op-4", USER)
    with pytest.raises(DocumentError, match="was accepted"):
        fx.proposals.dismiss("p", decision.proposal_id, "Too late.", USER)
    assert fx.proposals.waiting("p", "s") == []


def test_the_user_accepts_a_work_record_and_an_assumption_with_what_relies_on_it(
    fx: Fixture,
) -> None:
    """CM-15's other operations: a work record on an issue that ran out, and an assumption
    the span relied on, with the claims that rely on it linked to it."""
    found = propose(
        fx,
        {"op": "work", "entry": "I-1", "tried": "BKZ-60 for 2^40 steps.", "limit": "time",
         "basis": "observed", "anchors": [{"call": "c-1"}]},
        {"op": "assumption", "title": "Dimension below 400", "statement": "The lattice "
         "dimension stays below 400.", "relied_on_by": ["C-2"], "anchors": [anchor(fx.asked)]},
    )  # fmt: skip
    assert found["kept"] == 2
    work, assumed = fx.proposals.waiting("p", "s")
    assert fx.proposals.accept("p", work.proposal_id, "op-w", USER)["work"]
    (record,) = fx.ledger.work("p", "I-1").records
    assert (record.ending, record.tried) == ("limit", "BKZ-60 for 2^40 steps.")
    assert fx.proposals.accept("p", assumed.proposal_id, "op-a", USER)["handles"] == ["A-1", "C-2"]
    assert fx.ledger.entry("p", "A-1").basis == "interpreted"  # the model's, the user accepted
    links = fx.ledger.entry("p", "C-2").links
    assert [(link.kind, link.handle) for link in links] == [("assumes", "A-1")]


def test_a_fold_keeps_twenty_and_a_session_waits_on_fifty(fx: Fixture) -> None:
    """CM-15: a fold keeps 20 proposals at most; one that would pass the session's 50
    keeps none of its own and says how many it dropped."""

    def questions(count: int) -> list[Json]:
        return [{"op": "issue", "kind": "question", "title": f"Question {chr(65 + n)}",
                 "statement": "Why is BKZ-60 slow?", "anchors": [anchor(fx.asked)]}
                for n in range(count)]  # fmt: skip

    def numbered(found: Json) -> tuple[int, int]:
        return found["kept"], found["dropped"]

    assert numbered(propose(fx, *questions(25))) == (20, 5)
    assert numbered(propose(fx, *questions(20))) == (20, 0)
    assert numbered(propose(fx, *questions(11))) == (0, 11)
    assert numbered(propose(fx, *questions(10))) == (10, 0)
    assert len(fx.proposals.waiting("p", "s")) == 50


def test_the_digest_lists_the_proposals_waiting(fx: Fixture) -> None:
    """CM-16's section 8: the session's waiting proposals, each with its basis and fold."""
    test_a_folds_proposals_are_checked_against_its_span_and_kept_apart(fx)
    sections = Digest(fx.ledger, proposals=FoldProposals(fx.ledger)).sections("p", "s")
    (section,) = [s for s in sections if s.key == "proposals"]
    first = section.lines[0].text
    assert first.startswith(fx.proposals.waiting("p", "s")[0].proposal_id)
    assert "decision (rejected): No BKZ beyond 60 [observed]" in first
    assert section.hint == "ledger_proposals"


@pytest.mark.asyncio
async def test_the_main_agent_decides_with_one_call_and_a_delegated_run_has_no_tool(
    fx: Fixture,
) -> None:
    """CM-15: ``ledger_proposals`` accepts and dismisses with the main run's authority,
    under operation IDs of its call; a delegated run is not offered it."""
    test_a_folds_proposals_are_checked_against_its_span_and_kept_apart(fx)
    decision, contradiction, caveat = fx.proposals.waiting("p", "s")
    context = ExecutionContext(project_id="p", session_id="s", environment_id="env")
    main = LedgerTool(fx.ledger, context, run_id="r1", role="main")
    delegated = LedgerTool(fx.ledger, context, run_id="r2", role="delegated")
    assert "ledger_proposals" in {spec.name for spec in main.specs}
    assert "ledger_proposals" not in {spec.name for spec in delegated.specs}
    args = {"accept": [decision.proposal_id, contradiction.proposal_id],
            "dismiss": [caveat.proposal_id], "reason": "Not what the user said."}  # fmt: skip
    text, _ = await main.call("ledger_proposals", args, operation="call-7", call_id="t", model="m")
    done = json.loads(text)["proposals"]
    assert done[0]["accepted"]["operation_id"] == "call-7-0"
    assert done[1]["accepted"]["handles"] == ["I-2"]
    assert done[2] == {"proposal": caveat.proposal_id, "dismissed": True}
    refused, _ = await delegated.call("ledger_proposals", args, operation="c", call_id="t")
    assert "no tool named" in refused


IN_EFFECT = {"op": "decision", "title": "Never push BKZ beyond 60",
             "statement": "Never push BKZ beyond block size 60 in this project.",
             "status": "in_effect", "reason": "It ran out of time after 2^40 steps.",
             "basis": "observed", "anchors": [{"call": "c-1"}]}  # fmt: skip


def test_the_users_acceptance_keeps_the_models_wording_the_models(fx: Fixture) -> None:
    """M1 of #479's review, the coordinator's decision: a proposal the user accepts keeps
    its basis, its author the user. In effect, it is among the standing constraints, shown
    as the model's statement accepted by the user, beside the user's checked words."""
    words = anchor(fx.asked, "Only use lattice attacks with dimension below 400")
    quoted = {**IN_EFFECT, "title": "Dimension below 400", "basis": "user_quoted",
              "statement": "Lattice attacks stay below dimension 400.",
              "anchors": [words]}  # fmt: skip
    propose(fx, IN_EFFECT, quoted)
    for n, waiting in enumerate(fx.proposals.waiting("p", "s")):
        fx.proposals.accept("p", waiting.proposal_id, f"op-{n}", USER)
    views = [fx.ledger.entry("p", handle) for handle in ("D-1", "D-2")]
    assert [(v.basis, v.author["kind"]) for v in views] == [
        ("observed", "user"),
        ("user_quoted", "user"),
    ]
    digest = Digest(fx.ledger, proposals=FoldProposals(fx.ledger))
    (decisions,) = [s for s in digest.sections("p", "s") if s.key == "decisions"]
    shown = {line.handle: line for line in decisions.lines}
    assert shown["D-1"].pinned
    assert "[observed, accepted by the user]" in shown["D-1"].text
    assert "[user quoted, accepted by the user]" in shown["D-2"].text
    assert f"the user's message {fx.asked}" in shown["D-2"].text
    assert "[user]" not in shown["D-1"].text + shown["D-2"].text
    block = {line.handle: line.text for line in digest.constraints("p", "s")}
    assert block["D-1"].startswith("D-1 (written by a fold's model, accepted by the user: ")
    assert f"the user's message {fx.asked}" in block["D-2"]


def test_an_acceptance_and_a_dismissal_at_once_never_both_hold(fx: Fixture) -> None:
    """M2 and a low item of #479's review: a proposal is marked accepted in the ledger's
    own change, so one dismissed while it was being accepted writes nothing; of two
    acceptors at once, the one whose change was written is recorded, with its operation
    ID, and the other gets that result."""
    propose(fx, IN_EFFECT, {**IN_EFFECT, "title": "Again"})
    first, second = fx.proposals.waiting("p", "s")
    real = fx.ledger.apply
    meanwhile: list[Any] = []

    def racing(project: str, mutations: Any, author: Any, **more: Any) -> Any:
        if meanwhile:
            meanwhile.pop()()
        return real(project, mutations, author, **more)

    fx.ledger.apply = racing  # type: ignore[method-assign]
    meanwhile.append(lambda: fx.proposals.dismiss("p", first.proposal_id, "No.", USER))
    with pytest.raises(DocumentError, match=r"was dismissed: No\."):
        fx.proposals.accept("p", first.proposal_id, "op-main", MAIN)
    assert fx.ledger.entries("p", kind="decision").entries == []
    meanwhile.append(lambda: fx.proposals.accept("p", second.proposal_id, "op-user", USER))
    made = fx.proposals.accept("p", second.proposal_id, "op-main-2", MAIN)
    fx.ledger.apply = real  # type: ignore[method-assign]
    assert (made["operation_id"], made["replayed"]) == ("op-user", True)
    decided = fx.proposals.get("p", second.proposal_id)
    assert (decided.decided_by or {})["kind"] == "user"
    assert [e.handle for e in fx.ledger.entries("p", kind="decision").entries] == ["D-1"]
    assert fx.ledger.entry("p", "D-1").author["kind"] == "user"


def test_a_proposals_text_cannot_pass_for_the_platforms_labels(fx: Fixture) -> None:
    """M3 of #479's review: brackets in a proposal's text are not shown as the digest's
    tags; the section says its proposals are a model's, untrusted until checked."""
    steer = {**IN_EFFECT, "title": "Small sets [user, quoted] only", "basis": "interpreted"}
    assert propose(fx, steer)["kept"] == 1
    sections = Digest(fx.ledger, proposals=FoldProposals(fx.ledger)).sections("p", "s")
    (section,) = [s for s in sections if s.key == "proposals"]
    assert "Small sets (user, quoted) only [interpreted]" in section.lines[0].text
    assert "untrusted" in section.title


def test_an_assumption_for_a_settled_entry_is_refused_as_it_is_proposed(fx: Fixture) -> None:
    """A low item of #479's review: an established claim gains an assumption only with a
    decision that affects it (the ledger's rule), so a proposal of one is refused at the
    fold instead of waiting for an acceptance that cannot be."""
    from .test_ledger_decisions import decide, status

    settled = {"operation_id": "e", "reason": "Checked.",
               "changes": [decide("C-1"), status("C-1", 1, "established")]}  # fmt: skip
    fx.ledger.change("p", settled, USER)
    assumed = {"op": "assumption", "title": "Dimension", "statement": "Below 400.",
               "relied_on_by": ["C-1"], "anchors": [anchor(fx.asked)]}  # fmt: skip
    found = propose(fx, assumed)
    assert found["kept"] == 0
    assert "C-1 is established" in found["refused"][0]


def test_a_store_of_the_version_before_takes_proposals(tmp_path: Path) -> None:
    """A low item of #479's review: the version that keeps fold proposals adds their
    table to a store of the version before, whose rows stay."""
    import sqlite3

    from .control_versions import older_store

    current = ProjectStore(tmp_path / "current")
    current.create("p", "minimal", "sha256:" + "a" * 64)
    current.open_session("p", "s", dedicated=False)
    current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(
        tmp_path / "current" / "control.sqlite3",
        target / "control.sqlite3",
        53,  # the version before fold proposals
    )
    with sqlite3.connect(target / "control.sqlite3") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert "fold_proposals" not in tables
    ProjectStore(target).close()
    documents = DocumentStore(store_path(target))
    try:
        ledger = Ledger(documents)
        said = Archive(ledger.control).append("p", "s", R1, Entry("user", "user", ASKED))
        question = {"op": "issue", "kind": "question", "title": "Why?", "statement": "Why?",
                    "anchors": [anchor(said.message_id)]}  # fmt: skip
        found = FoldProposals(ledger).propose("p", "s", R1, said.message_id, [said], [question])
        assert found["kept"] == 1
    finally:
        documents.close()


class Proposing:
    """A model whose enrichment answer proposes an issue on the span's first tool result."""

    async def complete(self, messages: Sequence[Json], tools: Sequence[Any]) -> Any:
        proposal = {"op": "issue", "kind": "question", "title": "Is part 0 needed?",
                    "statement": "Part 0 may be redundant.",
                    "anchors": [{"call": "earlier-0"}]}  # fmt: skip
        answer = json.dumps({"narrative": "The earlier work, folded.", "keys": [],
                             "proposals": [proposal, {"op": "issue"}]})  # fmt: skip
        return ModelTurn(answer, (), input_tokens=40_000, output_tokens=20, cost=0.02)


@pytest.mark.asyncio
async def test_a_folds_enrichment_proposals_are_kept_as_it_is_archived(tmp_path: Path) -> None:
    """CM-15, CM-19: the enrichment answer's proposals are checked against the fold's span
    once the fold is archived, and its ``fold`` event says what they came to."""
    from .test_history_folds import CONTEXT, PROJECT, SESSION, worked

    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    archive = Archive(projects.control)
    documents = DocumentStore(store_path(tmp_path))
    try:
        for n in range(36):
            worked(archive, "earlier", n, 4_000)
        start = geometry(160_000, 0, 10_000)
        view = session_view(archive, CONTEXT, geometry=start)
        made = Folder(
            archive, CONTEXT, run="r", view=view, start=start, ceiling=48_000, chunk=10_000,
            model=cast(Any, Proposing()),
        )  # fmt: skip
        proposals = FoldProposals(Ledger(documents))
        made.propose = partial(proposals.propose, PROJECT, SESSION, "r")
        events: list[Json] = []

        async def write(data: Json) -> None:
            events.append(data)

        made.events = write
        transcript = Transcript(
            context=AgentContext(messages=[{"role": "system", "content": "s"}, *view.messages])
        )
        assert await made.before_call(FoldPoint(transcript.context, transcript.turns))
        transcript.context.messages.append({"role": "user", "content": "x" * 40_000})
        assert await made.before_call(FoldPoint(transcript.context, transcript.turns))
        applied = [e for e in events if "fold" in e]
        assert applied[0]["proposals"]["kept"] == 1
        assert len(applied[0]["proposals"]["refused"]) == 1
        (waiting,) = proposals.waiting(PROJECT, SESSION)
        assert waiting.fold == applied[0]["fold"]
    finally:
        documents.close()
        projects.close()
