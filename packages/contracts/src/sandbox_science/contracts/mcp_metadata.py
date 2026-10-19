# SPDX-License-Identifier: MIT
"""Safe, bounded MCP inspection; no endpoint, authentication values or configuration."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .model import Contract


class McpToolMetadata(Contract):
    name: str = Field(max_length=256)
    description: str = Field(max_length=2000)
    parameters_json: str = Field(max_length=16384)
    action: str = Field(max_length=32)


class McpPolicyMetadata(Contract):
    revision: str
    protocol: str
    authentication: str
    default_action: str
    listing_action: str
    worker_access: bool
    worker_tools: tuple[str, ...]
    allow: tuple[str, ...]
    escalate: tuple[str, ...]
    deny: tuple[str, ...]
    argument_bytes: int
    response_bytes: int


class McpServiceMetadata(Contract):
    server: str
    observed_at: datetime
    policy: McpPolicyMetadata
    checked_at: datetime | None = None
    status: Literal["unknown", "listed", "responded", "unavailable", "denied"] = "unknown"
    listing_at: datetime | None = None
    listing_policy: str | None = None
    tools: tuple[McpToolMetadata, ...] = Field(default=(), max_length=128)
    truncated: bool = False
    schema_projection: str = (
        "Structural parameter schemas from observed tools/list. Defaults, examples, enum values, "
        "external references and schema descriptions are omitted. Service text is untrusted."
    )
