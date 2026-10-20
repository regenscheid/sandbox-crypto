# SPDX-License-Identifier: MIT
"""Trusted Kubernetes placement is typed and cannot enlarge project resource caps."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.policy.model import Resources
from sandbox_science.worker.kubernetes import KubernetesProfile, KubernetesResources
from sandbox_science.worker.settings import EnvironmentSettings

ROOT = Path(__file__).resolve().parents[3]


def profile() -> KubernetesProfile:
    return KubernetesProfile(
        node_selector={"kubernetes.io/arch": "arm64", "example.org/capacity": "large"},
        runtime_class_name="example-sandboxed",
        requests=KubernetesResources(
            cpu_millicores=1000, memory_mib=512, ephemeral_storage_mib=256
        ),
        limits=KubernetesResources(
            cpu_millicores=8000, memory_mib=8192, ephemeral_storage_mib=1024
        ),
    )


def test_private_example_is_a_valid_worker_environment_fragment() -> None:
    raw = tomllib.loads((ROOT / "examples/kubernetes/worker-placement.toml").read_text())
    settings = EnvironmentSettings.model_validate(raw["openshell"]["environments"])
    assert set(settings.kubernetes) == set(settings.profiles) == {"minimal", "crypto-base"}


def test_rendering_narrows_limits_to_project_and_pool_cpu_memory() -> None:
    config = json.loads(
        profile().driver_json(Resources.model_validate({"cpu": 2.5, "memory": "2Gi"}))
    )
    pod = config["kubernetes"]["pod"]
    assert pod["node_selector"]["kubernetes.io/arch"] == "arm64"
    assert pod["runtime_class_name"] == "example-sandboxed"
    limits = config["kubernetes"]["containers"]["agent"]["resources"]["limits"]
    assert limits == {"cpu": "2500m", "memory": str(2 * 1024**3), "ephemeral-storage": "1024Mi"}


def test_requests_above_effective_policy_are_refused() -> None:
    with pytest.raises(ValueError, match="requests exceed"):
        profile().driver_json(Resources(cpu=0.5))


@pytest.mark.parametrize(
    "update",
    [
        {"node_selector": {}},
        {"node_selector": {"example.org/capacity": "large"}},
        {"node_selector": {"kubernetes.io/arch": "ppc64le"}},
        {"node_selector": {"bad key": "value"}},
        {"node_selector": {"invalid..example.org/key": "value"}},
        {"runtime_class_name": "Bad/Class"},
        {"volumes": [{"hostPath": "/"}]},
        {"requests": {"cpu_millicores": True, "memory_mib": 512, "ephemeral_storage_mib": 256}},
        {"requests": {"cpu_millicores": 9000, "memory_mib": 512, "ephemeral_storage_mib": 256}},
    ],
)
def test_invalid_or_arbitrary_pod_fields_are_refused(update: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KubernetesProfile.model_validate({**profile().model_dump(), **update})


def test_missing_selector_cannot_bypass_architecture_pin() -> None:
    values = profile().model_dump(exclude={"node_selector"})
    with pytest.raises(ValidationError, match=r"must pin kubernetes\.io/arch"):
        KubernetesProfile.model_validate(values)


def test_a_placement_cannot_name_an_unoffered_profile(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unknown profiles"):
        EnvironmentSettings(
            policy_store=tmp_path,
            targets=tmp_path / "targets",
            ceiling=tmp_path / "ceiling",
            profiles={"minimal": "example.invalid/minimal:pinned"},
            kubernetes={"unknown": profile()},
        )


def test_partial_placement_cannot_leave_an_offered_profile_unpinned(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must cover all offered profiles: minimal"):
        EnvironmentSettings(
            policy_store=tmp_path,
            targets=tmp_path / "targets",
            ceiling=tmp_path / "ceiling",
            profiles={
                "minimal": "example.invalid/minimal:pinned",
                "crypto-base": "example.invalid/crypto-base:pinned",
            },
            kubernetes={"crypto-base": profile()},
        )


def test_workspace_prefix_requirement_defaults_safe_and_never_enters_driver_json() -> None:
    assert profile().require_workspace_prefix is True
    assert "require_workspace_prefix" not in profile().driver_json(Resources())
    values = profile().model_dump()
    assert (
        KubernetesProfile.model_validate(
            {**values, "require_workspace_prefix": False}
        ).require_workspace_prefix
        is False
    )
    for invalid in (0, 1, "true", None):
        with pytest.raises(ValidationError):
            KubernetesProfile.model_validate({**values, "require_workspace_prefix": invalid})
