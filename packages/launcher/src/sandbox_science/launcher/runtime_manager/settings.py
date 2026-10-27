# SPDX-License-Identifier: MIT
"""The runtime manager's settings, ``runtime-manager.json`` (KRB-19, KRB-24).

They name control's two listeners on the control Service (the runtime's, 8443, and the
manager's, 8444), the CA that verifies both, the manager's bearer token, the runtime
image by digest, the agent runtime's OpenShell policy target and its Kata placement,
the model gateway and key, the OpenShell CLI that holds the gateway credential, and the
manager's own state directory (its PVC). Every file is only named: the manager reads
the token per request and uploads the model key without reading it.

The model part of ``control.json`` (aliases, APIs, efforts, embedding and the image
revision) comes from a copy of it, ``control_models_file``, so the runtime's settings
are derived from what control itself runs with (KRB-19).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from sandbox_science.contracts.model import Contract
from sandbox_science.launcher.config import LauncherError, McpDirectServer, ThinkingReplayConfig
from sandbox_science.launcher.runtime_owner import agent_settings
from sandbox_science.policy.kubernetes import KubernetesProfile
from sandbox_science.policy.model import Resources
from sandbox_science.policy.openshell import OrchestratorTarget

DEFAULT_STATE = Path("/var/lib/sandbox-science/runtime")
DEFAULT_XDG_CONFIG_HOME = "/private/xdg"
"""Where the bundle mounts the CLI's gateway profile (``kubernetes_bundle.XDG``)."""
_SETTINGS_BYTES = 64 * 1024
_KATA = re.compile(r"kata(?:-[a-z0-9]+)*")
"""Kata Containers' RuntimeClass names: ``kata``, ``kata-qemu``, ``kata-clh`` and so on."""
Alias = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]


class ManagerOpenShell(Contract):
    """The OpenShell CLI as the manager runs it, at the gateway's release."""

    cli: str = Field(default="openshell", min_length=1, max_length=4096)
    env: dict[str, str] = Field(
        default_factory=dict[str, str], max_length=64, validate_default=True
    )
    """What the CLI needs to reach the gateway. ``XDG_CONFIG_HOME``, where it finds the
    gateway's profile, is ``DEFAULT_XDG_CONFIG_HOME`` unless given."""

    @field_validator("env")
    @classmethod
    def _xdg(cls, value: dict[str, str]) -> dict[str, str]:
        return {"XDG_CONFIG_HOME": DEFAULT_XDG_CONFIG_HOME, **value}


Origin = Annotated[str, Field(min_length=1, max_length=2048)]


class WorkerLink(Contract):
    """The worker route that binds the runtime's sandbox in the policy middleware's store
    (``runtime_binding``): its origin, the CA that verifies it, an operator client's bearer
    token and the project whose policy revision decides the sandbox."""

    url: Origin
    ca_file: Path
    token_file: Path
    project: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @field_validator("url")
    @classmethod
    def _origin(cls, value: str) -> str:
        return _origin(value)


class ManagerMcp(Contract):
    """The direct MCP path's part of the runtime's sandbox (D98): the site's project
    policy, pool ceiling and targets, from which the manager compiles the egress at each
    creation of the sandbox (``direct_mcp.compile_direct``, as ``up`` does) with the
    project's classification revisions in force, read through the worker; the servers the
    runtime's client reaches, with their credential headers; and the providers attached to
    the sandbox."""

    project_policy_file: Path
    ceiling_file: Path
    targets_file: Path
    servers: dict[str, McpDirectServer] = Field(min_length=1, max_length=64)
    providers: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("providers")
    @classmethod
    def _named(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name) is None:
                raise ValueError("a provider is an OpenShell provider's name")
        return value


class ControlModels(BaseModel):
    """The non-secret model part of ``control.json``; its other keys are ignored, so the
    copy may be the whole file."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    main: Alias
    reviewer: Alias | None = None
    watcher: Alias | None = None
    delegated: Alias | None = None
    metadata: Alias | None = None
    embedding: Alias | None = None
    apis: dict[str, str] = Field(default_factory=dict[str, str], max_length=128)
    reasoning_effort: dict[str, str] = Field(default_factory=dict[str, str], max_length=16)
    image_revision: str = Field(pattern=r"^[0-9a-f]{40}$")

    def aliases(self) -> tuple[str, ...]:
        """The aliases the runtime may call, in ``orchestrator.prepare``'s order."""
        named = (
            self.main,
            self.reviewer,
            self.watcher,
            self.delegated,
            self.metadata,
            self.embedding,
        )
        return tuple(dict.fromkeys(alias for alias in named if alias))


