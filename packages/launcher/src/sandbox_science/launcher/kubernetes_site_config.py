# SPDX-License-Identifier: MIT
"""The private site file from which ``sandbox-science-kubernetes site`` generates every
Secret's contents (docs/KUBERNETES-BUNDLE.md#generate-the-secrets).

It names the bundle TOML the renderer reads, so the bundle's name, namespace, Secrets and
model catalog are given once, and adds what only the Secrets hold: the projects, the
model gateway, the trusted images, the placements and the paths of the files made
outside this tool (OpenShell's client identities and token key, the model key, the
policy, targets and ceiling). It holds no secret value, only paths; a relative path is
against the file's directory, as in the launcher's config.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, StringConstraints, ValidationError, model_validator
from pydantic_core import ErrorDetails

from sandbox_science.policy.kubernetes import KubernetesProfile, KubernetesResources

from .config import (
    CLUSTER_SERVICES,
    ConfigPath,
    Identifier,
    ModelConfig,
    ProjectConfig,
    SkillsConfig,
)
from .kubernetes_config import BundleConfig, BundleError, Model, Name, PinnedImage
from .kubernetes_config import load as load_bundle
from .kubernetes_models import ModelCatalog

ARCH = "kubernetes.io/arch"
Kata = Annotated[str, StringConstraints(pattern=r"^kata(?:-[a-z0-9]+)*$")]
"""Kata Containers' RuntimeClass names; every sandbox of the first cluster uses one (D92)."""
Role = Literal["control", "workspace", "worker", "middleware", "cache"]
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
_CATALOG_ROLES = ("main", "delegated", "reviewer")
_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_DOMAIN = rf"^{_LABEL}(?:\.{_LABEL})*$"
ClusterDomain = Annotated[str, StringConstraints(max_length=200, pattern=_DOMAIN)]
"""The cluster's DNS domain, CoreDNS's ``kubernetes <domain>`` line: ``cluster.local``
unless the cluster was set up with another."""
DEFAULT_DOMAIN = "cluster.local"
CACHE_ECOSYSTEMS = ("pypi", "conda", "cargo", "go", "npm", "opam", "hackage")
"""The package cache's ecosystems (``package_cache.settings.Settings``); the launcher
never imports the cache, so a test compares the two."""
_CLUSTER_CACHE_KEYS = frozenset(("listen", "public_url", "cache_dir", "worker_state_dir"))
"""Cache settings the bundle fixes: the Pod's listener and URL, and its PVC's paths."""


class Placement(Model):
    """A Kata, amd64 Kubernetes profile: the site's node labels and resources."""

    runtime_class_name: Kata = "kata"
    node_selector: dict[str, str] = Field(default_factory=dict[str, str], max_length=31)
    """Labels the nodes must carry besides ``kubernetes.io/arch = amd64``, which is fixed:
    arm64 nodes cannot run Kata yet."""
    requests: KubernetesResources
    limits: KubernetesResources

    def profile(self) -> KubernetesProfile:
        return KubernetesProfile(
            node_selector={**self.node_selector, ARCH: "amd64"},
            runtime_class_name=self.runtime_class_name,
            requests=self.requests,
            limits=self.limits,
        )

    @model_validator(mode="after")
    def _amd64(self) -> Self:
        if self.node_selector.get(ARCH, "amd64") != "amd64":
            raise ValueError("Kata sandboxes run on amd64 nodes only")
        self.profile()
        return self


class Profile(Placement):
    """A research base profile: its image and placement."""

    image: PinnedImage


class Runtime(Placement):
    """The agent runtime: the ``orchestrator`` image, its source revision and placement."""

    image: PinnedImage
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    """The image's full source revision, control's ``image_revision``."""
    recover_infrastructure: bool = False


class Worker(Model):
    """The worker's own options, ``[worker]`` in the site file."""

    label_environments: bool = False
    """Give each sandbox the worker creates the label ``sandbox-science.io/environment``
    (``worker.toml``'s ``openshell.environments.label_environments``). The key was
    confirmed on the test instance's OpenShell 0.1.5 gateway on 2026-10-26; the label
    appears on the OpenShell sandbox, not on the Kubernetes Sandbox object or its Pod."""


class Identity(Model):
    """An OpenShell CLI client identity issued by the gateway's client CA (PEM files)."""

    ca: ConfigPath
    certificate: ConfigPath
    key: ConfigPath


