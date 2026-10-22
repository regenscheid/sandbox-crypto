# SPDX-License-Identifier: MIT
"""Original-span validation stays scoped and bounded across pages and long histories."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import Archive, Archived, Entry, FoldRecord
from sandbox_science.orchestrator.context.history.enrich import check
from sandbox_science.orchestrator.context.history.evidence import (
    ArchiveEvidence,
    EvidenceMatches,
    EvidenceRequest,
)
from sandbox_science.orchestrator.context.history.operations import CHUNK_BYTES
from sandbox_science.orchestrator.projects import ProjectStore

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    try:
        yield Archive(projects.control)
    finally:
        projects.close()


def record(row: Archived) -> FoldRecord:
    return FoldRecord(
        level=2,
        first=(row.position, row.message_id),
        last=(row.position, row.message_id),
        runs=(row.run,),
        dates=(row.created, row.created),
        skeleton=(),
        markers=(),
        keys=(),
        parts=("msg-0000000000000000",),
    )


@pytest.mark.parametrize(
    ("source", "narrative"),
    [
        ("Bound 2^{80} holds.", "Bound 2**80 holds."),
        ("Bound 2⁸⁰ holds.", "Bound 2^80 holds."),
        ("Cost 1,000 and probability 9.9e-7.", "Cost 1000 and probability 9.9e-7."),
        ("Probability 1.2 \\times 10^{-5}.", "Probability 1.2e-5."),
        ("Signed difference -0.5.", "Signed difference -.5."),
        ("A 512bit width and 1 000 003 cases.", "A 512bit width and 1000003 cases."),
    ],
)
def test_original_supported_normalizations_survive_a_byte_boundary(
    archive: Archive,
    source: str,
    narrative: str,
) -> None:
    digit = next(index for index, char in enumerate(source) if char.isdecimal() or char == "\u2212")
    prefix = "x" * (CHUNK_BYTES - len(source[: digit + 1].encode()) - 2) + "; "
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", prefix + source))
    made, why = check(
        json.dumps({"narrative": narrative, "keys": []}),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
    )
    assert why == ""
    assert made is not None
    assert made.narrative == narrative


def test_keys_ids_and_complete_handles_cross_pages_without_accepting_fragments(
    archive: Archive,
) -> None:
    key = "Exact \u03b1😀 research checkpoint"
    body = "x" * (CHUNK_BYTES - 7) + "; " + key + "; C-12suffix is unrelated; magnitude 123456."
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", body))
    narrative = f"Continue {key} from {row.message_id}; C-1; magnitude 3456."
    made, why = check(
        json.dumps({"narrative": narrative, "keys": [key, "invented checkpoint"]}),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
    )
    assert why == ""
    assert made is not None
    assert (key, row.message_id) in made.keys
    assert all(k != "invented checkpoint" for k, _ in made.keys)
    assert row.message_id in (made.narrative or "")
    assert "; C-1;" not in (made.narrative or "")
    assert "3456" not in (made.narrative or "")


def test_delegated_evidence_excludes_parent_and_siblings(archive: Archive) -> None:
    parent = archive.append("p", "s", "parent", Entry("user", "user", "parent value 777"))
    sibling = archive.append(
        "p", "s", "sibling", Entry("user", "task", "sibling value 888"), parent="parent"
    )
    own = archive.append("p", "s", "child", Entry("user", "task", "own value 289"), parent="parent")
    archive.append("p", "s", "parent", Entry("user", "user", "later parent value 999"))
    source = ArchiveEvidence(archive, CONTEXT, parent.position, own.position + 1, run="child")
    result = source.verify(
        EvidenceRequest(
            numbers=frozenset({"289", "777", "888", "999"}),
            literals=frozenset({parent.message_id, sibling.message_id, own.message_id}),
            keys=("own value", "parent value", "sibling value"),
        )
    )
    assert result.numbers == {"289"}
    assert result.literals == {own.message_id}
    assert result.keys == {"own value": own.message_id}


def test_tool_projection_never_uses_extra_fields_or_native_payloads(archive: Archive) -> None:
    call = {
        "id": "call-a",
        "type": "function",
        "hidden_extra": "forged value 31337",
        "function": {"name": "python", "arguments": '{"x":289}'},
    }
    row = archive.append(
        "p",
        "s",
        "main",
        Entry(
            "assistant",
            "assistant",
            None,
            tool_calls=(call,),
            native=b"native-only 999" + b"n" * (12 * 1024 * 1024),
            native_format="fixture",
        ),
    )
    text = "The value 289 came from python; 31337 and 999 are unsupported."
    expected, _ = check(
        json.dumps({"narrative": text, "keys": ["python"]}), record(row), [row], share=4000
    )
    actual, why = check(
        json.dumps({"narrative": text, "keys": ["python"]}),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
    )
    assert why == ""
    assert expected is not None
    assert actual is not None
    assert actual == expected
    assert "31337" not in (actual.narrative or "")
    assert "999" not in (actual.narrative or "")


def test_unbounded_numeric_fragment_is_explicitly_unproved_without_losing_other_sources(
    archive: Archive,
) -> None:
    row = archive.append(
        "p",
        "s",
        "main",
        Entry(
            "assistant",
            "assistant",
            "1" * (2 * CHUNK_BYTES) + "; supported result 289; exact checkpoint",
        ),
    )
    reports: list[EvidenceMatches] = []
    made, why = check(
        json.dumps(
            {"narrative": "Result 289 remains; 111 is unproved.", "keys": ["exact checkpoint"]}
        ),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
        evidence_report=reports.append,
    )
    assert why == ""
    assert made is not None
    assert "289" in (made.narrative or "")
    assert "111" not in (made.narrative or "")
    assert made.keys == (("exact checkpoint", row.message_id),)
    assert reports[0].diagnostic()["unproved_numeric_fragments"] > 0


@pytest.mark.slow
def test_overview_evidence_exceeds_old_payload_ceiling_without_retaining_originals(
    archive: Archive,
) -> None:
    # The original is larger than the old 64 MiB aggregate cap. The bounded candidate
    # still proves its source value and exact key near the end of that same message.
    row = archive.append(
        "p",
        "s",
        "main",
        Entry(
            "tool",
            "tool_result",
            ("x" * 60000 + ";") * 1120 + "result 2^{80}; exact checkpoint",
            source="python",
        ),
    )
    source = ArchiveEvidence(archive, CONTEXT, row.position, row.position)
    tracemalloc.start()
    try:
        found = source.verify(
            EvidenceRequest(numbers=frozenset({"2^80"}), keys=("exact checkpoint",))
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found.numbers == {"2^80"}
    assert found.keys == {"exact checkpoint": row.message_id}
    assert peak < 8 * 1024 * 1024


@pytest.mark.slow
def test_overview_evidence_exceeds_old_row_count_without_rebuilding_a_working_view(
    archive: Archive,
) -> None:
    first = last = None
    for index in range(8200):
        row = archive.append("p", "s", "main", Entry("assistant", "assistant", f"case {index};"))
        first = first or row
        last = row
    assert first is not None
    assert last is not None
    found = ArchiveEvidence(archive, CONTEXT, first.position, last.position).verify(
        EvidenceRequest(numbers=frozenset({"0", "8199"}), keys=("case 8199",))
    )
    assert found.numbers == {"0", "8199"}
    assert found.keys == {"case 8199": last.message_id}
