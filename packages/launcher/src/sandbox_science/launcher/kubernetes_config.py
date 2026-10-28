# SPDX-License-Identifier: MIT
"""Trusted, site-private inputs for the offline Kubernetes bundle renderer."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .kubernetes_models import ModelCatalog

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
Count = Annotated[int, Field(strict=True, ge=1, le=1048576)]
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$"
)
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9.:/-]*@sha256:([0-9a-f]{64})$")
_LABEL = re.compile(
    r"^(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9][-A-Za-z0-9_.]{0,62}$"
)
_VALUE = re.compile(r"^(?:[A-Za-z0-9][-A-Za-z0-9_.]{0,61}[A-Za-z0-9]|[A-Za-z0-9])?$")
REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "control": (
        "control.json",
        "runtime-manager-token.sha256",
        "worker-token",
        "control.crt",
        "control.key",
        "worker-ca.crt",
    ),
    "workspace": ("worker-token", "worker-ca.crt"),
    "worker": ("worker.toml", "worker.crt", "worker.key"),
    "middleware": ("middleware.toml", "middleware.crt", "middleware.key"),
    "cache": ("cache.toml",),
    "runtime-manager": (
        "runtime-manager.json",
        "control-models.json",
        "manager-control.token",
        "model.key",
        "control-ca.crt",
        "gateway-ca.crt",
        "gateway-tls.crt",
        "gateway-tls.key",
        "gateway-metadata.json",
        "active-gateway",
    ),
}
GATEWAY_PROFILE: dict[str, str] = {
    "active-gateway": "active_gateway",
    "gateway-metadata.json": "metadata.json",
    "gateway-ca.crt": "mtls/ca.crt",
    "gateway-tls.crt": "mtls/tls.crt",
    "gateway-tls.key": "mtls/tls.key",
}
"""The manager Secret's OpenShell CLI profile keys and their places in the CLI's tree."""
DIRECT_MANAGER_FILES: tuple[str, ...] = (
    "manager-worker.token",
    "agent-mcp-project-policy.yaml",
    "agent-mcp-ceiling.yaml",
    "agent-mcp-targets.yaml",
)
"""The runtime manager's further keys with ``[mcp_direct]``: its binding-only token for the
worker (never its control token), which binds the runtime's sandbox and reads the
project's classification revisions in force (``WorkerLink``), and the site's project
policy, pool ceiling and targets, from which it compiles the runtime sandbox's MCP egress
at each creation (``ManagerMcp``)."""
CHECK_TOKEN = "mcp-check-token"  # noqa: S105 - a Secret key's name, not a token
CHECK_DIGEST = "mcp-check-token.sha256"
DIRECT_CHECK_FILES: dict[str, tuple[str, ...]] = {
    "control": (CHECK_DIGEST,),
    "workspace": (CHECK_TOKEN,),
}
"""With ``[mcp_direct]``, the workspace's token for the user's availability check
(``POST /mcp-check``, D98) and its SHA-256, which control keeps; no other Secret holds the
token."""
MANAGER_ONLY = frozenset(
    (*GATEWAY_PROFILE, "manager-control.token", "model.key", *DIRECT_MANAGER_FILES)
)
"""Keys no other service's projection may name: the gateway credential stays in the
manager's Pod (KRB-1)."""
_AWS_FILES = frozenset(("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_CA_BUNDLE"))
_MANAGED_DIRECTORIES = {"HOME": "home", "XDG_CONFIG_HOME": "config"}
_ENVIRONMENT = (
    _AWS_FILES
    | _MANAGED_DIRECTORIES.keys()
    | {
        "AWS_REGION",
        "AWS_PROFILE",
        "AWS_EC2_METADATA_DISABLED",
    }
)


class BundleError(Exception):
    """Invalid or unreadable trusted deployment inputs."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _pinned(value: str) -> str:
    match = _IMAGE.fullmatch(value)
    host = value.partition("/")[0]
    if (
        match is None
        or match[1] == "0" * 64
        or "/" not in value
        or not ("." in host or ":" in host)
        or host.endswith((".example", ".invalid"))
        or any(word in value for word in ("placeholder", "replace", "example.", ".invalid/"))
    ):
        raise ValueError("use an operator-selected registry image with a nonzero SHA256 digest")
    return value


PinnedImage = Annotated[str, AfterValidator(_pinned)]
"""A registry image pinned by a real digest, never a tag or a template's placeholder."""


