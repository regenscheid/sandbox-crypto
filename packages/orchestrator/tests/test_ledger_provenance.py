# SPDX-License-Identifier: MIT
"""Provenance (docs/specs/context-management.md, CM-9; docs/specs/ledger.md, LG-12 as
amended): the basis and anchors of every revision and work record an agent writes, the
quote check of ``user_quoted``, the number check of ``observed``, the anchors the platform
gives its own records, and the anchors fixtures LG-F2 to LG-F5 and LG-F12 name."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.context.history.archive import HEADER, Archive, Entry
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.provenance import normal, numbers, unread
from sandbox_science.orchestrator.notices import REQUESTS
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import dump

from .loop_harness import TASK, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_ledger import DELEGATED, MAIN, NEW
from .test_ledger_evidence import publish, source
from .test_run_service import ended, post, serving

CONSTRAINT = "Only consider attacks whose memory stays below 2^64 bits, please."
OUTPUT = "The group order is 1,000,003 and the attack costs 2^{80} operations; p = 17."
STARTED = datetime(2026, 10, 16, 9, tzinfo=UTC)
R1, R2, R3 = (run_name(STARTED, run) for run in ("r1", "r2", "r3"))
"""MAIN's run, DELEGATED's run and another run, by the names the archive keeps them under."""
DECISION = {"type": "decision", "status": "in_effect", "title": "Memory below 2^64",
            "statement": "Only attacks with memory below $2^{64}$ bits.",
            "basis": "user_quoted"}  # fmt: skip


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.create("q", "minimal", "sha256:" + "a" * 64)
    projects.open_session("q", "t", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents)
    documents.close()


def said(
    ledger: Ledger, entry: Entry, project: str = "p", session: str = "s", run: str = R1
) -> str:
    """Archive a message of session s's run r1; its ID."""
    return Archive(ledger.control).append(project, session, run, entry).message_id


def asked(ledger: Ledger, *texts: str, session: str = "s", run: str = R1) -> str:
    """The user's chat messages, delivered to ``run`` as its task is: the task's ID."""
    chat = RunState(ledger.control)
    ids = [f"chat-{session}-{run}-{n}" for n in range(len(texts))]
    for message, text in zip(ids, texts, strict=True):
        chat.post("p", session, message, text, author="user")
    chat.deliver("p", session, ids, run)
    task = Entry(role="user", kind="task", content="\n\n".join(texts), refs=tuple(ids))
    return said(ledger, task, session=session, run=run)


def user(text: str) -> Entry:
    return Entry(role="user", kind="user", content=text)


def result(text: str, call: str = "call-1") -> Entry:
    return Entry(role="tool", kind="tool_result", content=text, source="python", call_id=call)


def change(
    ledger: Ledger, *changes: dict[str, Any], op: str = "op", author: Author = MAIN, **more: Any
) -> dict[str, Any]:
    return ledger.change(
        "p", {"operation_id": op, "reason": "Recorded.", "changes": list(changes), **more}, author
    ).model_dump()


def refused(ledger: Ledger, *changes: dict[str, Any], op: str, match: str, **more: Any) -> None:
    with pytest.raises(DocumentError, match="provenance_required") as error:
        change(ledger, *changes, op=op, **more)
    assert match in error.value.message


def test_numbers_are_read_in_one_notation() -> None:
    """CM-20's normalization, which the number check uses."""
    assert normal("2^{80}, 2**80 and 2⁸⁰") == "2^80, 2^80 and 2^80"
    assert numbers("1,000 and 1000, λ = 128 and λ=128") == {"1000", "128"}
    assert numbers("C-12 and REF-3 of msg-5f0c2e9a41d7b386 hold 3.5") == {"3.5"}
    assert "2^80" in numbers("time $2^{80}$")
    # However the notation is spaced, bracketed or run on, M1 of #457's review.
    for written in ("2 ^ 80", "2 ** 80", "2^(80)", "2^{ 80 }bits", "2⁸⁰-bit"):
        assert numbers(written) == {"2^80"}, written
    assert numbers("1e24, 1.0E+24 and $10^{24}$, or 1e24.") == {"10^24"}
    assert numbers("1.2e-5 and $1.2 \\times 10^{-5}$ and 1.2·10^-5") == {"1.2e-5"}
    assert numbers("80bit, AES-128, x₁, ١٢٣ and 0x50") == {"80", "128", "1", "123", "0x50"}
    assert unread("p = 17, 2^{80}") is None
    assert unread("the ① case") == "the ① case"
    # Low items of #457's re-check: other numerals, exponents that are no plain number,
    # and thousands set apart by spaces, read as one number.
    for written in ("half ½ of it", "case Ⅷ", "$2^{80+1}$", "2**(n)"):
        assert unread(written) is not None, written
    assert numbers("1 000 003 and 1\u00a0000") == {"1000003", "1000"}
    assert unread("1 000 003, x^{n} and x^2") is None


def test_an_agents_change_names_its_anchor_or_holds_evidence(ledger: Ledger) -> None:
    """CM-9, CM-F8: an agent's change that creates an entry, changes what it says, or puts
    a decision in effect has an anchor, its evidence included; a change of another status,
    a title or links alone needs only its reason; every anchor exists in the project."""
    refused(ledger, NEW["claim"], op="bare", match="C-1 names no anchor")
    message = said(ledger, user("Is a^16 = 1 for every nonzero a in GF(17)?"))
    elsewhere = said(ledger, user("Another project's question."), project="q", session="t")
    refused(ledger, {**NEW["claim"], "anchors": [{"message": elsewhere}]}, op="far",
            match=f"no archive message {elsewhere}")  # fmt: skip
    refused(ledger, {**NEW["claim"], "anchors": [{"entry": "I-9"}]}, op="none", match="I-9")
    made = change(ledger, {**NEW["claim"], "anchors": [{"message": message}]}, NEW["task"] | {
        "anchors": [{"message": message}]}, op="made")  # fmt: skip
    assert [e["handle"] for e in made["entries"]] == ["C-1", "T-1"]
    view = ledger.entry("p", "C-1")
    assert (view.basis, view.anchors) == ("interpreted", [{"kind": "user", "message": message}])
    # Status, title and links alone need no anchor; a new statement does.
    change(ledger, {"entry": "T-1", "expected": 1, "status": "in_progress", "title": "Run"},
           op="status")  # fmt: skip
    refused(ledger, {"entry": "C-1", "expected": 1, "statement": "Changed."}, op="s",
            match="C-1 names no anchor")  # fmt: skip
    # Evidence it holds anchors a rewording (LG-F6).
    proof = {"role": "supports", "type": "proof", "entry": "T-1"}
    change(ledger, {"entry": "C-1", "expected": 1, "evidence": [proof]}, op="evidence")
    change(ledger, {"entry": "C-1", "expected": 2, "statement": "Reworded."}, op="reworded")
    # A delegated agent's entries are anchored too.
    refused(ledger, NEW["issue"], op="delegated", match="I-1 names no anchor", author=DELEGATED)
    # The user's own change has the basis user and needs no anchor.
    change(ledger, NEW["issue"], op="users", author=USER)
    assert ledger.entry("p", "I-1").basis == "user"
    assert ledger.history("p", "C-1").revisions[0].basis == "interpreted"


