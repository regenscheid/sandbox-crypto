# SPDX-License-Identifier: MIT
"""Lineage pins include original raw and normalized identities, never current time."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import (
    KernelBirthPin,
    KernelInfo,
    KernelLineageEntry,
    KernelLineageIdentity,
    KernelLineageObservation,
    kernel_lineage_sha256,
)


def birth(**change: object) -> KernelBirthPin:
    return KernelBirthPin.model_validate(
        {
            "project_id": "p",
            "environment_id": "e",
            "session_id": "s",
            "language": "python",
            "incarnation": "inc-" + "a" * 24,
            "kernel_id": "k",
            "sandbox": "ss-original",
            "sandbox_id": "vm-original",
            "generation": 2,
            "birth_id": "birth-" + "a" * 24,
            "start_revision": 0,
            **change,
        }
    )


def entry() -> KernelLineageEntry:
    return KernelLineageEntry(
        kernel=KernelInfo(
            kernel_id="k",
            language="python",
            generation=5,
            start_revision=0,
            current_revision=1,
            revision_status="predates",
        ),
        birth=birth(),
        lineage_status="proved",
    )


def test_lineage_key_is_canonical_and_pins_every_original_identity() -> None:
    original = KernelLineageIdentity(birth=birth(), generation=5)
    digest = kernel_lineage_sha256(original)
    assert len(digest) == 64
    assert (
        kernel_lineage_sha256(KernelLineageIdentity.model_validate_json(original.model_dump_json()))
        == digest
    )
    changes = {
        "project_id": "other",
        "environment_id": "other",
        "session_id": "other",
        "language": "sage",
        "incarnation": "inc-" + "b" * 24,
        "kernel_id": "other",
        "sandbox": "ss-other",
        "sandbox_id": "vm-other",
        "generation": 3,
        "birth_id": "birth-" + "b" * 24,
        "start_revision": 1,
    }
    for key, value in changes.items():
        assert (
            kernel_lineage_sha256(KernelLineageIdentity(birth=birth(**{key: value}), generation=5))
            != digest
        )
    assert kernel_lineage_sha256(KernelLineageIdentity(birth=birth(), generation=6)) != digest


@pytest.mark.parametrize(
    "change",
    [
        {"sandbox": None, "sandbox_id": None},
        {"start_revision": None},
        {"generation": True},
        {"generation": 1.0},
        {"incarnation": "unknown"},
        {"birth_id": "legacy"},
    ],
)
def test_unproved_or_coerced_birth_never_gets_a_key(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KernelLineageIdentity.model_validate(
            {"birth": {**birth().model_dump(), **change}, "generation": 5}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"kernel_id": "other"},
        {"language": "sage"},
        {"start_revision": 1},
    ],
)
def test_proved_entry_binds_original_to_observed_kernel(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KernelLineageEntry(kernel=entry().kernel, birth=birth(**change), lineage_status="proved")


def test_unknown_has_no_partial_birth_authority() -> None:
    for change in ({"birth": birth()}, {"unknown_reason": None}):
        with pytest.raises(ValidationError):
            KernelLineageEntry.model_validate(
                {
                    "kernel": entry().kernel,
                    "lineage_status": "unknown",
                    "unknown_reason": "missing_birth",
                    **change,
                }
            )
    unknown = KernelLineageEntry(
        kernel=entry().kernel, lineage_status="unknown", unknown_reason="missing_birth"
    )
    assert unknown.birth is None


@pytest.mark.parametrize(
    "change",
    [
        {"birth": birth().model_copy(update={"generation": True})},
        {"birth": birth().model_copy(update={"sandbox": None})},
        {"generation": True},
        {"generation": 1},
    ],
)
def test_lineage_key_revalidates_unchecked_model_copies(change: dict[str, object]) -> None:
    original = KernelLineageIdentity(birth=birth(), generation=5)
    with pytest.raises(ValidationError):
        kernel_lineage_sha256(original.model_copy(update=change))


def test_known_birth_with_unavailable_current_revision_is_not_a_stale_claim() -> None:
    old = entry()
    result = KernelLineageEntry(
        kernel=old.kernel.model_copy(
            update={"current_revision": None, "revision_status": "unknown"}
        ),
        birth=old.birth,
        lineage_status="proved",
    )
    observed = KernelLineageObservation(
        project_id="p",
        environment_id="e",
        session_id="s",
        kernels=(result,),
        observed_at=datetime.now(UTC),
    )
    assert observed.current_revision is None
    assert observed.kernels[0].kernel.revision_status == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other"},
        {"environment_id": "other"},
        {"session_id": "other"},
        {"kernels": (entry(), entry())},
        {"current_revision": 0},
        {"observed_at": datetime(2026, 10, 12)},
    ],
)
def test_observation_scope_and_comparison_are_coherent(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KernelLineageObservation.model_validate(
            {
                "project_id": "p",
                "environment_id": "e",
                "session_id": "s",
                "current_revision": 1,
                "kernels": (entry(),),
                "observed_at": datetime.now(UTC),
                **change,
            }
        )


@pytest.mark.parametrize("timestamp", [0, 1.5, True])
def test_observation_receipt_requires_an_explicit_aware_timestamp(timestamp: object) -> None:
    import json

    with pytest.raises(ValidationError):
        KernelLineageObservation.model_validate_json(
            json.dumps(
                {
                    "project_id": "p",
                    "environment_id": "e",
                    "session_id": "s",
                    "observed_at": timestamp,
                }
            )
        )
