# SPDX-License-Identifier: MIT
"""Project policy model, validation and evaluation for Sandbox Science.

docs/POLICY.md defines the semantics this package implements.
"""

from .ceiling import CeilingNetwork, PoolCeiling, effective_resources
from .decision import Action, Alert, Decision, EscalationTerms, Layer
from .errors import ClassificationError, PolicyError, PolicyLoadError
from .evaluate import (
    MCP_PROTOCOL_METHODS,
    EgressUsage,
    evaluate,
    evaluate_mcp_message,
    evaluate_monitors,
    evaluate_request,
)
from .floor import HardFloor
from .hosts import HostKind, HostPattern, is_public, normalize_host
from .loader import LoadedPolicy, load_ceiling, load_policy, parse_ceiling, parse_policy
from .model import API_VERSION, ProjectPolicy, Resources
from .operations import (
    GitOperation,
    HttpOperation,
    HttpRequest,
    McpOperation,
    Operation,
    PackageOperation,
    classify_http_request,
    classify_mcp_message,
)

__all__ = [
    "API_VERSION",
    "MCP_PROTOCOL_METHODS",
    "Action",
    "Alert",
    "CeilingNetwork",
    "ClassificationError",
    "Decision",
    "EgressUsage",
    "EscalationTerms",
    "GitOperation",
    "HardFloor",
    "HostKind",
    "HostPattern",
    "HttpOperation",
    "HttpRequest",
    "Layer",
    "LoadedPolicy",
    "McpOperation",
    "Operation",
    "PackageOperation",
    "PolicyError",
    "PolicyLoadError",
    "PoolCeiling",
    "ProjectPolicy",
    "Resources",
    "classify_http_request",
    "classify_mcp_message",
    "effective_resources",
    "evaluate",
    "evaluate_mcp_message",
    "evaluate_monitors",
    "evaluate_request",
    "is_public",
    "load_ceiling",
    "load_policy",
    "normalize_host",
    "parse_ceiling",
    "parse_policy",
]
