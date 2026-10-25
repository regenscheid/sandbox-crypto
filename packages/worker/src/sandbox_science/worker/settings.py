# SPDX-License-Identifier: MIT
"""The worker service's settings, read from a TOML file (docs/WORKER.md).

```toml
listen = "127.0.0.1:8100"
token_file = "worker.token"      # the pool's own bearer token, on one line
state_dir = "state"              # executions, events and published artifacts
pool_id = "babbage"
max_artifact_size = 1073741824   # bytes

[openshell]                      # exactly one backend table
cli = "~/.local/opt/openshell-<version>/bin/openshell"
# python = "/opt/sandbox-science/bin/python"   # default: the base images' interpreter
project_root = "/sandbox/project"
executor_dir = "/sandbox/.sandbox-science"
isolation_tier = 3               # required: the tier the driver gives (3 for the VM driver)
node_kernel_releases = ["*-talos"]   # optional: the nodes' own kernels; a match caps the tier at 1
package_cache = "http://host.openshell.internal:8090"   # the cache as sandboxes reach it
env = {XDG_CONFIG_HOME = "~/.local/state/openshell-lab/config"}
sandboxes = {"env-1" = "research-1"}   # logical environment -> sandbox name

[openshell.environments]         # optional: the worker creates sandboxes itself (D73)
policy_store = "/var/lib/sandbox-science/middleware"   # the policy middleware's state_dir
targets = "targets.yaml"         # where the platform services run (OpenShell targets)
ceiling = "pool-ceiling.yaml"    # the same pool ceiling the middleware applies
profiles = {minimal = "docker.io/library/buildpack-deps:noble-scm"}   # profile -> image
event_store = "/var/lib/sandbox-science/middleware/events.db"   # optional: the default

[openshell.environments.job_profiles.large]   # optional: job sandbox sizes (D93)
description = "12 cores, 32 GiB memory, up to 7 days"
runtime_class_name = "kata"      # required: kata or kata-* (D92)
node_selector = {"kubernetes.io/arch" = "amd64"}
requests = {cpu_millicores = 12000, memory_mib = 32768, ephemeral_storage_mib = 65536}
limits = {cpu_millicores = 12000, memory_mib = 32768, ephemeral_storage_mib = 65536}

[local]                          # development and tests only: no isolation
python = "/usr/bin/python3"      # default: the worker's own interpreter

[[clients]]                      # optional: tokens that may act for some projects only
name = "orchestrator-p1"
token_sha256 = "<64 hex digits>" # sandbox-science-worker token --out FILE prints it
projects = ["p1"]
max_environments = 8             # live environments across those projects
```

Relative paths resolve against the settings file's directory.
"""

from __future__ import annotations

import hashlib
import re
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
from sandbox_science.contracts.worker import (
    DEFAULT_COLLECT_MAX_BYTES,
    DEFAULT_STAGE_MAX_BYTES,
    JobProfileName,
)

from .documents_catalog import CatalogLimits
from .kubernetes import KubernetesProfile
from .package_clients import check_cache_url
from .tls import ServerTLS

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
    daemon: bool = False
    """Opt into the reconnectable executor for development tests; still no isolation."""


JOB_WALL_TIME_MAX: Final = 7 * 24 * 3600.0
"""The longest any job may run (D83), and so a job profile's ceiling."""
JOB_SANDBOX_LABEL: Final = ("sandbox-science.io/job-sandbox", "true")
"""The label on every job sandbox's workload Pod (OpenShell 0.1.5 ``allowed_pod_labels``),
which the deployment's PodDisruptionBudget selects so a drain waits for running jobs."""
_KATA: Final = re.compile(r"^kata(?:-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)?$")


