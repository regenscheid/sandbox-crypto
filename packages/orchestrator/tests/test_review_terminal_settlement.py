# SPDX-License-Identifier: MIT
"""A requested checkpoint review survives a conversational final answer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelError, ModelTurn

from .test_consistency import claim, context_for
from .test_consistency import store as store
from .test_review_limits import LabReviewer


class DelayedReviewer(LabReviewer):
    def __init__(self, *, finding=False, failed=False):
        super().__init__()
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.finding, self.failed, self.raised = finding, failed, False

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        self.started.set()
        await self.release.wait()
        if self.failed:
            raise ModelError("synthetic interrupted response")
        answer = await super().complete(messages, tools)
        if self.finding and not self.raised and answer.text is not None:
            self.raised = True
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            evidence = next(
                result["evidence"][0]["evidence"]
                for result in results
                if isinstance(result.get("evidence"), list)
            )
            return self.turn(
                [
                    (
                        "review_finding",
                        {
                            "severity": "fail",
                            "category": "contradicting_value",
                            "statement": "The recorded count requires correction.",
                            "anchors": [{"evidence": evidence}],
                            "operation_id": "delayed-finding",
                        },
                    )
                ],
                100,
            )
        return answer


async def start(reviews, model, *, hold=True, messages=None):
    reviews.defaults = ReviewConfig(terminal_hold=hold, min_interval_seconds=0)
    claim(reviews)
    reviews.request("p", "s")
    controller = ReviewController(
        reviews,
        context_for(reviews),
        run_id="author-1",
        model=model,
        alias="reviewer",
        messages=messages,
    )
    await controller.safe_point([])
    await model.started.wait()
    return controller, Reviews(controller)


@pytest.mark.parametrize("finding", [False, True])
async def test_conversational_final_waits_for_requested_review_and_offers_corrections(
    store, finding
):
    reviews, _, _ = store
    model = DelayedReviewer(finding=finding)
    controller, hook = await start(reviews, model)
    closing = asyncio.create_task(hook.closing(Transcript(messages=[]), "I am waiting for review."))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)
        model.release.set()
        result = await closing
        assert result.action == ("respond" if finding else "accept")
        summary = hook.summary()
        assert isinstance(summary, dict)
        assert summary["classifier"]["label"] == "conversation"
        assert summary["outcome"] == ("findings" if finding else "passed")
        assert not summary.get("pending")
        listed = reviews.listing("p", "s")["reviews"]
        assert len(listed) == 1
        assert listed[0]["state"] == summary["outcome"]
        if finding:
            assert any(notice.ids for notice in result.notices)
    finally:
        model.release.set()
        await controller.close()


async def test_failed_requested_review_stays_pending_without_dispatch_at_closing(store):
    reviews, _, _ = store
    model = DelayedReviewer(failed=True)
    controller, hook = await start(reviews, model)
    try:
        model.release.set()
        result = await hook.closing(Transcript(messages=[]), "I am waiting for review.")
        assert result.action == "accept"
        summary = hook.summary()
        assert isinstance(summary, dict)
        assert summary["outcome"] == "failed"
        assert summary["pending"]
        assert len(reviews.listing("p", "s")["reviews"]) == 1
        progress = reviews.progress("p", "s")
        assert progress["watermark"] == 0
        assert progress["retry_end"] == 1
    finally:
        await controller.close()


async def test_disabled_terminal_hold_does_not_wait_for_requested_review(store):
    reviews, _, _ = store
    model = DelayedReviewer()
    controller, hook = await start(reviews, model, hold=False)
    try:
        result = await asyncio.wait_for(
            hook.closing(Transcript(messages=[]), "I am waiting for review."), timeout=1
        )
        assert result.action == "accept"
        summary = hook.summary()
        assert isinstance(summary, dict)
        assert summary["pending"]
        assert reviews.listing("p", "s")["reviews"][0]["state"] == "running"
    finally:
        await controller.close()


async def test_new_user_input_interrupts_requested_terminal_review_wait(store):
    reviews, _, _ = store
    model = DelayedReviewer()
    messages: asyncio.Queue[str] = asyncio.Queue()
    controller, hook = await start(reviews, model, messages=messages)
    try:
        messages.put_nowait("Please use the corrected premise.")
        result = await asyncio.wait_for(
            hook.closing(Transcript(messages=[]), "I am waiting for review."), timeout=1
        )
        assert result.action == "stop"
        assert result.stopped == "user_message"
        summary = hook.summary()
        assert isinstance(summary, dict)
        assert summary["user_message"] == "Please use the corrected premise."
    finally:
        await controller.close()
