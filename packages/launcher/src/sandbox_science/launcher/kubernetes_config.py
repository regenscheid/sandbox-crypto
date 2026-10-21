# SPDX-License-Identifier: MIT
"""Trusted, site-private inputs for the offline Kubernetes bundle renderer."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
Count = Annotated[int, Field(strict=True, ge=1, le=1048576)]
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9.:/-]*@sha256:([0-9a-f]{64})$")
_LABEL = re.compile(
    r"^(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9][-A-Za-z0-9_.]{0,62}$"
)
_VALUE = re.compile(r"^(?:[A-Za-z0-9][-A-Za-z0-9_.]{0,61}[A-Za-z0-9]|[A-Za-z0-9])?$")


class BundleError(Exception):
    """Invalid or unreadable trusted deployment inputs."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Images(Model):
    app: str
    worker: str

    @field_validator("app", "worker")
    @classmethod
    def pinned(cls, value: str) -> str:
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
    cache: Secret | None = None


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


class Network(Model):
    dns: Peer
    runtimes: Annotated[tuple[Peer, ...], Field(min_length=1, max_length=16)]
    supervisors: Annotated[tuple[Peer, ...], Field(min_length=1, max_length=16)]
    sandboxes: Annotated[tuple[Peer, ...], Field(max_length=16)] = ()
    app_egress: Annotated[tuple[Egress, ...], Field(max_length=16)] = ()
    pool_egress: Annotated[tuple[Egress, ...], Field(max_length=16)] = ()


class BundleConfig(Model):
    name: Annotated[Name, StringConstraints(max_length=40)]
    namespace: Name
    images: Images
    app: Placement
    pool: Placement
    secrets: Secrets
    network: Network
    control: Capacity = Capacity()
    workspace: Capacity = Capacity()
    worker: Capacity = Capacity()
    middleware: Capacity = Capacity()
    cache: Capacity = Capacity()

    @model_validator(mode="after")
    def cache_peers(self) -> Self:
        if self.secrets.cache is not None and not self.network.sandboxes:
            raise ValueError("a package cache requires approved sandbox peers")
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
