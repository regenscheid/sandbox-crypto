# SPDX-License-Identifier: MIT
"""``sandbox-science-kubernetes site``: every Secret's contents, consistent with each
other and with the bundle, each settings file loaded through its service's own model."""

from __future__ import annotations

import datetime
import hashlib
import json
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID

from sandbox_science.launcher import toml
from sandbox_science.launcher.kubernetes_bundle import render
from sandbox_science.launcher.kubernetes_cli import main
from sandbox_science.launcher.kubernetes_config import REQUIRED_FILES, BundleError
from sandbox_science.launcher.kubernetes_config import load as load_bundle
from sandbox_science.launcher.kubernetes_site import plan, write
from sandbox_science.launcher.kubernetes_site_config import CACHE_ECOSYSTEMS, load
from sandbox_science.launcher.runtime_manager import settings as manager_settings
from sandbox_science.orchestrator.confinement.server import ControlSettings
from sandbox_science.package_cache.settings import Settings as CacheSettings
from sandbox_science.package_cache.settings import load_settings as load_cache
from sandbox_science.policy.openshell import compile_orchestrator
from sandbox_science.policy_middleware.settings import load_settings as load_middleware
from sandbox_science.worker.settings import load_settings as load_worker
from sandbox_science.worker.settings import token_digest

ROOT = Path(__file__).resolve().parents[3]
NAME, NAMESPACE, GATEWAY = "research", "research-test", "test-gateway"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _image(name: str) -> str:
    return f"ghcr.io/test/{name}@sha256:{_digest(name)}"


def _resources(cpu: int, memory: int) -> dict[str, int]:
    return {"cpu_millicores": cpu, "memory_mib": memory, "ephemeral_storage_mib": 1024}


DIRECT: dict[str, Any] = {
    "project": "crypto",
    "servers": {
        "crypto-research-mcp": {"credentials": {"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"}}
    },
    "about": {"crypto-research-mcp": "Cryptography research servers"},
    "provider_secret": "test-mcp-provider",
}
"""The bundle's ``[mcp_direct]``: the example policy's server, on the direct path (D98)."""


def bundle_values(*, cache: bool = False, direct: bool = False) -> dict[str, Any]:
    secrets: dict[str, Any] = {
        "control": {"name": "test-control", "extra_files": ["s3-credentials"]},
        "workspace": {"name": "test-workspace"},
        "worker": {
            "name": "test-worker",
            "extra_files": ["pool-token", "ceiling.yaml", "targets.yaml"],
        },
        "middleware": {
            "name": "test-middleware",
            "extra_files": ["ceiling.yaml", "targets.yaml", "gateway-public.pem"],
        },
        "runtime_manager": {"name": "test-runtime-manager"},
    }
    network: dict[str, Any] = {
        "dns": {"namespace_labels": {"kubernetes.io/metadata.name": "kube-system"}},
        "runtimes": [
            {
                "namespace_labels": {"kubernetes.io/metadata.name": "runtimes"},
                "pod_labels": {
                    "openshell.ai/component": "supervisor",
                    "openshell.ai/gateway-id": "openshell",
                },
            }
        ],
        "supervisors": [{"cidr": "192.0.2.15/32"}],
        "gateway": {"peer": {"cidr": "192.0.2.16/32"}, "ports": [8080]},
    }
    if cache:
        secrets["cache"] = {"name": "test-cache"}
        network["sandboxes"] = [{"cidr": "192.0.2.17/32"}]
    return {
        "name": NAME,
        "namespace": NAMESPACE,
        "images": {role: _image(f"trusted-{role}") for role in ("app", "worker")},
        "models": {
            "main": {"alias": "test-main"},
            "delegated": {"alias": "test-main"},
            "reviewer": {"alias": "test-reviewer", "effort": "low"},
        },
        "app": {"architecture": "arm64", "storage_gib": 10},
        "pool": {"architecture": "amd64", "storage_gib": 20},
        "runtime_manager": {
            "architecture": "arm64",
            "storage_gib": 1,
            "openshell_gateway": GATEWAY,
        },
        "secrets": secrets,
        "network": network,
        **({"mcp_direct": DIRECT} if direct else {}),
    }


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _certificate(
    subject: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Name, signer: Any, ca: bool
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(signer, hashes.SHA256())
    )


