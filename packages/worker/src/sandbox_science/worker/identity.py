# SPDX-License-Identifier: MIT
"""The digests that bind operation IDs to their requests (``contracts.canonical``).

The worker stores a request's digest with its operation, and an operation ID sent again
with a request of another digest is a conflict. Operations stored before protocol 2 hold
the older digest, pydantic's JSON of the whole request; it is still accepted, so they
replay across the upgrade. That fallback goes once no worker state from before protocol 2
remains.
"""

from __future__ import annotations

import hashlib

from sandbox_science.contracts import ExecutionRequest, JobRequest, ProvisionRequest
from sandbox_science.contracts.canonical import request_digest

_LEGACY_JOB_TIMEOUT = 86400.0
"""The job time limit an unset ``timeout_seconds`` serialized as before protocol 2."""


def execution_digest(request: ExecutionRequest) -> str:
    return request_digest(request, f"worker.execution.{request.kind}")


def provision_digest(request: ProvisionRequest) -> str:
    return request_digest(request, "worker.provision")


def same_execution(digest: str | None, request: ExecutionRequest) -> bool:
    """Whether a stored ``digest`` identifies ``request``, under either scheme."""
    return digest is not None and digest in (
        execution_digest(request),
        _legacy_execution(request),
    )


def same_provision(digest: str | None, request: ProvisionRequest) -> bool:
    """Whether a stored ``digest`` identifies ``request``, under either scheme."""
    legacy = request.model_dump_json(exclude={"action"} if request.action == "install" else None)
    return digest is not None and digest in (
        provision_digest(request),
        hashlib.sha256(legacy.encode()).hexdigest(),
    )


def _legacy_execution(request: ExecutionRequest) -> str:
    if not isinstance(request, JobRequest):
        return hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    explicit = request.timeout_seconds is not None
    if not explicit:
        request = request.model_copy(update={"timeout_seconds": _LEGACY_JOB_TIMEOUT})
    encoded = request.model_dump_json() + f":explicit_timeout={explicit}"
    return hashlib.sha256(encoded.encode()).hexdigest()
