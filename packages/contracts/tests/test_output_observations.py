# SPDX-License-Identifier: MIT
"""Lossless output identities and deliberately incomplete source claims."""

import base64

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.output_observations import (
    OutputBinding,
    OutputCoverage,
    OutputObservation,
    OutputPath,
    OutputReport,
    OutputStat,
)


def test_raw_names_and_roots_never_alias() -> None:
    paths = [
        OutputPath.model_validate({"area": area, "path_b64": base64.b64encode(raw).decode()})
        for area, raw in (
            ("session", b"\xfe"),
            ("session", b"\xff"),
            ("session", "�".encode()),
            ("session", b"shared/x"),
            ("shared", b"x"),
            ("session", b"line\nbreak"),
        )
    ]
    assert len({path.identity() for path in paths}) == len(paths)
    assert paths[0].raw() == b"\xfe"


@pytest.mark.parametrize(
    "raw", [b"", b"/x", b"../x", b"x/./y", b"x//y", b"x/", b"x\0", b"x" * 4097]
)
def test_invalid_raw_paths_are_refused(raw: bytes) -> None:
    with pytest.raises(ValidationError):
        OutputPath(area="session", path_b64=base64.b64encode(raw).decode())


@pytest.mark.parametrize("encoded", ["!", "eB==", "eA", "eA==\n"])
def test_base64_is_strict_and_canonical(encoded: str) -> None:
    with pytest.raises(ValidationError):
        OutputPath(area="session", path_b64=encoded)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1, 2**63])
def test_stat_counters_are_exact_bounded_integers(value: object) -> None:
    with pytest.raises(ValidationError):
        OutputStat(mtime_ns=0, size=value, inode=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_protocol_cannot_be_coerced(value: object) -> None:
    with pytest.raises(ValidationError):
        OutputReport(protocol=value)  # type: ignore[arg-type]


def test_coverage_cannot_claim_all_mutations_or_observed_links() -> None:
    for field, value in (
        ("mutation_coverage", "complete"),
        ("links", "complete"),
        ("before_error", 1),
    ):
        with pytest.raises(ValidationError):
            OutputCoverage.model_validate({field: value})
    report = OutputReport()
    assert report.coverage.mutation_coverage == "incomplete"
    assert report.coverage.deletions == "unproven"


def test_report_semantics_duplicates_and_byte_bound() -> None:
    stat = OutputStat(mtime_ns=0, size=0, inode=1)
    with pytest.raises(ValidationError):
        OutputObservation(kind="not_seen_after", area="session", path_b64="eA==", after=stat)
    entry = OutputObservation(
        kind="regular_stat_difference", area="session", path_b64="eA==", after=stat
    )
    with pytest.raises(ValidationError, match="duplicate"):
        OutputReport(entries=(entry, entry))
    entries = tuple(
        entry.model_copy(update={"path_b64": base64.b64encode(bytes([65 + i]) * 4096).decode()})
        for i in range(4)
    )
    with pytest.raises(ValidationError, match="byte limit"):
        OutputReport(entries=entries)


@pytest.mark.parametrize(
    "fields",
    [
        {"before_started_ns": 3, "before_finished_ns": 2},
        {"after_finished_ns": 2},
        {"after_started_ns": True},
    ],
)
def test_source_intervals_are_typed_without_inventing_host_time(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OutputReport.model_validate(fields)
    assert OutputReport().after_finished_ns is None


@pytest.mark.parametrize(
    "fields",
    [
        {"incarnation": "inc-" + "a" * 24},
        {"dispatched": True, "incarnation": "wrong"},
        {"dispatched": True, "sandbox": "name"},
    ],
)
def test_original_dispatch_tuple_cannot_self_contradict(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OutputBinding.model_validate(
            {
                "project_id": "p",
                "session_id": "s",
                "environment_id": "e",
                "execution_id": "exe",
                "attempt_id": "att",
                "operation_id": "op",
                "actor": "agent",
                "kind": "shell",
                "request_sha256": None,
                **fields,
            }
        )
