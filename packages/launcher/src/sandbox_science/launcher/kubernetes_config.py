# SPDX-License-Identifier: MIT
"""Trusted, site-private inputs for the offline Kubernetes bundle renderer."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self, cast

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
MANAGER_ONLY = frozenset((*GATEWAY_PROFILE, "manager-control.token", "model.key"))
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
    alerts: Alerts | None = None
    """Absent, no alert rules are rendered."""

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
                f"/private/{name}" for name in (*REQUIRED_FILES[role], *secret.extra_files)
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