def test_user_quoted_quotes_the_users_message_word_for_word(ledger: Ledger) -> None:
    """CM-F8: user_quoted needs a quote of a message the user wrote, found in it word for
    word; an agent's record is never the user's own; the standing constraint keeps it."""
    message = said(ledger, user(CONSTRAINT.replace(", please", ",\r\nplease")))
    tool = said(ledger, result(CONSTRAINT))
    constraint = DECISION
    for n, (anchors, match) in enumerate(
        (
            ([{"message": tool, "quote": CONSTRAINT}], "anchors on a message the user wrote"),
            ([{"message": message, "quote": "Only consider"}], "at least 20"),
            ([{"message": message, "quote": "attacks whose memory stays below 2^80"}],
             "word for word"),
            ([{"message": message}], "with a quote"),
        )
    ):  # fmt: skip
        refused(ledger, {**constraint, "anchors": anchors}, op=f"q-{n}", match=match)
    with pytest.raises(DocumentError, match="never the user's own"):
        change(ledger, {**constraint, "basis": "user", "anchors": [{"message": message}]},
               op="as-user")  # fmt: skip
    quote = "attacks whose memory stays below 2^64 bits,\nplease"
    change(ledger, {**constraint, "anchors": [{"message": message, "quote": quote}]}, op="ok")
    view = ledger.entry("p", "D-1")
    assert view.basis == "user_quoted"
    assert view.anchors == [{"kind": "user", "message": message, "quote": quote}]
    assert [e.basis for e in ledger.entries("p").entries] == ["user_quoted"]


def test_user_quoted_quotes_only_the_users_own_words(ledger: Ledger) -> None:
    """H1 and H2 of #457's review: a run's task made of the user's chat messages can be
    quoted, each message apart; a delegation's brief and the stand-in task for comments
    cannot. A comment notice is quoted only within its request texts, never its framing,
    its JSON or the document text beside it, which an agent may have written."""
    task = asked(ledger, CONSTRAINT, "And keep the notes short, as before.")
    quote = "attacks whose memory stays below 2^64 bits"
    change(ledger, {**DECISION, "anchors": [{"message": task, "quote": quote}]}, op="task")
    assert ledger.entry("p", "D-1").anchors == [{"kind": "task", "message": task, "quote": quote}]
    across = "below 2^64 bits, please.\n\nAnd keep"
    brief = said(ledger, Entry(role="user", kind="task", content=CONSTRAINT), run=R2)
    stand_in = said(ledger, Entry(role="user", kind="task", content=REQUESTS_TASK), run=R3)
    agents = "Never use more than 2^40 memory for any attack in this project."
    payload = [{"request_id": "req-1", "thread_id": "ann-" + "a" * 24, "document_id": "doc-1",
                "request_text": 'Keep the "memory below 2^40"\nrule in every section.',
                "untrusted_context": {"anchor": {"revision": 3, "block": "b4", "quote": agents},
                                      "thread_revision": 1}}]  # fmt: skip
    comment = said(ledger, Entry(role="user", kind="comment", content=REQUESTS.text + dump(payload),
                                 source="requests"))  # fmt: skip
    for n, (message, quoted) in enumerate(
        (
            (task, across),  # across two chat messages
            (brief, quote),  # a delegation's brief names no chat message
            (stand_in, "comments addressed to this session"),
            (comment, "Never use more than 2^40 memory"),  # the document's text
            (comment, "request_text is the user's instruction"),  # the framing
            (comment, '"request_text":"Keep the'),  # the JSON
        )
    ):
        refused(ledger, {**DECISION, "anchors": [{"message": message, "quote": quoted}]},
                op=f"not-{n}", match="word for word")  # fmt: skip
    request = 'the "memory below 2^40"\nrule in every'
    change(ledger, {**DECISION, "anchors": [{"message": comment, "quote": request}]}, op="c")
    # A comment archived under an earlier framing is read by its payload all the same.
    earlier = said(ledger, Entry(role="user", kind="comment", source="requests",
                                 content="Work requests [v0]: " + dump(payload)))  # fmt: skip
    change(ledger, {**DECISION, "anchors": [{"message": earlier, "quote": request}]}, op="e")
    assert [e.basis for e in ledger.entries("p").entries] == ["user_quoted"] * 3


