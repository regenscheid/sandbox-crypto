# SPDX-License-Identifier: MIT
"""The change tool carries its own rules (docs/specs/ledger.md, LG-30, LG-42): its
schema states what a valid entry needs, with worked examples that apply as they are
shown; a refusal says what to add or change, in the field's form, and lists the change's
independent problems together; and a dry run checks a change and records nothing. The
defect tests mirror the kinds of refusal the lab's agents met most (the 2026-11-03 lab
round), with arguments written for the test."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator.context.history.archive import Archive, Entry
from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.ledger import Ledger as Unanchored
from sandbox_science.orchestrator.ledger import records
from sandbox_science.orchestrator.ledger.models import LINKS, Mutation
from sandbox_science.orchestrator.ledger.tool import (
    CHANGE_DESCRIPTION,
    EXAMPLE_FLAGS,
    EXAMPLES,
    SPECS,
    LedgerTool,
)
from sandbox_science.orchestrator.projects import run_name

from .ledger_support import AnchoredLedger as Ledger
from .test_ledger import NEW
from .test_ledger_evidence import publish
from .test_ledger_tools import ledger as ledger
from .test_ledger_tools import state as state
from .test_ledger_tools import tool, use

(CHANGE,) = [spec for spec in SPECS if spec.name == "ledger_change"]
CALL = "<call ID>"
"""The call the examples anchor on, archived as a tool result of run r1 by ``observed``."""
OUTPUT = "largest prime factor: 125 bits"


def observed(ledger: Ledger, output: str = OUTPUT, call: str = CALL) -> None:
    """A tool result of session s's run r1, which ``call`` names."""
    entry = Entry(role="tool", kind="tool_result", content=output, source="python", call_id=call)
    run = run_name(datetime(2026, 11, 3, tzinfo=UTC), "r1")
    Archive(ledger.control).append("p", "s", run, entry)


async def refusal(ledger: Ledger, op: str, *changes: dict[str, Any], **more: Any) -> str:
    """The message of the change's refusal, through the tool, as the model reads it."""
    out = await use(tool(ledger), "ledger_change", op, reason="r", changes=list(changes), **more)
    assert "error" in out, out
    return cast(str, out["error"]["message"])


async def made(ledger: Ledger, op: str, *changes: dict[str, Any], **more: Any) -> Any:
    out = await use(tool(ledger), "ledger_change", op, reason="r", changes=list(changes), **more)
    assert "error" not in out, out
    return out


# The schema states the rules.


def test_the_change_tool_states_the_rules_a_valid_entry_needs() -> None:
    """The rules the main prompt gave (loop.py) are the tool's own: which entries hold
    each kind of link and what it points to, as LINKS has them; whose each field is; a
    decision's reopen condition and its reopen_on links; a reference's locator and date;
    caveats an agent adds and never removes; and anchors, with their forms."""
    params = CHANGE.parameters["properties"]
    change = params["changes"]["items"]
    fields = change["properties"]["fields"]["properties"]
    link = change["properties"]["links"]["items"]["description"]
    for kind, (holders, _) in LINKS.items():
        assert f"{kind}: {'/'.join(holders)} to" in link
    assert "builds_on: claim/result/issue to claim/result" in link
    assert "affects: decision to any entry" in link
    assert "reopen_on" in fields["reopen_condition"]["description"]
    assert "or none" in fields["reopen_condition"]["description"]
    assert "pdf" in fields["identifiers"]["description"]
    assert "version or retrieved" in fields["published"]["description"]
    assert "removes none" in fields["caveats"]["description"]
    assert "lemma to other" in fields["kind"]["description"]
    assert "never type, origin or key" in change["description"]
    assert "links replaces" in change["description"]
    assert "source_claim" in change["properties"]["evidence"]["items"]["description"]
    anchors = change["properties"]["anchors"]["description"]
    assert "call (a tool call's ID" in anchors
    assert "message (msg-" in anchors
    assert "dry_run" in params
    assert "dry_run" in CHANGE_DESCRIPTION
    assert len(CHANGE.description) <= 1024


def test_the_change_tool_costs_what_it_did_before_its_rules_moved_in() -> None:
    """The system message and tools stay under a fifth of the context ceiling (CM-27): the
    rules and examples took the room the tool's repeated text held. 15,565 characters
    was its description and schema, as JSON, before (6ffdb9bc)."""
    size = len(CHANGE.description) + len(json.dumps(CHANGE.parameters))
    assert size <= 15565, size
    # No character the JSON escapes as six.
    assert json.dumps(CHANGE.parameters, ensure_ascii=False) == json.dumps(CHANGE.parameters)


def test_the_examples_are_shown_as_they_are_kept() -> None:
    shown = CHANGE.parameters["properties"]["changes"]["description"]
    for label, changes in EXAMPLES:
        assert label in shown
        for change in changes:
            for key in change:
                assert f"{key}: " in shown
    assert "[{entry: C-4, cause: assumption_revised}]" in str(
        CHANGE.parameters["properties"]["flags_cleared_on"]["description"]
    )
    # A plain word goes unquoted; one a parser would read as another type keeps its quotes.
    assert 'reopen_condition: "none"' in shown
    assert 'published: "2021"' in shown
    assert "{call: <call ID>}" in shown


def filled(value: Any, artifact: str) -> Any:
    """An example's arguments with the caller's own values for its placeholders."""
    if isinstance(value, dict):
        return {key: filled(item, artifact) for key, item in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [filled(item, artifact) for item in cast(list[Any], value)]
    if value == "...":
        return "Example text."
    if value == "artifact-...":
        return artifact
    return value


async def setup(ledger: Ledger, label: str, tmp_path: Path) -> str:
    """The entries an example names, at the revisions it names; the artifact it pins."""
    ledgers = tool(ledger)
    artifact = ""
    if label == "a finding, observed":
        artifact, _ = publish(cast(Any, ledger), tmp_path, b"largest prime factor bits: 125\n")
    claims = [{**NEW["claim"]} for _ in range(4)]
    if label == "a decision reversing D-2":
        await use(ledgers, "ledger_change", "setup", reason="r", changes=[
            *claims, {**NEW["decision"], "status": "in_effect"},
            {**NEW["decision"], "status": "in_effect"}])  # fmt: skip
    if label == "a revision":
        await use(ledgers, "ledger_change", "setup", reason="r", changes=claims)
        retitled = {"entry": "C-4", "expected": 1, "title": "Retitled"}
        await use(ledgers, "ledger_change", "retitle", reason="r", changes=[retitled])
    if label == "an obstructed issue reopened":
        await use(ledgers, "ledger_change", "setup", reason="r", changes=[NEW["issue"]] * 3)
        decision = {**NEW["decision"], "status": "in_effect",
                    "fields": {"excluded_scope": "Index calculus below 2^80."},
                    "links": [{"kind": "affects", "entry": "I-3"}]}  # fmt: skip
        obstructs = [decision, {"entry": "I-3", "expected": 1, "status": "obstructed"}]
        ledger.change("p", {"operation_id": "obstruct", "reason": "r", "changes": obstructs}, USER)
    return artifact


@pytest.mark.parametrize("label", [label for label, _ in EXAMPLES])
async def test_each_example_applies_as_it_is_shown(ledger: Ledger, state: Path, label: str) -> None:
    """Each example, its placeholders filled, checks with dry_run and records nothing,
    then records what the dry run said it would."""
    observed(ledger)
    artifact = await setup(ledger, label, state)
    (changes,) = [changes for name, changes in EXAMPLES if name == label]
    args = {"reason": "As the example shows.", "changes": filled(changes, artifact)}
    before = [(e.handle, e.revision, e.status) for e in ledger.entries("p").entries]
    ledgers = tool(ledger)
    checked = await use(ledgers, "ledger_change", "example", **args, dry_run=True)
    assert (checked["dry_run"], checked["recorded"]) == (True, False), checked
    assert [(e.handle, e.revision, e.status) for e in ledger.entries("p").entries] == before
    done = await use(ledgers, "ledger_change", "example", **args)
    assert "error" not in done, done
    assert not done["replayed"]
    assert [(e["handle"], e["revision"], e["status"]) for e in checked["would_record"]] == [
        (e["handle"], e["revision"], e["status"]) for e in done["entries"]
    ]


async def test_the_flag_example_clears_the_flags_it_names(ledger: Ledger) -> None:
    ledgers = tool(ledger)
    claims = [{**NEW["claim"]} for _ in range(3)]
    assumes = {**NEW["claim"], "links": [{"kind": "assumes", "entry": "A-1"}]}
    await use(ledgers, "ledger_change", "a", reason="r", changes=[NEW["assumption"], *claims])
    await use(ledgers, "ledger_change", "b", reason="r", changes=[assumes])
    revised = {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}
    await use(ledgers, "ledger_change", "c", reason="r", changes=[revised])
    assert [f.cause for f in ledger.entry("p", "C-4").flags] == ["assumption_revised"]
    out = await use(ledgers, "ledger_change", "d", reason="It holds.",
                    flags_cleared_on=EXAMPLE_FLAGS)  # fmt: skip
    assert (out["cleared_total"], ledger.entry("p", "C-4").flags) == (1, [])


# A dry run.


async def test_a_dry_run_records_nothing_not_even_its_operation(ledger: Ledger) -> None:
    """A dry run makes the change in its transaction, every check included, and rolls it
    back: its answer names what it would record, its refusal is the change's, and its
    record names no entry, so that nothing counts it as made."""
    ledgers = tool(ledger)
    text, record = await ledgers.call(
        "ledger_change",
        {"reason": "r", "changes": [NEW["task"], NEW["issue"]], "dry_run": True},
        operation="op-dry",
        call_id="c1",
        model="m",
    )
    out = json.loads(text)
    assert out["would_record"] == [
        {"handle": "T-1", "revision": 1, "status": "todo", "created": True},
        {"handle": "I-1", "revision": 1, "status": "open", "created": True},
    ]
    assert "entry_id" not in text
    assert record == {"ledger": "ledger_change", "dry_run": True}
    assert ledger.entries("p").entries == []
    # Nothing of it was kept, not even its receipt: the same operation records anew.
    real = await use(ledgers, "ledger_change", "op-dry", reason="r", changes=[NEW["task"]])
    assert (real["replayed"], [e["handle"] for e in real["entries"]]) == (False, ["T-1"])
    # A refusal is the one the change would get, marked as a dry run's.
    wrong = {"entry": "T-1", "expected": 1, "status": "finished"}
    text, record = await ledgers.call(
        "ledger_change",
        {"reason": "r", "changes": [wrong], "dry_run": True},
        operation="op-wrong",
        call_id="c2",
        model="m",
    )
    assert "a task is never finished" in json.loads(text)["error"]["message"]
    assert record == {"error": "invalid_request", "dry_run": True}
    refused = await use(ledgers, "ledger_change", "op-x", reason="r", changes=[], dry_run="yes")
    assert refused["error"]["message"] == "dry_run: give true or false"
    # A dry run of an operation recorded before says it would replay it.
    again = await use(ledgers, "ledger_change", "op-dry", reason="r", changes=[NEW["task"]],
                      dry_run=True)  # fmt: skip
    assert again["replayed"] is True


# Refusals name the fix: the lab's most frequent kinds, each in its shape.


async def test_a_reversal_hears_every_rule_of_both_decisions_at_once(ledger: Ledger) -> None:
    """The lab's most frequent refusals (19 of 82): a new decision reversing D-2 with a
    reopen condition but no reopen_on links, and D-2 reversed without a condition, were
    refused one rule at a time, three times. Now one refusal names both, with the forms
    to give, and following it records the change."""
    ledgers = tool(ledger)
    await use(ledgers, "ledger_change", "setup", reason="r", changes=[
        NEW["assumption"], {**NEW["decision"], "status": "in_effect"},
        {**NEW["decision"], "status": "in_effect"}])  # fmt: skip
    new = {**NEW["decision"], "key": "d3", "status": "in_effect",
           "fields": {"reopen_condition": "The factor table is corrected again."},
           "links": [{"kind": "affects", "entry": "D-2"}]}  # fmt: skip
    reverse = {"entry": "D-2", "expected": 1, "status": "reversed"}
    message = await refusal(ledger, "first", new, reverse)
    assert message.startswith("2 problems, each to fix: (1) D-3 (new:d3): its reopen condition")
    assert '{"kind": "reopen_on", "entry": "A-1"}' in message
    assert 'or make the condition "none"' in message
    assert "(2) D-2 becomes reversed: state what would justify reopening it" in message
    assert '"fields": {"reopen_condition": "none"}' in message
    assert "links replaces its links: give those it holds too" in message
    watched = {**new, "links": [*new["links"], {"kind": "reopen_on", "entry": "A-1"}]}
    stated = {**reverse, "fields": {"reopen_condition": "none"}}
    done = await made(ledger, "second", watched, stated)
    assert [(e["handle"], e["status"]) for e in done["entries"]] == [
        ("D-3", "in_effect"),
        ("D-2", "reversed"),
    ]


async def test_a_decision_in_effect_is_changed_by_a_new_decision(ledger: Ledger) -> None:
    """Five lab refusals: an agent revised a decision in effect to add its reopen
    condition. The refusal says how: a new decision in effect that affects it."""
    await made(ledger, "setup", NEW["assumption"], {**NEW["decision"], "status": "in_effect"})
    condition = {"fields": {"reopen_condition": "p changes."},
                 "links": [{"kind": "reopen_on", "entry": "A-1"}]}  # fmt: skip
    revised = {"entry": "D-1", "expected": 1, **condition}
    message = await refusal(ledger, "revise", revised)
    assert "D-1 is in_effect: its statement, fields, links and evidence change only" in message
    assert '{"kind": "affects", "entry": "D-1"}' in message
    assert "which may keep D-1 in_effect, or reverse it" in message
    affecting = {**NEW["decision"], "status": "in_effect", "fields": {"reopen_condition": "none"},
                 "links": [{"kind": "affects", "entry": "D-1"}]}  # fmt: skip
    await made(ledger, "decided", affecting, revised)
    assert ledger.entry("p", "D-1").fields == {"reopen_condition": "p changes."}


async def test_a_link_of_the_wrong_kind_names_what_to_give_instead(ledger: Ledger) -> None:
    """Eleven lab refusals: builds_on to a reference or a decision, affects held by a
    claim, builds_on held by a decision. Each says to remove the link, what holds it,
    what the entry holds, and the form that says what was meant; two links of one kind
    are one problem."""
    await made(ledger, "setup", NEW["reference"], {**NEW["decision"], "status": "in_effect"})
    cited = {**NEW["claim"], "links": [{"kind": "builds_on", "entry": "REF-1"}]}
    message = await refusal(ledger, "cites", cited)
    assert "a builds_on link cannot point to REF-1, a reference" in message
    assert '{"role": "supports", "type": "source_claim", "entry": "REF-1"}' in message
    links = [{"kind": "affects", "entry": "D-1"}, {"kind": "affects", "entry": "REF-1"}]
    affects = {**NEW["claim"], "key": "c", "links": links}
    message = await refusal(ledger, "affects", affects)
    assert message.startswith("C-1 (new:c): a claim holds no affects link: remove it")
    assert "a claim holds answers, assumes, builds_on, restates, compares_to or supersedes" in (
        message
    )
    assert "record an issue that concerns it" in message
    assert "problems" not in message  # the same problem twice is said once
    rests = {**NEW["decision"], "links": [{"kind": "builds_on", "entry": "REF-1"}]}
    message = await refusal(ledger, "rests", rests)
    assert "a decision holds no builds_on link: remove it" in message
    assert '{"role": "supports", "type": "exact_computation", "entry": "C-4"}' in message


async def test_fields_of_the_wrong_type_or_form_name_the_ones_to_give(ledger: Ledger) -> None:
    """Ten lab refusals: an issue of a claim's kind, a decision with caveats, a caveat over
    300 characters, a reference with no locator; and, in the seed, an empty reopen
    condition and a reference with no date. Each names the change, the field's path and
    what to give, and the change's independent problems come together."""
    observation = {**NEW["issue"], "fields": {"kind": "observation"}}
    caveated = {**NEW["decision"], "fields": {"caveats": ["Synthetic data."]}}
    message = await refusal(ledger, "fields", observation, caveated)
    assert message.startswith("2 problems, each to fix: (1) changes.0 (new issue): issue ")
    assert "fields.kind: Input should be 'question'" in message
    assert "(got 'observation')" in message
    assert '"type": "claim", "origin": "project"' in message
    assert "(2) changes.1 (new decision): decision fields: Extra inputs" not in message
    assert "fields.caveats: Extra inputs are not permitted: leave caveats out" in message
    assert "(caveats is a field of a claim or a result)" in message
    long = {**NEW["claim"], "fields": {"kind": "observation", "caveats": ["x" * 301]}}
    message = await refusal(ledger, "long", long)
    assert "changes.0 (new claim): claim fields.caveats.0: String should have at most 300" in (
        message
    )
    assert "(it has 301): shorten it; split a longer caveat into several" in message
    empty = {**NEW["decision"], "status": "rejected", "fields": {"reopen_condition": ""}}
    message = await refusal(ledger, "empty", empty)
    assert 'or "none" when nothing would' in message
    undated = {**NEW["reference"], "fields": {"identifiers": {"eprint": "2024/123"}}}
    message = await refusal(ledger, "undated", undated)
    assert 'give fields.published (such as "2021"' in message
    unplaced = {**NEW["reference"], "fields": {"published": "2024"}}
    assert "give fields.identifiers" in await refusal(ledger, "unplaced", unplaced)


async def test_keys_given_where_they_do_not_belong_say_where_they_go(ledger: Ledger) -> None:
    """Lab refusals of a change that held work records, or a reference's reads; of a
    revision naming its type; of an origin on a reference. Each says to leave the key out
    or move it, and what its place takes."""
    await made(ledger, "setup", NEW["reference"], NEW["claim"])
    work = {"entry": "REF-1", "expected": 1, "work": [{"tried": "Read it."}]}
    message = await refusal(ledger, "work", work)
    assert message.startswith("changes.0.work: Extra inputs are not permitted: move it to")
    assert '"work": [{"entry": "I-1"' in message
    assert "its place takes entry, expected, key, type, origin" in message
    reads = {"entry": "REF-1", "expected": 1, "read": [{"pages": "1-24"}]}
    assert "changes.0.read: Extra inputs are not permitted: leave it out: the ledger records" in (
        await refusal(ledger, "reads", reads)
    )
    given = {"entry": "C-1", "expected": 1, "type": "claim", "status": "withdrawn"}
    origin = {**NEW["reference"], "origin": "published"}
    message = await refusal(ledger, "shape", given, origin)
    assert "(1) changes.0 (C-1): a revision keeps the entry's type and origin: leave out type" in (
        message
    )
    assert "(2) changes.1 (new reference): claims and results, and only they, have an origin" in (
        message
    )
    assert "leave out origin for a reference" in message


async def test_a_new_entry_is_named_by_its_key_and_the_refusal_says_so(ledger: Ledger) -> None:
    """Lab refusals: evidence naming a new entry by its bare key, and links naming the
    handle a new entry would get."""
    reference = {**NEW["reference"], "key": "ref"}
    bare = {**NEW["claim"], "evidence": [{"role": "supports", "type": "source_claim",
                                          "entry": "ref"}]}  # fmt: skip
    message = await refusal(ledger, "bare", reference, bare)
    assert "'ref' names no entry: name the entry this change creates keyed ref as new:ref" in (
        message
    )
    guessed = {**NEW["claim"], "links": [{"kind": "builds_on", "entry": "C-1"}]}
    message = await refusal(ledger, "guessed", {**NEW["claim"], "key": "c1"}, guessed)
    assert "no entry C-1 in this project: the entries this change creates have no handle" in (
        message
    )
    assert "name them by their keys, new:c1" in message
    assert "name one it holds (ledger_list lists them)" in await refusal(ledger, "none", guessed)


async def test_provenance_refusals_give_the_anchor_forms(ledger: Ledger) -> None:
    """Sixteen lab refusals were provenance_required: a revision with no anchor, and
    observed records whose numbers its observations did not hold. Each gives the anchor
    forms, why it needs one, and how to record a number it derived."""
    plain = Unanchored(ledger.documents)  # adds no anchor of its own
    plain_tool = LedgerTool(plain, tool(ledger).context, run_id="r1", role="main")
    observed(ledger, "order 1000003")
    await made(ledger, "setup", NEW["claim"])
    reworded = {"entry": "C-1", "expected": 1, "statement": "Reworded."}
    out = await use(plain_tool, "ledger_change", "bare", reason="r", changes=[reworded])
    message = out["error"]["message"]
    assert "C-1 names no anchor: give it anchors" in message
    assert "which it needs since its statement changes" in message
    assert '[{"call": "<call ID>"}]' in message
    assert '[{"message": "msg-…"}]' in message
    wrong = {**NEW["claim"], "statement": "The order is 1000005.", "basis": "observed",
             "anchors": [{"call": CALL}]}  # fmt: skip
    out = await use(plain_tool, "ledger_change", "wrong", reason="r", changes=[wrong])
    message = out["error"]["message"]
    assert "1000005 is in none of its observations" in message
    assert 'give "basis": "interpreted" for a number you derived' in message


async def test_an_agent_that_drops_a_caveat_hears_which_and_how_to_keep_it(
    ledger: Ledger,
) -> None:
    """Three lab refusals: a revision whose fields dropped a caveat it held."""
    caveated = {**NEW["claim"], "fields": {"kind": "observation", "caveats": ["Small p only."]}}
    await made(ledger, "setup", caveated)
    rewritten = {"entry": "C-1", "expected": 1,
                 "fields": {"kind": "observation", "caveats": ["For p = 17 only."]}}  # fmt: skip
    message = await refusal(ledger, "rewritten", rewritten)
    assert "an agent adds caveats to C-1 and removes none" in message
    assert "give fields.caveats with its 1 caveats as they read, then the new ones" in message
    assert "('Small p only.' would go)" in message


async def test_work_and_flags_refusals_say_what_to_give(ledger: Ledger) -> None:
    """Lab refusals of work on a reference, of a produced ending naming nothing, and of
    flags cleared by a cause the entry's flags do not have."""
    await made(ledger, "setup", NEW["reference"], NEW["assumption"])
    await made(ledger, "assumes", {**NEW["claim"], "links": [{"kind": "assumes", "entry": "A-1"}]})
    on = await use(tool(ledger), "ledger_change", "on", reason="r",
                   work=[{"entry": "REF-1", "tried": "Read it.", "ending": "limit",
                          "limit": "tools"}])  # fmt: skip
    assert "name in entry the issue, claim or result the work was for" in on["error"]["message"]
    produced = {"entry": "C-1", "tried": "Ran it.", "ending": "produced"}
    nothing = await use(tool(ledger), "ledger_change", "nothing", reason="r", work=[produced])
    assert 'give "produced": the handles, or new:<key>' in nothing["error"]["message"]
    revised = {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}
    await made(ledger, "revised", revised)
    out = await use(tool(ledger), "ledger_change", "clear", reason="r",
                    flags_cleared_on=[{"entry": "C-1", "cause": "spread"}])  # fmt: skip
    assert out["error"]["message"] == (
        "C-1 has no open flag of cause spread: its open flags are of cause "
        "assumption_revised: name one, or leave out cause"
    )
    quote = {"source": "pdf", "page": 1, "first_line": 1, "last_line": 2, "text": "q" * 20}
    cited = {"role": "supports", "type": "source_claim", "artifact": "a", "revision": 1}
    unnamed = {**NEW["claim"], "evidence": [{**cited, "quote": quote}]}
    message = await refusal(ledger, "unnamed", unnamed)
    assert "give the evidence entry, the handle of the reference, or new:<key>" in message


# How refusals are put together.


def test_validation_problems_name_their_path_and_what_to_give() -> None:
    with pytest.raises(ValidationError) as error:
        Mutation.model_validate(
            {
                "operation_id": "o b",
                "reason": "",
                "changes": [{"type": "claim", "caveats": ["x"], "expected": "1"}],
            }
        )
    message = records.validation_error(error.value, Mutation)
    assert "operation_id: give letters, digits and . _ : -" in message
    # The reason is required: it is never one to leave out.
    assert "reason: String should have at least 1 character: give the text;" in message
    assert "changes.0.expected: Input should be a valid integer (got the text '1')" in message
    assert "changes.0.caveats: Extra inputs are not permitted: give it in fields" in message
    deep = {"operation_id": "o", "reason": "r", "changes": [{"title": "t" * 600}] * 6}
    with pytest.raises(ValidationError) as error:
        Mutation.model_validate(deep)
    message = records.validation_error(error.value, Mutation)
    assert message.endswith("; and 2 more")
    assert message.count("(it has 600)") == 4


def test_problems_are_reported_together_and_other_refusals_at_once() -> None:
    problems = records.Problems()
    for message in ("first is wrong", "second is wrong", "first is wrong"):
        with problems.check():
            raise DocumentError("invalid_request", message)
    with pytest.raises(DocumentError) as error:
        problems.stop()
    assert error.value.message == "2 problems, each to fix: (1) first is wrong (2) second is wrong"
    with pytest.raises(DocumentError, match="policy"), problems.check():
        raise DocumentError("policy_denied", "policy")
    records.Problems().stop()  # none found, none raised
