# SPDX-License-Identifier: MIT
"""Job requests cannot widen shell command rules or the seven-day hard bound."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ExecutionContext, ExecutionRequest, JobRequest

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")


def test_job_request_discriminator_and_defaults() -> None:
    request = TypeAdapter[ExecutionRequest](ExecutionRequest).validate_python(
        {
            "kind": "job",
            "operation_id": "op",
            "context": CONTEXT,
            "argv": ["true"],
            "purpose": "search parameters",
            "expected_progress": "line each minute",
        }
    )
    assert isinstance(request, JobRequest)
    assert request.timeout_seconds is None  # the policy's default, chosen by the worker
    assert not request.wake_session
    # The policy default survives a round trip; it never turns into an explicit limit.
    assert JobRequest.model_validate(request.model_dump()).timeout_seconds is None
    assert JobRequest.model_validate_json(request.model_dump_json()).timeout_seconds is None


@pytest.mark.parametrize(
    "override",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 604801},
        {"purpose": " "},
        {"expected_progress": ""},
        {"purpose": "a\x00b"},
        {"expected_progress": "a" * 4097},
        {"argv": ["true"], "script": "true"},
        {"script": "\x00"},
        {"code": "42"},
    ],
)
def test_job_requests_refuse_invalid_commands_and_metadata(override: dict[str, object]) -> None:
    body = {
        "operation_id": "op",
        "context": CONTEXT,
        "script": "true",
        "purpose": "search",
        "expected_progress": "progress",
    }
    with pytest.raises(ValidationError):
        JobRequest.model_validate(body | override)