class JobProfile(KubernetesProfile):
    """A size a job sandbox may have (D93, JS-8): trusted worker configuration, never an
    agent's. Its Kubernetes settings place the job's own workload Pod; the session
    environment's CPU and memory caps do not narrow them, since the profile list a
    project's policy grants is the grant (JS-9)."""

    description: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    """What the agent is told, such as "12 cores, 32 GiB memory, up to 7 days"."""
    wall_time_max: Annotated[float, Field(gt=0, le=JOB_WALL_TIME_MAX)] = JOB_WALL_TIME_MAX
    """Seconds a job of this size may run at most; the policy's maximum also applies."""
    schedule_timeout: Annotated[float, Field(gt=0, le=24 * 3600)] = 1800.0
    """Seconds the sandbox may wait for a node with room before the job fails (JS-23)."""
    max_concurrent: Annotated[int, Field(ge=1, le=64)] | None = None
    """Job sandboxes of this profile at once across the pool; the pool's limit applies too."""
    stage_max_bytes: Annotated[int, Field(ge=0, le=64 * 1024**3)] = DEFAULT_STAGE_MAX_BYTES
    """Bytes of inputs copied in (JS-13)."""
    collect_max_bytes: Annotated[int, Field(ge=0, le=256 * 1024**3)] = DEFAULT_COLLECT_MAX_BYTES
    """Bytes of files copied back (JS-15); the owner's 4 GiB by default."""

    @model_validator(mode="after")
    def _kata(self) -> Self:
        # D92: every sandbox of the first cluster runs under Kata (tier 3); a job profile
        # without it would place a big job on the node's own kernel.
        if self.runtime_class_name is None or _KATA.fullmatch(self.runtime_class_name) is None:
            raise ValueError("a job profile's runtime_class_name must be kata or kata-* (D92)")
        return self


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
    kubernetes: dict[Identifier, KubernetesProfile] = Field(default_factory=dict)
    """Trusted placement and resource settings by profile; only Kubernetes gateways accept these."""
    create_timeout: Annotated[float, Field(gt=0, le=3600)] = 600.0
    """How long a sandbox may take to become ready."""
    creation_grace: Annotated[float, Field(ge=300, le=86400)] = 420.0
    """Seconds after an environment's record before a creation with an unknown outcome may
    be found absent: OpenShell 0.1.5's fixed bootstrap grace of 300 s, after which its
    driver rolls a sandbox back, plus a margin. Never shorter than that grace."""
    creation_recheck: Annotated[float, Field(ge=1, le=3600)] = 30.0
    """Seconds between the two reads of the gateway that confirm such a sandbox absent, and
    between the running worker's passes over those environments."""
    admission: bool = True
    """Admit hosts on demand (D69): when OpenShell drafts a proposal for a host a sandbox
    could not resolve, and the policy could allow a request to it, name it in a revision."""
    admission_interval: Annotated[float, Field(ge=0.5, le=300)] = 3.0
    """Seconds between checks of each ready environment's drafts."""
    max_admitted_hosts: Annotated[int, Field(ge=0, le=1024)] = 128
    """Entries admitted on demand per environment: hosts, and other ports of hosts. Each
    new host adds an endpoint with the public address ranges (about 4 KiB of YAML), and
    OpenShell limits a sandbox's policy to 256 KiB, so the worker also stops admitting
    before the compiled policy grows too large: at about 80 hosts beside a policy that
    names none, and about 45 beside the example's pre-admitted research hosts. The
    default is above both, so that size decides; D95 leaves the names a sandbox looks up
    out of scope, so the count no longer bounds a threat."""
    admin_hosts: tuple[str, ...] = ()
    """The deployment's administration endpoints, as the policy middleware's settings list
    them: the compiler and admission apply the same hard floor."""
    suspend_after: Annotated[float, Field(gt=0)] | None = None
    """Seconds an environment may go without executions, and without CPU use by its kernels,
    before the worker stops its sandbox; the next use starts it again (W3). Off when unset,
    the default (D100): suspension discards kernel state a long investigation may need."""
    event_store: Path | None = None
    """The policy middleware's event store, where provisioning operations ask for permission
    (D76): ``events.db`` in ``policy_store`` unless the middleware's settings name another."""
    job_profiles: dict[JobProfileName, JobProfile] = Field(default_factory=dict, max_length=32)
    """The sizes a job may ask for its own sandbox in (D93); none, the default, offers none."""
    max_job_sandboxes: Annotated[int, Field(ge=0, le=64)] = 2
    """Job sandboxes creating, preparing, running or collecting at once across the pool."""
    job_retention: Annotated[float, Field(ge=0, le=30 * 24 * 3600)] = 3 * 24 * 3600.0
    """Seconds a failed job's stopped sandbox is kept, its disk only (JS-25)."""
    job_retention_per_project: Annotated[int, Field(ge=0, le=16)] = 2
    """Retained job sandboxes per project; one more releases the oldest early."""

    @property
    def event_store_path(self) -> Path:
        return self.event_store if self.event_store is not None else self.policy_store / "events.db"

    @model_validator(mode="after")
    def _placement_profiles(self) -> Self:
        if unknown := self.kubernetes.keys() - self.profiles.keys():
            raise ValueError(
                f"Kubernetes settings name unknown profiles: {', '.join(sorted(unknown))}"
            )
        if self.kubernetes and (missing := self.profiles.keys() - self.kubernetes.keys()):
            raise ValueError(
                f"Kubernetes settings must cover all offered profiles: {', '.join(sorted(missing))}"
            )
        return self


