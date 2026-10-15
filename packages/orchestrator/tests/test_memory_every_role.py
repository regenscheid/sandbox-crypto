# SPDX-License-Identifier: MIT
"""PM-F4 through ``cli.main``: a run and the review of its answer are given the project
memory as the run started with it, though the user accepts a proposal during the run;
the next run is given the accepted entry (docs/specs/project-memory.md, PM-5, PM-8)."""

from __future__ import annotations

from typing import cast

from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.memory import ProjectMemory
from sandbox_science.orchestrator.memory_prompt import REVIEWER_FRAMING, RUN_FRAMING
from sandbox_science.orchestrator.prompts import PromptPins

from .loop_harness import PRIOR, PROJECT, Harness, Reply, Reviewer
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

KNOWN = "Write finite fields as GF(p)."
ACCEPTED = "Install Python packages with provision."


def system(messages: list[dict[str, object]]) -> str:
    return cast(str, messages[0]["content"])


async def test_a_run_and_its_review_share_the_memory_it_started_with(harness: Harness) -> None:
    harness.configure_reviews(ReviewConfig())
    harness.gateway.reviewer = Reviewer(flags=0)
    documents = harness.documents()
    try:
        memory = ProjectMemory(documents)
        memory.change(PROJECT, {"operation_id": "u1", "action": "add", "text": KNOWN}, USER)
        proposal = {"action": "add", "text": ACCEPTED, "reason": "Lost on a kernel restart."}
        memory.propose(PROJECT, proposal, PRIOR, "op-1")
    finally:
        documents.close()

    async def accept() -> None:
        documents = harness.documents()
        try:
            change = {"operation_id": "u2", "action": "accept", "proposal": "prop-1"}
            ProjectMemory(documents).change(PROJECT, change, USER)
        finally:
            documents.close()

    record = await harness.run("run-1", [Reply("The task is done.", before=accept)], reviewer=True)
    (main,) = harness.gateway.main
    assert RUN_FRAMING in system(main)
    assert KNOWN in system(main)
    assert ACCEPTED not in system(main)
    reviewed = harness.gateway.requests["reviewer"]
    assert reviewed
    assert REVIEWER_FRAMING in system(reviewed[0])
    assert KNOWN in system(reviewed[0])
    assert ACCEPTED not in system(reviewed[0])
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.memory is not None
    assert pins.memory.version == 1

    later = await harness.run("run-2", [Reply("Noted.")])
    pins = later.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.memory is not None
    assert pins.memory.version == 2
    assert ACCEPTED in system(harness.gateway.main[0])
