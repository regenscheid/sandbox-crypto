# SPDX-License-Identifier: MIT
"""What a run did, for the reviewer to match its narration against (docs/specs/
consistency-review.md, CR-2 as the coordinator amended it for lab 40): the toolbox names
each call's effect class, the review controller records each call that changes something,
and a review's snapshot holds one ``actions`` item for its window, listing them by run with
their outcomes and what they made, and the blocks each document revision changed."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.consistency.actions import action_of
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_consistency import MAIN, context_for, dispatch
from .test_consistency import store as store


def called(number: int, tool: str, arguments: dict[str, Any], **record: Any) -> tuple[
    list[dict[str, Any]], dict[str, Any]
]:  # fmt: skip
    """The transcript's call of ``tool`` and its result, and the call as the run executed
    it, with its record (``tool_records``)."""
    call_id = f"call-{number}"
    encoded = json.dumps(arguments)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": tool, "arguments": encoded}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "done"},
    ]  # fmt: skip
    return messages, {"tool": tool, "arguments": encoded, **record}


def test_an_action_says_what_the_call_did_and_how_it_ended() -> None:
    """Only a call whose effect class is effect is an action; a gate's or a store's
    refusal is refused, a failure an error, and what the call made comes from its record."""
    ledger = {"ledger": "ledger_change", "effect": "effect",
              "result": {"entries": [{"handle": "C-1", "revision": 2}],
                         "work": ["work-" + "1" * 24]}}  # fmt: skip
    messages, call = called(1, "ledger_change", {"reason": "r"}, **ledger)
    assert action_of(messages, call) == {
        "tool": "ledger_change", "call": "call-1", "target": {}, "outcome": "ok",
        "made": ["ledger C-1 r2", "work record work-" + "1" * 24],
    }  # fmt: skip
    for record, outcome in (
        ({"error": "policy_denied"}, "refused"),
        ({"error": "internal"}, "error"),
        ({"document": {"outcome": "conflict", "document_id": "doc-1", "revision": 3}},
         "refused"),
    ):  # fmt: skip
        messages, call = called(2, "document_patch", {"document": "notes"}, effect="effect",
                                **record)  # fmt: skip
        found = action_of(messages, call)
        assert found is not None
        assert (found["outcome"], found["made"], found["target"]) == (
            outcome, [], {"document": "notes"}
        )  # fmt: skip
    messages, call = called(3, "ledger_read", {"entry": "C-1"}, ledger="ledger_read", effect="read")
    assert action_of(messages, call) is None
    assert action_of(messages, None) is None


@pytest.mark.asyncio
async def test_the_toolbox_names_each_calls_effect_class(store: Any) -> None:
    from sandbox_science.orchestrator.ledger.tool import LedgerTool

    from .test_ledger_tools import CONTEXT

    reviews, _, _ = store
    tool = LedgerTool(Ledger(reviews.documents), CONTEXT, run_id="r1", role="main")
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, ledger=tool)
    listed = await toolbox.call(ToolCall("c1", "ledger_list", "{}"), "op-1")
    task = {"type": "task", "title": "Run it", "statement": "Run it."}
    changed = await toolbox.call(
        ToolCall("c2", "ledger_change", json.dumps({"reason": "r", "changes": [task]})), "op-2"
    )
    assert (listed.record["effect"], changed.record["effect"]) == ("read", "effect")


@pytest.mark.asyncio
async def test_a_reviews_snapshot_lists_the_windows_actions_and_changed_blocks(
    store: Any,
) -> None:
    """Lab 40's agent narrated edits it never made. The snapshot holds one actions item
    for the window: each run's calls that changed something, in order, an ok call with
    what it made and a refused one with nothing, and the blocks each document revision
    changed; the calls are not items one by one, and the reads are not listed."""
    reviews, documents, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    transcript: list[dict[str, Any]] = []
    made = {"ledger": "ledger_change", "effect": "effect",
            "result": {"entries": [{"handle": "C-1", "revision": 1}]}}  # fmt: skip
    for number, (tool, record) in enumerate(
        [("ledger_change", made), ("ledger_read", {"ledger": "ledger_read", "effect": "read"}),
         ("python", {"effect": "effect", "error": "policy_denied"})], 1
    ):  # fmt: skip
        exchange, call = called(number, tool, {"entry": "C-1"}, **record)
        transcript += exchange
        await controller.safe_point(transcript, tool=record, call=call)
    notes = documents.create(
        "p", name="notes", title="Notes", text="First.\n\nThe order is 17.\n", author=MAIN,
        session_id="s",
    )  # fmt: skip
    second = documents.mapped("p", notes.document_id, 1).block_ids[1]
    documents.patch(
        "p", notes.document_id, base=1, reason="Fix the order.", author=MAIN, operation_id="fix",
        operations=[{"op": "replace", "block": second, "markdown": "The order is 19."}],
    )  # fmt: skip
    reviews.ingest_documents("p", "s", drain=True)
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    assert "action" not in {i["kind"] for i in items}
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", review, listed["evidence"])["text"])
    assert [(a["tool"], a["outcome"], a["made"]) for a in found["actions"]] == [
        ("ledger_change", "ok", ["ledger C-1 r1"]),
        ("python", "refused", []),
    ]
    assert {a["run"] for a in found["actions"]} == {"author-1"}
    (second_revision,) = [d for d in found["documents"] if d["revision"] == 2]
    assert (second_revision["changed"], second_revision["since"]) == ([second], 1)


def test_a_review_without_actions_or_documents_has_no_actions_item(store: Any) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    kinds = {i["kind"] for i in cast(ReviewStore, reviews).manifest("p", "s", review)["evidence"]}
    assert "actions" not in kinds