class Images(Model):
    app: PinnedImage
    worker: PinnedImage


class Capacity(Model):
    cpu_request_millis: Count = 250
    cpu_limit_millis: Count = 1000
    memory_request_mib: Count = 256
    memory_limit_mib: Count = 1024
    ephemeral_request_mib: Count = 128
    ephemeral_limit_mib: Count = 1024
    scratch_mib: Count = 128

    @model_validator(mode="after")
    def fits(self) -> Self:
        if (
            self.cpu_request_millis > self.cpu_limit_millis
            or self.memory_request_mib > self.memory_limit_mib
            or self.ephemeral_request_mib > self.ephemeral_limit_mib
            or self.scratch_mib > self.ephemeral_limit_mib
        ):
            raise ValueError("requests and scratch must fit their limits")
        return self


class Service(Capacity):
    environment: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("environment")
    @classmethod
    def allowed_environment(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if name not in _ENVIRONMENT or not 1 <= len(value) <= 256:
                raise ValueError("unsupported service environment")
            if name == "AWS_EC2_METADATA_DISABLED" and value != "true":
                raise ValueError("node metadata credentials must remain disabled")
            if (
                name in ("AWS_REGION", "AWS_PROFILE")
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", value) is None
            ):
                raise ValueError("use a bounded region or profile name")
        return values


class Placement(Model):
    architecture: Literal["amd64", "arm64"]
    storage_gib: Annotated[int, Field(strict=True, ge=1, le=65536)]
    storage_class: Name | None = None


class Secret(Model):
    name: Name
    extra_files: tuple[str, ...] = ()

    @field_validator("extra_files", mode="before")
    @classmethod
    def files(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("at most 24 additional credential files")
        items = cast("list[object] | tuple[object, ...]", value)
        if len(items) > 24:
            raise ValueError("at most 24 additional credential files")
        result: list[str] = []
        for item in items:
            if (
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item) is None
                or item in result
            ):
                raise ValueError("credential files must be unique, bounded leaf names")
            result.append(item)
        return tuple(result)


class Secrets(Model):
    control: Secret
    workspace: Secret
    worker: Secret
    middleware: Secret
    runtime_manager: Secret
    cache: Secret | None = None

    @model_validator(mode="after")
    def manager_alone(self) -> Self:
        others = (self.control, self.workspace, self.worker, self.middleware, self.cache)
        if self.runtime_manager.extra_files:
            raise ValueError("the runtime manager's Secret projects only its fixed keys")
        for secret in others:
            if secret is None:
                continue
            if secret.name == self.runtime_manager.name:
                raise ValueError("the runtime manager's Secret is its own")
            if MANAGER_ONLY.intersection(secret.extra_files):
                raise ValueError("only the runtime manager projects its gateway and model keys")
        for secret in (self.control, self.worker, self.middleware, self.cache):
            if secret is not None and CHECK_TOKEN in secret.extra_files:
                raise ValueError("only the workspace projects its availability-check token")
        return self


class Peer(Model):
    cidr: str | None = None
    namespace_labels: dict[str, str] = Field(default_factory=dict)
    pod_labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scoped(self) -> Self:
        if self.cidr is not None:
            if self.namespace_labels or self.pod_labels:
                raise ValueError("use CIDR or selectors, not both")
            try:
                network = ipaddress.ip_network(self.cidr, strict=True)
            except ValueError as exc:
                raise ValueError("use a canonical CIDR") from exc
            if network.prefixlen == 0 or any(
                (
                    network.is_loopback,
                    network.is_multicast,
                    network.is_link_local,
                    network.is_unspecified,
                )
            ):
                raise ValueError(
                    "unscoped, loopback, multicast and link-local CIDRs are unsupported"
                )
        elif not self.namespace_labels:
            raise ValueError("selector peers require explicit namespace labels")
        for labels in (self.namespace_labels, self.pod_labels):
            if len(labels) > 8 or any(
                _LABEL.fullmatch(key) is None or _VALUE.fullmatch(value) is None
                for key, value in labels.items()
            ):
                raise ValueError("invalid or excessive Kubernetes labels")
        return self


class Egress(Model):
    peer: Peer
    ports: Annotated[tuple[Port, ...], Field(min_length=1, max_length=16)]


SUPERVISOR = ("openshell.ai/component", "supervisor")
GATEWAY_ID = "openshell.ai/gateway-id"


class Network(Model):
    dns: Peer
    runtimes: Annotated[tuple[Peer, ...], Field(min_length=1, max_length=16)]
    """The runtime's traffic leaves through its sandbox's supervisor Pod (KRB-4)."""
    supervisors: Annotated[tuple[Peer, ...], Field(min_length=1, max_length=16)]
    gateway: Egress
    """The OpenShell gateway's Pods and ports. The runtime manager and the worker reach it,
    each with its own client identity (the worker creates every research sandbox), and it
    reaches the middleware: it connects to each registered middleware at startup."""
    sandboxes: Annotated[tuple[Peer, ...], Field(max_length=16)] = ()
    app_egress: Annotated[tuple[Egress, ...], Field(max_length=16)] = ()
    pool_egress: Annotated[tuple[Egress, ...], Field(max_length=16)] = ()

    @field_validator("runtimes")
    @classmethod
    def supervisor_peers(cls, peers: tuple[Peer, ...]) -> tuple[Peer, ...]:
        # A selector naming anything but this gateway's supervisors would admit Pods
        # that never carry the runtime's traffic, or every gateway's supervisors.
        for peer in peers:
            if peer.cidr is None and (
                peer.pod_labels.get(SUPERVISOR[0]) != SUPERVISOR[1]
                or not peer.pod_labels.get(GATEWAY_ID)
            ):
                raise ValueError("runtime peers select OpenShell supervisor Pods of one gateway")
        return peers


class Workspace(Service):
    """The workspace container: loopback only, unless the bundle's ingress is on."""

    allow_remote: bool = False
    """Listen on the Pod network (``--allow-remote``). Only an authenticated, TLS-terminating
    ingress may set it: the workspace itself speaks plain HTTP."""


def _hostname(value: str) -> str:
    # The name goes into a Traefik ``Host(`...`)`` rule and a certificate, so only a
    # lowercase DNS name with at least two labels passes: no wildcard, IP address or quote.
    if _HOSTNAME.fullmatch(value) is None or not any(
        char.isalpha() for char in value.rpartition(".")[2]
    ):
        raise ValueError("use a lowercase DNS host name with at least two labels")
    return value


Hostname = Annotated[str, AfterValidator(_hostname)]
_PLAIN_ENTRY_POINTS = frozenset(("web", "http"))
"""Entry point names that conventionally carry plain HTTP; the ingress requires TLS."""


class Ingress(Model):
    """The opt-in route from a browser to the workspace and its preview origin: Traefik
    terminates TLS with a cert-manager certificate and asks Authentik (forward auth) before
    it forwards a request. Absent, nothing leaves the application Pod's loopback."""

    workspace_host: Hostname
    preview_host: Hostname
    """The PDF viewer's and figures' origin, apart from the workspace's (D54)."""
    issuer: Name
    """The cert-manager issuer that signs both certificates."""
    issuer_kind: Literal["ClusterIssuer", "Issuer"] = "ClusterIssuer"
    authentik_middleware: Name
    """The Traefik forward-auth ``Middleware`` in the bundle's namespace; the operator
    creates it, with the Authentik provider and outpost it points at."""
    traefik: Annotated[tuple[Peer, ...], Field(min_length=1, max_length=4)]
    """The Traefik controller Pods, the only peers admitted to the workspace's ports."""
    entry_point: Name = "websecure"
    ingress_class: Name | None = None

    @field_validator("traefik")
    @classmethod
    def controller_pods(cls, peers: tuple[Peer, ...]) -> tuple[Peer, ...]:
        # A bare namespace would admit every Pod in it, a CIDR any workload at an address.
        if any(peer.cidr is not None or not peer.pod_labels for peer in peers):
            raise ValueError("select the ingress controller's Pods by namespace and Pod labels")
        return peers

    @field_validator("entry_point")
    @classmethod
    def tls_entry_point(cls, value: str) -> str:
        if value in _PLAIN_ENTRY_POINTS:
            raise ValueError("TLS is required: name the HTTPS entry point")
        return value

    @model_validator(mode="after")
    def two_origins(self) -> Self:
        if self.workspace_host == self.preview_host:
            raise ValueError("the preview needs a host name of its own: it is a separate origin")
        return self


class Sandboxes(Model):
    """Where OpenShell creates sandboxes, for the objects the bundle must put there. The
    renderer cannot derive the namespace (the peers select it by label only), so the
    operator names it: ``server.sandboxNamespace`` of the OpenShell chart."""

    namespace: Name
    job_budget: bool = True
    """Render the PodDisruptionBudget that makes a planned drain wait for job sandboxes
    (D93, JS-20). Harmless with no job profile configured: it selects no Pod. Turn it off
    in all but one instance that shares a sandbox namespace: two budgets selecting the same
    Pod make every eviction of it fail."""


class Alerts(Model):
    """The opt-in Loki ruler rules over the services' structured logs (D103,
    docs/KUBERNETES-BUNDLE.md, "Alert rules"): one ConfigMap of LogQL alerting rules in the
    Prometheus rule format Loki's ruler reads. The rules select this instance's lines by the
    labels the sample Alloy configuration gives them (``namespace``, ``instance``,
    ``service``, ``level``)."""

    namespace: Name | None = None
    """Where the ConfigMap goes: Loki's namespace, when its rules sidecar watches only
    that one. Absent, the bundle's own."""
    tenant: Name = "fake"
    """The Loki tenant the rules belong to: ``fake`` for a single-tenant Loki
    (``auth_enabled: false``). The ConfigMap's ``k8s-sidecar-target-directory`` names it."""
    configmap_labels: dict[str, str] = Field(
        default_factory=lambda: {"loki_rule": "true"}, max_length=8
    )
    """The labels Loki's rules sidecar selects ConfigMaps by (the chart's
    ``sidecar.rules.label``)."""
    error_threshold: Annotated[int, Field(strict=True, ge=1, le=100000)] = 10
    """More ``error`` records than this from one service within 5 minutes alerts."""
    canary_window_minutes: Annotated[int, Field(strict=True, ge=2, le=1440)] = 20
    """The window in which every MCP canary of a server must have failed for
    ``SandboxScienceMcpCanaryFailing``: longer than the canaries' interval (15 minutes by
    default), so that it holds at least one result."""

    @field_validator("configmap_labels")
    @classmethod
    def label_names(cls, labels: dict[str, str]) -> dict[str, str]:
        if not labels or any(
            _LABEL.fullmatch(key) is None or _VALUE.fullmatch(value) is None
            for key, value in labels.items()
        ):
            raise ValueError("invalid Kubernetes labels for the alert rules")
        return labels


class RuntimeManager(Placement, Capacity):
    """The runtime manager's Pod: its node architecture, PVC and container bounds."""

    openshell_gateway: Name
    """The OpenShell CLI profile's gateway name: ``openshell/gateways/<name>/``."""

    @model_validator(mode="after")
    def memory_scratch(self) -> Self:
        # The manager's /tmp is a memory volume, since the runtime token passes there.
        if self.scratch_mib > self.memory_limit_mib:
            raise ValueError("the manager's memory scratch must fit its memory limit")
        return self


_SCHEDULE = (
    r"^(?:@(?:yearly|annually|monthly|weekly|daily|midnight|hourly)"
    r"|[0-9*/,-]{1,32}(?: [0-9*/,-]{1,32}){4})$"
)


class Maintenance(Model):
    """Opt-in scheduled control-cache trimming (D92): a CronJob in the bundle's namespace
    runs one bounded ``sandbox-science-artifacts maintain --apply`` pass, which does nothing
    while the app's volume is less than ``at_use_percent`` full. The bounds mirror the
    orchestrator's ``MaintenancePolicy``, which a test loads the rendered policy through."""

    schedule: Annotated[str, StringConstraints(pattern=_SCHEDULE)] = "@daily"
    """A five-field cron expression or a macro such as ``@daily``, in the controller's time
    zone; names (``MON``), ``?`` and ``TZ=`` are refused."""
    at_use_percent: Annotated[int, Field(strict=True, ge=1, le=100)] = 70
    max_references: Annotated[int, Field(strict=True, ge=1, le=100)] = 25
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=64 * 1024**3)] = 1024**3
    min_age_seconds: Annotated[int, Field(strict=True, ge=0, le=3650 * 86400)] = 86400
    deadline_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] = 3600
    """The Job's ``activeDeadlineSeconds``: a pass still running then is stopped; the next
    one resumes from the cursor."""


CHECKPOINT_WORKER_FILES: tuple[str, ...] = (
    "checkpoint-upload.toml",
    "checkpoint-kept.toml",
    "checkpoint-s3-credentials",
    "checkpoint-s3-config",
)
"""With ``[checkpoint]``, the worker's further keys: the two buckets' destination settings
(no credential) and the SDK files of its read-only verify key."""
CHECKPOINT_UPLOAD_KEYS: tuple[str, ...] = ("credentials", "config")
"""The keys of the checkpoint Job's Secret in the sandbox namespace: the upload key's SDK
credential and config files, and nothing else."""
_BUCKET = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
_PROFILE = r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$"


def _object_store(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("checkpoint.endpoint is the object store's http(s)://host[:port]")
    return value


class CheckpointJob(Capacity):
    """The checkpoint Job's container bounds: its scratch holds one 16 MiB chunk and the
    encoder's path index, which grows with the number of files (about 100 bytes each)."""

    cpu_request_millis: Count = 250
    cpu_limit_millis: Count = 2000
    memory_request_mib: Count = 256
    memory_limit_mib: Count = 1024
    ephemeral_request_mib: Count = 512
    ephemeral_limit_mib: Count = 2048
    scratch_mib: Count = 1024


class Checkpoint(Model):
    """Workspace checkpoints by Job (D92, docs/KUBERNETES-BUNDLE.md#workspace-checkpoints):
    a short-lived Job the operator creates in the sandbox namespace mounts a suspended
    sandbox's volume read-only and writes its checkpoint to the upload bucket with a key
    that can write nothing else; the worker verifies it with a read-only key and records
    it; the operator's keeper key copies it into the kept bucket. The bundle renders the
    Job's NetworkPolicy and the pool's egress to the object store; ``checkpoint-job``
    renders each Job."""

    upload_secret: Name
    """The Secret in the sandbox namespace with the upload key's SDK files (``credentials``
    and ``config``), which only checkpoint Jobs mount. The operator creates it."""
    upload_profile: Annotated[str, StringConstraints(pattern=_PROFILE)] = "checkpoint-upload"
    verify_profile: Annotated[str, StringConstraints(pattern=_PROFILE)] = "checkpoint-verify"
    """The profile in the worker's ``checkpoint-s3-*`` files: read-only on both buckets."""
    upload_bucket: Annotated[str, StringConstraints(pattern=_BUCKET)]
    kept_bucket: Annotated[str, StringConstraints(pattern=_BUCKET)]
    prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = "sandbox-science"
    endpoint: Annotated[str, StringConstraints(max_length=2048), AfterValidator(_object_store)]
    region: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,64}$")] = "garage"
    addressing_style: Literal["auto", "path", "virtual"] = "path"
    object_store: Egress
    """The object store's Pods and port: the Job's only peer besides DNS, and the pool's,
    whose worker verifies there."""
    run_as_user: Annotated[int, Field(strict=True, ge=1, le=2147483647)] = 10001
    """The sandbox user, who owns the workspace's files: the Job reads them as it, so a
    file the sandbox made unreadable fails the checkpoint by name rather than being read
    with more authority."""
    run_as_group: Annotated[int, Field(strict=True, ge=1, le=2147483647)] = 10001
    deadline_seconds: Annotated[int, Field(strict=True, ge=60, le=86400)] = 7200
    keep: Annotated[int, Field(strict=True, ge=1, le=100)] = 2
    """Kept checkpoints per workspace; older ones are cleanup candidates, never removed
    automatically."""
    hold_seconds: Annotated[int, Field(strict=True, ge=600, le=7 * 86400)] = 21600
    """How long a plan holds the sandbox stopped: longer than the Job's deadline."""
    job: CheckpointJob = Field(default_factory=CheckpointJob)

    @model_validator(mode="after")
    def separate(self) -> Self:
        if self.upload_bucket == self.kept_bucket:
            raise ValueError("the upload bucket is separate from the kept bucket")
        if self.hold_seconds <= self.deadline_seconds:
            raise ValueError("checkpoint.hold_seconds must outlast the Job's deadline")
        return self


