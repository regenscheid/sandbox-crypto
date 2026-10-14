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
from typing import Any, Final

from pydantic import BaseModel

from sandbox_science.contracts import ExecutionRequest, JobRequest, ProvisionRequest
from sandbox_science.contracts.canonical import request_digest

_LEGACY_JOB_TIMEOUT: Final = 86400.0
"""The job time limit an unset ``timeout_seconds`` serialized as before protocol 2."""
_CONTEXT: Final = dict.fromkeys(
    ("project_id", "session_id", "environment_id", "policy_revision", "actor"), True
)
_COMMAND: Final = ("script", "argv")
_LIMITS: Final = ("timeout_seconds", "output_limit_bytes")
_LEGACY_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "shell": ("kind", "operation_id", "context", *_COMMAND, "cwd", *_LIMITS),
    "job": (
        *("kind", "operation_id", "context", *_COMMAND),
        *("purpose", "expected_progress", "wake_session", "cwd", *_LIMITS),
    ),
    "cell": ("kind", "operation_id", "context", "language", "code", *_LIMITS),
    "pdf": (
        *("kind", "operation_id", "context", "operation", "path", "pages", "query", "page"),
        *("region", "dpi", "include_artifacts", "out", *_LIMITS),
    ),
    "provision": (
        *("operation_id", "context", "purpose", "capability", "conda", "pip", "pip_reason"),
        *("system", "action"),
    ),
}
"""Each request type's fields as protocol 1 wrote them, in order. A field added since is
left out of the old digest; a request that sets one is never matched to an old digest."""


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
    fields = _LEGACY_FIELDS["provision"]
    if request.action == "install":
        fields = tuple(name for name in fields if name != "action")
    return digest is not None and digest in (
        provision_digest(request),
        _legacy_digest(request, fields, ""),
    )


def _legacy_execution(request: ExecutionRequest) -> str | None:
    suffix = ""
    if isinstance(request, JobRequest):
        explicit = request.timeout_seconds is not None
        if not explicit:
            request = request.model_copy(update={"timeout_seconds": _LEGACY_JOB_TIMEOUT})
        suffix = f":explicit_timeout={explicit}"
    return _legacy_digest(request, _LEGACY_FIELDS[request.kind], suffix)


def _legacy_digest(request: BaseModel, fields: tuple[str, ...], suffix: str) -> str | None:
    """The protocol 1 digest, or None when the request sets a field protocol 1 lacked."""
    given: dict[str, Any] = request.model_dump(exclude_defaults=True)
    if not set(given) <= set(fields) or not set(given.get("context", {})) <= _CONTEXT.keys():
        return None
    include: dict[str, Any] = {name: _CONTEXT if name == "context" else True for name in fields}
    encoded = request.model_dump_json(include=include) + suffix
    return hashlib.sha256(encoded.encode()).hexdigest()
