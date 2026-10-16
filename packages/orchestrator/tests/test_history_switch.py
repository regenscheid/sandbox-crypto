# SPDX-License-Identifier: MIT
"""A switch to a model with a smaller ceiling (docs/specs/context-management.md, CM-31):
CM-F12's refit, as far as step (f2b) builds it. The engine is driven on archives written
through the archive's own API with scripted models, and through the command line against
the scripted gateway; no real gateway is called."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.history import Archive, Entry, Folder, geometry, session_view
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_history_folds import CONTEXT, ENRICHED, Answering, folds, worked

Json = dict[str, Any]


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield Archive(projects.control)
    projects.close()


def switching(
    archive: Archive, ceiling: int, former: object | None
) -> tuple[Folder, Transcript, list[Json]]:
    """A run on a model whose ceiling is ``ceiling``, after one at 400,000 tokens."""
    start = geometry(ceiling, 0, 10_000)
    view = session_view(archive, CONTEXT, geometry=start)
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=ceiling,
                  chunk=10_000, model=cast(Any, Answering()), switched=("larger", 400_000),
                  former=cast(Any, former))  # fmt: skip
    events: list[Json] = []

    async def write(data: Json) -> None:
        events.append(data)

    made.events = write
    return made, Transcript(messages=[{"role": "system", "content": "s"}, *view.messages]), events


async def test_a_switch_to_a_smaller_model_refits_the_view_under_its_trigger(
    archive: Archive,
) -> None:
    """CM-31, CM-F12: before the smaller model's first call, the view is refitted to its
    geometry, under its trigger, the earlier model writing the folds' narratives."""
    for n in range(60):
        worked(archive, "earlier", n, 4_000)
    made, transcript, events = switching(archive, 60_000, Answering())
    assert made.tokens(transcript) > geometry(60_000, 0, 10_000).wall
    assert await made.before_call(transcript)
    assert made.geometry is not None
    assert made.tokens(transcript) <= made.geometry.trigger
    assert events[0]["state"] == "refit"
    applied = [fold for fold in made.folds if "parts" not in fold]
    assert applied
    assert all(fold["state"] == "applied" for fold in applied)  # the earlier model's
    refit = cast(Json, made.summary())["refit"]
    assert (refit["from"], refit["ceiling"]) == ("larger", 400_000)
    assert refit["after"] <= made.geometry.trigger < refit["before"]


async def test_without_the_earlier_model_the_refit_is_skeleton_only(archive: Archive) -> None:
    for n in range(60):
        worked(archive, "earlier", n, 4_000)
    made, transcript, _ = switching(archive, 60_000, None)
    assert await made.before_call(transcript)
    assert made.geometry is not None
    assert made.tokens(transcript) <= made.geometry.trigger
    assert {fold["state"] for fold in made.folds} <= {"skeleton_only", "code_merge"}


async def test_a_refit_that_cannot_fit_refuses_the_switch(archive: Archive) -> None:
    """CM-31: a view that would still be irreducible under the smaller model refuses the
    switch with the reason."""
    archive.append(PROJECT, SESSION, "earlier", Entry("user", "user", "x" * 300_000))
    made, transcript, _ = switching(archive, 60_000, Answering())
    assert not await made.before_call(transcript)
    assert made.stopped == "irreducible"
    assert made.detail is not None
    assert made.detail.startswith("the switch from larger, whose ceiling was 400,000 tokens")


async def test_the_harness_names_the_earlier_model_of_the_session(harness: Harness) -> None:
    """The run after one on another alias with a larger ceiling refits; one on the same
    alias does not."""
    # The scripted gateway answers the alias "delegated" from its delegated script.
    await harness.run("run-1", [], options=["--context-ceiling", "400000", "--model",
                                            "delegated"], delegated=[Reply("First.")])  # fmt: skip
    record = await harness.run("run-2", [Reply("Second.")], options=ENRICHED)
    refit = folds(record)["refit"]
    assert (refit["from"], refit["ceiling"]) == ("delegated", 400_000)
    again = await harness.run("run-3", [Reply("Third.")], options=ENRICHED)
    assert "refit" not in folds(again)
