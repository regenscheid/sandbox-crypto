# SPDX-License-Identifier: MIT
"""The worker service's settings, read from a TOML file (docs/WORKER.md).

```toml
listen = "127.0.0.1:8100"
token_file = "worker.token"      # the pool's own bearer token, on one line
state_dir = "state"              # executions, events and published artifacts
pool_id = "babbage"
max_artifact_size = 1073741824   # bytes

[openshell]                      # exactly one backend table
cli = "/home/andy/.local/opt/openshell-0.1.3/bin/openshell"
python = "/sandbox/mamba/envs/tools/bin/python"
project_root = "/sandbox/project"
executor_dir = "/sandbox/.sandbox-science"
isolation_tier = 3               # required: the tier the driver gives (3 for the VM driver)
package_cache = "http://host.openshell.internal:8090"   # the cache as sandboxes reach it
env = {XDG_CONFIG_HOME = "/home/andy/.local/state/openshell-lab/config"}
sandboxes = {"env-1" = "research-1"}   # logical environment -> sandbox name

[openshell.environments]         # optional: the worker creates sandboxes itself (D73)
policy_store = "/var/lib/sandbox-science/middleware"   # the policy middleware's state_dir
targets = "targets.yaml"         # where the platform services run (OpenShell targets)
ceiling = "pool-ceiling.yaml"    # the same pool ceiling the middleware applies
profiles = {minimal = "docker.io/library/buildpack-deps:noble-scm"}   # profile -> image

[local]                          # development and tests only: no isolation
python = "/usr/bin/python3"      # default: the worker's own interpreter

[[clients]]                      # optional: tokens that may act for some projects only
name = "orchestrator-p1"
token_sha256 = "<64 hex digits>" # sandbox-science-worker token --out FILE prints it
projects = ["p1"]
```

Relative paths resolve against the settings file's directory.
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Final, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from sandbox_science.contracts import Identifier

from .package_clients import check_cache_url

MIN_TOKEN: Final = 32
"""The fewest characters a bearer token may have."""


class SettingsError(Exception):
    """The settings file is missing or invalid."""


def token_digest(token: str) -> str:
    """The SHA-256 by which the settings name a client's token without holding it."""
    return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalBackendSettings(_Model):
    """Runs the executor as a plain subprocess of the worker: no isolation (tier 0)."""

    python: str = sys.executable


class EnvironmentSettings(_Model):
    """How the worker creates research sandboxes: policies, targets and images (D73)."""

    policy_store: Path
    """The policy middleware's state directory: stored policies and sandbox bindings."""
    targets: Path
    """The OpenShell targets file the compiler uses (docs/POLICY.md)."""
    ceiling: Path
    """The pool ceiling, the same file the policy middleware applies."""
    profiles: dict[Identifier, str] = Field(min_length=1)
    """Base profile (D46) to image reference. The orchestrator picks a profile, not an image."""
    create_timeout: Annotated[float, Field(gt=0, le=3600)] = 600.0
    """How long a sandbox may take to become ready."""
    admission: bool = True
    """Admit hosts on demand (D69): when OpenShell drafts a proposal for a host a sandbox
    could not resolve, and the policy could allow a request to it, name it in a revision."""
    admission_interval: Annotated[float, Field(ge=0.5, le=300)] = 3.0
    """Seconds between checks of each ready environment's drafts."""
    max_admitted_hosts: Annotated[int, Field(ge=0, le=100)] = 64
    """Hosts admitted on demand per environment. Each adds an endpoint with the public
    address ranges (about 4 KiB of YAML), and OpenShell limits a sandbox's policy to
    256 KiB; the worker also stops admitting before the compiled policy grows too large."""
    admin_hosts: tuple[str, ...] = ()
    """The deployment's administration endpoints, as the policy middleware's settings list
    them: the compiler and admission apply the same hard floor."""
    suspend_after: Annotated[float, Field(gt=0)] | None = None
    """Seconds an environment may go without executions, and without CPU use by its kernels,
    before the worker stops its sandbox; the next use starts it again (W3). Off when unset."""