def identity(directory: Path, name: str) -> dict[str, str]:
    """A synthetic OpenShell client identity: its CA, certificate and key."""
    directory.mkdir()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test gateway client CA")])
    ca = _certificate("test gateway client CA", ca_key, ca_name, ca_key, True)
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = _certificate(name, key, ca_name, ca_key, False)
    paths = {"ca": directory / "ca.crt", "certificate": directory / "tls.crt"}
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths["certificate"].write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    paths["key"] = directory / "tls.key"
    paths["key"].write_bytes(_pem_key(key))
    paths["key"].chmod(0o600)
    return {field: str(path) for field, path in paths.items()}


def _private(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def site_values(
    tmp_path: Path, *, cache: bool = False, domain: str | None = None, direct: bool = False
) -> dict[str, Any]:
    """A site file's values and every file it names, all synthetic; ``domain`` is the
    cluster's DNS domain, the default when None."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    bundle = inputs / "bundle.toml"
    bundle.write_text(toml.dumps(bundle_values(cache=cache, direct=direct)))
    targets = (ROOT / "examples/openshell/targets.yaml").read_text()
    if cache:
        targets = targets.replace(
            "package-cache.sandbox-science.internal",
            f"{NAME}-cache.{NAMESPACE}.svc.{domain or 'cluster.local'}",
        )
    (inputs / "targets.yaml").write_text(targets)
    (inputs / "ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    (inputs / "gateway-public.pem").write_bytes(
        public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    placement = {
        "node_selector": {"test.example/kata-capable": "true"},
        "requests": _resources(1000, 1024),
        "limits": _resources(2000, 2048),
    }
    values: dict[str, Any] = {
        "bundle": "inputs/bundle.toml",
        "worker_policy": str(ROOT / "examples/policy.yaml"),
        "targets": "inputs/targets.yaml",
        "ceiling": "inputs/ceiling.yaml",
        "projects": [
            {"id": "crypto", "profile": "crypto-base", "sessions": ["main"]},
            {"id": "notes", "profile": "minimal"},
        ],
        "skills": {"embedding_model": "test-embedding", "metadata_model": "test-main"},
        "model": {
            "url": "http://litellm.litellm.svc.cluster.local:4000/v1",
            "api_key_file": _private(inputs / "model.key", "test-secret-model-key"),
            "watcher": "test-watcher",
            "api": {"test-watcher": "chat"},
            "reasoning_effort": {"watcher": "low"},
        },
        "runtime": {"image": _image("orchestrator"), "revision": REVISION, **placement},
        "profiles": {
            "minimal": {"image": _image("minimal"), **placement},
            "crypto-base": {
                "image": _image("crypto-base"),
                **placement,
                "node_selector": {"test.example/compute-tier": "high"},
            },
        },
        "openshell": {
            "endpoint": "https://openshell.openshell.svc.cluster.local:8080",
            "jwt_public_key": "inputs/gateway-public.pem",
            "manager": identity(inputs / "manager", "sandbox-science-manager"),
            "worker": identity(inputs / "worker", "sandbox-science-worker"),
        },
        "extra_files": {"control": {"s3-credentials": _private(inputs / "s3", "test-secret-s3")}},
    }
    if cache:
        values["cache"] = {"max_size": "10Gi", "pypi": {}}
    if domain is not None:
        values["cluster_domain"] = domain
    return values


def site_file(tmp_path: Path, values: dict[str, Any] | None = None, **options: Any) -> Path:
    path = tmp_path / "site.toml"
    path.write_text(toml.dumps(site_values(tmp_path, **options) if values is None else values))
    return path


def generate(tmp_path: Path, **options: Any) -> Path:
    output = tmp_path / "out"
    write(output, plan(*load(site_file(tmp_path, **options))))
    return output


@pytest.mark.parametrize("cache", [False, True])
def test_each_settings_file_loads_through_the_model_its_service_starts_with(tmp_path, cache):
    output = generate(tmp_path, cache=cache)
    secrets = output / "secrets"
    control = ControlSettings.model_validate_json((secrets / "control/control.json").read_text())
    assert control.listen == "0.0.0.0:8443"
    assert control.manager_listen == "0.0.0.0:8444"
    assert control.tls is not None
    assert str(control.tls.certificate) == "/private/control.crt"
    assert control.worker == f"https://{NAME}-worker.{NAMESPACE}.svc:8100"
    assert str(control.runtime_token_file).startswith("/run/sandbox-science/runtime-credentials/")
    assert str(control.generation_file) == "/var/lib/sandbox-science/control/runtime-generation"
    assert str(control.manager_token_sha256_file) == "/private/runtime-manager-token.sha256"
    assert control.projects == ("crypto", "notes")
    assert control.image_revision == REVISION
    # The catalog's efforts, defaulted as the workspace's options default them.
    assert control.reasoning_effort == {
        "main": "max",
        "delegated": "max",
        "reviewer": "low",
        "watcher": "low",
    }
    assert control.apis == {
        "test-main": "responses",
        "test-reviewer": "responses",
        "test-watcher": "chat",
    }

    worker = load_worker(secrets / "worker/worker.toml")
    assert worker.listen == "0.0.0.0:8100"
    assert worker.tls is not None
    openshell = worker.openshell
    assert openshell is not None
    assert openshell.environments is not None
    assert openshell.isolation_tier == 3
    assert openshell.node_kernel_releases == ("*-talos",)
    assert openshell.env == {"XDG_CONFIG_HOME": "/var/lib/sandbox-science/worker-config"}
    for profile in openshell.environments.kubernetes.values():
        assert profile.runtime_class_name == "kata"
        assert profile.node_selector["kubernetes.io/arch"] == "amd64"
    assert openshell.environments.kubernetes["crypto-base"].node_selector == {
        "kubernetes.io/arch": "amd64",
        "test.example/compute-tier": "high",
    }
    assert openshell.environments.profiles["minimal"] == _image("minimal")
    assert (openshell.package_cache is not None) == cache
    assert not openshell.environments.label_environments

    middleware = load_middleware(secrets / "middleware/middleware.toml")
    assert middleware.listen == "0.0.0.0:50051"
    assert middleware.tokens is not None
    assert str(middleware.tokens.public_key) == "/private/gateway-public.pem"
    assert middleware.admin_hosts == ("openshell.openshell.svc.cluster.local",)
    assert openshell.environments.admin_hosts == middleware.admin_hosts
    if cache:
        # A sandbox resolves only the fully qualified name.
        assert openshell.package_cache == f"http://{NAME}-cache.{NAMESPACE}.svc.cluster.local:8090"
        settings = load_cache(secrets / "cache/cache.toml")
        assert settings.public_url == openshell.package_cache
        assert str(settings.cache_dir) == "/var/lib/sandbox-science/package-cache"
        # The worker's state, on the same PVC: its lockfiles pin what environments use.
        assert str(settings.worker_state_dir) == "/var/lib/sandbox-science/worker"
    else:
        assert not (secrets / "cache").exists()

    manager = manager_settings.load(secrets / "runtime-manager/runtime-manager.json")
    # The runtime resolves control inside its sandbox: only the qualified name works.
    control_host = f"{NAME}-control.{NAMESPACE}.svc.cluster.local"
    assert manager.control_url == f"https://{control_host}:8443"
    assert manager.manager_url == f"https://{control_host}:8444"
    assert manager.image == _image("orchestrator")
    assert manager.revision == REVISION
    assert manager.runtime.node_selector["test.example/kata-capable"] == "true"
    assert manager.policy.state_service is not None
    assert manager.policy.state_service.host == control_host
    assert manager.policy.model_gateway is not None
    assert manager.policy.model_gateway.host == "litellm.litellm.svc.cluster.local"
    # The runtime completes TLS to control and the worker itself, against their CA.
    assert manager.policy.state_service.relay_tls
    assert manager.policy.worker_api.relay_tls
    assert not manager.policy.model_gateway.relay_tls
    relayed = compile_orchestrator(manager.policy).network_policies
    assert relayed["sandbox-science-state-service"].endpoints[0].tls == "skip"
    models = manager.model_copy(
        update={"control_models_file": secrets / "runtime-manager/control-models.json"}
    ).models()
    assert models.aliases() == (
        "test-main",
        "test-reviewer",
        "test-watcher",
        "test-embedding",
    )
    runtime = json.loads(manager.runtime_json(models, "runtime-test"))
    assert runtime["control_url"] == f"https://{control_host}:8443/control"


def test_the_direct_path_binds_the_runtimes_sandbox_and_gives_it_its_servers(tmp_path):
    output = generate(tmp_path, direct=True)
    secrets = output / "secrets"
    server = "crypto-research-mcp"
    # Control's run service takes the direct path, with no proxy.
    control = ControlSettings.model_validate_json((secrets / "control/control.json").read_text())
    assert control.proxy is None
    assert control.direct is not None
    assert (control.direct.project, control.direct.servers) == ("crypto", (server,))
    assert control.direct.about == {server: "Cryptography research servers"}
    # The worker knows the manager as an operator of the direct path's project alone.
    worker = load_worker(secrets / "worker/worker.toml")
    (client,) = [client for client in worker.clients if client.name == "manager"]
    assert client.binding_only
    assert not client.operator
    assert client.projects == ("crypto",)
    assert not client.recovery_control
    token = (secrets / "runtime-manager/manager-worker.token").read_text()
    assert client.token_sha256 == token_digest(token)
    # The middleware reads whole listings for the direct path.
    assert load_middleware(secrets / "middleware/middleware.toml").mcp_listings
    # The manager binds through the worker, with the CA that signs the worker's certificate.
    manager = manager_settings.load(secrets / "runtime-manager/runtime-manager.json")
    assert manager.worker is not None
    assert manager.worker.url == f"https://{NAME}-worker.{NAMESPACE}.svc:8100"
    assert manager.worker.project == "crypto"
    assert str(manager.worker.token_file) == "/private/manager-worker.token"
    ca = x509.load_pem_x509_certificate((secrets / "runtime-manager/control-ca.crt").read_bytes())
    issued = x509.load_pem_x509_certificate((secrets / "worker/worker.crt").read_bytes())
    assert issued.issuer == ca.subject
    assert manager.mcp is not None
    assert manager.mcp.providers == ("crypto-research-mcp",)
    # The runtime's sandbox gets the server's endpoint and the middleware beside its services,
    # and its client the server's tool admission, both compiled from the site's policy.
    local = manager.model_copy(
        update={
            "mcp": manager.mcp.model_copy(
                update={
                    "policy_file": secrets / "runtime-manager/agent-mcp-policy.yaml",
                    "runtime_file": secrets / "runtime-manager/runtime-mcp.json",
                }
            ),
            "control_models_file": secrets / "runtime-manager/control-models.json",
        }
    )
    fragment = yaml.safe_load(local.direct_policy() or "")
    assert list(fragment["network_policies"]) == [f"mcp-{server}"]
    (middleware,) = fragment["network_middlewares"].values()
    assert middleware["config"]["mcp"] == {"project": "crypto", "caller": "agent-runtime"}
    runtime = json.loads(local.runtime_json(local.models(), "runtime-test"))
    assert runtime["mcp"]["project"] == "crypto"
    assert runtime["mcp"]["servers"][server]["tools"]["allow"]
    # The README says what the operator creates: the provider's Secret, by its file.
    readme = (output / "README.md").read_text()
    assert "kubectl -n research-test create secret generic test-mcp-provider" in readme
    assert "--from-env-file=<path>" in readme


def test_the_managers_worker_token_is_not_its_control_token(tmp_path):
    """Review of a2/direct-wiring (HIGH): control sees the manager's bearer token on every
    manager request, so the manager's worker token must be another value, and control's
    stored digest must not be the worker token's."""
    output = generate(tmp_path, direct=True)
    manager = output / "secrets/runtime-manager"
    control = (manager / "manager-control.token").read_text()
    worker = (manager / "manager-worker.token").read_text()
    assert control != worker
    stored = (output / "secrets/control/runtime-manager-token.sha256").read_text()
    assert stored == _digest(control)
    assert stored != _digest(worker)
    clients = load_worker(output / "secrets/worker/worker.toml").clients
    assert {client.token_sha256 for client in clients}.isdisjoint({_digest(control)})


def test_a_direct_path_for_another_project_is_refused(tmp_path):
    values = site_values(tmp_path, direct=True)
    bundle = tmp_path / "inputs/bundle.toml"
    document = tomllib.loads(bundle.read_text())
    document["mcp_direct"]["project"] = "elsewhere"
    bundle.write_text(toml.dumps(document))
    with pytest.raises(BundleError, match="not a site project"):
        plan(*load(site_file(tmp_path, values)))


def test_secrets_hold_exactly_the_projected_keys_and_everything_is_private(tmp_path):
    output = generate(tmp_path, cache=True)
    bundle = load_bundle(tmp_path / "inputs/bundle.toml")
    for role in REQUIRED_FILES:
        secret = getattr(bundle.secrets, role.replace("-", "_"))
        keys = {path.name for path in (output / "secrets" / role).iterdir()}
        assert keys == {*REQUIRED_FILES[role], *secret.extra_files}, role
    for path in (output, *output.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    assert (output / "secrets/control/s3-credentials").read_text() == "test-secret-s3"
    tree = output / "pool/worker-config/openshell"
    assert (tree / "active_gateway").read_bytes() == GATEWAY.encode()
    metadata = json.loads((tree / f"gateways/{GATEWAY}/metadata.json").read_text())
    assert metadata == {
        "name": GATEWAY,
        "gateway_endpoint": "https://openshell.openshell.svc.cluster.local:8080",
        "is_remote": False,
        "gateway_port": 0,
        "auth_mode": "mtls",
    }
    manager = output / "secrets/runtime-manager"
    for name in ("ca.crt", "tls.crt", "tls.key"):
        worker = (tmp_path / "inputs/worker" / name).read_bytes()
        assert (tree / f"gateways/{GATEWAY}/mtls/{name}").read_bytes() == worker
        own = (tmp_path / "inputs/manager" / name).read_bytes()
        assert (manager / f"gateway-{name}").read_bytes() == own != worker
    assert (manager / "active-gateway").read_bytes() == GATEWAY.encode()
    assert (manager / "model.key").read_text() == "test-secret-model-key"


def test_tokens_are_fresh_and_their_digests_match(tmp_path):
    output = generate(tmp_path)
    secrets = output / "secrets"
    token = (secrets / "runtime-manager/manager-control.token").read_bytes()
    assert len(token) == 43
    assert not token.endswith(b"\n")
    digest = (secrets / "control/runtime-manager-token.sha256").read_text()
    assert digest == hashlib.sha256(token).hexdigest()
    worker = load_worker(secrets / "worker/worker.toml")
    clients = {client.name: client for client in worker.clients}
    harness = (secrets / "control/worker-token").read_text()
    workspace = (secrets / "workspace/worker-token").read_text()
    assert clients["harness"].token_sha256 == token_digest(harness)
    assert clients["harness"].recovery_control
    assert clients["harness"].artifact_retention
    assert clients["workspace"].token_sha256 == token_digest(workspace)
    assert clients["workspace"].operator
    assert not clients["workspace"].artifact_retention
    pool = (secrets / "worker/pool-token").read_text()
    assert len({harness, workspace, pool, token.decode()}) == 4
    assert worker.model_copy(update={"token_file": secrets / "worker/pool-token"}).token() == pool
    again = tmp_path / "again"
    again.mkdir()
    write(again / "out", plan(*load(tmp_path / "site.toml")))
    assert (again / "out/secrets/worker/pool-token").read_text() != pool


def test_certificates_chain_to_the_private_ca_and_name_their_services(tmp_path):
    output = generate(tmp_path)
    secrets = output / "secrets"
    ca_pem = (secrets / "control/worker-ca.crt").read_bytes()
    for copy in (
        "workspace/worker-ca.crt",
        "runtime-manager/control-ca.crt",
    ):
        assert (secrets / copy).read_bytes() == ca_pem
    assert (output / "gateway/middleware-ca.crt").read_bytes() == ca_pem
    ca = x509.load_pem_x509_certificate(ca_pem)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    public = ca.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert public.curve.name == "secp256r1"
    for role in ("control", "worker", "middleware"):
        certificate = x509.load_pem_x509_certificate((secrets / f"{role}/{role}.crt").read_bytes())
        certificate.verify_directly_issued_by(ca)
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        host = f"{NAME}-{role}.{NAMESPACE}.svc"
        assert names.value.get_values_for_type(x509.DNSName) == [host, host + ".cluster.local"]
        assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        key = serialization.load_pem_private_key(
            (secrets / f"{role}/{role}.key").read_bytes(), password=None
        )
        assert key.public_key() == certificate.public_key()


def test_the_gateway_values_mount_the_ca_and_register_the_middleware(tmp_path):
    output = generate(tmp_path)
    assert not (output / "gateway/middleware-registration.toml").exists()
    values = yaml.safe_load((output / "gateway/openshell-values-middleware.yaml").read_text())
    volume = f"{NAME}-middleware-ca"
    directory = f"/etc/openshell-middleware/{NAME}"
    # Only the gateway's Pod mounts the CA: the gateway ships the PEM to the supervisors.
    assert values == {
        "server": {
            "extraVolumes": [{"name": volume, "configMap": {"name": volume}}],
            "extraVolumeMounts": [{"name": volume, "mountPath": directory, "readOnly": True}],
        },
        # The chart takes the literal dotted key; a nested map renders invalid TOML.
        "gatewayConfig": {
            "openshell.supervisor": {
                "middleware": [
                    {
                        "name": "sandbox-science-policy",
                        "grpc_endpoint": f"https://{NAME}-middleware.{NAMESPACE}.svc:50051",
                        "tls_ca_cert_path": f"{directory}/ca.crt",
                        "max_payload_bytes": 4194304,
                        "timeout": "500ms",
                    }
                ]
            }
        },
    }
    readme = (output / "README.md").read_text()
    assert "TODO-operator" not in readme + json.dumps(values)
    assert (
        f"kubectl -n openshell create configmap {volume} \\\n"
        "  --from-file=ca.crt=gateway/middleware-ca.crt"
    ) in readme
    assert "-f gateway/openshell-values-middleware.yaml" in readme
    assert "connects to\n   the middleware at startup" in readme


def test_a_cluster_domain_qualifies_every_name_a_sandbox_resolves(tmp_path):
    values = site_values(tmp_path, cache=True, domain="test-cluster.internal")
    values["model"]["url"] = "http://litellm.litellm.svc.test-cluster.internal:4000/v1"
    values["openshell"]["endpoint"] = "https://openshell.openshell.svc:8080"
    output = tmp_path / "out"
    write(output, plan(*load(site_file(tmp_path, values))))
    secrets = output / "secrets"
    qualified = f"{NAME}-cache.{NAMESPACE}.svc.test-cluster.internal"
    worker = load_worker(secrets / "worker/worker.toml")
    assert worker.openshell is not None
    assert worker.openshell.package_cache == f"http://{qualified}:8090"
    assert load_cache(secrets / "cache/cache.toml").public_url == f"http://{qualified}:8090"
    # Trusted Pods keep the short name their search domains complete.
    control = ControlSettings.model_validate_json((secrets / "control/control.json").read_text())
    assert control.worker == f"https://{NAME}-worker.{NAMESPACE}.svc:8100"
    for role in ("control", "worker", "middleware"):
        certificate = x509.load_pem_x509_certificate((secrets / f"{role}/{role}.crt").read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        host = f"{NAME}-{role}.{NAMESPACE}.svc"
        assert names.value.get_values_for_type(x509.DNSName) == [
            host,
            f"{host}.test-cluster.internal",
        ]
    assert "cluster.local" not in (secrets / "worker/worker.toml").read_text()
    # The agent runtime runs in a sandbox: its control URL and policy hosts are qualified.
    manager = manager_settings.load(secrets / "runtime-manager/runtime-manager.json")
    control_host = f"{NAME}-control.{NAMESPACE}.svc.test-cluster.internal"
    assert manager.control_url == f"https://{control_host}:8443"
    assert manager.manager_url == f"https://{control_host}:8444"
    policy = manager.policy
    assert policy.state_service is not None
    assert policy.model_gateway is not None
    assert policy.state_service.host == control_host
    assert policy.worker_api.host == f"{NAME}-worker.{NAMESPACE}.svc.test-cluster.internal"
    assert policy.model_gateway.host == "litellm.litellm.svc.test-cluster.internal"
    assert "cluster.local" not in (secrets / "runtime-manager/runtime-manager.json").read_text()


def test_the_targets_file_must_name_the_cache_by_its_fully_qualified_name(tmp_path):
    values = site_values(tmp_path, cache=True)
    targets = tmp_path / "inputs/targets.yaml"
    qualified = f"{NAME}-cache.{NAMESPACE}.svc.cluster.local"
    targets.write_text(targets.read_text().replace(qualified, f"{NAME}-cache.{NAMESPACE}.svc"))
    _refused(tmp_path, values, f"package cache at http://{qualified}:8090")


@pytest.mark.parametrize("domain", ["Cluster.Local", "cluster..local", "-cluster.local", ""])
def test_a_malformed_cluster_domain_is_refused(tmp_path, domain):
    _refused(tmp_path, site_values(tmp_path, domain=domain), "cluster_domain")


@pytest.mark.parametrize(
    "url", ["http://litellm.litellm.svc:4000/v1", "https://litellm.litellm.svc:4000/v1"]
)
def test_an_in_cluster_model_gateway_must_be_fully_qualified(tmp_path, url):
    values = site_values(tmp_path, domain="test-cluster.internal")
    values["model"]["url"] = url
    _refused(tmp_path, values, r"fully qualified <service>\.<namespace>\.svc\.test-cluster")


def test_an_http_model_gateway_must_be_in_the_sites_cluster_domain(tmp_path):
    values = site_values(tmp_path, domain="test-cluster.internal")
    values["model"]["url"] = "http://litellm.litellm.svc.other.internal:4000/v1"
    _refused(tmp_path, values, r"model\.url")


@pytest.mark.parametrize("cache", [{}, {"max_size": "10Gi"}])
def test_a_cache_that_enables_no_ecosystem_is_refused(tmp_path, cache):
    values = site_values(tmp_path, cache=True)
    values["cache"] = cache
    with pytest.raises(BundleError, match="enables no ecosystem") as raised:
        plan(*load(site_file(tmp_path, values)))
    assert "pypi, conda, cargo, go, npm, opam, hackage" in str(raised.value)


def test_the_cache_ecosystems_are_the_package_caches():
    fields = CacheSettings.model_fields
    assert set(CACHE_ECOSYSTEMS) <= fields.keys()
    assert all(fields[name].default is None for name in CACHE_ECOSYSTEMS)


def test_the_script_names_files_only_and_the_bundle_holds_no_generated_value(tmp_path):
    output = generate(tmp_path)
    script = (output / "secrets.sh").read_text()
    rendered = json.dumps(render(load_bundle(tmp_path / "inputs/bundle.toml")))
    for path in (output / "secrets").rglob("*"):
        if path.is_file():
            role, key = path.parent.name, path.name
            if key == "active-gateway":
                continue  # the gateway's name, which the bundle names too
            assert f"--from-file={key}=secrets/{role}/{key}" in script
            content = path.read_text()
            assert content not in script, path
            assert content not in rendered, path
    for secret in ("test-control", "test-workspace", "test-worker", "test-runtime-manager"):
        assert f"-n {NAMESPACE} create secret generic {secret}" in script
    readme = (output / "README.md").read_text()
    assert "TODO(operator)" in readme
    assert "sandbox-science-harness project create" in readme


def test_a_non_empty_output_directory_is_refused(tmp_path):
    files = plan(*load(site_file(tmp_path)))
    used = tmp_path / "used"
    used.mkdir()
    (used / "keep").write_text("kept")
    with pytest.raises(BundleError, match="nothing is overwritten"):
        write(used, files)
    assert [path.name for path in used.iterdir()] == ["keep"]
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o755)
    write(empty, files)
    assert stat.S_IMODE(empty.stat().st_mode) == 0o700
    with pytest.raises(BundleError):
        write(tmp_path / "site.toml", files)


def test_cli_generates_and_prints_no_secret(tmp_path, capsys):
    config = site_file(tmp_path)
    output = tmp_path / "out"
    assert main(["site", "--config", str(config), "--output", str(output)]) == 0
    said = capsys.readouterr()
    token = (output / "secrets/runtime-manager/manager-control.token").read_text()
    assert token not in said.out + said.err
    assert "README.md" in said.out
    assert main(["site", "--config", str(config), "--output", str(output)]) == 1
    assert "nothing is overwritten" in capsys.readouterr().err


def _refused(tmp_path: Path, values: dict[str, Any], match: str) -> None:
    with pytest.raises(BundleError, match=match):
        plan(*load(site_file(tmp_path, values)))


def test_mismatched_identity_and_open_keys_are_refused(tmp_path):
    values = site_values(tmp_path)
    other = identity(tmp_path / "other", "other")
    values["openshell"]["worker"]["key"] = other["key"]
    _refused(tmp_path, values, "worker identity's key")
    values["openshell"]["worker"]["key"] = values["openshell"]["manager"]["key"]
    values["openshell"]["worker"]["ca"] = other["ca"]
    _refused(tmp_path, values, "do not belong together")
    (tmp_path / "second").mkdir()
    values = site_values(tmp_path / "second")
    Path(values["model"]["api_key_file"]).chmod(0o644)
    _refused(tmp_path / "second", values, "open to other users")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda v: v["model"].update(url="http://gateway.example.org/v1"), r"model\.url"),
        (lambda v: v["model"].update(name="other"), "bundle's catalog"),
        (lambda v: v["model"]["api"].update({"test-main": "chat"}), "bundle's catalog"),
        (lambda v: v["profiles"]["minimal"].update(runtime_class_name="runc"), "profiles"),
        (
            lambda v: v["profiles"]["minimal"]["node_selector"].update(
                {"kubernetes.io/arch": "arm64"}
            ),
            "profiles",
        ),
        (lambda v: v["projects"][0].update(profile="unknown"), "profile is not one of"),
        (lambda v: v.update(cache={"pypi": {}}), r"\[cache\] exactly"),
        (lambda v: v["extra_files"]["control"].pop("s3-credentials"), "s3-credentials"),
        (lambda v: v["extra_files"].update(workspace={"x": "inputs/s3"}), "extra_files"),
        (lambda v: v["openshell"].update(registration="other"), "middleware registration"),
    ],
)
def test_inconsistent_sites_are_refused(tmp_path, change, match):
    values = site_values(tmp_path)
    change(values)
    _refused(tmp_path, values, match)