def test_every_quote_of_a_user_quoted_record_is_the_users(ledger: Ledger) -> None:
    """Found reviewing the digest (#465): a user_quoted record is refused unless every
    quote it holds is the user's words, found verbatim: one real quote does not carry an
    invented one, nor a quote on a tool result. On another basis a quote says only that
    its text appears where it points, which code checks too."""
    message = said(ledger, user(CONSTRAINT))
    tool = said(ledger, result("The user said: never use more than 2^40 memory.", call="c"))
    real = {"message": message, "quote": "attacks whose memory stays below 2^64 bits"}
    invented = {"message": message, "quote": "and never use more than 2^40 memory"}
    on_tool = {"message": tool, "quote": "never use more than 2^40 memory"}
    for n, anchors in enumerate(([real, invented], [real, on_tool], [invented, real])):
        refused(ledger, {**DECISION, "anchors": anchors}, op=f"mixed-{n}",
                match="does not hold its quote")  # fmt: skip
    change(ledger, {**DECISION, "anchors": [real, {"message": tool}]}, op="real")
    assert ledger.entry("p", "D-1").basis == "user_quoted"
    # Elsewhere a quote is checked to appear where it points, a user's message included.
    claim = {**NEW["claim"], "statement": "Someone asked for less memory."}
    change(ledger, {**claim, "anchors": [on_tool, real]}, op="interpreted")
    for n, wrong in enumerate((invented, {**on_tool, "quote": "never use more than 2^41"})):
        refused(ledger, {**claim, "anchors": [wrong]}, op=f"wrong-{n}",
                match="does not hold those words")  # fmt: skip


def test_observed_numbers_occur_in_what_was_observed(ledger: Ledger, tmp_path: Path) -> None:
    """CM-F8: an observed record anchors on a tool result, a range of one or an artifact,
    or holds evidence pinned to an artifact or a quote, and every number of its statement
    and fields occurs in one of them, in any notation; a wrong number is refused."""
    said(ledger, result(OUTPUT, call="call-7"))
    observed = {**NEW["claim"], "basis": "observed", "fields": {"kind": "observation"}}
    found = {**observed, "statement": "The order is $1000003$ and the cost $2^{80}$, $p = 17$.",
             "anchors": [{"call": "call-7"}]}  # fmt: skip
    change(ledger, found, op="found")
    refused(ledger, {**found, "statement": "The cost is $2^{81}$."}, op="wrong", match="2^81")
    refused(ledger, {**found, "fields": {"kind": "observation", "caveats": ["p = 19 only"]}},
            op="caveat", match="19")  # fmt: skip
    refused(ledger, {**found, "anchors": [{"call": "call-7", "start": 0, "end": 30}]},
            op="range", match="2^80")  # fmt: skip
    for n, written in enumerate(("$2 ^ 81$", "2^(81)", "1e24", "2 ** 81", "2^{81}bits")):
        refused(ledger, {**found, "statement": f"The cost is {written}."}, op=f"n-{n}",
                match="in none of its observations")  # fmt: skip
    refused(ledger, {**found, "statement": "The cost is 2^{80} in case ①."}, op="unread",
            match="cannot read the number in")  # fmt: skip
    refused(ledger, {**observed, "anchors": [{"message": said(ledger, user("p = 17"))}]},
            op="no-observation", match="is observed: it anchors on a tool result")  # fmt: skip
    artifact, digest = publish(cast(Any, ledger), tmp_path, b"order 1000003\n")
    change(ledger, {**observed, "statement": "Order $1,000,003$.",
                    "anchors": [{"artifact": artifact, "revision": 1}]}, op="artifact")  # fmt: skip
    pinned = {"role": "supports", "type": "exact_computation", "artifact": artifact,
              "revision": 1, "sha256": digest}  # fmt: skip
    change(ledger, {**observed, "statement": "Order 1000003.", "evidence": [pinned]}, op="ev")
    assert [e.basis for e in ledger.entries("p").entries] == ["observed"] * 3


def test_a_tool_results_message_id_anchors_its_call(ledger: Ledger) -> None:
    """Owner's first run: the model read the ID in a tool result's header, named it as the
    anchor's ``call``, and was refused. It names the result, as ``message`` does; an ID of
    another kind of message, or one out of the author's sight, says what to do instead."""
    projects = ProjectStore(ledger.documents.path.parent)
    projects.open_session("p", "s2", dedicated=False)
    projects.close()
    shown = said(ledger, result(OUTPUT, call="call-7"))
    asked_of = said(ledger, user("Is p = 17 the prime you meant? Yes, it is."))
    elsewhere = said(ledger, result("p = 19", call="call-8"), session="s2")
    observed = {**NEW["claim"], "basis": "observed", "statement": "The cost is $2^{80}$."}
    change(ledger, {**observed, "anchors": [{"call": shown}]}, op="by-message")
    stored = ledger.entry("p", "C-1")
    assert stored.basis == "observed"
    # With a range of its output, as a call ID gives one.
    ranged = {**observed, "statement": "The cost is $2^{81}$."}
    refused(ledger, {**ranged, "anchors": [{"call": shown, "start": 0, "end": 30}]},
            op="range", match="2^81")  # fmt: skip
    # A message that is no tool result: the error says how to anchor it.
    refused(ledger, {**NEW["claim"], "anchors": [{"call": asked_of}]}, op="user",
            match="is the ID of a user message, not of a tool result")  # fmt: skip
    # One the author may not see reads as missing, and says what kind of ID it was given.
    refused(ledger, {**NEW["claim"], "anchors": [{"call": elsewhere}]}, op="far",
            match="looks like a message ID")  # fmt: skip


def test_work_records_are_anchored_and_the_platforms_anchor_themselves(
    ledger: Ledger, tmp_path: Path
) -> None:
    """CM-9: every work record of an agent has an anchor, or a reference to a delegation,
    an artifact or a document; an import is observed on its results file's line, and a
    check's outcome on its delegation."""
    message = said(ledger, user("Try a lattice attack."))
    change(ledger, {**NEW["issue"], "anchors": [{"message": message}]}, op="issue")
    tried = {"entry": "I-1", "tried": "A lattice attack.", "ending": "limit", "limit": "time"}
    with pytest.raises(DocumentError, match="work 1 names no anchor"):
        change(ledger, op="w0", work=[tried])
    change(ledger, op="w1", work=[{**tried, "anchors": [{"message": message}]}])
    artifact, _ = publish(cast(Any, ledger), tmp_path, b"ran 2^40 trials\n")
    cited = {**tried, "basis": "observed", "tried": "Ran $2^{40}$ trials.",
             "references": [{"artifact": artifact, "revision": 1}]}  # fmt: skip
    change(ledger, op="w2", work=[cited])
    with pytest.raises(DocumentError, match=r"2\^41"):
        change(ledger, op="w3", work=[{**cited, "tried": "Ran $2^{41}$ trials."}])
    (latest, earlier) = ledger.work("p", "I-1").records
    assert (latest.basis, latest.anchors) == ("observed", [])
    assert (earlier.basis, earlier.anchors) == (
        "interpreted",
        [{"kind": "user", "message": message}],
    )
    # A check's outcome is the platform's record, observed on its delegation.
    outcome = {
        "operation_id": "work-dlg",
        "reason": "The check ended.",
        "work": [
            {"entry": "I-1", "tried": "An independent check.", "ending": "limit", "limit": "tools"}
        ],
    }
    ledger.change("p", outcome, DELEGATED, provenance={"delegation": "dlg-" + "0" * 24})
    record = ledger.work("p", "I-1").records[0]
    assert (record.basis, record.anchors) == ("observed", [{"delegation": "dlg-" + "0" * 24}])


