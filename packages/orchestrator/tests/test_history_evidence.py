# SPDX-License-Identifier: MIT
"""Original-span validation stays scoped and bounded across pages and long histories."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import Archive, Archived, Entry, FoldRecord
from sandbox_science.orchestrator.context.history.enrich import check
from sandbox_science.orchestrator.context.history.evidence import (
    ArchiveEvidence,
    EvidenceMatches,
    EvidenceRequest,
    RowEvidence,
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
    # Each original respects the per-message limit; together they exceed the old
    # 64 MiB aggregate cap while a fixed candidate proves the last result and key.
    first = archive.append(
        "p", "s", "main", Entry("tool", "tool_result", ("x" * 60000 + ";") * 600, source="python")
    )
    row = archive.append(
        "p",
        "s",
        "main",
        Entry(
            "tool",
            "tool_result",
            ("x" * 60000 + ";") * 600 + "result 2^{80}; exact checkpoint",
            source="python",
        ),
    )
    source = ArchiveEvidence(archive, CONTEXT, first.position, row.position)
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


@pytest.mark.parametrize("separator", [",", ", "])
def test_long_short_numeric_arrays_remain_provable_across_pages(
    archive: Archive, separator: str
) -> None:
    text = "[" + separator.join(["7"] * 70_000) + "]"
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", text))
    request = EvidenceRequest(numbers=frozenset({"7", "77", "777"}))
    for source in (
        RowEvidence([row]),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
    ):
        matches = source.verify(request)
        assert matches.numbers == {"7"}
        assert matches.diagnostic() == {}


@pytest.mark.parametrize("digits_before_page_end", [0, 1, 2, 3])
@pytest.mark.parametrize(
    ("original", "supported", "unsupported"),
    [
        ("1,000,003", {"1000003"}, {"1", "000", "003", "1000"}),
        ("12,3456", {"12", "3456"}, {"123456", "345"}),
        ("1,234", {"1234"}, {"1", "234"}),
        ("1,23", {"1", "23"}, {"123", "2"}),
    ],
)
def test_page_split_commas_preserve_thousands_and_reject_numeric_prefixes(
    archive: Archive,
    digits_before_page_end: int,
    original: str,
    supported: set[str],
    unsupported: set[str],
) -> None:
    # Put the comma and at most its first three following digits in this page.
    before_comma = original.index(",")
    prefix = "x" * (CHUNK_BYTES - before_comma - digits_before_page_end - 3) + "; "
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", prefix + original))
    request = EvidenceRequest(numbers=frozenset(supported | unsupported))
    matches = ArchiveEvidence(archive, CONTEXT, row.position, row.position).verify(request)
    assert matches.numbers == supported
    assert matches.diagnostic() == {}


def test_unicode_decimal_thousands_keep_the_existing_normalization(archive: Archive) -> None:
    row = archive.append(
        "p", "s", "main", Entry("assistant", "assistant", "1,₂₃₄; \u0661,\u0662\u0663\u0664; 7,8,9")
    )
    request = EvidenceRequest(numbers=frozenset({"1234", "1", "234", "7", "8", "9"}))
    matches = ArchiveEvidence(archive, CONTEXT, row.position, row.position).verify(request)
    assert matches.numbers == {"1234", "7", "8", "9"}
    assert matches.diagnostic() == {}


@pytest.mark.parametrize(
    ("original", "expected"),
    [("Difference -0.5.", True), ("Difference -0.5.0.", False), ("Difference -0.50.", False)],
)
def test_signed_sentence_punctuation_is_not_a_numeric_prefix(
    archive: Archive, original: str, expected: bool
) -> None:
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", original))
    made, why = check(
        json.dumps({"narrative": "Difference -.5.", "keys": []}),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
    )
    assert why == ""
    assert made is not None
    assert (made.narrative == "Difference -.5.") is expected


def test_completed_candidate_stops_body_reads_but_missing_facts_scan_the_span(
    archive: Archive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = archive.append("p", "s", "main", Entry("assistant", "assistant", "known result 289;"))
    last = archive.append("p", "s", "main", Entry("assistant", "assistant", "later result 999;"))
    read = archive.message_field_chunk
    bodies: list[str] = []

    def observed(project, message, field, **kwargs):
        bodies.append(message)
        return read(project, message, field, **kwargs)

    monkeypatch.setattr(archive, "message_field_chunk", observed)
    source = ArchiveEvidence(archive, CONTEXT, first.position, last.position)
    request = EvidenceRequest(
        numbers=frozenset({"289"}), literals=frozenset({first.message_id}), keys=("known result",)
    )
    found = source.verify(request)
    assert found.numbers == {"289"}
    assert found.keys == {"known result": first.message_id}
    assert set(bodies) == {first.message_id}
    bodies.clear()
    found = source.verify(replace(request, numbers=frozenset({"289", "777"})))
    assert found.numbers == {"289"}
    assert set(bodies) == {first.message_id, last.message_id}


@pytest.mark.parametrize(
    ("following", "supported"),
    [("0;", False), ("0, -0.7];", False), (" next observation;", True), ("", True)],
)
def test_negative_sentence_period_waits_for_the_next_page_before_proving_a_value(
    archive: Archive, following: str, supported: bool
) -> None:
    # The earlier positive value supplies numeric support, so only the signed
    # whole-value pattern can prevent a premature successful verification.
    prefix, boundary = "Positive 0.5; ", "; -0.5."
    text = prefix + "x" * (CHUNK_BYTES - len(prefix) - len(boundary)) + boundary + following
    row = archive.append("p", "s", "main", Entry("assistant", "assistant", text))
    made, why = check(
        json.dumps({"narrative": "Difference -.5.", "keys": []}),
        record(row),
        ArchiveEvidence(archive, CONTEXT, row.position, row.position),
        share=4000,
    )
    assert why == ""
    assert made is not None
    assert (made.narrative == "Difference -.5.") is supported