NodeKernelPattern = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._+~*?\[\]!-]{1,64}$")]
"""A kernel release pattern: release characters and fnmatch's ``*?[]!``."""


class OpenShellBackendSettings(_Model):
    """Runs the executor in OpenShell sandboxes through the ``openshell`` CLI."""

    daemon: bool = True
    """Keep the sandbox executor alive across worker bridge disconnections."""
    cli: str = "openshell"
    python: str = "/opt/sandbox-science/bin/python"
    """The interpreter that runs the executor in a sandbox: the base images' Conda prefix
    (docs/IMAGES.md), which has ipykernel and jupyter_client. A system ``python3`` has no
    kernels, so an image without the prefix sets this."""
    project_root: str = "/sandbox/project"
    executor_dir: str = "/sandbox/.sandbox-science"
    isolation_tier: Annotated[int, Field(ge=1, le=3)]
    """The tier the gateway's driver gives (D43), with no default: a deployment must say.
    The worker's probe can lower it, never raise it."""
    node_kernel_releases: tuple[NodeKernelPattern, ...] = Field(default=(), max_length=16)
    """Patterns (fnmatch, case-sensitive) for the kernel releases of the nodes sandboxes run
    on, such as ``*-talos``. A sandbox that reports a matching release, or none while patterns
    are set, shares a node's kernel: tier 1 at most, as for the worker host's boot ID."""
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
    max_environments: Annotated[int, Field(ge=1, le=1000)] = 8
    """The most environments the client's projects may have at once, deleted ones aside.
    Each is a sandbox, a VM with the VM driver, so a client cannot fill the pool's host."""
    document_catalog: bool = Field(default=False, strict=True)
    """Explicit control-service permission to ingest/status document catalogs. Ordinary
    execution clients remain denied, even for their own projects."""
    recovery_control: bool = Field(default=False, strict=True)
    """Trusted host control service may install recovery epochs for its projects."""
    operator: bool = Field(default=False, strict=True)
    """Authenticated maintenance may mutate fenced sessions without agent authority."""
    artifact_retention: bool = Field(default=False, strict=True)
    """Control service may acknowledge durable artifact retention for its projects."""


class Settings(_Model):
    listen: str = "127.0.0.1:8100"
    tls: ServerTLS | None = None
    token_file: Path
    """The pool's own token, which may act for every project."""
    state_dir: Path
    pool_id: Identifier = "local"
    max_artifact_size: Annotated[int, Field(gt=0)] = 1 << 30
    shared_sync: bool = True
    """Keep each project's ``shared/`` area in step across its environments (D79)."""
    document_catalog: CatalogLimits = Field(default_factory=CatalogLimits)
    """Durable catalog budgets; environment publication remains disabled."""
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
    tls: object = data.get("tls")
    if isinstance(tls, dict):
        identity = cast(dict[str, object], tls)
        for key in ("certificate", "private_key"):
            value = identity.get(key)
            if isinstance(value, str):
                identity[key] = base / value
    openshell: object = data.get("openshell")
    if isinstance(openshell, dict):
        environments: object = cast(dict[str, object], openshell).get("environments")
        if isinstance(environments, dict):
            table = cast(dict[str, object], environments)
            for key in ("policy_store", "targets", "ceiling", "event_store"):
                value = table.get(key)
                if isinstance(value, str) and value:
                    table[key] = base / value
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc
