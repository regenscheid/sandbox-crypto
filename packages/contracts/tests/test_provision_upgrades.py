# SPDX-License-Identifier: MIT
"""Explicit upgrade intent with historical install-byte compatibility."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext, ProvisionRequest

# Captured from the original 8675 request encoder; this is not built from the new model.
LEGACY = (
    '{"operation_id":"upgrade-test","context":{"project_id":"p","session_id":"s",'
    '"environment_id":"e","policy_revision":null,"actor":"agent"},"purpose":"synthetic",'
    '"capability":null,"conda":["pari"],"pip":[],"pip_reason":null,"system":[]}'
)


def request(**fields: object) -> ProvisionRequest:
    return ProvisionRequest.model_validate({**json.loads(LEGACY), **fields})


def test_legacy_install_bytes_and_digest_remain_exact() -> None:
    for value in (request(), request(action="install")):
        assert value.model_dump_json() == LEGACY
        assert (
            hashlib.sha256(value.model_dump_json().encode()).digest()
            == hashlib.sha256(LEGACY.encode()).digest()
        )
    assert ProvisionRequest.model_validate_json(LEGACY).action == "install"


def test_upgrade_is_explicit_and_changes_original_request_digest() -> None:
    value = request(action="upgrade")
    assert value.action == "upgrade"
    assert json.loads(value.model_dump_json())["action"] == "upgrade"
    assert value.model_dump_json() != LEGACY
    assert ProvisionRequest.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "action", [None, True, 1, "", "remove", "restore", "upgrade-all", "UPGRADE"]
)
def test_unknown_action_never_defaults_to_install(action: object) -> None:
    with pytest.raises(ValidationError):
        request(action=action)


@pytest.mark.parametrize(
    "fields",
    [
        {"conda": [], "capability": "gp"},
        {"conda": [], "pip": ["example"], "pip_reason": "synthetic"},
        {"system": ["libgmp-dev"]},
        {"conda": []},
        {"pip": ["example"], "pip_reason": "synthetic"},
    ],
)
def test_upgrade_is_named_conda_only(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(action="upgrade", **fields)


def test_regular_python_constructor_keeps_same_legacy_bytes() -> None:
    assert (
        ProvisionRequest(
            operation_id="upgrade-test",
            context=ExecutionContext(project_id="p", session_id="s", environment_id="e"),
            purpose="synthetic",
            conda=("pari",),
        ).model_dump_json()
        == LEGACY
    )
