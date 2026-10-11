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


class Settings(_Model):
    """Settings for one middleware service."""

    registration: _Name = "sandbox-science-policy"
    listen: Annotated[str, StringConstraints(min_length=1)] = "127.0.0.1:50051"
    state_dir: Path
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
    for key in ("state_dir", "ceiling"):
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
