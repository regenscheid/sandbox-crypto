# SPDX-License-Identifier: MIT
"""The MCP proxy's settings, read from a TOML file (docs/MCP-CONNECTORS.md, "Running it").

Relative paths are resolved against the settings file's directory.

```toml
listen = "127.0.0.1:8080"
state_dir = "state"              # the policy store: policies/ and projects/
ceiling = "pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal"]   # added to the hard floor
max_body_bytes = "1Mi"           # the largest request a caller may send
max_sessions = 256
max_sessions_per_caller = 16     # a caller's next session ends its least recently used one
session_idle = "1h"              # a session unused this long ends
upstream_timeout = "5m"          # one call to a server, from start to end

[credentials]   # environment variables that hold OpenShell credential placeholders
crypto-research-mcp = "CRYPTO_RESEARCH_MCP_TOKEN"     # bearer and oauth: the provider's token
"example-header-server.API_KEY" = "EXAMPLE_API_KEY"   # headers: a ${provider:...} reference

[[callers]]
name = "orchestrator"
token_sha256 = "<64 hex digits>"   # SHA-256 of the caller's bearer token
kind = "orchestrator"
project = "crypto"

[[callers]]
name = "crypto-default"
token_sha256 = "<64 hex digits>"
kind = "worker"
project = "crypto"
container = "ss-crypto-default-1a2b3c"   # the sandbox the token was issued to
# session = "s-42"                        # only for a container dedicated to one session
```

Settings hold token hashes, not tokens, so the file is not itself a secret.
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from sandbox_science.contracts import Identifier
from sandbox_science.policy.units import parse_duration, parse_size

from .errors import SettingsError

MAX_BODY_BYTES: Final = 16 * 1024 * 1024
"""The largest ``max_body_bytes`` the settings accept."""

_HOSTNAME_RE: Final = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


def split_listen(value: str) -> tuple[str, int]:
    """Split ``host:port``; IPv6 addresses are written in brackets."""
    host, separator, port = value.rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"listen must be host:port with a port from 1 to 65535, not {value!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError(f"invalid IPv6 address in listen {value!r}") from exc
    elif not _HOSTNAME_RE.fullmatch(host):
        raise ValueError(f"invalid host in listen {value!r} (write IPv6 addresses in brackets)")
    return host, int(port)


def _check_listen(value: str) -> str:
    split_listen(value)
    return value


Size = Annotated[int, BeforeValidator(parse_size), Field(gt=0, le=MAX_BODY_BYTES)]
Duration = Annotated[timedelta, BeforeValidator(parse_duration)]
_ProviderName = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
CredentialKey = Annotated[
    str, StringConstraints(pattern=rf"^{_ProviderName}(?:\.[A-Z][A-Z0-9_]*)?$")
]
"""A provider (``bearer`` and ``oauth`` servers) or a ``provider.KEY`` header reference."""
EnvironmentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CallerSettings(_Model):
    """One caller: its token's hash, its kind and the project it acts for.

    A worker token is issued to one sandbox (``container``). A token for a
    sandbox dedicated to one session names that ``session``; calls with a
    token for a shared project container are attributed to the container,
    because any process there can use the token (D67).
    """

    name: Identifier
    token_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    kind: Literal["orchestrator", "worker"]
    project: Identifier
    container: Identifier | None = None
    session: Identifier | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind == "orchestrator" and (self.container or self.session):
            raise ValueError(f"caller {self.name!r}: an orchestrator has no container or session")
        if self.kind == "worker" and self.container is None:
            raise ValueError(f"caller {self.name!r}: a worker token names its container")
        return self


class Settings(_Model):
    """Settings for one MCP proxy."""

    listen: Annotated[str, AfterValidator(_check_listen)] = "127.0.0.1:8080"
    state_dir: Path
    event_store: Path | None = None
    """The event store's database file; by default ``events.db`` in the state directory."""
    ceiling: Path
    admin_hosts: tuple[str, ...] = ()
    max_body_bytes: Size = 1024 * 1024
    max_sessions: Annotated[int, Field(ge=1, le=100_000)] = 256
    max_sessions_per_caller: Annotated[int, Field(ge=1, le=100_000)] = 16
    session_idle: Duration = timedelta(hours=1)
    upstream_timeout: Duration = timedelta(minutes=5)
    credentials: dict[CredentialKey, EnvironmentName] = Field(default_factory=dict[str, str])
    callers: Annotated[tuple[CallerSettings, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_callers(self) -> Self:
        names = [caller.name for caller in self.callers]
        if len(names) != len(set(names)):
            raise ValueError("caller names must be unique")
        hashes = [caller.token_sha256 for caller in self.callers]
        if len(hashes) != len(set(hashes)):
            raise ValueError("each caller needs its own token")
        if self.max_sessions_per_caller >= self.max_sessions:
            # Otherwise one caller could take every session.
            raise ValueError("max_sessions_per_caller must be smaller than max_sessions")
        return self

    @property
    def event_store_path(self) -> Path:
        """Where the proxy records escalations, alerts and calls."""
        return self.event_store if self.event_store is not None else self.state_dir / "events.db"

    @property
    def listen_host(self) -> str:
        """The address to listen on, without IPv6 brackets."""
        return split_listen(self.listen)[0]

    @property
    def listen_port(self) -> int:
        """The port to listen on."""
        return split_listen(self.listen)[1]


def load_settings(path: Path) -> Settings:
    """Read and validate a settings file."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read settings {path}: {exc}") from exc
    base = path.absolute().parent
    for key in ("state_dir", "event_store", "ceiling"):
        value = data.get(key)
        if isinstance(value, str) and value:
            data[key] = base / value
    try:
        return Settings.model_validate(data, strict=False)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc
