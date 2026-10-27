# SPDX-License-Identifier: MIT
"""The middleware service's own settings, read from a TOML file.

Relative paths are resolved against the settings file's directory.

```toml
registration = "sandbox-science-policy"  # the gateway's [[openshell.supervisor.middleware]] name
listen = "0.0.0.0:50051"
state_dir = "/var/lib/sandbox-science/policy-middleware"
ceiling = "/etc/sandbox-science/pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal", "10.0.0.2"]
package_cache_url = "http://127.0.0.1:8090"   # optional: names opam packages in escalations
mcp_listings = true                           # for direct MCP: filter tool listings (D98)
# listing_report_grace = 30                    # seconds the runtime has to report an unread listing
mcp_tool_pins = "mcp-tools.json"              # optional: {"<server>": ["<tool>", ...]}
response_statuses = true                      # optional: count 4xx/5xx for detection (D103)

[inspect]                                     # MCP traffic inspection on the direct path
argument_bytes = 16384                        # each call's recorded arguments
answer_bytes = 65536                          # each answer's recorded excerpt
run_bytes = 8388608                           # one run's recorded excerpts together
defaults = true                               # the shipped patterns
patterns = [{ name = "internal-host", regex = "corp\\.example\\.invalid", action = "alert" }]

[tls]
certificate = "tls/middleware.pem"
private_key = "tls/middleware-key.pem"

[tokens]
gateway_id = "openshell"
public_key = "/path/to/gateway/jwt/public.pem"
```
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from sandbox_science.policy.model import MAX_INSPECTION_PATTERNS, InspectionPattern

from .errors import SettingsError

MAX_PAYLOAD_BYTES: Final = 4 * 1024 * 1024
"""OpenShell's largest inspectable payload."""

_Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
_ServiceUrl = Annotated[
    str,
    StringConstraints(pattern=r"^https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"),
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TlsSettings(_Model):
    """The service's TLS certificate chain and key, PEM encoded."""

    certificate: Path
    private_key: Path


class TokenSettings(_Model):
    """How to verify the gateway's extension tokens."""

    gateway_id: Annotated[str, StringConstraints(min_length=1)]
    public_key: Path
    audience: Annotated[str, StringConstraints(min_length=1)] | None = None
    """Defaults to OpenShell's ``urn:openshell:extension:middleware:<registration>``."""
    leeway_seconds: Annotated[int, Field(ge=0, le=300)] = 30


class InspectSettings(_Model):
    """MCP traffic inspection's bounds and the deployment's patterns (docs/
    POLICY-MIDDLEWARE.md, "MCP traffic inspection"). A project policy's
    ``mcp.inspection`` replaces a pattern of the same name, or drops the defaults."""

    argument_bytes: Annotated[int, Field(ge=256, le=64 * 1024)] = 16 * 1024
    """The most of a call's parameters recorded, as redacted JSON text."""
    answer_bytes: Annotated[int, Field(ge=0, le=128 * 1024)] = 64 * 1024
    """The most of an answer's text recorded: 0 records sizes and findings only. Both
    bounds keep a record within the event store's 1 MiB payload, which canonical JSON's
    escapes can make six times the text."""
    run_bytes: Annotated[int, Field(ge=0, le=1024 * 1024 * 1024)] = 8 * 1024 * 1024
    """The most answer text recorded for one run; past it, sizes and findings only."""
    defaults: bool = True
    """Whether the shipped patterns apply (``policy.mcp_inspection.DEFAULT_PATTERNS``)."""
    patterns: Annotated[
        tuple[InspectionPattern, ...], Field(max_length=MAX_INSPECTION_PATTERNS)
    ] = ()


class Settings(_Model):
    """Settings for one middleware service."""

    registration: _Name = "sandbox-science-policy"
    listen: Annotated[str, StringConstraints(min_length=1)] = "127.0.0.1:50051"
    state_dir: Path
    event_store: Path | None = None
    """The event store's database file; by default ``events.db`` in the state directory."""
    ceiling: Path
    admin_hosts: tuple[str, ...] = ()
    max_payload_bytes: Annotated[int, Field(ge=1, le=MAX_PAYLOAD_BYTES)] = MAX_PAYLOAD_BYTES
    workers: Annotated[int, Field(ge=1, le=256)] = 16
    tls: TlsSettings | None = None
    tokens: TokenSettings | None = None
    allow_unauthenticated: bool = False
    """Accept calls without tokens, for a registration with ``allow_insecure_transport``."""
    package_cache_url: _ServiceUrl | None = None
    """The package cache as this host reaches it, without a path. With it, the record of an
    escalated opam archive names the packages the cache's opam index lists for its digest."""
    mcp_listings: bool = False
    """Read the answers to the ``tools/list`` requests of sandboxes that call MCP servers
    directly (``HttpResponsePreReturn``), whole: the tools the policy denies are dropped
    and the rest marked before the agent sees them, their hints decide later calls of tools
    the policy does not classify, and a listing that differs from its server's pinned list,
    the classification's or ``mcp_tool_pins``, is a ``tool_list_drift`` finding (D96).
    Without it, such sandboxes are refused (``mcp_listings_off``)."""
    mcp_tool_pins: Path | None = None
    """A JSON file of each MCP server's pinned tool names, over its classification's pins;
    it implies ``mcp_listings``."""
    listing_report_grace: Annotated[float, Field(ge=1, le=3600)] = 30.0
    """Seconds the agent runtime has to report a listing this service let pass unread (an
    event stream OpenShell would not offer whole, D98) before the server's non-read calls
    ask, as for an unknown listing."""
    inspect: InspectSettings = InspectSettings()
    """MCP traffic inspection of the sandboxes that call MCP servers directly (D98): each
    call's arguments and answer recorded, bounded and redacted, and answers checked for
    patterns. It needs ``mcp_listings``, whose response binding it uses."""
    response_statuses: bool = False
    """Read every response's status (``HttpResponsePreReturn`` preflight, which costs one
    exchange per response), so that 4xx and 5xx answers count towards the ``host-failures``
    detection rule (D103). Without it, only the middleware's own refusals count."""

    @property
    def event_store_path(self) -> Path:
        """Where the middleware records escalations, alerts and monitor findings."""
        return self.event_store if self.event_store is not None else self.state_dir / "events.db"

    @model_validator(mode="after")
    def _authentication(self) -> Self:
        if self.tokens is None and not self.allow_unauthenticated:
            raise ValueError(
                "configure [tokens] to verify the gateway's calls, or set "
                "allow_unauthenticated = true for local development"
            )
        if self.tokens is not None and self.allow_unauthenticated:
            raise ValueError("[tokens] and allow_unauthenticated = true are mutually exclusive")
        if self.tokens is not None and self.tls is None:
            raise ValueError("OpenShell sends tokens only over TLS: configure [tls] with [tokens]")
        return self


def load_settings(path: Path) -> Settings:
    """Read and validate a settings file."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read settings {path}: {exc}") from exc
    base = path.parent
    for key in ("state_dir", "event_store", "ceiling", "mcp_tool_pins"):
        _resolve(data, key, base)
    for section, keys in (("tls", ("certificate", "private_key")), ("tokens", ("public_key",))):
        table = data.get(section)
        if isinstance(table, dict):
            for key in keys:
                _resolve(table, key, base)  # pyright: ignore[reportUnknownArgumentType]
    try:
        return Settings.model_validate(data, strict=False)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc


def _resolve(table: dict[str, object], key: str, base: Path) -> None:
    value = table.get(key)
    if isinstance(value, str):
        table[key] = base / value