class OpenShell(Model):
    """The OpenShell gateway, its token key and the two client identities."""

    endpoint: Annotated[str, StringConstraints(max_length=2048)]
    """The gateway's ``https://host:port`` as the CLI profiles name it."""
    gateway_id: Name = "openshell"
    """The issuer's ``<gateway_id>`` in the extension tokens the middleware verifies."""
    registration: Name = "sandbox-science-policy"
    """The middleware's registration name in the gateway's configuration."""
    audience: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    jwt_public_key: ConfigPath
    """The gateway's extension-token public key (``[openshell.gateway.gateway_jwt]``), from
    OpenShell's own Secrets: this tool cannot make it."""
    admin_hosts: tuple[Annotated[str, StringConstraints(min_length=1, max_length=253)], ...] = ()
    """Administration endpoints added to the hard floor, besides the gateway's host."""
    manager: Identity
    worker: Identity

    @model_validator(mode="after")
    def _endpoint(self) -> Self:
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if parsed.scheme != "https" or not parsed.hostname or port is None or parsed.path:
            raise ValueError("openshell.endpoint is an explicit https://host:port")
        return self

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).hostname or ""


class SiteCheckpoint(Model):
    """The object-store keys of workspace checkpoints, given exactly when the bundle has
    ``[checkpoint]``: SDK credential and config files the site's Garage workflow made.
    ``upload_*`` is the checkpoint Jobs' key (write on the upload bucket only), which goes
    into its own Secret in the sandbox namespace; ``verify_*`` the worker's (read on both
    buckets). The operator's keeper key (read on the upload bucket, write on the kept one)
    never enters the cluster: only its profile name is given, for the generated storage
    files the operator's commands use."""

    upload_credentials: ConfigPath
    upload_config: ConfigPath
    verify_credentials: ConfigPath
    verify_config: ConfigPath
    keeper_endpoint: Annotated[str, StringConstraints(max_length=2048)] | None = None
    """Where the operator's machine reaches the store, such as ``http://127.0.0.1:3900``
    through ``kubectl port-forward``; by default that, on the store's first port."""


