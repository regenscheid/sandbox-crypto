# SPDX-License-Identifier: MIT
"""Trusted Kubernetes placement is typed and cannot enlarge project resource caps."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import cast

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


# The Kubernetes caller driver config as OpenShell v0.1.5 parses it
# (crates/openshell-driver-kubernetes/src/driver.rs, KubernetesSandboxDriverConfig
# and its nested structs): every struct is `deny_unknown_fields`, so one key outside
# this tree makes the gateway refuse the sandbox. `None` marks a free-form map or
# list whose contents are not keyed by the schema. The CLI wraps it in "kubernetes".
OPENSHELL_0_1_5_DRIVER_CONFIG: dict[str, object] = {
    "pod": {
        "node_selector": None,
        "runtime_class_name": None,
        "tolerations": None,
        "priority_class_name": None,
    },
    "containers": {
        "agent": {"resources": {"requests": None, "limits": None}, "volume_mounts": None}
    },
    "volumes": None,
}


def _unaccepted_paths(document: object, allowed: object, path: str = "") -> list[str]:
    if allowed is None:
        return []
    if not isinstance(document, dict) or not isinstance(allowed, dict):
        return [path or "<root>"]
    found: list[str] = []
    for key, value in cast(dict[str, object], document).items():
        if key not in allowed:
            found.append(f"{path}/{key}")
        else:
            found += _unaccepted_paths(
                value, cast(dict[str, object], allowed)[key], f"{path}/{key}"
            )
    return found


def test_driver_json_uses_only_keys_openshell_0_1_5_accepts() -> None:
    raw = tomllib.loads((ROOT / "examples/kubernetes/worker-placement.toml").read_text())
    settings = EnvironmentSettings.model_validate(raw["openshell"]["environments"])
    for placement in [profile(), *settings.kubernetes.values()]:
        document = json.loads(placement.driver_json(Resources()))
        assert set(document) == {"kubernetes"}
        assert _unaccepted_paths(document["kubernetes"], OPENSHELL_0_1_5_DRIVER_CONFIG) == []
        pod = document["kubernetes"]["pod"]
        assert all(isinstance(v, str) for v in pod["node_selector"].values())
        assert set(pod) <= {"node_selector", "runtime_class_name"}


def test_the_schema_check_rejects_keys_openshell_0_1_5_would() -> None:
    document = json.loads(profile().driver_json(Resources()))["kubernetes"]
    document["pod"]["nodeSelector"] = {}
    document["containers"]["agent"]["resources"]["claims"] = []
    assert set(_unaccepted_paths(document, OPENSHELL_0_1_5_DRIVER_CONFIG)) == {
        "/pod/nodeSelector",
        "/containers/agent/resources/claims",
    }


def test_example_files_name_the_kata_runtime_class_and_gateway_settings() -> None:
    raw = tomllib.loads((ROOT / "examples/kubernetes/worker-placement.toml").read_text())
    settings = EnvironmentSettings.model_validate(raw["openshell"]["environments"])
    assert {p.runtime_class_name for p in settings.kubernetes.values()} == {"kata"}
    assert {p.node_selector["kubernetes.io/arch"] for p in settings.kubernetes.values()} == {
        "amd64"
    }
    gateway = tomllib.loads((ROOT / "examples/kubernetes/gateway-driver.toml").read_text())
    driver = gateway["openshell"]["drivers"]["kubernetes"]
    assert driver["allow_driver_config"] is True
    assert driver["default_runtime_class_name"] == "kata"
    assert driver["resource_admission"]["enabled"] is True
    runtime_class = (ROOT / "examples/kubernetes/runtimeclass-kata.yaml").read_text()
    assert "handler: kata\n" in runtime_class
    assert 'openshell.ai/sandbox-attachable: "true"' in runtime_class
