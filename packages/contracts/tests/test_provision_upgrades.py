# SPDX-License-Identifier: MIT
"""Explicit upgrade intent; an omitted action is an install, with the same identity."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext, ProvisionRequest
from sandbox_science.contracts.canonical import request_digest

# A request as the original 8675 encoder wrote it, before the action field existed.
LEGACY = (
    '{"operation_id":"upgrade-test","context":{"project_id":"p","session_id":"s",'
    '"environment_id":"e","policy_revision":null,"actor":"agent"},"purpose":"synthetic",'
    '"capability":null,"conda":["pari"],"pip":[],"pip_reason":null,"system":[]}'
)


def request(**fields: object) -> ProvisionRequest:
    return ProvisionRequest.model_validate({**json.loads(LEGACY), **fields})


def test_an_omitted_action_is_an_install_with_the_same_identity() -> None:
    digests = {
        request_digest(value, "provision") for value in (request(), request(action="install"))
    }
    assert len(digests) == 1
    assert ProvisionRequest.model_validate_json(LEGACY).action == "install"


def test_upgrade_is_explicit_and_changes_the_request_digest() -> None:
    value = request(action="upgrade")
    assert value.action == "upgrade"
    assert json.loads(value.model_dump_json())["action"] == "upgrade"
    assert request_digest(value, "provision") != request_digest(request(), "provision")
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


def test_regular_python_constructor_has_the_same_identity() -> None:
    value = ProvisionRequest(
        operation_id="upgrade-test",
        context=ExecutionContext(project_id="p", session_id="s", environment_id="e"),
        purpose="synthetic",
        conda=("pari",),
    )
    assert request_digest(value, "provision") == request_digest(request(), "provision")