class Site(Model):
    """The site file; see the module's docstring."""

    bundle: ConfigPath
    """The bundle TOML that ``sandbox-science-kubernetes render`` reads."""
    worker_policy: ConfigPath
    """The project policy granted to every project."""
    targets: ConfigPath
    """The OpenShell targets file the worker compiles research sandboxes' policies with."""
    ceiling: ConfigPath
    """The pool ceiling, which the worker and the middleware both apply."""
    pool_id: Identifier | None = None
    """The worker's pool; the bundle's name by default."""
    cluster_domain: ClusterDomain = DEFAULT_DOMAIN
    """The cluster's DNS domain. A sandbox resolves only fully qualified Service names
    (OpenShell's sandbox DNS ignores the Pod's search domains), so every name a sandbox
    reaches is ``<service>.<namespace>.svc.<cluster_domain>``."""
    projects: tuple[ProjectConfig, ...] = Field(min_length=1, max_length=128)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    model: ModelConfig
    runtime: Runtime
    worker: Worker = Field(default_factory=Worker)
    profiles: dict[Identifier, Profile] = Field(min_length=1, max_length=32)
    openshell: OpenShell
    cache: dict[str, JsonValue] | None = None
    """The package cache's settings besides its fixed listener, URL and directories; given
    exactly when the bundle enables the cache."""
    extra_files: dict[Role, dict[Key, ConfigPath]] = Field(
        default_factory=dict[Role, dict[str, Path]]
    )
    checkpoint: SiteCheckpoint | None = None
    """Files for the bundle's other ``extra_files``, such as S3 credentials, by service."""
    forward_auth_secret: ConfigPath | None = None
    """With the bundle's HS256 forward auth (D104): the Authentik provider's client secret,
    a private file (mode 600) the operator writes once from ``ak shell``
    (docs/KUBERNETES-BUNDLE.md#login-through-authentik-opt-in), such as
    ``secure/authentik-forward-auth-client-secret``; it goes into the workspace's Secret
    only."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [project.id for project in self.projects]
        if len(set(ids)) != len(ids):
            raise ValueError("each project is listed once")
        for project in self.projects:
            if project.profile not in self.profiles:
                raise ValueError(f"project {project.id}'s profile is not one of profiles")
            if any(pack not in self.skills.packs for pack in project.skills):
                raise ValueError(f"project {project.id} names a pack skills.packs does not")
        if self.cache is not None and _CLUSTER_CACHE_KEYS & self.cache.keys():
            raise ValueError("the cache's listener, URL and directories are the cluster's")
        model = urlsplit(self.model.url).hostname or ""
        if model.endswith(".svc"):
            # The agent runtime reaches the model gateway from its sandbox, whose DNS
            # ignores the Pod's search domains.
            raise ValueError(
                "model.url names an in-cluster Service the agent runtime resolves inside its "
                f"sandbox: give the fully qualified <service>.<namespace>.svc.{self.cluster_domain}"
            )
        if self.cache is not None and not set(CACHE_ECOSYSTEMS) & self.cache.keys():
            # A cache with no ecosystem starts, then answers 404 to every request.
            raise ValueError(
                "[cache] enables no ecosystem: add at least one of "
                f"{', '.join(CACHE_ECOSYSTEMS)} (an empty table, such as pypi = {{}}, "
                "takes that ecosystem's defaults)"
            )
        return self


def _efforts(catalog: ModelCatalog) -> tuple[dict[str, str], dict[str, str]]:
    """The catalog's aliases' APIs and its roles' efforts, defaulted as the workspace's
    options default them (``ModelCatalog.workspace_arguments``), so control and the GUI
    agree."""
    apis: dict[str, str] = {}
    efforts: dict[str, str] = {}
    for role in _CATALOG_ROLES:
        profile = getattr(catalog, role)
        if profile is None:
            continue
        apis[profile.alias] = profile.api
        if profile.api != "messages":
            efforts[role] = profile.effort or ("medium" if role == "reviewer" else "max")
    return apis, efforts


def _model(raw: object, catalog: ModelCatalog) -> dict[str, object]:
    """The site's ``[model]`` with the bundle's catalog roles merged in; the catalog is
    their only source, so the two cannot disagree."""
    if not isinstance(raw, dict):
        raise BundleError("the site file needs a [model] table")
    model: dict[str, object] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    if {"name", "delegated", "reviewer"} & model.keys():
        raise BundleError("the main, delegated and reviewer models come from the bundle's catalog")
    apis, efforts = _efforts(catalog)
    extra_apis = model.get("api", {})
    extra_efforts = model.get("reasoning_effort", {})
    if not isinstance(extra_apis, dict) or not isinstance(extra_efforts, dict):
        raise BundleError("model.api and model.reasoning_effort are tables")
    if apis.keys() & extra_apis.keys() or efforts.keys() & extra_efforts.keys():
        raise BundleError("the bundle's catalog sets its own aliases' APIs and roles' efforts")
    model["name"] = catalog.main.alias
    model["delegated"] = catalog.delegated.alias
    if catalog.reviewer is not None:
        model["reviewer"] = catalog.reviewer.alias
    model["api"] = {**apis, **extra_apis}
    model["reasoning_effort"] = {**efforts, **extra_efforts}
    return model


def load(path: Path) -> tuple[Site, BundleConfig]:
    """The site file and the bundle it names, both checked; BundleError says what is wrong
    without printing configuration values."""
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise BundleError("site configuration exceeds 64 KiB")
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BundleError("cannot read a valid site TOML configuration") from exc
    base = path.absolute().parent
    bundle_path = document.get("bundle")
    if not isinstance(bundle_path, str):
        raise BundleError("the site file names its bundle TOML: bundle = PATH")
    bundle = load_bundle(base / Path(bundle_path).expanduser())
    document["model"] = _model(document.get("model"), bundle.models)
    # The model gateway's http Service name may carry the cluster's domain; an invalid
    # domain is refused below, at its own field.
    domain = document.get("cluster_domain", DEFAULT_DOMAIN)
    if not isinstance(domain, str) or len(domain) > 200 or re.fullmatch(_DOMAIN, domain) is None:
        domain = DEFAULT_DOMAIN
    try:
        site = Site.model_validate_json(
            json.dumps(document), context={"base": base, CLUSTER_SERVICES: domain}
        )
    except ValidationError as exc:
        # Field locations only, since a value may be private; a rule across fields says
        # which, in the site's own names.
        where = sorted({_location(error) for error in exc.errors()})
        raise BundleError(f"invalid site configuration at: {'; '.join(where)}") from None
    return site, bundle


def _location(error: ErrorDetails) -> str:
    """Where a validation error is, in the site file's names. A project's ID is no secret
    (the generated README lists it), so its error also says why: the ID is too long for a
    label value, and what to do."""
    loc = error["loc"]
    where = ".".join(str(part) for part in loc)
    if len(loc) == 3 and loc[0] == "projects" and loc[2] == "id":
        return f"{where}: {error['msg']}"
    return where or error["msg"]