class OpenShellBackendSettings(_Model):
    """Runs the executor in OpenShell sandboxes through the ``openshell`` CLI."""

    cli: str = "openshell"
    python: str = "python3"
    project_root: str = "/sandbox/project"
    executor_dir: str = "/sandbox/.sandbox-science"
    isolation_tier: Annotated[int, Field(ge=1, le=3)]
    """The tier the gateway's driver gives (D43), with no default: a deployment must say.
    The worker's probe can lower it, never raise it."""
    sched_shim: bool = True
    """Preload a shim that lets libzmq (Jupyter) start under OpenShell's seccomp filter."""
    cc: str = "cc"
    """The C compiler that builds the shim on the worker host."""
    package_cache: Annotated[str, AfterValidator(check_cache_url)] | None = None
    """The package cache's URL as sandboxes reach it; the package clients are pointed at it.
    With ``environments``, it must name the host and port the targets file gives the cache."""
    env: dict[str, str] = Field(default_factory=dict[str, str])
    """Extra environment for the CLI, such as XDG overrides naming the gateway."""
    sandboxes: dict[Identifier, str] = Field(default_factory=dict[str, str])
    """Logical environment ID to sandbox name, for sandboxes made outside the worker."""
    environments: EnvironmentSettings | None = None
    """Lets the worker create, revise and delete research sandboxes itself (D73)."""


class ClientSettings(_Model):
    """A client whose token may act for some projects only: their executions, files,
    artifacts and environments. The token itself never appears in the settings, and
    such clients need ``[openshell.environments]``: only the environments the worker
    creates are known to belong to a project."""

    name: Identifier
    token_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    """The SHA-256 of the client's bearer token, as ``sandbox-science-worker token`` prints it."""
    projects: tuple[Identifier, ...] = Field(min_length=1)


class Settings(_Model):
    listen: str = "127.0.0.1:8100"
    token_file: Path
    """The pool's own token, which may act for every project."""
    state_dir: Path
    pool_id: Identifier = "local"
    max_artifact_size: Annotated[int, Field(gt=0)] = 1 << 30
    kernel_idle_timeout: Annotated[float, Field(gt=0)] | None = None
    """Stop a kernel nobody has used for this many seconds; its session's next cell reports
    the lost state. Unset keeps kernels until they are closed, restarted or die."""
    local: LocalBackendSettings | None = None
    openshell: OpenShellBackendSettings | None = None
    clients: tuple[ClientSettings, ...] = ()
    """Clients limited to some projects, such as one orchestrator per project."""

    @model_validator(mode="after")
    def _one_backend(self) -> Self:
        if (self.local is None) == (self.openshell is None):
            raise ValueError("configure exactly one backend: [local] or [openshell]")
        names = [client.name for client in self.clients]
        digests = [client.token_sha256 for client in self.clients]
        if len(set(names)) < len(names) or len(set(digests)) < len(digests):
            raise ValueError("each client needs its own name and token")
        if self.clients and (self.openshell is None or self.openshell.environments is None):
            raise ValueError(
                "clients need [openshell.environments]: only environments the worker creates "
                "are known to belong to a project, so only they can keep projects apart"
            )
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
        if len(token) < MIN_TOKEN:
            raise SettingsError(
                f"the token in {self.token_file} is shorter than {MIN_TOKEN} characters"
            )
        if token_digest(token) in {client.token_sha256 for client in self.clients}:
            raise SettingsError("a client has the pool's own token; give it a token of its own")
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
    openshell: object = data.get("openshell")
    if isinstance(openshell, dict):
        environments: object = cast(dict[str, object], openshell).get("environments")
        if isinstance(environments, dict):
            table = cast(dict[str, object], environments)
            for key in ("policy_store", "targets", "ceiling"):
                value = table.get(key)
                if isinstance(value, str) and value:
                    table[key] = base / value
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc
