# SPDX-License-Identifier: MIT
"""Offline boundary tests: no credentials, cluster access, agent containers or publication."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.launcher.kubernetes_bundle import render
from sandbox_science.launcher.kubernetes_cli import main, write_bundle
from sandbox_science.launcher.kubernetes_config import BundleConfig, BundleError, load


def values() -> dict:
    return {
        "name": "research",
        "namespace": "research-test",
        "images": {
            role: f"ghcr.io/test/{role}@sha256:{hashlib.sha256(role.encode()).hexdigest()}"
            for role in ("app", "worker")
        },
        "app": {"architecture": "arm64", "storage_gib": 10},
        "pool": {"architecture": "amd64", "storage_gib": 20, "storage_class": "test-block"},
        "secrets": {
            role: {"name": f"test-{role}"}
            for role in ("control", "workspace", "worker", "middleware")
        },
        "network": {
            "dns": {
                "namespace_labels": {"kubernetes.io/metadata.name": "kube-system"},
                "pod_labels": {"k8s-app": "kube-dns"},
            },
            "runtimes": [
                {
                    "namespace_labels": {"kubernetes.io/metadata.name": "runtime"},
                    "pod_labels": {"role": "orchestrator"},
                }
            ],
            "supervisors": [{"cidr": "192.0.2.15/32"}],
            "app_egress": [{"peer": {"cidr": "192.0.2.20/32"}, "ports": [443]}],
        },
    }


def configured(data: dict | None = None) -> BundleConfig:
    return BundleConfig.model_validate_json(json.dumps(values() if data is None else data))


def objects(config: BundleConfig | None = None) -> list[dict]:
    return json.loads(json.dumps(render(config or configured())))["items"]


def test_only_trusted_singletons_and_retained_block_claims():
    items = objects()
    assert {item["kind"] for item in items} == {
        "Deployment",
        "PersistentVolumeClaim",
        "Service",
        "NetworkPolicy",
    }
    deployments = [item for item in items if item["kind"] == "Deployment"]
    assert len(deployments) == 2
    for deployment in deployments:
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}
        pod = deployment["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["enableServiceLinks"] is False
        assert "serviceAccountName" not in pod
        assert pod["securityContext"]["fsGroup"] == 1000
        for container in pod["containers"] + pod["initContainers"]:
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True
            assert security["allowPrivilegeEscalation"] is False
            assert security["capabilities"] == {"drop": ["ALL"]}
            assert security["runAsUser"] == 1000
            assert security["seccompProfile"] == {"type": "RuntimeDefault"}
            assert set(container["resources"]["limits"]) == {"cpu", "memory", "ephemeral-storage"}
            assert "agent" not in container["command"][0]
    assert deployments[0]["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/arch": "arm64"
    }
    assert deployments[1]["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/arch": "amd64"
    }
    claims = [item for item in items if item["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 2
    assert all(item["spec"]["accessModes"] == ["ReadWriteOnce"] for item in claims)
    assert all("ownerReferences" not in item["metadata"] for item in claims)
    assert "storageClassName" not in claims[0]["spec"]
    assert claims[1]["spec"]["storageClassName"] == "test-block"


def test_credentials_projected_only_to_init_and_each_service_has_private_volume():
    for item in objects():
        if item["kind"] != "Deployment":
            continue
        pod = item["spec"]["template"]["spec"]
        source_names = {volume["name"] for volume in pod["volumes"] if "secret" in volume}
        for volume in pod["volumes"]:
            if "secret" in volume:
                assert volume["secret"]["defaultMode"] == 0o444
            if volume["name"].endswith("credentials"):
                assert volume["emptyDir"] == {"medium": "Memory", "sizeLimit": "4Mi"}
        private_names = []
        for container in pod["containers"]:
            assert not source_names.intersection(
                mount["name"] for mount in container["volumeMounts"]
            )
            private = next(
                mount for mount in container["volumeMounts"] if mount["mountPath"] == "/private"
            )
            assert private["readOnly"] is True
            private_names.append(private["name"])
        assert len(set(private_names)) == len(private_names)
        for init in pod["initContainers"]:
            assert init["command"] == [
                "python3",
                "-I",
                "/usr/local/libexec/sandbox-science/copy-credentials.py",
            ]
            pairs = json.loads(init["args"][-1])
            assert all(source == destination for source, destination in pairs)
            assert len(pairs) <= 32


def test_workspace_loopback_only_and_health_does_not_require_runtime_token():
    items = objects()
    assert {item["spec"]["ports"][0]["port"] for item in items if item["kind"] == "Service"} == {
        8443,
        8100,
        50051,
    }
    app = next(
        item
        for item in items
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "research-app"
    )
    control, workspace = app["spec"]["template"]["spec"]["containers"]
    assert control["readinessProbe"]["httpGet"] == {
        "path": "/ready",
        "port": 8443,
        "scheme": "HTTPS",
    }
    assert control["livenessProbe"]["httpGet"]["path"] == "/live"
    assert "httpHeaders" not in control["readinessProbe"]["httpGet"]
    assert "127.0.0.1:8200" in workspace["command"]
    assert "127.0.0.1:8201" in workspace["command"]
    assert "https://research-worker.research-test.svc:8100" in workspace["command"]
    assert "--allow-remote" not in workspace["command"]
    assert "--worker-ca-file" in workspace["command"]


def test_default_deny_explicit_peers_and_no_wildcard_egress():
    policies = [item for item in objects() if item["kind"] == "NetworkPolicy"]
    deny = policies[0]["spec"]
    assert deny["policyTypes"] == ["Ingress", "Egress"]
    assert deny["ingress"] == deny["egress"] == []
    app = policies[1]["spec"]
    pool = policies[2]["spec"]
    assert app["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert pool["ingress"][1]["from"] == [{"ipBlock": {"cidr": "192.0.2.15/32"}}]
    assert app["egress"][1] == {
        "to": [{"ipBlock": {"cidr": "192.0.2.20/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    assert not any(rule == {} for policy in policies for rule in policy["spec"]["egress"])
    assert "0.0.0.0/0" not in json.dumps(policies)
    assert "::/0" not in json.dumps(policies)


def test_optional_cache_requires_sandbox_scope_and_adds_only_cache_flow():
    data = values()
    data["secrets"]["cache"] = {"name": "test-cache"}
    with pytest.raises(ValidationError, match="package cache"):
        configured(data)
    data["network"]["sandboxes"] = [
        {
            "namespace_labels": {"kubernetes.io/metadata.name": "workloads"},
            "pod_labels": {"role": "sandbox"},
        }
    ]
    items = objects(configured(data))
    assert (
        next(item for item in items if item["metadata"]["name"] == "research-cache")["spec"][
            "ports"
        ][0]["port"]
        == 8090
    )
    pool = next(
        item
        for item in items
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "research-pool"
    )
    assert {container["name"] for container in pool["spec"]["template"]["spec"]["containers"]} == {
        "worker",
        "middleware",
        "cache",
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("images", "app"), "ghcr.io/test/app:latest"),
        (("images", "worker"), "ghcr.io/test/worker@sha256:" + "0" * 64),
        (("images", "app"), "registry.example/app@sha256:" + "f" * 64),
        (("namespace",), "Bad Namespace"),
        (("app", "architecture"), "aarch64"),
        (("pool", "storage_gib"), 0),
        (("pool", "storage_class"), "nfs/path"),
        (("control",), {"cpu_limit_millis": 1}),
        (("worker",), {"scratch_mib": 2048}),
        (("workspace",), {"memory_limit_mib": "1024"}),
        (("network", "dns"), {}),
        (("network", "dns"), {"cidr": "0.0.0.0/0"}),
        (("network", "dns"), {"cidr": "::/0"}),
        (("network", "dns"), {"cidr": "192.0.2.5/24"}),
        (("network", "dns"), {"cidr": "192.0.2.5/32", "pod_labels": {"role": "dns"}}),
        (("network", "dns"), {"namespace_labels": {"bad key": "value"}}),
        (("network", "app_egress"), [{"peer": {"cidr": "192.0.2.1/32"}, "ports": [0]}]),
        (("network", "runtimes"), []),
        (("secrets", "control", "extra_files"), ["../key"]),
        (("secrets", "control", "extra_files"), ["key", "key"]),
        (("secrets", "control", "extra_files"), ["x" + str(index) for index in range(25)]),
        (("unexpected",), "test-secret-should-not-be-accepted"),
    ],
)
def test_rejects_unsafe_or_ambiguous_configuration(path, value):
    data = values()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        configured(data)


def test_template_requires_real_image_pins_and_loading_is_bounded(tmp_path):
    example = Path(__file__).resolve().parents[3] / "examples/kubernetes-bundle.toml"
    with pytest.raises(BundleError, match="required fields"):
        load(example)
    config = tmp_path / "oversize.toml"
    config.write_bytes(b"x" * 65537)
    with pytest.raises(BundleError, match="64 KiB"):
        load(config)


def test_cli_deterministic_private_output_no_overwrite_or_secret_access(tmp_path, capsys):
    example = Path(__file__).resolve().parents[3] / "examples/kubernetes-bundle.toml"
    text = example.read_text()
    for role in ("app", "worker"):
        digest = hashlib.sha256(role.encode()).hexdigest()
        line = f'{role} = "ghcr.io/test/{role}@sha256:{digest}"'
        text = text.replace(
            f'# {role} = "YOUR_REGISTRY/trusted-{role}@sha256:YOUR_VERIFIED_DIGEST"', line
        )
    config = tmp_path / "private.toml"
    config.write_text(text)
    output = tmp_path / "bundle.json"
    assert main(["check", "--config", str(config)]) == 0
    assert main(["render", "--config", str(config), "--output", str(output)]) == 0
    original = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    other = tmp_path / "other.json"
    assert main(["render", "--config", str(config), "--output", str(other)]) == 0
    assert other.read_bytes() == original
    assert main(["render", "--config", str(config), "--output", str(output)]) == 1
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".kubernetes-*"))
    assert "no overwrite" in capsys.readouterr().err


def test_invalid_config_does_not_echo_sensitive_input_or_create_output(tmp_path, capsys):
    config = tmp_path / "bad.toml"
    config.write_text('name = "test-secret-input-must-not-leak"\nunknown = "test-secret-value"')
    output = tmp_path / "bundle.json"
    assert main(["render", "--config", str(config), "--output", str(output)]) == 1
    assert "test-secret" not in capsys.readouterr().err
    assert not output.exists()


def test_output_symlink_is_never_followed(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b"preserve")
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    with pytest.raises(FileExistsError):
        write_bundle(linked, b"replace")
    assert real.read_bytes() == b"preserve"
