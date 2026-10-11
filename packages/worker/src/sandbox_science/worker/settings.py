# SPDX-License-Identifier: MIT
"""The worker service's settings, read from a TOML file (docs/WORKER.md).

```toml
listen = "127.0.0.1:8100"
token_file = "worker.token"      # the bearer token clients present, on one line
state_dir = "state"              # executions, events and published artifacts
pool_id = "babbage"
max_artifact_size = 1073741824   # bytes

[openshell]                      # exactly one backend table
cli = "~/.local/opt/openshell-0.1.3/bin/openshell"
python = "/sandbox/mamba/envs/tools/bin/python"
project_root = "/sandbox/project"
executor_dir = "/sandbox/.sandbox-science"
isolation_tier = 3
env = {XDG_CONFIG_HOME = "~/.local/state/openshell-lab/config"}
sandboxes = {"env-1" = "research-1"}   # logical environment -> sandbox name

[local]                          # development and tests only: no isolation
python = "/usr/bin/python3"      # default: the worker's own interpreter
```

Relative paths resolve against the settings file's directory.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sandbox_science.contracts import Identifier


class SettingsError(Exception):
    """The settings file is missing or invalid."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalBackendSettings(_Model):
    """Runs the executor as a plain subprocess of the worker: no isolation (tier 0)."""

    python: str = sys.executable


class OpenShellBackendSettings(_Model):
    """Runs the executor in OpenShell sandboxes through the ``openshell`` CLI."""

    cli: str = "openshell"
    python: str = "python3"
    project_root: str = "/sandbox/project"
    executor_dir: str = "/sandbox/.sandbox-science"
    isolation_tier: Annotated[int, Field(ge=1, le=3)] = 3
    env: dict[str, str] = Field(default_factory=dict[str, str])
    """Extra environment for the CLI, such as XDG overrides naming the gateway."""
    sandboxes: dict[Identifier, str] = Field(default_factory=dict[str, str])
    """Logical environment ID to sandbox name."""


class Settings(_Model):
    listen: str = "127.0.0.1:8100"
    token_file: Path
    state_dir: Path
    pool_id: Identifier = "local"
    max_artifact_size: Annotated[int, Field(gt=0)] = 1 << 30
    local: LocalBackendSettings | None = None
    openshell: OpenShellBackendSettings | None = None

    @model_validator(mode="after")
    def _one_backend(self) -> Self:
        if (self.local is None) == (self.openshell is None):
            raise ValueError("configure exactly one backend: [local] or [openshell]")
        host, _, port = self.listen.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"listen must be host:port, not {self.listen!r}")
        return self

    @property
    def listen_host(self) -> str:
        return self.listen.rpartition(":")[0].strip("[]")

    @property
    def listen_port(self) -> int:
        return int(self.listen.rpartition(":")[2])

    def token(self) -> str:
        try:
            token = self.token_file.read_text().strip()
        except OSError as exc:
            raise SettingsError(f"cannot read the token file {self.token_file}: {exc}") from exc
        if len(token) < 32:
            raise SettingsError(f"the token in {self.token_file} is shorter than 32 characters")
        return token


def load_settings(path: Path) -> Settings:
    """Read and validate a settings file."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read settings {path}: {exc}") from exc
    base = path.absolute().parent
    for key in ("token_file", "state_dir"):
        value = data.get(key)
        if isinstance(value, str) and value:
            data[key] = base / value
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc
