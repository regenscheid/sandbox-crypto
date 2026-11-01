# SPDX-License-Identifier: MIT
"""Enrichment uses actual available room and retains supported continuation facts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from sandbox_science.orchestrator.context.history import Archive, Archived, Entry, FoldRecord
from sandbox_science.orchestrator.context.history.enrich import EnrichmentProgress, enrich
from sandbox_science.orchestrator.model import Message, ModelTurn, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore


@pytest.fixture
def original(tmp_path: Path) -> Iterator[tuple[list[Archived], FoldRecord]]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    row = Archive(projects.control).append(
        "p",
        "s",
        "r",
        Entry("assistant", "assistant", "The small-case proof failed. Next: test the open lemma."),
    )
    record = FoldRecord(
        level=1,
        first=(row.position, row.message_id),
        last=(row.position, row.message_id),
        runs=("r",),
        dates=(row.created, row.created),
        skeleton=("Exact original remains available.",),
        markers=({"kind": "caveat", "subject": "lemma", "text": "The proof is incomplete."},),
        keys=(("open lemma", row.message_id),),
    )
    try:
        yield [row], record
    finally:
        projects.close()


async def test_first_request_accounts_for_outline_and_retains_handoff_without_a_repair(
    original: tuple[list[Archived], FoldRecord],
) -> None:
    rows, record = original
    record = replace(record, skeleton=("x" * 3400,))
    requests: list[list[Message]] = []
    narrative = "The proof failed. The open lemma needs checking."

    class Model:
        async def complete(
            self, messages: Sequence[Message], tools: Sequence[ToolSpec]
        ) -> ModelTurn:
            requests.append(list(messages))
            return ModelTurn(json.dumps({"narrative": narrative, "keys": []}))

    prefix: list[Message] = [{"role": "system", "content": "Unchanged cached prefix"}]
    result = await enrich(Model(), prefix, (), record, rows, share=4000)
    assert result.failed is None
    assert result.record.narrative == narrative
    assert result.record.markers == record.markers
    assert result.record.keys == record.keys
    assert result.record.chars <= 4000
    assert len(requests) == 1
    assert requests[0][:-1] == prefix
    instruction = str(requests[0][-1]["content"])
    target = re.search(r"narrative target is at most ([\d,]+)", instruction)
    assert target is not None
    assert len(narrative) <= int(target[1].replace(",", "")) < 1000
    assert "failed approaches" in instruction
    assert "open questions" in instruction
    assert "exact retrieval references" in instruction


async def test_sparse_outline_keeps_full_existing_narrative_cap(
    original: tuple[list[Archived], FoldRecord],
) -> None:
    rows, record = original
    requests: list[list[Message]] = []

    class Model:
        async def complete(
            self, messages: Sequence[Message], tools: Sequence[ToolSpec]
        ) -> ModelTurn:
            requests.append(list(messages))
            return ModelTurn(
                json.dumps({"narrative": "The open lemma needs checking.", "keys": []})
            )

    made = await enrich(Model(), (), (), record, rows, share=4000)
    assert made.failed is None
    assert "narrative target is at most 1,000 characters" in str(requests[0][-1]["content"])


async def test_full_retained_record_does_not_dispatch_or_claim_usage(
    original: tuple[list[Archived], FoldRecord],
) -> None:
    rows, record = original
    record = replace(record, keys=())

    class NeverCalled:
        async def complete(
            self, messages: Sequence[Message], tools: Sequence[ToolSpec]
        ) -> ModelTurn:
            raise AssertionError("no narrative fits; no model may be called")

    async def admit(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> bool:
        raise AssertionError("there is no dispatch to reserve")

    progress = EnrichmentProgress()
    result = await enrich(
        NeverCalled(), (), (), record, rows, share=record.chars, admit=admit, progress=progress
    )
    assert result.record == record
    assert result.calls == ()
    assert result.failed is not None
    assert result.attempts[0]["outcome"] == "no_narrative_room"
    assert progress.inflight_input_tokens is None
    assert result.record.skeleton == record.skeleton
    assert result.record.first == record.first
    assert result.record.markers == record.markers