def test_an_import_is_observed_only_on_an_executions_output(ledger: Ledger, tmp_path: Path) -> None:
    """M2 of #457's review, the coordinator's decision: an import is observed only when its
    results file is an artifact revision an execution produced, anchored on that revision
    and the line; a file without that lineage, or that is no recorded artifact, imports as
    interpreted."""
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    inventory = ArtifactInventory(ledger.documents)
    executed = source(tmp_path).model_copy(
        update={"kind": "execution_record", "execution_id": "exec-1", "attempt_state": "succeeded"}
    )
    failed = executed.model_copy(update={"attempt_state": "failed"})
    digests = {name: f"{n:02d}" * 32 for n, name in enumerate(("ran", "failed", "written"))}
    made = {
        name: inventory.record_metadata(
            by, f"results/{name}.jsonl", sha256=digests[name], size=10,
            media_type="application/jsonl", note="too_large",
        )[0].artifact_id
        for name, by in (("ran", executed), ("failed", failed), ("written", source(tmp_path)))
    }  # fmt: skip

    def imported(n: int, name: str, **line: Any) -> tuple[str | None, list[dict[str, Any]]]:
        mutation = {"operation_id": f"line-{n}", "reason": "From the helper.",
                    "changes": [{**NEW["claim"], "statement": f"Order {n}."}]}  # fmt: skip
        given = {"file": f"results/{name}.jsonl", "sha256": digests.get(name, "ab" * 32),
                 "line": 1, "environment": "env", **line}  # fmt: skip
        ledger.change("p", mutation, MAIN, provenance=given)
        view = ledger.entry("p", f"C-{n}")
        return view.basis, view.anchors

    on = {"sha256": digests["ran"], "line": 1}
    assert imported(1, "ran", artifact=made["ran"], revision=1) == (
        "observed", [{"artifact": made["ran"], "revision": 1, **on}],
    )  # fmt: skip
    assert imported(2, "failed", artifact=made["failed"], revision=1)[0] == "interpreted"
    assert imported(3, "written", artifact=made["written"], revision=1)[0] == "interpreted"
    # Another file's revision, or none, anchors on the file itself.
    assert imported(4, "other", artifact=made["ran"], revision=1) == (
        "interpreted", [{"file": "results/other.jsonl", "line": 1, "sha256": "ab" * 32}],
    )  # fmt: skip
    assert imported(5, "other")[0] == "interpreted"


def test_anchors_resolve_only_where_their_author_may_look(ledger: Ledger) -> None:
    """L1 and L2 of #457's review (CM-37, PD-13): a main agent anchors within its session,
    a delegated one within its own run and the messages its brief names; a call ID is
    looked for in the author's run first, and one that two other runs share is
    ambiguous. What it may not see reads as missing, its content unshown."""
    projects = ProjectStore(ledger.documents.path.parent)
    projects.open_session("p", "s2", dedicated=False)
    projects.close()
    secret = "A message in another session, said by the user there."
    elsewhere = said(ledger, user(secret), session="s2")
    with pytest.raises(DocumentError, match=f"no archive message {elsewhere}") as error:
        change(ledger, {**NEW["claim"], "anchors": [{"message": elsewhere}]}, op="far")
    assert "said by the user" not in error.value.message
    assert error.value.message.count("provenance_required") == 1
    # A call ID is looked for in the author's own run first.
    said(ledger, result("mine: 7", call="call_0"))
    said(ledger, result("theirs: 2^128", call="call_0"), session="s2")
    said(ledger, result("one: 2^128", call="call_1"), run=R2)
    said(ledger, result("two: 2^129", call="call_1"), run=R3)
    observed = {**NEW["claim"], "basis": "observed", "statement": "It is 7."}
    change(ledger, {**observed, "anchors": [{"call": "call_0"}]}, op="own")
    refused(ledger, {**observed, "anchors": [{"call": "call_1"}]}, op="two",
            match="call call_1 has a tool result in more than one run")  # fmt: skip
    # A delegated run sees its own run and the messages its brief names.
    named = said(ledger, user("Is 2^16+1 prime? It seems so to me."))
    unnamed = said(ledger, user("Something the brief does not name at all."))
    own = said(ledger, result("checked: 65537 is prime", call="call_9"), run=R2)
    DelegationRecords(ledger.documents.control).start("p", "s", Delegation.model_validate({
        "delegation_id": "dlg-" + "1" * 24, "parent": R1, "run": R2, "kind": "task",
        "brief": {"task": "Check it.", "inputs": [f"The user's question, {named}."]},
        "started_at": STARTED.isoformat()}))  # fmt: skip
    issue = {**NEW["issue"], "status": None}
    for n, anchor in enumerate(({"message": named}, {"message": own}, {"call": "call_9"})):
        change(ledger, {**issue, "anchors": [anchor]}, op=f"seen-{n}", author=DELEGATED)
    for n, anchor in enumerate(({"message": unnamed}, {"call": "call_0"})):
        with pytest.raises(DocumentError, match="in what this run may see"):
            change(ledger, {**issue, "anchors": [anchor]}, op=f"unseen-{n}", author=DELEGATED)