_HEADER = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$"
_VARIABLE = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
_SERVER = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_PROJECT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class McpDirectServer(Model):
    """A server the agent runtime's direct client reaches: its credential headers, each the
    variable in the runtime's sandbox that holds its provider's placeholder. Its URL is the
    project policy's."""

    credentials: dict[
        Annotated[str, StringConstraints(pattern=_HEADER)],
        Annotated[str, StringConstraints(pattern=_VARIABLE)],
    ] = Field(default_factory=dict, max_length=16)


class McpCanaries(Model):
    """Scheduled MCP canaries of the direct path (D103, docs/MCP-CONNECTORS.md, "Canaries"):
    every ``interval`` seconds control asks the agent runtime to list each server. They are
    also what lets control accept the workspace's "Check availability" listing, so they are
    on unless the operator turns them off. The host launcher's ``[canaries]`` has the same
    keys and the same 15-minute default."""

    enabled: bool = True
    interval: Annotated[float, Field(ge=60, le=86400)] = 900.0
    """Seconds between runs: 15 minutes by default."""


class McpDirect(Model):
    """The direct MCP path in the cluster (D98, docs/MCP-CONNECTORS.md): the agent's
    connector calls made by the runtime's own client in its sandbox, through OpenShell's
    native rules and the policy middleware. The site generator puts the site's policy,
    pool ceiling and targets into the manager's Secret, from which the manager compiles
    the runtime sandbox's egress at each creation with the project's classification
    revisions in force, and a binding-only token for the worker, which binds the sandbox
    and reads those revisions. The
    manager Pod registers ``provider`` with the gateway from the Secret
    ``provider_secret`` (made by the operator with ``kubectl create secret generic …
    --from-env-file=<path>``, docs/KUBERNETES-PREP.md) in an init container of its own,
    and attaches it to the runtime's sandbox."""

    project: Annotated[str, StringConstraints(pattern=_PROJECT)]
    servers: dict[Annotated[str, StringConstraints(pattern=_PROJECT)], McpDirectServer] = Field(
        min_length=1, max_length=64
    )
    about: dict[
        Annotated[str, StringConstraints(pattern=rf"^{_SERVER}(?:/{_SERVER})?$")],
        Annotated[str, StringConstraints(max_length=200)],
    ] = Field(default_factory=dict)
    index: (
        tuple[Annotated[str, StringConstraints(pattern=rf"^{_SERVER}(?:/{_SERVER})?$")], ...] | None
    ) = None
    provider: Name = "crypto-research-mcp"
    """The OpenShell provider with the servers' credentials."""
    canaries: McpCanaries = Field(default_factory=McpCanaries)
    provider_secret: Name
    """The Secret with the provider's ``CF_ACCESS_CLIENT_ID`` and ``CF_ACCESS_CLIENT_SECRET``,
    which only the manager Pod's registering init container reads, as its environment."""

    @model_validator(mode="after")
    def indexed(self) -> Self:
        for key in (*self.about, *(self.index or ())):
            if key.partition("/")[0] not in self.servers:
                raise ValueError("mcp_direct.about and index name one of mcp_direct.servers")
        return self


