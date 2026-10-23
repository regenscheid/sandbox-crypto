# SPDX-License-Identifier: MIT
"""Operator-owned workload placement for OpenShell's pinned Kubernetes driver.

The worker places research sandboxes with it. It lives in the policy library so that the
launcher can place the agent runtime with it too, since deployable members never import
each other.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_FLOOR, Decimal
from typing import Annotated, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .model import Resources

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?")
_DNS_PART = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
Positive = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]


def _field(value: object, *path: str) -> object:
    for name in path:
        if not isinstance(value, dict):
            raise ValueError("saved Kubernetes launch settings are malformed")
        value = cast(dict[str, object], value).get(name)
    return value


def check_pinned_policy(driver_json: str | None, cap: Resources) -> None:
    """A policy update cannot pretend to resize an existing Kubernetes allocation."""
    if driver_json in (None, "{}"):
        return
    document: object = json.loads(driver_json)
    limits = _field(document, "kubernetes", "containers", "agent", "resources", "limits")
    cpu = _field(limits, "cpu")
    memory = _field(limits, "memory")
    if (
        not isinstance(cpu, str)
        or re.fullmatch(r"[0-9]+m", cpu) is None
        or not isinstance(memory, str)
        or not memory.isascii()
        or not memory.isdecimal()
    ):
        raise ValueError("saved Kubernetes limits are malformed")
    if (cap.cpu is not None and Decimal(cpu[:-1]) > Decimal(str(cap.cpu)) * 1000) or (
        cap.memory is not None and int(memory) > int(cap.memory)
    ):
        raise ValueError(
            "saved Kubernetes allocation exceeds the current CPU/memory policy; "
            "create a new environment with smaller resource limits"
        )


class KubernetesResources(BaseModel):
    """Explicit requests or limits; integers avoid ambiguous Kubernetes units."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu_millicores: Positive
    memory_mib: Positive
    ephemeral_storage_mib: Positive


class KubernetesProfile(BaseModel):
    """Trusted worker config, never an agent-provided pod template or volume mount."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    node_selector: dict[str, str] = Field(default_factory=dict[str, str], max_length=32)
    require_workspace_prefix: bool = Field(default=True, strict=True)
    runtime_class_name: str | None = None
    requests: KubernetesResources
    limits: KubernetesResources

    @field_validator("runtime_class_name")
    @classmethod
    def _runtime(cls, name: str | None) -> str | None:
        if name is not None and (
            len(name) > 253 or any(_DNS_PART.fullmatch(part) is None for part in name.split("."))
        ):
            raise ValueError("runtime_class_name must be a Kubernetes DNS subdomain")
        return name

    @field_validator("node_selector")
    @classmethod
    def _labels(cls, labels: dict[str, str]) -> dict[str, str]:
        for key, value in labels.items():
            prefix, separator, name = key.rpartition("/")
            name = name if separator else key
            if _LABEL.fullmatch(name) is None or (value and _LABEL.fullmatch(value) is None):
                raise ValueError("node_selector contains an invalid label name or value")
            if separator and (
                len(prefix) > 253
                or any(_DNS_PART.fullmatch(part) is None for part in prefix.split("."))
            ):
                raise ValueError("node_selector contains an invalid DNS prefix")
        return labels

    @model_validator(mode="after")
    def _capacity(self) -> Self:
        # OpenShell keeps the image reference, not the selected platform. Retained
        # native packages must not silently move architectures after pod replacement.
        if self.node_selector.get("kubernetes.io/arch") not in {"amd64", "arm64"}:
            raise ValueError("node_selector must pin kubernetes.io/arch to amd64 or arm64")
        for field in KubernetesResources.model_fields:
            if getattr(self.requests, field) > getattr(self.limits, field):
                raise ValueError(f"{field} request exceeds its limit")
        return self

    def driver_json(self, cap: Resources) -> str:
        """Render v0.1.3's typed config, narrowing CPU/memory by project/pool caps.

        Ephemeral storage is not the persistent workspace's disk allowance. PVC
        capacity and archive/restore remain separate lifecycle configuration.
        """
        cpu = self.limits.cpu_millicores
        memory = self.limits.memory_mib * 1024**2
        if cap.cpu is not None:
            ceiling = int((Decimal(str(cap.cpu)) * 1000).to_integral_value(rounding=ROUND_FLOOR))
            cpu = min(cpu, ceiling)
        if cap.memory is not None:
            memory = min(memory, int(cap.memory))
        if self.requests.cpu_millicores > cpu or self.requests.memory_mib * 1024**2 > memory:
            raise ValueError("Kubernetes profile requests exceed the effective CPU/memory policy")
        pod: dict[str, object] = {"node_selector": self.node_selector}
        if self.runtime_class_name is not None:
            pod["runtime_class_name"] = self.runtime_class_name
        resources = {
            "requests": {
                "cpu": f"{self.requests.cpu_millicores}m",
                "memory": f"{self.requests.memory_mib}Mi",
                "ephemeral-storage": f"{self.requests.ephemeral_storage_mib}Mi",
            },
            "limits": {
                "cpu": f"{cpu}m",
                "memory": str(memory),
                "ephemeral-storage": f"{self.limits.ephemeral_storage_mib}Mi",
            },
        }
        return json.dumps(
            {"kubernetes": {"pod": pod, "containers": {"agent": {"resources": resources}}}},
            sort_keys=True,
            separators=(",", ":"),
        )