def test_the_anchors_the_ledger_fixtures_name(ledger: Ledger) -> None:
    """LG-F2 to LG-F5 and LG-F12 as amended: a project result proposed without evidence
    is anchored on the tool result that measured it (LG-F2); the main agent's decisions
    name the claim's revision (LG-F3); a claim with no evidence, the message that states it
    (LG-F4); an assumption, the tool result that fixed the prime, and its revision, the
    edited block (LG-F5); an issue, the notice that delivered a finding, and an assumption's
    revision, the user's edited block (LG-F12)."""
    measured = said(ledger, result("time 2^105 (105), memory 80", call="measure"))
    r1 = {**NEW["result"], "basis": "observed", "anchors": [{"call": "measure"}]}
    change(ledger, r1, op="lg-f2")
    stated = said(ledger, user("Note that a^16 = 1 for each nonzero a mod 17, it seems."))
    c5 = {**NEW["claim"], "anchors": [{"message": stated}]}
    change(ledger, c5, op="lg-f4")
    decision = {"type": "decision", "status": "in_effect", "title": "Dispute C-1",
                "statement": "C-1 rests on nothing yet.",
                "links": [{"kind": "affects", "entry": "C-1"}],
                "anchors": [{"entry": "C-1", "revision": 1}]}  # fmt: skip
    change(ledger, decision, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="lg-f3")
    fixed = said(ledger, result("p = 17", call="prime"))
    a1 = {**NEW["assumption"], "basis": "observed", "anchors": [{"call": "prime"}]}
    change(ledger, a1, op="lg-f5")
    documents = ledger.documents
    doc = documents.create("p", name="ff", title="FF", text="# F\n\n$p = 17$.\n", author=USER)
    block = documents.mapped("p", doc.document_id, 1).block_ids[1]
    edited = {"entry": "A-1", "expected": 1, "statement": "$p = 17$, edited.",
              "anchors": [{"document": "ff", "revision": 1, "block": block}]}  # fmt: skip
    change(ledger, edited, op="lg-f5-r")
    notice = said(ledger, Entry(role="user", kind="notice", content="A warn finding on C-1.",
                                source="review"))  # fmt: skip
    i1 = {**NEW["issue"], "links": [{"kind": "concerns", "entry": "C-1"}],
          "anchors": [{"message": notice}]}  # fmt: skip
    change(ledger, i1, op="lg-f12")
    assert ledger.entry("p", "R-1").anchors == [{"kind": "tool_result", "message": measured}]
    assert ledger.entry("p", "D-1").anchors == [
        {"entry": ledger.entry("p", "C-1").entry_id, "revision": 1}
    ]
    assert ledger.entry("p", "A-1").anchors == [
        {"block": block, "document": doc.document_id, "revision": 1}
    ]
    assert ledger.entry("p", "A-1", revision=1).basis == "observed"
    assert ledger.entry("p", "A-1", revision=1).anchors == [
        {"kind": "tool_result", "message": fixed}
    ]
    assert ledger.entry("p", "I-1").anchors == [{"kind": "notice", "message": notice}]