class BundleConfig(Model):
    name: Annotated[Name, StringConstraints(max_length=40)]
    namespace: Name
    images: Images
    app: Placement
    pool: Placement
    runtime_manager: RuntimeManager
    secrets: Secrets
    network: Network
    models: ModelCatalog
    control: Service = Service()
    workspace: Workspace = Workspace()
    worker: Service = Service()
    middleware: Service = Service()
    cache: Service = Service()
    ingress: Ingress | None = None
    sandboxes: Sandboxes | None = None
    """Absent, nothing is rendered outside the bundle's own namespace."""
    maintenance: Maintenance | None = None
    """Absent, no cache trimming is scheduled."""
    alerts: Alerts | None = None
    """Absent, no alert rules are rendered."""
    mcp_direct: McpDirect | None = None
    """Absent, the agent runtime makes no MCP request."""
    checkpoint: Checkpoint | None = None
    """Absent, no checkpoint Job can be rendered and the pool does not reach the store."""
    image_pull_secret: Name = "ghcr-pull"  # noqa: S105 - a Secret's name, not a value
    """The registry Secret (``kubernetes.io/dockerconfigjson``) in the bundle's namespace that
    every rendered Pod names in ``imagePullSecrets``: the app, the pool, the runtime manager,
    the maintenance CronJob's and the release Job's. The images are private
    (docs/KUBERNETES-PREP.md, step 1); a Secret that does not exist only draws a kubelet
    warning, and a public image still pulls."""

    @model_validator(mode="after")
    def remote_workspace(self) -> Self:
        # The workspace leaves loopback only behind the ingress that authenticates and
        # encrypts, and that ingress needs it to (a Pod IP is not loopback).
        if self.workspace.allow_remote and self.ingress is None:
            raise ValueError("workspace.allow_remote requires an [ingress] section")
        if self.ingress is not None and not self.workspace.allow_remote:
            raise ValueError("an [ingress] section requires workspace.allow_remote = true")
        return self

    @model_validator(mode="after")
    def checkpoint_namespace(self) -> Self:
        # The Job runs beside the volumes, in the namespace OpenShell creates sandboxes in.
        if self.checkpoint is not None and self.sandboxes is None:
            raise ValueError("[checkpoint] requires [sandboxes] to name the sandbox namespace")
        return self

    @model_validator(mode="after")
    def cache_peers(self) -> Self:
        if self.secrets.cache is not None and not self.network.sandboxes:
            raise ValueError("a package cache requires approved sandbox peers")
        for role, service, secret in (
            ("control", self.control, self.secrets.control),
            ("workspace", self.workspace, self.secrets.workspace),
            ("worker", self.worker, self.secrets.worker),
            ("middleware", self.middleware, self.secrets.middleware),
            ("cache", self.cache, self.secrets.cache),
        ):
            if secret is None:
                if service.environment:
                    raise ValueError("environment requires an enabled service")
                continue
            projected = {
                f"/private/{name}" for name in (*projected_files(self, role), *secret.extra_files)
            }
            for name, value in service.environment.items():
                if name in _AWS_FILES and value not in projected:
                    raise ValueError(
                        "SDK credential/config paths require that service's projection"
                    )
                if name in _MANAGED_DIRECTORIES and value != (
                    f"/var/lib/sandbox-science/{role}-{_MANAGED_DIRECTORIES[name]}"
                ):
                    raise ValueError("service directories must use the role's managed PVC path")
        return self


def projected_files(config: BundleConfig, role: str) -> tuple[str, ...]:
    """The keys ``role``'s Secret must hold: ``REQUIRED_FILES``'s, and with ``[mcp_direct]``
    the runtime manager's direct MCP ones and the availability check's token (the
    workspace's) and its digest (control's)."""
    files = REQUIRED_FILES[role]
    if config.checkpoint is not None and role == "worker":
        files = (*files, *CHECKPOINT_WORKER_FILES)
    if config.mcp_direct is not None:
        if role == "runtime-manager":
            files = (*files, *DIRECT_MANAGER_FILES)
        files = (*files, *DIRECT_CHECK_FILES.get(role, ()))
    return files


def load(path: Path) -> BundleConfig:
    """Read only the bounded TOML input, never referenced Secrets or kubeconfig."""
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise BundleError("deployment configuration exceeds 64 KiB")
        # JSON mode accepts TOML arrays for immutable tuples while retaining strict scalars.
        import json

        return BundleConfig.model_validate_json(json.dumps(tomllib.loads(raw.decode("utf-8"))))
    except ValidationError:
        raise BundleError(
            "invalid deployment configuration; check required fields and bounds"
        ) from None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BundleError("cannot read a valid deployment TOML configuration") from exc
