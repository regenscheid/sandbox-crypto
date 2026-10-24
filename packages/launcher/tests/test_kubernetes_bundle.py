# SPDX-License-Identifier: MIT
"""Offline boundary tests: no credentials, cluster access, agent containers or publication."""

from __future__ import annotations

import hashlib
import json
import stat
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.launcher import unit
from sandbox_science.launcher.kubernetes_bundle import render
from sandbox_science.launcher.kubernetes_cli import main, write_bundle
from sandbox_science.launcher.kubernetes_config import BundleConfig, BundleError, load


def values() -> dict:
    return {
        "name": "research",
        "namespace": "research-test",
        "models": {
            role: {"alias": f"research-{role}"} for role in ("main", "delegated", "reviewer")
        },
        "images": {
            role: f"ghcr.io/test/{role}@sha256:{hashlib.sha256(role.encode()).hexdigest()}"
            for role in ("app", "worker")
        },
        "app": {"architecture": "arm64", "storage_gib": 10},
        "pool": {"architecture": "amd64", "storage_gib": 20, "storage_class": "test-block"},
        "runtime_manager": {
            "architecture": "arm64",
            "storage_gib": 1,
            "storage_class": "test-block",
            "openshell_gateway": "test-gateway",
        },
        "secrets": {
            role: {"name": f"test-{role.replace('_', '-')}"}
            for role in ("control", "workspace", "worker", "middleware", "runtime_manager")
        },
        "network": {
            "dns": {
                "namespace_labels": {"kubernetes.io/metadata.name": "kube-system"},
                "pod_labels": {"k8s-app": "kube-dns"},
            },
            "runtimes": [
                {
                    "namespace_labels": {"kubernetes.io/metadata.name": "runtime"},
                    "pod_labels": {
                        "openshell.ai/component": "supervisor",
                        "openshell.ai/gateway-id": "test-gateway-id",
                    },
                }
            ],
            "supervisors": [{"cidr": "192.0.2.15/32"}],
            "gateway": {
                "peer": {
                    "namespace_labels": {"kubernetes.io/metadata.name": "openshell"},
                    "pod_labels": {"app.kubernetes.io/name": "openshell-gateway"},
                },
                "ports": [8080],
            },
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
        "ConfigMap",
        "Deployment",
        "PersistentVolumeClaim",
        "Service",
        "NetworkPolicy",
    }
    deployments = [item for item in items if item["kind"] == "Deployment"]
    assert len(deployments) == 3
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
    assert deployments[2]["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/arch": "arm64"
    }
    claims = [item for item in items if item["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 3
    assert all(item["spec"]["accessModes"] == ["ReadWriteOnce"] for item in claims[:2])
    assert all("ownerReferences" not in item["metadata"] for item in claims)
    assert "storageClassName" not in claims[0]["spec"]
    assert claims[1]["spec"]["storageClassName"] == "test-block"


def pool_containers(data: dict | None = None) -> dict[str, dict]:
    items = objects(configured(data))
    pool = next(
        item
        for item in items
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "research-pool"
    )
    return {c["name"]: c for c in pool["spec"]["template"]["spec"]["containers"]}


def with_cache() -> dict:
    data = values()
    data["secrets"]["cache"] = {"name": "test-cache"}
    data["network"]["sandboxes"] = [
        {
            "namespace_labels": {"kubernetes.io/metadata.name": "workloads"},
            "pod_labels": {"role": "sandbox"},
        }
    ]
    return data


def test_pool_services_run_the_unit_entry_point_on_one_config_each_in_its_container():
    for data, services in (
        (None, ["worker", "middleware"]),
        (with_cache(), ["worker", "middleware", "cache"]),
    ):
        containers = pool_containers(data)
        assert list(containers) == services
        for name, container in containers.items():
            assert container["command"] == [
                "sandbox-science-unit",
                "serve",
                "--config",
                "/etc/sandbox-science/unit.toml",
                "--only",
                name,
            ]
            mount = next(m for m in container["volumeMounts"] if m["name"] == "unit-config")
            assert mount == {
                "name": "unit-config",
                "mountPath": "/etc/sandbox-science",
                "readOnly": True,
            }
            # The services' own commands and settings paths are the unit's to run.
            assert "--settings" not in container["command"]


def unit_toml(data: dict | None = None) -> dict:
    items = objects(configured(data))
    config_map = next(item for item in items if item["kind"] == "ConfigMap")
    assert config_map["metadata"]["name"] == "research-pool-unit"
    assert list(config_map["data"]) == ["unit.toml"]
    return tomllib.loads(config_map["data"]["unit.toml"])


def test_pool_unit_config_names_each_projected_settings_file_in_the_local_shape():
    assert unit_toml() == {
        "unit": {
            "pass_env": {
                "worker": ["AWS_EC2_METADATA_DISABLED", "HOME", "PYTHONDONTWRITEBYTECODE"],
                "middleware": ["AWS_EC2_METADATA_DISABLED", "HOME", "PYTHONDONTWRITEBYTECODE"],
            }
        },
        "worker": {"settings_file": "/private/worker.toml"},
        "middleware": {"settings_file": "/private/middleware.toml"},
    }
    cached = unit_toml(with_cache())
    assert cached["cache"] == {"settings_file": "/private/cache.toml"}
    assert set(cached["unit"]["pass_env"]) == {"worker", "middleware", "cache"}


def test_pool_unit_config_passes_a_service_the_variables_its_container_has():
    data = values()
    data["worker"] = {"environment": {"AWS_REGION": "us-east-1"}}
    passed = unit_toml(data)["unit"]["pass_env"]
    assert "AWS_REGION" in passed["worker"]
    assert "AWS_REGION" not in passed["middleware"]
    # The container still has its variables, which the unit would otherwise not hand on.
    worker = pool_containers(data)["worker"]
    assert {"name": "AWS_REGION", "value": "us-east-1"} in worker["env"]


def test_pool_unit_config_is_a_valid_unit_config_for_every_container(tmp_path: Path):
    """The renderer's file loads in the unit for each ``--only`` with only that service's
    settings present, as in the container, and as a whole when all three are."""
    data = with_cache()
    text = next(i for i in objects(configured(data)) if i["kind"] == "ConfigMap")["data"][
        "unit.toml"
    ]
    config = tmp_path / "unit.toml"
    config.write_text(text)
    settings = {
        "worker": 'listen = "0.0.0.0:8100"\n',
        "middleware": 'listen = "0.0.0.0:50051"\n',
        "cache": 'listen = "0.0.0.0:8090"\n',
    }
    # /private is the container's; the unit reads the file the config names.
    private = tmp_path / "private"
    private.mkdir()
    rewritten = text.replace("/private", str(private))
    config.write_text(rewritten)
    for name, body in settings.items():
        (private / f"{name}.toml").write_text(body)
        loaded = unit.load(config, only=[name])
        assert list(loaded.services) == [name]
        (private / f"{name}.toml").unlink()
    for name, body in settings.items():
        (private / f"{name}.toml").write_text(body)
    assert list(unit.load(config).services) == ["cache", "middleware", "worker"]


def test_credentials_projected_only_to_init_and_each_service_has_private_volume():
    for item in objects():
        if item["kind"] != "Deployment":
            continue
        pod = item["spec"]["template"]["spec"]
        source_names = {volume["name"] for volume in pod["volumes"] if "secret" in volume}
        for volume in pod["volumes"]:
            if "secret" in volume:
                assert volume["secret"]["defaultMode"] == 0o444
            if volume["name"] == "runtime-credentials":
                assert volume["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Mi"}
            elif volume["name"].endswith("credentials"):
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
            if init["name"] == "initialize-state":
                assert init["command"] == [
                    "python3",
                    "-I",
                    "/usr/local/libexec/sandbox-science/initialize-state.py",
                ]
                assert init["volumeMounts"] == [
                    {"name": "data", "mountPath": "/var/lib/sandbox-science"}
                ]
                assert init["args"][:2] == ["--root", "/var/lib/sandbox-science"]
                continue
            assert init["command"] == [
                "python3",
                "-I",
                "/usr/local/libexec/sandbox-science/copy-credentials.py",
            ]
            pairs = json.loads(init["args"][-1])
            if init["name"] == "copy-runtime-manager":
                assert all(
                    source == destination or destination.startswith("xdg/openshell/")
                    for source, destination in pairs
                )
            else:
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
    init = pool["spec"]["template"]["spec"]["initContainers"][0]
    assert init["name"] == "initialize-state"
    assert init["args"][-1] == "--package-cache"


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


def test_standard_sdk_paths_are_role_scoped_and_rendered_without_credential_contents():
    data = values()
    for role, profile in (("control", "artifact-writer"), ("workspace", "artifact-reader")):
        data["secrets"][role]["extra_files"] = ["s3-credentials", "s3-config", "s3-ca.crt"]
        data[role] = {
            "environment": {
                "AWS_SHARED_CREDENTIALS_FILE": "/private/s3-credentials",
                "AWS_CONFIG_FILE": "/private/s3-config",
                "AWS_CA_BUNDLE": "/private/s3-ca.crt",
                "AWS_REGION": "us-east-1",
                "AWS_PROFILE": profile,
            }
        }
    data["worker"] = {
        "environment": {
            "HOME": "/var/lib/sandbox-science/worker-home",
            "XDG_CONFIG_HOME": "/var/lib/sandbox-science/worker-config",
        }
    }
    deployments = [item for item in objects(configured(data)) if item["kind"] == "Deployment"]
    for deployment in deployments:
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            environment = {item["name"]: item["value"] for item in container["env"]}
            assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
            role = container["name"]
            if role in ("control", "workspace"):
                assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/private/s3-credentials"
                expected = "artifact-writer" if role == "control" else "artifact-reader"
                assert environment["AWS_PROFILE"] == expected
            elif role == "worker":
                assert environment["HOME"] == "/var/lib/sandbox-science/worker-home"
                assert environment["XDG_CONFIG_HOME"] == "/var/lib/sandbox-science/worker-config"
                assert "AWS_SHARED_CREDENTIALS_FILE" not in environment
            else:
                assert "AWS_PROFILE" not in environment
    assert "test-secret" not in json.dumps(deployments)


@pytest.mark.parametrize(
    ("role", "environment"),
    [
        ("control", {"AWS_SHARED_CREDENTIALS_FILE": "/private/not-projected"}),
        ("workspace", {"AWS_CONFIG_FILE": "/private/control.json"}),
        ("control", {"AWS_CA_BUNDLE": "/etc/ssl/private/ca.crt"}),
        ("control", {"AWS_SHARED_CREDENTIALS_FILE": "/private/../worker-token"}),
        ("worker", {"AWS_ACCESS_KEY_ID": "test-secret-key"}),
        ("worker", {"AWS_SECRET_ACCESS_KEY": "test-secret-value"}),
        ("worker", {"AWS_SESSION_TOKEN": "test-secret-token"}),
        ("worker", {"AWS_EC2_METADATA_DISABLED": "false"}),
        ("worker", {"AWS_PROFILE": "test\nprofile"}),
        ("worker", {"AWS_REGION": "x" * 65}),
        ("worker", {"HOME": "/var/lib/sandbox-science/control-home"}),
        ("worker", {"HOME": "/root"}),
        ("worker", {"XDG_CONFIG_HOME": "/private"}),
        ("worker", {"PYTHONPATH": "/private/code"}),
        ("worker", {"LD_PRELOAD": "/private/test.so"}),
        ("worker", {"HTTPS_PROXY": "https://192.0.2.1"}),
        ("worker", {"NODE_TLS_REJECT_UNAUTHORIZED": "0"}),
        ("cache", {"AWS_REGION": "us-east-1"}),
    ],
)
def test_rejects_secret_environment_bypass_or_unprojected_files(role, environment):
    data = values()
    data[role] = {"environment": environment}
    with pytest.raises(ValidationError):
        configured(data)


def test_standard_shared_credential_provider_can_read_an_explicit_private_file(tmp_path):
    session_module = pytest.importorskip("botocore.session")
    credentials = tmp_path / "s3-credentials"
    credentials.write_text(
        "[artifact-reader]\naws_access_key_id=test-secret-access\n"
        "aws_secret_access_key=test-secret-value\n"
    )
    session = session_module.Session(profile="artifact-reader")
    session.set_config_variable("credentials_file", str(credentials))
    session.set_config_variable("config_file", str(tmp_path / "absent-config"))
    provider = session.get_component("credential_provider").get_provider("shared-credentials-file")
    loaded = provider.load()
    assert loaded.method == "shared-credentials-file"
    assert loaded.access_key == "test-secret-access"


def test_workspace_role_catalog_defaults_and_explicit_efforts_reach_cli():
    data = values()
    data["models"]["delegated"]["effort"] = "high"
    items = objects(configured(data))
    app = next(
        item
        for item in items
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "research-app"
    )
    workspace = next(
        item
        for item in app["spec"]["template"]["spec"]["containers"]
        if item["name"] == "workspace"
    )
    command = workspace["command"]
    assert command.count("--agent-model") == command.count("--agent-api") == 3
    assert "main=research-main" in command
    assert "delegated=research-delegated" in command
    assert "reviewer=research-reviewer" in command
    assert "main=max" in command
    assert "delegated=high" in command
    assert "reviewer=medium" in command
    assert "research-main=responses" in command
    assert "--api-key-file" not in command
    assert "--model-url" not in command


def test_shared_alias_has_one_adapter_and_messages_have_no_effort():
    data = values()
    data["models"] = {
        "main": {"alias": "research-shared", "api": "messages"},
        "delegated": {"alias": "research-shared", "api": "messages"},
    }
    arguments = configured(data).models.workspace_arguments()
    assert arguments.count("--agent-api") == 1
    assert "research-shared=messages" in arguments
    assert "--agent-effort" not in arguments
    assert not any(argument.startswith("reviewer=") for argument in arguments)


@pytest.mark.parametrize(
    "alias", ["research=main", "-research-main", "research\0main", "research\nmain"]
)
def test_catalog_alias_must_roundtrip_through_workspace_cli_arguments(alias):
    data = values()
    data["models"]["main"]["alias"] = alias
    with pytest.raises(ValidationError, match="CLI delimiters"):
        configured(data)


@pytest.mark.parametrize(
    "models",
    [
        None,
        {},
        {"main": {"alias": "research-main"}},
        {"main": {"alias": ""}, "delegated": {"alias": "research-delegated"}},
        {
            "main": {"alias": "research-main", "api": "unknown"},
            "delegated": {"alias": "research-delegated"},
        },
        {
            "main": {"alias": "research-main", "api": "messages", "effort": "max"},
            "delegated": {"alias": "research-delegated"},
        },
        {
            "main": {"alias": "research-shared", "api": "chat"},
            "delegated": {"alias": "research-shared", "api": "responses"},
        },
        {
            "main": {"alias": "research-main", "effort": "MAX"},
            "delegated": {"alias": "research-delegated"},
        },
        {
            "main": {"alias": "research-main"},
            "delegated": {"alias": "research-delegated"},
            "endpoint": "https://192.0.2.1",
        },
    ],
)
def test_role_catalog_rejects_missing_roles_incompatible_adapters_and_untyped_fields(models):
    data = values()
    data["models"] = models
    with pytest.raises(ValidationError):
        configured(data)


def named(items: list[dict], kind: str, name: str) -> dict:
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


def pod(items: list[dict], component: str) -> dict:
    return named(items, "Deployment", f"research-{component}")["spec"]["template"]["spec"]


GATEWAY_KEYS = {
    "active-gateway",
    "gateway-metadata.json",
    "gateway-ca.crt",
    "gateway-tls.crt",
    "gateway-tls.key",
}


def test_runtime_manager_is_a_confined_singleton_without_listener_or_probes():
    items = objects()
    deployment = named(items, "Deployment", "research-runtime-manager")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    spec = pod(items, "runtime-manager")
    assert spec["automountServiceAccountToken"] is False
    assert spec["nodeSelector"] == {"kubernetes.io/arch": "arm64"}
    (manager,) = spec["containers"]
    assert manager["image"] == values()["images"]["app"]
    assert manager["command"] == ["sandbox-science-runtime-manager", "serve"]
    assert not {"ports", "readinessProbe", "livenessProbe", "startupProbe"} & manager.keys()
    environment = {item["name"]: item["value"] for item in manager["env"]}
    assert environment["SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS"] == (
        "/private/runtime-manager.json"
    )
    assert manager["securityContext"]["readOnlyRootFilesystem"] is True
    assert manager["securityContext"]["runAsUser"] == 1000
    assert manager["resources"]["limits"] == {
        "cpu": "1000m",
        "memory": "1024Mi",
        "ephemeral-storage": "1024Mi",
    }
    mounts = {mount["mountPath"]: mount for mount in manager["volumeMounts"]}
    assert mounts["/var/lib/sandbox-science"]["name"] == "data"
    assert mounts["/private"]["readOnly"] is True
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    # The runtime token passes through /tmp before upload: never node disk.
    assert volumes[mounts["/tmp"]["name"]]["emptyDir"] == {"medium": "Memory", "sizeLimit": "128Mi"}
    assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "research-runtime-manager-data"
    initialize = spec["initContainers"][0]
    assert initialize["args"] == [
        "--root",
        "/var/lib/sandbox-science",
        "--component",
        "runtime-manager",
    ]
    assert not any(
        item["kind"] == "Service" and "manager" in item["metadata"]["name"] for item in items
    )
    claim = named(items, "PersistentVolumeClaim", "research-runtime-manager-data")
    # A second manager Pod, even on the same node, can never mount the state directory.
    assert claim["spec"]["accessModes"] == ["ReadWriteOncePod"]
    assert claim["spec"]["storageClassName"] == "test-block"
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"


def test_runtime_manager_architecture_and_capacity_are_configurable():
    data = values()
    data["runtime_manager"] |= {"architecture": "amd64", "memory_limit_mib": 512, "scratch_mib": 64}
    data["runtime_manager"].pop("storage_class")
    items = objects(configured(data))
    spec = pod(items, "runtime-manager")
    assert spec["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    assert spec["containers"][0]["resources"]["limits"]["memory"] == "512Mi"
    claim = named(items, "PersistentVolumeClaim", "research-runtime-manager-data")
    assert "storageClassName" not in claim["spec"]


def test_only_the_manager_projects_the_gateway_profile_into_its_cli_tree():
    items = objects()
    projected: dict[str, set[str]] = {}
    for component in ("app", "pool", "runtime-manager"):
        for volume in pod(items, component)["volumes"]:
            if "secret" in volume:
                keys = {item["key"] for item in volume["secret"]["items"]}
                projected[volume["secret"]["secretName"]] = keys
                if component != "runtime-manager":
                    assert not keys & (GATEWAY_KEYS | {"manager-control.token", "model.key"})
    assert projected["test-runtime-manager"] == GATEWAY_KEYS | {
        "runtime-manager.json",
        "control-models.json",
        "manager-control.token",
        "model.key",
        "control-ca.crt",
    }
    assert "test-runtime-manager" not in json.dumps(
        [pod(items, component) for component in ("app", "pool")]
    )
    copy = next(
        init
        for init in pod(items, "runtime-manager")["initContainers"]
        if init["name"] == "copy-runtime-manager"
    )
    pairs = dict(json.loads(copy["args"][-1]))
    profile = "xdg/openshell/gateways/test-gateway"
    assert pairs == {
        "active-gateway": "xdg/openshell/active_gateway",
        "gateway-metadata.json": f"{profile}/metadata.json",
        "gateway-ca.crt": f"{profile}/mtls/ca.crt",
        "gateway-tls.crt": f"{profile}/mtls/tls.crt",
        "gateway-tls.key": f"{profile}/mtls/tls.key",
        "runtime-manager.json": "runtime-manager.json",
        "control-models.json": "control-models.json",
        "manager-control.token": "manager-control.token",
        "model.key": "model.key",
        "control-ca.crt": "control-ca.crt",
    }


def test_control_no_longer_needs_a_static_runtime_token():
    items = objects()
    assert "control-token" not in json.dumps(items)
    spec = pod(items, "app")
    keys = next(
        {item["key"] for item in volume["secret"]["items"]}
        for volume in spec["volumes"]
        if volume.get("secret", {}).get("secretName") == "test-control"
    )
    assert "runtime-manager-token.sha256" in keys
    control, workspace = spec["containers"]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["runtime-credentials"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Mi"}
    assert {
        "name": "runtime-credentials",
        "mountPath": "/run/sandbox-science/runtime-credentials",
    } in control["volumeMounts"]
    for container in (workspace, *spec["initContainers"]):
        assert "runtime-credentials" not in {mount["name"] for mount in container["volumeMounts"]}
    assert [port["containerPort"] for port in control["ports"]] == [8443, 8444]
    service = named(items, "Service", "research-control")
    assert [(port["name"], port["port"]) for port in service["spec"]["ports"]] == [
        ("control", 8443),
        ("manager", 8444),
    ]


def test_app_containers_mount_only_their_own_state_directories():
    data = values()
    data["control"] = {"environment": {"HOME": "/var/lib/sandbox-science/control-home"}}
    spec = pod(objects(configured(data)), "app")
    mounts = {
        container["name"]: [
            (mount["mountPath"], mount.get("subPath"))
            for mount in container["volumeMounts"]
            if mount["name"] == "data"
        ]
        for container in spec["containers"]
    }
    assert mounts == {
        "control": [
            ("/var/lib/sandbox-science/control", "control"),
            ("/var/lib/sandbox-science/control-home", "control-home"),
        ],
        # The workspace reads the harness's records in control's state directory (D80).
        "workspace": [("/var/lib/sandbox-science/control", "control")],
    }
    initialize = spec["initContainers"][0]
    assert initialize["volumeMounts"] == [{"name": "data", "mountPath": "/var/lib/sandbox-science"}]


def test_manager_port_is_reachable_only_from_the_manager_and_it_only_reaches_out():
    policies = {
        item["metadata"]["name"]: item["spec"]
        for item in objects()
        if item["kind"] == "NetworkPolicy"
    }
    app = policies["research-app"]
    manager_labels = {
        "app.kubernetes.io/part-of": "research",
        "app.kubernetes.io/component": "runtime-manager",
    }
    assert app["ingress"][1] == {
        "from": [{"podSelector": {"matchLabels": manager_labels}}],
        "ports": [{"protocol": "TCP", "port": 8444}],
    }
    inbound = [
        rule
        for policy in policies.values()
        for rule in policy["ingress"]
        if {"protocol": "TCP", "port": 8444} in rule["ports"]
    ]
    assert inbound == [app["ingress"][1]]
    assert app["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert app["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {
        "openshell.ai/component": "supervisor",
        "openshell.ai/gateway-id": "test-gateway-id",
    }
    manager = policies["research-runtime-manager"]
    assert manager["podSelector"] == {"matchLabels": manager_labels}
    assert manager["policyTypes"] == ["Ingress", "Egress"]
    assert manager["ingress"] == []
    gateway = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshell"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "openshell-gateway"}},
    }
    assert manager["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 53}, {"protocol": "UDP", "port": 53}],
        },
        {
            "to": [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/part-of": "research",
                            "app.kubernetes.io/component": "app",
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8444}],
        },
        {"to": [gateway], "ports": [{"protocol": "TCP", "port": 8080}]},
    ]
    for name in ("research-app", "research-pool"):
        assert gateway not in [peer for rule in policies[name]["egress"] for peer in rule["to"]]


def test_render_is_deterministic():
    assert json.dumps(render(configured()), sort_keys=True) == json.dumps(
        render(configured()), sort_keys=True
    )
    assert json.dumps(render(configured())) == json.dumps(render(configured()))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("network", "runtimes"), [{"namespace_labels": {"kubernetes.io/metadata.name": "r"}}]),
        (
            ("network", "runtimes"),
            [
                {
                    "namespace_labels": {"kubernetes.io/metadata.name": "r"},
                    "pod_labels": {"openshell.ai/component": "workload"},
                }
            ],
        ),
        (
            ("network", "runtimes"),
            [
                {
                    "namespace_labels": {"kubernetes.io/metadata.name": "r"},
                    "pod_labels": {"openshell.ai/component": "supervisor"},
                }
            ],
        ),
        (("network", "gateway"), None),
        (("network", "gateway"), {"peer": {"cidr": "0.0.0.0/0"}, "ports": [8080]}),
        (("network", "gateway"), {"peer": {"cidr": "192.0.2.9/32"}, "ports": []}),
        (("runtime_manager",), None),
        (("runtime_manager", "architecture"), "riscv64"),
        (("runtime_manager", "openshell_gateway"), "../gateway"),
        (("runtime_manager", "storage_gib"), 0),
        (
            ("runtime_manager",),
            {
                "architecture": "arm64",
                "storage_gib": 1,
                "openshell_gateway": "test-gateway",
                "memory_request_mib": 128,
                "memory_limit_mib": 256,
                "scratch_mib": 512,
            },
        ),
        (("runtime_manager", "environment"), {"HOME": "/var/lib/sandbox-science/x"}),
        (("secrets", "runtime_manager"), None),
        (("secrets", "runtime_manager", "name"), "test-control"),
        (("secrets", "runtime_manager", "extra_files"), ["extra"]),
        (("secrets", "control", "extra_files"), ["gateway-tls.key"]),
        (("secrets", "workspace", "extra_files"), ["manager-control.token"]),
        (("secrets", "worker", "extra_files"), ["model.key"]),
    ],
)
def test_rejects_unsafe_runtime_manager_configuration(path, value):
    data = values()
    target = data
    for key in path[:-1]:
        target = target[key]
    if value is None:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    with pytest.raises(ValidationError):
        configured(data)


def test_every_pod_carries_the_labels_the_log_collector_selects_by():
    items = objects()
    for component in ("app", "pool", "runtime-manager"):
        labels = named(items, "Deployment", f"research-{component}")["spec"]["template"][
            "metadata"
        ]["labels"]
        assert labels == {
            "app.kubernetes.io/name": "sandbox-science",
            "app.kubernetes.io/instance": "research",
            "app.kubernetes.io/part-of": "research",
            "app.kubernetes.io/component": component,
        }
    # Selectors and network policies match the component labels only: the log labels
    # never decide who may talk to whom, and a Deployment's selector is immutable.
    for item in items:
        if item["kind"] == "Deployment":
            assert "app.kubernetes.io/name" not in item["spec"]["selector"]["matchLabels"]
        if item["kind"] == "NetworkPolicy":
            assert '"sandbox-science"' not in json.dumps(item["spec"])
    for item in items:
        assert item["metadata"]["labels"]["app.kubernetes.io/instance"] == "research"


def test_every_container_logs_json_lines():
    items = objects()
    for component in ("app", "pool", "runtime-manager"):
        for container in pod(items, component)["containers"]:
            assert {"name": "SS_LOG_FORMAT", "value": "json"} in container["env"], container["name"]


def with_ingress(data: dict | None = None) -> dict:
    data = values() if data is None else data
    data["workspace"] = {"allow_remote": True}
    data["ingress"] = {
        "workspace_host": "workspace.research.test",
        "preview_host": "preview.research.test",
        "issuer": "test-issuer",
        "authentik_middleware": "test-authentik",
        "traefik": [
            {
                "namespace_labels": {"kubernetes.io/metadata.name": "traefik-public"},
                "pod_labels": {"app.kubernetes.io/name": "traefik"},
            }
        ],
    }
    return data


def workspace_command(items: list[dict]) -> list[str]:
    return next(c for c in pod(items, "app")["containers"] if c["name"] == "workspace")["command"]


def test_without_ingress_nothing_is_exposed_and_the_workspace_stays_on_loopback():
    items = objects()
    kinds = {item["kind"] for item in items}
    assert not kinds & {"IngressRoute", "Certificate"}
    assert {item["metadata"]["name"] for item in items if item["kind"] == "Service"} == {
        "research-control",
        "research-worker",
        "research-middleware",
    }
    workspace = next(c for c in pod(items, "app")["containers"] if c["name"] == "workspace")
    assert "ports" not in workspace
    assert not any(a.startswith("--allow-remote") for a in workspace["command"])
    assert "--preview-name" not in workspace["command"]


def test_ingress_moves_the_workspace_to_the_pod_network_with_its_own_preview_name():
    command = workspace_command(objects(configured(with_ingress())))
    assert command[command.index("--listen") + 1] == "0.0.0.0:8200"
    assert command[command.index("--preview-listen") + 1] == "0.0.0.0:8201"
    assert "--allow-remote" in command
    assert command[command.index("--preview-name") + 1] == "preview.research.test"
    # Nothing else about the command changes.
    plain = workspace_command(objects())
    drop = {"--allow-remote", "--preview-name", "preview.research.test"}
    assert [a for a in command if a not in drop and not a.endswith((":8200", ":8201"))] == [
        a for a in plain if not a.endswith((":8200", ":8201"))
    ]


def test_ingress_renders_a_service_certificate_and_authenticated_route_per_host():
    items = objects(configured(with_ingress()))
    for suffix, host, port in (
        ("workspace", "workspace.research.test", 8200),
        ("preview", "preview.research.test", 8201),
    ):
        service = named(items, "Service", f"research-{suffix}")
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["selector"] == {
            "app.kubernetes.io/part-of": "research",
            "app.kubernetes.io/component": "app",
        }
        assert [p["port"] for p in service["spec"]["ports"]] == [port]
        certificate = named(items, "Certificate", f"research-{suffix}")
        assert certificate["apiVersion"] == "cert-manager.io/v1"
        assert certificate["spec"]["dnsNames"] == [host]
        assert certificate["spec"]["issuerRef"] == {
            "name": "test-issuer",
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        }
        route = named(items, "IngressRoute", f"research-{suffix}")
        assert route["apiVersion"] == "traefik.io/v1alpha1"
        assert route["spec"]["entryPoints"] == ["websecure"]
        assert route["spec"]["tls"] == {"secretName": certificate["spec"]["secretName"]}
        (rule,) = route["spec"]["routes"]
        assert rule["match"] == f"Host(`{host}`)"
        assert rule["middlewares"] == [{"name": "test-authentik"}]
        assert rule["services"] == [{"name": f"research-{suffix}", "port": port, "scheme": "http"}]
        assert "annotations" not in route["metadata"]
    # Control's ports are never behind a route.
    exposed = {
        service["port"]
        for route in items
        if route["kind"] == "IngressRoute"
        for rule in route["spec"]["routes"]
        for service in rule["services"]
    }
    assert exposed == {8200, 8201}


def test_ingress_class_and_issuer_kind_are_passed_through():
    data = with_ingress()
    data["ingress"] |= {"ingress_class": "traefik-public", "issuer_kind": "Issuer"}
    items = objects(configured(data))
    route = named(items, "IngressRoute", "research-workspace")
    assert route["metadata"]["annotations"] == {"kubernetes.io/ingress.class": "traefik-public"}
    assert named(items, "Certificate", "research-preview")["spec"]["issuerRef"]["kind"] == "Issuer"


def test_ingress_network_policy_admits_only_the_controller_pods_to_the_two_ports():
    items = objects(configured(with_ingress()))
    policy = named(items, "NetworkPolicy", "research-ingress")["spec"]
    assert policy["policyTypes"] == ["Ingress"]
    assert policy["podSelector"] == {
        "matchLabels": {
            "app.kubernetes.io/part-of": "research",
            "app.kubernetes.io/component": "app",
        }
    }
    (rule,) = policy["ingress"]
    assert rule["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik-public"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
        }
    ]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8200}, {"protocol": "TCP", "port": 8201}]
    # No other policy admits anything to those ports, and none opens an egress.
    for item in items:
        if item["kind"] != "NetworkPolicy" or item["metadata"]["name"] == "research-ingress":
            continue
        for ingress_rule in item["spec"]["ingress"]:
            assert {p["port"] for p in ingress_rule["ports"]}.isdisjoint({8200, 8201})
    assert "egress" not in policy
    # Without ingress the policy does not exist.
    assert not [i for i in objects() if i["metadata"]["name"] == "research-ingress"]


def test_ingress_exposes_no_other_pod():
    items = objects(configured(with_ingress()))
    routed = {
        service["name"]
        for item in items
        if item["kind"] == "IngressRoute"
        for rule in item["spec"]["routes"]
        for service in rule["services"]
    }
    assert routed == {"research-workspace", "research-preview"}
    for name in routed:
        selector = named(items, "Service", name)["spec"]["selector"]
        assert selector["app.kubernetes.io/component"] == "app"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"workspace_host": "Workspace.research.test"}, "lowercase"),
        ({"workspace_host": "workspace"}, "lowercase"),
        ({"workspace_host": "*.research.test"}, "lowercase"),
        ({"workspace_host": "10.0.0.1"}, "lowercase"),
        ({"workspace_host": "w.research.test`) || Host(`x.test"}, "lowercase"),
        ({"workspace_host": "workspace.research.test."}, "lowercase"),
        ({"preview_host": "workspace.research.test"}, "separate origin"),
        ({"issuer": "Bad Issuer"}, "issuer"),
        ({"issuer": ""}, "issuer"),
        ({"issuer_kind": "Other"}, "issuer_kind"),
        ({"authentik_middleware": "x/y"}, "authentik_middleware"),
        ({"entry_point": "web"}, "TLS is required"),
        ({"entry_point": "http"}, "TLS is required"),
        ({"traefik": []}, "traefik"),
        ({"traefik": [{"cidr": "192.0.2.9/32"}]}, "Pods by namespace"),
        (
            {"traefik": [{"namespace_labels": {"kubernetes.io/metadata.name": "traefik"}}]},
            "Pods by namespace",
        ),
        ({"unexpected": "x"}, "unexpected"),
    ],
)
def test_ingress_validation_failures(change, message):
    data = with_ingress()
    data["ingress"] |= change
    with pytest.raises(ValidationError, match=message):
        configured(data)


@pytest.mark.parametrize("missing", ["workspace_host", "preview_host", "issuer", "traefik"])
def test_ingress_requires_hosts_issuer_and_authentication(missing):
    data = with_ingress()
    del data["ingress"][missing]
    with pytest.raises(ValidationError):
        configured(data)
    data = with_ingress()
    del data["ingress"]["authentik_middleware"]
    with pytest.raises(ValidationError, match="authentik_middleware"):
        configured(data)


def test_allow_remote_requires_ingress_and_ingress_requires_allow_remote():
    data = values()
    data["workspace"] = {"allow_remote": True}
    with pytest.raises(ValidationError, match="requires an \\[ingress\\] section"):
        configured(data)
    data = with_ingress()
    del data["workspace"]
    with pytest.raises(ValidationError, match="allow_remote = true"):
        configured(data)
    data["workspace"] = {"allow_remote": False}
    with pytest.raises(ValidationError, match="allow_remote = true"):
        configured(data)