def test_a_store_of_the_version_before_shows_no_basis(tmp_path: Path) -> None:
    """The version that keeps bases and anchors adds them as columns: a store's earlier
    revisions show no basis, and its later ones do."""
    import sqlite3

    from sandbox_science.orchestrator.control import migrations

    from .control_versions import older_store

    current = ProjectStore(tmp_path / "current")
    try:
        current.create("p", "minimal", "sha256:" + "a" * 64)
        current.open_session("p", "s", dedicated=False)
    finally:
        current.close()
    documents = DocumentStore(store_path(tmp_path / "current"))
    try:
        change(Ledger(documents), NEW["issue"], op="made", author=USER)
    finally:
        documents.close()
    target = tmp_path / "old"
    target.mkdir()
    before = 50  # the version before 51, which keeps bases and anchors
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", before)
    ProjectStore(target).close()
    documents = DocumentStore(store_path(target))
    try:
        migrated = Ledger(documents)
        assert migrated.entry("p", "I-1").basis is None
        change(migrated, {"entry": "I-1", "expected": 1, "title": "Later"}, op="later", author=USER)
        assert migrated.entry("p", "I-1").basis == "user"
    finally:
        documents.close()
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)


async def test_the_user_quoted_from_the_message_that_started_the_run(harness: Harness) -> None:
    """H2 of #457's review, on the real path: a queued chat message starts a run, which
    receives it as the user's message, archived with the chat message's ID; the agent
    quotes it by the archive ID its header shows, and the standing constraint is recorded
    as user_quoted."""
    post(harness, "m-1", CONSTRAINT)

    def quoting(messages: list[Any]) -> list[tuple[str, dict[str, Any]]]:
        (asked,) = [m for m in messages if m["role"] == "user" and CONSTRAINT in m["content"]]
        found = HEADER.match(asked["content"])
        assert found is not None
        anchor = {"message": found[1], "quote": "attacks whose memory stays below 2^64 bits"}
        return [call("ledger_change", reason="The user's constraint.",
                     changes=[{**DECISION, "anchors": [anchor]}])]  # fmt: skip

    async with serving(harness, Reply(calls_from=quoting), Reply("Recorded.")):
        await ended(harness)
    documents = harness.documents()
    try:
        view = Ledger(documents).entry("p", "D-1")
    finally:
        documents.close()
    assert view.basis == "user_quoted"
    (anchor,) = view.anchors
    projects = ProjectStore(harness.state)
    try:
        archived = Archive(projects.control).message("p", anchor["message"])
    finally:
        projects.close()
    assert (archived.entry.kind, archived.entry.refs) == ("user", ("m-1",))


async def test_the_user_quoted_from_the_operators_task(harness: Harness) -> None:
    """A run started with ``run --task`` archives the task naming its own run as the
    operator's (CM-9): the agent quotes it as the user's words."""

    def quoting(messages: list[Any]) -> list[tuple[str, dict[str, Any]]]:
        (task,) = [m for m in messages if m["role"] == "user" and TASK in m["content"]]
        found = HEADER.match(task["content"])
        assert found is not None
        anchor = {"message": found[1], "quote": TASK}
        return [call("ledger_change", reason="The operator's task.",
                     changes=[{**DECISION, "anchors": [anchor]}])]  # fmt: skip

    record = await harness.run("run-cli", [Reply(calls_from=quoting), Reply("Recorded.")])
    documents = harness.documents()
    try:
        view = Ledger(documents).entry("p", "D-1")
    finally:
        documents.close()
    assert view.basis == "user_quoted"
    projects = ProjectStore(harness.state)
    try:
        archived = Archive(projects.control).message("p", view.anchors[0]["message"])
    finally:
        projects.close()
    name = run_name(record.started_at, record.run_id)
    assert (archived.entry.kind, archived.entry.refs) == ("task", (f"cli:{name}",))