def _origin(value: str) -> str:
    """An explicit ``https://host:port`` origin, without a path, query or credentials."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "https"
        or not value.startswith("https://")
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ValueError("control URLs are explicit https://host:port origins")
    return value.rstrip("/")


class RuntimeManagerSettings(Contract):
    """What ``sandbox-science-runtime-manager`` runs with; see the module's docstring."""

    state_dir: Path = DEFAULT_STATE
    control_url: Origin
    """Control's runtime listener on the control Service (8443), as the runtime reaches it."""
    manager_url: Origin
    """Control's manager listener (8444) on the same Service."""
    control_ca_file: Path
    manager_token_file: Path
    control_models_file: Path
    model_url: str = Field(min_length=1, max_length=2048)
    model_key_file: Path
    thinking_replay: ThinkingReplayConfig | None = None
    image: str = Field(pattern=r"^[^\s@]{1,400}@sha256:[0-9a-f]{64}$")
    """The runtime image, pinned by digest."""
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    python: str = Field(default="/opt/sandbox-science/bin/python", min_length=1, max_length=4096)
    policy: OrchestratorTarget
    """The services the runtime's sandbox may reach, compiled to ``policy.yaml``."""
    runtime: KubernetesProfile
    """The runtime sandbox's placement: Kata on amd64 only (tier 3)."""
    openshell: ManagerOpenShell = Field(default_factory=ManagerOpenShell)
    worker: WorkerLink | None = None
    """When given, the runtime's sandbox is bound to the project's policy revision through
    the worker when it is Ready, and unbound when it is deleted. Without it nothing binds
    the sandbox, and the policy middleware refuses its MCP requests (``sandbox_unbound``)."""
    mcp: ManagerMcp | None = None
    """The direct MCP path (D98): needs ``worker``, which binds the sandbox."""
    start_timeout: float = Field(default=300.0, gt=0, le=3600)
    stop_timeout: float = Field(default=30.0, gt=0, le=600)
    recover_infrastructure: bool = False
    """Reattach after a runtime exit under the same control, and replace a sandbox that
    is confirmed gone or dead, within the ten-attempt bound."""

    @field_validator("control_url", "manager_url")
    @classmethod
    def _origins(cls, value: str) -> str:
        return _origin(value)

    @field_validator("runtime")
    @classmethod
    def _kata(cls, profile: KubernetesProfile) -> KubernetesProfile:
        if profile.runtime_class_name is None or not _KATA.fullmatch(profile.runtime_class_name):
            raise ValueError("the agent runtime runs only under a Kata RuntimeClass (tier 3)")
        if profile.node_selector.get("kubernetes.io/arch") != "amd64":
            raise ValueError("the agent runtime's Kata placement must pin amd64 nodes")
        return profile

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if self.mcp is not None and self.worker is None:
            raise ValueError("the direct MCP path needs the worker link that binds the sandbox")
        return self

    @model_validator(mode="after")
    def _one_service(self) -> Self:
        control, manager = urlsplit(self.control_url), urlsplit(self.manager_url)
        if control.hostname != manager.hostname or control.port == manager.port:
            raise ValueError("control_url and manager_url are two ports of the control Service")
        return self

    def driver_config(self) -> str:
        """The configured Kata placement as OpenShell's ``--driver-config-json``."""
        return self.runtime.driver_json(Resources())

    def models(self) -> ControlModels:
        """Control's model part, refused when its revision is not the runtime image's."""
        try:
            models = ControlModels.model_validate_json(_bounded(self.control_models_file))
        except ValidationError as exc:
            raise LauncherError(f"control's model settings are invalid: {exc}") from None
        if models.image_revision != self.revision:
            raise LauncherError("control's image revision differs from the runtime image's")
        return models

    def runtime_json(
        self, models: ControlModels, generation: str, mcp: dict[str, object] | None = None
    ) -> str:
        """The runtime's ``runtime.json`` for an issued generation, as ``prepare`` makes it;
        ``mcp`` is the direct path's servers as the sandbox's fence was compiled
        (``DirectEgress.runtime``)."""
        settings = agent_settings(
            control_url=self.control_url,
            model_url=self.model_url,
            models=models.aliases(),
            apis=models.apis,
            reasoning_effort=models.reasoning_effort,
            embedding=models.embedding,
            thinking_replay=self.thinking_replay.model_dump(mode="json")
            if self.thinking_replay is not None
            else None,
            generation=generation,
            control_ca=True,
            mcp=mcp,
        )
        return json.dumps(settings) + "\n"


def _bounded(path: Path, limit: int = _SETTINGS_BYTES) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise LauncherError(f"cannot read {path}: {exc.strerror}") from None
    if len(data) > limit:
        raise LauncherError(f"{path} exceeds its byte bound")
    return data


def load(path: Path) -> RuntimeManagerSettings:
    """The settings in ``path``; anything malformed is refused."""
    try:
        return RuntimeManagerSettings.model_validate_json(_bounded(path))
    except ValidationError as exc:
        raise LauncherError(f"invalid runtime manager settings: {exc}") from None
