# SPDX-License-Identifier: MIT
"""Reason codes for the policy library's decisions, shared by every component that enforces
them: the policy middleware and the MCP proxy return them with a refusal, and the worker
recognizes them in a sandbox's output. The agent reads them, so each names what it can do.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .decision import Layer

APPROVAL_REQUIRED: Final = "approval_required"
"""An escalate rule matched; the request is pending approval and may be retried after it."""
APPROVAL_DENIED: Final = "approval_denied"
"""An operator denied the permission request this escalation matches."""
ESCALATION_TIMED_OUT: Final = "escalation_timed_out"
"""Nobody answered the permission request within its rule's escalation timeout."""
POLICY_DENIED: Final = "policy_denied"
"""A project rule or the policy's default denies the request."""
POOL_CEILING_DENIED: Final = "pool_ceiling_denied"
"""The worker pool's ceiling denies the destination."""
DESTINATION_FORBIDDEN: Final = "destination_forbidden"
"""The hard floor denies the destination."""
REQUEST_UNCLASSIFIABLE: Final = "request_unclassifiable"
"""The request is ambiguous or unsupported."""
AUDIT_UNAVAILABLE: Final = "audit_unavailable"
"""An alert or permission request the decision needs could not be recorded."""
POLICY_UNAVAILABLE: Final = "policy_unavailable"
"""The bound policy revision cannot be loaded."""

LAYER_CODES: Final[Mapping[Layer, str]] = {
    "classification": REQUEST_UNCLASSIFIABLE,
    "floor": DESTINATION_FORBIDDEN,
    "ceiling": POOL_CEILING_DENIED,
    "policy": POLICY_DENIED,
    "default": POLICY_DENIED,
}
"""The reason code of a denial, by the layer of the policy library that decided it."""
