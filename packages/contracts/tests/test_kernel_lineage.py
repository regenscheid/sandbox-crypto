# SPDX-License-Identifier: MIT
"""A kernel's recorded birth: exact identities, and a VM binding given whole or not at all."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import KernelBirthPin

BIRTH: dict[str, object] = {
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
}


def test_a_recorded_birth_round_trips() -> None:
    birth = KernelBirthPin.model_validate(BIRTH)
    assert KernelBirthPin.model_validate_json(birth.model_dump_json()) == birth
    unbound = KernelBirthPin.model_validate({**BIRTH, "sandbox": None, "sandbox_id": None})
    assert unbound.sandbox is None


@pytest.mark.parametrize(
    "change",
    [
        {"sandbox": None},
        {"sandbox_id": None},
        {"incarnation": "inc-short"},
        {"birth_id": "birth-" + "A" * 24},
        {"generation": 0},
        {"generation": "2"},
        {"start_revision": -1},
        {"language": "r"},
        {"observed": "extra"},
    ],
)
def test_a_birth_that_is_not_exact_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KernelBirthPin.model_validate({**BIRTH, **change})