def test_the_bundle_must_project_what_the_worker_and_middleware_need(tmp_path):
    values = site_values(tmp_path)
    bundle = bundle_values()
    bundle["secrets"]["worker"]["extra_files"] = ["ceiling.yaml", "targets.yaml"]
    (tmp_path / "inputs/bundle.toml").write_text(toml.dumps(bundle))
    _refused(tmp_path, values, "pool-token")
    bundle["secrets"]["worker"]["extra_files"].append("pool-token")
    bundle["secrets"]["control"]["extra_files"] = ["pool-token"]
    (tmp_path / "inputs/bundle.toml").write_text(toml.dumps(bundle))
    _refused(tmp_path, values, "only the worker")


@pytest.mark.parametrize("label", [True, False])
def test_the_sites_worker_section_sets_label_environments(tmp_path, label):
    values = site_values(tmp_path)
    values["worker"] = {"label_environments": label}
    path = site_file(tmp_path, values)
    output = tmp_path / "out"
    write(output, plan(*load(path)))
    worker = load_worker(output / "secrets/worker/worker.toml")
    assert worker.openshell is not None
    assert worker.openshell.environments is not None
    assert worker.openshell.environments.label_environments is label


def test_the_worker_section_refuses_unknown_options(tmp_path):
    values = site_values(tmp_path)
    values["worker"] = {"label_environment": True}
    _refused(tmp_path, values, "worker")


def test_the_example_site_file_loads_once_its_placeholders_are_filled(tmp_path):
    example = tomllib.loads((ROOT / "examples/kubernetes/site.toml").read_text())
    values = site_values(tmp_path)
    for key in ("bundle", "worker_policy", "targets", "ceiling", "extra_files"):
        example[key] = values[key]
    example["model"]["api_key_file"] = values["model"]["api_key_file"]
    example["openshell"].update(
        {key: values["openshell"][key] for key in ("jwt_public_key", "manager", "worker")}
    )
    example["runtime"].update(image=values["runtime"]["image"], revision=REVISION)
    for name, profile in example["profiles"].items():
        profile["image"] = _image(name)
    site, _ = load(site_file(tmp_path, example))
    assert site.runtime.profile().runtime_class_name == "kata"


def test_only_a_site_may_name_an_http_cluster_gateway():
    from pydantic import ValidationError

    from sandbox_science.launcher.config import ModelConfig

    model = {
        "url": "http://litellm.litellm.svc.cluster.local:4000/v1",
        "name": "test-main",
        "api_key_file": "model.key",
    }
    with pytest.raises(ValidationError, match="in the clear"):
        ModelConfig.model_validate(model)
