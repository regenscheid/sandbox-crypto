# SPDX-License-Identifier: MIT
"""The reviewer's ``view_image`` (docs/specs/figures.md, FG-39; CR-2 as amended): an image
of its snapshot, offered when the reviewer's model accepts images, framed as data, and
counted against the review's budget by its pixels. The gateway is a fake LiteLLM."""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import images
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.notices import IMAGES
from sandbox_science.orchestrator.view_image import DRAWN
from sandbox_science.testing.pdfs import make_pdf

from .loop_harness import Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_view_image import png

Json = dict[str, Any]
FINISH = json.dumps(
    {"outcome": "inconclusive", "summary": "Looked at the plot.", "cap_reached": False}
)


class Looker:
    """A reviewer that reads the manifest, views the snapshot's first artifact, and ends."""

    def __init__(self) -> None:
        self.seen: list[Message] | None = None

    def __call__(self, messages: list[Message]) -> Reply:
        last = messages[-1]
        if last["role"] == "user" and IMAGES.text in str(last.get("content")):
            self.seen = messages
            return Reply(FINISH)
        if last["role"] == "tool":
            result = cast(Json, json.loads(last["content"]))
            items = cast(list[Json], result.get("evidence") or [])
            artifacts = [item["evidence"] for item in items if item["kind"] == "artifact"]
            if artifacts:
                return Reply(calls=[call("view_image", evidence=artifacts[0])])
            return Reply(FINISH)
        return Reply(calls=[call("review_manifest")])


def _plot_run(harness: Harness) -> list[Reply]:
    data = base64.b64encode(png(6, 4)).decode()
    write = f"mkdir -p plots && echo {data} | base64 -d > plots/t.png"
    return [Reply(calls=[call("shell", script=write)]), Reply("The plot is done.")]


@pytest.mark.asyncio
async def test_the_reviewer_looks_at_a_plot_of_its_snapshot(harness: Harness) -> None:
    harness.configure_reviews(ReviewConfig())
    harness.gateway.limits = {"reviewer": {"supports_vision": True}}
    looker = Looker()
    harness.gateway.reviewer = looker
    await harness.run("r1", _plot_run(harness), reviewer=True)
    assert looker.seen is not None
    offered = {tool["function"]["name"] for tool in harness.gateway.tools["reviewer"][0]}
    assert "view_image" in offered
    framed = looker.seen[-1]
    text, image = framed["content"]
    assert IMAGES.text in text["text"]
    assert (
        image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png(6, 4)).decode()
    )
    result = cast(Json, json.loads(looker.seen[-2]["content"]))
    assert (result["viewed"]["path"], result["viewed"]["revision"]) == ("sessions/s/plots/t.png", 1)


@pytest.mark.asyncio
async def test_no_view_image_for_a_reviewer_that_cannot_see(harness: Harness) -> None:
    harness.configure_reviews(ReviewConfig())
    harness.gateway.limits = {"reviewer": {"supports_vision": False}}
    looker = Looker()
    harness.gateway.reviewer = looker
    await harness.run("r1", _plot_run(harness), reviewer=True)
    offered = {tool["function"]["name"] for tool in harness.gateway.tools["reviewer"][0]}
    assert "view_image" not in offered


def test_an_image_counts_by_its_pixels() -> None:
    token = images.keep(images.Image("image/png", png(1600, 1200), 1600, 1200))
    try:
        messages = [{"role": "user", "content": "line", images.IMAGE: token}]
        assert images.tokens(messages) == 2560  # 1,600 x 1,200 pixels / 750
    finally:
        images.forget([token])
    assert images.tokens(messages) == 0


@pytest.mark.asyncio
async def test_a_drawn_figure_says_how_far_it_can_be_trusted(harness: Harness) -> None:
    """A PDF figure is drawn by the platform in the session's environment, and what the
    reviewer is shown says that it is only as trustworthy as that environment."""
    harness.configure_reviews(ReviewConfig())
    harness.gateway.limits = {"reviewer": {"supports_vision": True}}
    looker = Looker()
    harness.gateway.reviewer = looker
    data = base64.b64encode(make_pdf([[(72, 700, "Timing")]])).decode()
    write = f"mkdir -p plots && echo {data} | base64 -d > plots/fig.pdf"
    await harness.run("r1", [Reply(calls=[call("shell", script=write)]), Reply("Done.")],
                      reviewer=True)  # fmt: skip
    assert looker.seen is not None
    result = cast(Json, json.loads(looker.seen[-2]["content"]))
    assert result["viewed"]["drawn"]["trust"] == DRAWN
    assert DRAWN in looker.seen[-1]["content"][0]["text"]
