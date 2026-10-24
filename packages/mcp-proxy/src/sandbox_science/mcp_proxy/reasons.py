# SPDX-License-Identifier: MIT
"""Reason codes the proxy returns with a refusal, and the JSON-RPC errors that carry them.

A refusal is a JSON-RPC error whose ``data.reason`` is one of these codes. The
codes the policy middleware also uses mean the same here (docs/POLICY-MIDDLEWARE.md).
The message is fixed text for each code: the library's reason, which names the
rule that matched, stays in the proxy's log and the event store, as it does for
the middleware.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from sandbox_science.policy import reasons as policy

REFUSED: Final = -31001
"""JSON-RPC error code: the proxy refused the request, and nothing was sent to the server."""
UPSTREAM_FAILED: Final = -31002
"""JSON-RPC error code: the request was sent and may have had effects, but no usable answer came."""

# Decisions of the policy library, defined in policy/reasons.py.
APPROVAL_REQUIRED: Final = policy.APPROVAL_REQUIRED
APPROVAL_DENIED: Final = policy.APPROVAL_DENIED
ESCALATION_TIMED_OUT: Final = policy.ESCALATION_TIMED_OUT
POLICY_DENIED: Final = policy.POLICY_DENIED
POOL_CEILING_DENIED: Final = policy.POOL_CEILING_DENIED
DESTINATION_FORBIDDEN: Final = policy.DESTINATION_FORBIDDEN
REQUEST_UNCLASSIFIABLE: Final = policy.REQUEST_UNCLASSIFIABLE
AUDIT_UNAVAILABLE: Final = policy.AUDIT_UNAVAILABLE
POLICY_UNAVAILABLE: Final = policy.POLICY_UNAVAILABLE
LAYER_CODES: Final = policy.LAYER_CODES
"""The reason code of a denial, by the layer of the policy library that decided it."""

# The project's binding to a policy revision.
PROJECT_UNBOUND: Final = "project_unbound"

# The proxy's own checks.
UNAUTHENTICATED: Final = "unauthenticated"
ORIGIN_FORBIDDEN: Final = "origin_forbidden"
REQUEST_TOO_LARGE: Final = "request_too_large"
SESSION_REQUIRED: Final = "session_required"
SESSION_UNKNOWN: Final = "session_unknown"
PROTOCOL_UNSUPPORTED: Final = "protocol_unsupported"
TOOL_NOT_ADVERTISED: Final = "tool_not_advertised"
ARGUMENTS_TOO_LARGE: Final = policy.ARGUMENTS_TOO_LARGE
CREDENTIAL_UNAVAILABLE: Final = "credential_unavailable"
CREDENTIAL_IN_BODY: Final = policy.CREDENTIAL_IN_BODY
PROXY_BUSY: Final = "proxy_busy"

# The server's side.
FENCE_DENIED: Final = "fence_denied"
"""The proxy sandbox's OpenShell fence refused the call: its compiled policy is narrower."""
RESULT_TOO_LARGE: Final = "result_too_large"
UPSTREAM_ERROR: Final = "upstream_error"
UPSTREAM_UNAUTHORIZED: Final = "upstream_unauthorized"
UPSTREAM_UNAVAILABLE: Final = "upstream_unavailable"

MESSAGES: Final[Mapping[str, str]] = {
    APPROVAL_REQUIRED: "this call needs the user's approval; it is recorded as a pending request",
    APPROVAL_DENIED: "the user denied this call's permission request",
    ESCALATION_TIMED_OUT: "nobody answered this call's permission request in time",
    POLICY_DENIED: "the project policy denies this call",
    POOL_CEILING_DENIED: "the worker pool's ceiling denies this call",
    DESTINATION_FORBIDDEN: "the server's address is forbidden",
    REQUEST_UNCLASSIFIABLE: "the request is malformed or uses a feature the proxy does not support",
    AUDIT_UNAVAILABLE: "the call needs an alert or permission request that could not be stored",
    POLICY_UNAVAILABLE: "the project's policy cannot be loaded",
    PROJECT_UNBOUND: "no policy revision is bound to the project",
    UNAUTHENTICATED: "a valid bearer token is required",
    ORIGIN_FORBIDDEN: "requests from web pages are not accepted",
    REQUEST_TOO_LARGE: "the request body is too large",
    SESSION_REQUIRED: "an Mcp-Session-Id header from initialize is required",
    SESSION_UNKNOWN: "the session does not exist or has ended; initialize a new one",
    PROTOCOL_UNSUPPORTED: "the MCP protocol revision is not supported",
    TOOL_NOT_ADVERTISED: "the server's tool list has no tool of this exact name",
    ARGUMENTS_TOO_LARGE: "the request's parameters are larger than the project allows",
    CREDENTIAL_UNAVAILABLE: "the proxy has no credential placeholder for this server",
    CREDENTIAL_IN_BODY: "the request contains a credential placeholder",
    PROXY_BUSY: "the proxy has too many sessions; retry later",
    FENCE_DENIED: "the MCP proxy sandbox's fence refused the call; its policy is narrower",
    RESULT_TOO_LARGE: "the server's answer is larger than the project allows",
    UPSTREAM_ERROR: "the server's answer was malformed or an error",
    UPSTREAM_UNAUTHORIZED: "the server refused the proxy's credentials",
    UPSTREAM_UNAVAILABLE: "the server could not be reached",
}
