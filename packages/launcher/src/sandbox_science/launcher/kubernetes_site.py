# SPDX-License-Identifier: MIT
"""Every Secret's contents for one bundle, generated to match each other
(docs/KUBERNETES-BUNDLE.md#generate-the-secrets).

``sandbox-science-kubernetes site`` writes, into a new private directory, one directory
per Secret of the bundle with exactly the keys its copy init projects (the required ones
and the bundle's ``extra_files``), the worker's OpenShell CLI tree for the pool PVC, the
middleware's CA and the gateway's Helm values that register it, a ``secrets.sh`` of
``kubectl create secret generic --from-file`` commands, and a README with what is left to
the operator.

It makes a private CA (ECDSA P-256), the control, worker and middleware Services'
server certificates, and a 32-byte random token for each link: control to the worker,
the workspace to the worker, the worker's own pool token, and the runtime manager to
control, whose SHA-256 control keeps. The CA's key is used once and never written, so
nothing can mint another certificate it signs: renewing means generating anew. The
settings come from the launcher's own builders and models where they exist
(``orchestrator``, ``parts``, ``RuntimeManagerSettings``); the services' settings models
are in their own packages, which the launcher never imports, so the tests load each
generated file through them. Nothing here reads kubeconfig or contacts a cluster.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import shlex
import stat
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION
from sandbox_science.policy import load_ceiling, load_policy
from sandbox_science.policy.errors import PolicyLoadError
from sandbox_science.policy.openshell import load_targets

from . import direct_mcp, toml
from .config import LauncherError, McpDirectServer, RunServiceConfig
from .kubernetes_bundle import (
    CONTROL_PORT,
    DATA,
    MANAGER,
    MANAGER_PORT,
    POD_NETWORK,
    PRIVATE,
    RUNTIME_CREDENTIALS,
)
from .kubernetes_checkpoint import storage_settings
from .kubernetes_config import (
    CHECK_DIGEST,
    CHECK_TOKEN,
    CHECKPOINT_UPLOAD_KEYS,
    FORWARD_AUTH_SECRET,
    GATEWAY_PROFILE,
    BundleConfig,
    BundleError,
    Secret,
    projected_files,
)
from .kubernetes_site_config import Identity, Site
from .orchestrator import checks, control_models, run_settings, runtime_target
from .parts import worker_client
from .runtime_manager.settings import ManagerMcp, RuntimeManagerSettings, WorkerLink

WORKER_PORT = 8100
MIDDLEWARE_PORT = 50051
CACHE_PORT = 8090
NODE_KERNELS = ("*-talos",)
"""The cluster's Talos nodes: a sandbox that reports one of their kernels shares it."""
TRUSTED_NODE = "/usr/local/bin/node"
TRUSTED_MATH = Path("/usr/share/sandbox-science/math")
"""Where the ``trusted-app`` image keeps Node and the math checker (images/trusted)."""
UNISSUED = "unissued"
"""Control's placeholder generation, never issued: control mints each one (KRB-6)."""
WORKER_CONFIG = f"{DATA}/worker-config"
"""The worker's OpenShell CLI tree on the pool PVC (``XDG_CONFIG_HOME``)."""
SHARED = ("ceiling.yaml", "targets.yaml", "gateway-public.pem")
"""Nonsecret site files any service's ``extra_files`` may name."""
POOL_TOKEN = "pool-token"  # noqa: S105 - a Secret key's name, not a token
MANAGER_CLIENT = "manager"
"""The runtime manager's worker client on the direct MCP path, which may only bind and unbind
the runtime's sandbox for one project (``binding_only``)."""
MANAGER_WORKER_TOKEN = "manager-worker"  # noqa: S105 - a token's key, not a token
"""The key of that client's token among the generated tokens: never ``manager``, the
manager's bearer token for control, which control sees on every manager request."""
GATEWAY_VALUES = "gateway/openshell-values-middleware.yaml"
"""The OpenShell chart's values that register the middleware (``_gateway_values``)."""
INSPECT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "argument_bytes": 16 * 1024,
    "answer_bytes": 64 * 1024,
    "run_bytes": 8 * 1024 * 1024,
    "defaults": True,
}
"""The middleware's ``[inspect]`` defaults (``policy_middleware.settings.InspectSettings``,
which the launcher never imports; a test compares them)."""
_INPUT_BYTES = 64 * 1024
_CA_DAYS, _LEAF_DAYS = 3650, 1095


def _read(path: Path, *, private: bool = False) -> bytes:
    """A small regular input file; a private one must be closed to other users."""
    try:
        found = path.stat()
        if not stat.S_ISREG(found.st_mode) or found.st_size > _INPUT_BYTES:
            raise BundleError(f"{path} is not a regular file of at most 64 KiB")
        if private and found.st_mode & 0o077:
            raise BundleError(f"{path} is open to other users: chmod 600 {path}")
        return path.read_bytes()
    except OSError as exc:
        raise BundleError(f"cannot read {path}: {exc.strerror}") from None


def _token() -> str:
    """32 random bytes, URL-safe, without a newline, so a digest is of the exact file."""
    return secrets.token_urlsafe(32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Authority:
    """The site's private CA: it signs the Services' certificates, then is forgotten."""

    def __init__(self, name: str) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} private CA")])
        public = self._key.public_key()
        self.certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(public)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=_CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(_usage(cert_sign=True), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
            .sign(self._key, hashes.SHA256())
        )

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    def issue(self, names: list[str]) -> tuple[bytes, bytes]:
        """A server certificate for ``names`` (DNS SANs) and its key, both PEM."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        issuer = self.certificate.subject
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=_LEAF_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_usage(cert_sign=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def _usage(*, cert_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not cert_sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def _identity(identity: Identity, what: str) -> dict[str, bytes]:
    """An OpenShell client identity's three PEM files, checked to belong together: the
    certificate signed by the CA, the key its own."""
    files = {
        "ca.crt": _read(identity.ca),
        "tls.crt": _read(identity.certificate),
        "tls.key": _read(identity.key, private=True),
    }
    try:
        ca = x509.load_pem_x509_certificate(files["ca.crt"])
        certificate = x509.load_pem_x509_certificate(files["tls.crt"])
        key = serialization.load_pem_private_key(files["tls.key"], password=None)
        certificate.verify_directly_issued_by(ca)
        matches = key.public_key() == certificate.public_key()
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise BundleError(f"the {what} identity's PEM files do not belong together") from exc
    if not matches:
        raise BundleError(f"the {what} identity's key is not its certificate's")
    return files


def _cli_tree(gateway: str, endpoint: str, identity: dict[str, bytes]) -> dict[str, bytes]:
    """The OpenShell 0.1.5 CLI's profile tree, as ``openshell gateway add`` writes it
    (``gateway_port`` 0 when the endpoint's URL carries the port)."""
    metadata = {
        "name": gateway,
        "gateway_endpoint": endpoint,
        "is_remote": False,
        "gateway_port": 0,
        "auth_mode": "mtls",
    }
    return {
        "active_gateway": gateway.encode(),
        "metadata.json": (json.dumps(metadata) + "\n").encode(),
        **{f"mtls/{name}": content for name, content in identity.items()},
    }


def _host(bundle: BundleConfig, role: str) -> str:
    """A Service's name as trusted Pods use it, completed by their search domains."""
    return f"{bundle.name}-{role}.{bundle.namespace}.svc"


def _qualified(site: Site, bundle: BundleConfig, role: str) -> str:
    """A Service's fully qualified name: the only form a sandbox resolves, since
    OpenShell's sandbox DNS ignores the Pod's search domains."""
    return f"{_host(bundle, role)}.{site.cluster_domain}"


def _tls(role: str) -> dict[str, str]:
    return {"certificate": f"{PRIVATE}/{role}.crt", "private_key": f"{PRIVATE}/{role}.key"}


def control_settings(site: Site, bundle: BundleConfig) -> dict[str, Any]:
    """``control.json`` with the bundle's cluster bindings, and with ``[mcp_direct]`` the
    run service's connectors on the direct path (D98)."""
    direct = bundle.mcp_direct
    connectors = (
        {
            "direct": {
                "project": direct.project,
                "servers": list(direct.servers),
                "about": dict(direct.about),
                "index": list(direct.index) if direct.index is not None else None,
            },
            # The workspace's token for the user's availability check (D98).
            "check_token_sha256_file": f"{PRIVATE}/{CHECK_DIGEST}",
            # Control accepts the availability check's listing report, and lists the servers
            # on a schedule, only with canaries, as the host launcher writes them.
            **(
                {
                    "canaries": {
                        "project": direct.project,
                        "servers": list(direct.servers),
                        "interval": direct.canaries.interval,
                    }
                }
                if direct.canaries.enabled
                else {}
            ),
        }
        if direct is not None
        else {}
    )
    return {
        **connectors,
        "state_dir": f"{DATA}/control",
        "listen": f"{POD_NETWORK}:{CONTROL_PORT}",
        "tls": _tls("control"),
        "worker": f"https://{_host(bundle, 'worker')}:{WORKER_PORT}",
        "worker_token_file": f"{PRIVATE}/worker-token",
        "worker_ca_file": f"{PRIVATE}/worker-ca.crt",
        "runtime_token_file": f"{RUNTIME_CREDENTIALS}/runtime.token",
        "generation": UNISSUED,
        "generation_file": f"{DATA}/control/runtime-generation",
        "projects": [project.id for project in site.projects],
        **control_models(site.model, site.skills, site.runtime.revision),
        "settings": run_settings(
            site.model, RunServiceConfig(), site.runtime.recover_infrastructure
        ),
        "checks": checks(TRUSTED_NODE, TRUSTED_MATH),
        "manager_listen": f"{POD_NETWORK}:{MANAGER_PORT}",
        "manager_token_sha256_file": f"{PRIVATE}/runtime-manager-token.sha256",
    }


def _cache_url(site: Site, bundle: BundleConfig) -> str:
    """The package cache as sandboxes reach it."""
    return f"http://{_qualified(site, bundle, 'cache')}:{CACHE_PORT}"


def _admin_hosts(site: Site) -> list[str]:
    return list(dict.fromkeys((site.openshell.host, *site.openshell.admin_hosts)))


def worker_settings(site: Site, bundle: BundleConfig, digests: dict[str, str]) -> dict[str, Any]:
    """``worker.toml``: tier 3 under Kata on amd64, the clients of control and the
    workspace, and the CLI tree on the pool PVC; with ``[mcp_direct]`` also the runtime
    manager's, an operator client of the direct path's project alone, which binds the
    runtime's sandbox (D98)."""
    projects = [project.id for project in site.projects]
    openshell: dict[str, Any] = {
        "isolation_tier": 3,
        "node_kernel_releases": list(NODE_KERNELS),
        "env": {"XDG_CONFIG_HOME": WORKER_CONFIG},
        "environments": {
            "policy_store": f"{DATA}/middleware",
            "targets": f"{PRIVATE}/targets.yaml",
            "ceiling": f"{PRIVATE}/ceiling.yaml",
            "admin_hosts": _admin_hosts(site),
            "label_environments": site.worker.label_environments,
            "profiles": {name: profile.image for name, profile in site.profiles.items()},
            "kubernetes": {
                name: profile.profile().model_dump(mode="json")
                for name, profile in site.profiles.items()
            },
        },
    }
    if bundle.secrets.cache is not None:
        openshell["package_cache"] = _cache_url(site, bundle)
    return {
        "listen": f"{POD_NETWORK}:{WORKER_PORT}",
        "tls": _tls("worker"),
        "token_file": f"{PRIVATE}/{POOL_TOKEN}",
        "state_dir": f"{DATA}/worker",
        "pool_id": site.pool_id or bundle.name,
        "openshell": openshell,
        "clients": [
            worker_client(
                name,
                digests[name],
                [bundle.mcp_direct.project]
                if name == MANAGER_CLIENT and bundle.mcp_direct is not None
                else projects,
            )
            for name in digests
        ],
    }


def middleware_settings(site: Site, bundle: BundleConfig) -> dict[str, Any]:
    """``middleware.toml``: TLS for the gateway's registration and its tokens verified; with
    ``[mcp_direct]``, the listings read and MCP traffic inspection at its defaults."""
    tokens: dict[str, str] = {
        "gateway_id": site.openshell.gateway_id,
        "public_key": f"{PRIVATE}/gateway-public.pem",
    }
    if site.openshell.audience is not None:
        tokens["audience"] = site.openshell.audience
    settings: dict[str, Any] = {
        "registration": site.openshell.registration,
        "listen": f"{POD_NETWORK}:{MIDDLEWARE_PORT}",
        "state_dir": f"{DATA}/middleware",
        "ceiling": f"{PRIVATE}/ceiling.yaml",
        "admin_hosts": _admin_hosts(site),
        "tls": _tls("middleware"),
        "tokens": tokens,
    }
    if bundle.secrets.cache is not None:
        settings["package_cache_url"] = f"http://127.0.0.1:{CACHE_PORT}"  # the same Pod
    if bundle.mcp_direct is not None:
        # The direct path's listings: read whole when OpenShell offers them (D98).
        settings["mcp_listings"] = True
        # MCP traffic inspection of the direct path, written out at the middleware's own
        # defaults (InspectSettings) so that the operator sees and can change them.
        settings["inspect"] = dict(INSPECT_DEFAULTS)
    return settings


def cache_settings(site: Site, bundle: BundleConfig) -> dict[str, Any]:
    return {
        **(site.cache or {}),
        "listen": f"{POD_NETWORK}:{CACHE_PORT}",
        "public_url": _cache_url(site, bundle),
        "cache_dir": f"{DATA}/package-cache",
        # The worker's state on the same PVC: files its environments' current and
        # known-good lockfiles pin are protected from eviction, up to a ceiling
        # (docs/PACKAGE-CACHE.md, "Storage and eviction").
        "worker_state_dir": f"{DATA}/worker",
    }


def _direct_servers(bundle: BundleConfig) -> dict[str, McpDirectServer]:
    direct = bundle.mcp_direct
    if direct is None:
        return {}
    return {
        name: McpDirectServer(credentials=dict(server.credentials))
        for name, server in direct.servers.items()
    }


def direct_egress(site: Site, bundle: BundleConfig) -> direct_mcp.DirectEgress | None:
    """The runtime sandbox's egress to the ``[mcp_direct]`` servers (D98), compiled from
    the site's policy, ceiling and targets as ``sandbox-science up`` compiles it, to refuse
    a bundle whose files do not compile. The runtime manager compiles it again at each
    creation of the runtime's sandbox from the same files, which its Secret carries, with
    the project's classification revisions in force in the cluster (``runtime_manager.fence``)."""
    direct = bundle.mcp_direct
    if direct is None:
        return None
    if direct.project not in {project.id for project in site.projects}:
        raise BundleError(f"mcp_direct.project {direct.project!r} is not a site project")
    try:
        return direct_mcp.compile_direct(
            policy=site.worker_policy,
            ceiling=site.ceiling,
            targets=site.targets,
            project=direct.project,
            servers=_direct_servers(bundle),
            python=RuntimeManagerSettings.model_fields["python"].default,
        )
    except LauncherError as exc:
        raise BundleError(str(exc)) from None


def manager_settings(site: Site, bundle: BundleConfig) -> RuntimeManagerSettings:
    """``runtime-manager.json``, through the manager's own model. The agent runtime runs
    in an OpenShell sandbox, so its control URL and its policy's hosts are fully
    qualified; the manager's own URL names the same host, which the model requires and
    which resolves from the manager's Pod as well. With ``[mcp_direct]``, the manager gets
    its worker link, which binds the runtime's sandbox, and the direct path's files."""
    control = f"https://{_qualified(site, bundle, 'control')}"
    direct = bundle.mcp_direct
    link = (
        WorkerLink(
            url=f"https://{_host(bundle, 'worker')}:{WORKER_PORT}",
            ca_file=Path(PRIVATE, "control-ca.crt"),  # the private CA signs both
            token_file=Path(PRIVATE, "manager-worker.token"),
            project=direct.project,
        )
        if direct is not None
        else None
    )
    mcp = (
        ManagerMcp(
            project_policy_file=Path(PRIVATE, "agent-mcp-project-policy.yaml"),
            ceiling_file=Path(PRIVATE, "agent-mcp-ceiling.yaml"),
            targets_file=Path(PRIVATE, "agent-mcp-targets.yaml"),
            servers=_direct_servers(bundle),
            providers=(direct.provider,),
        )
        if direct is not None
        else None
    )
    return RuntimeManagerSettings(
        worker=link,
        mcp=mcp,
        control_url=f"{control}:{CONTROL_PORT}",
        manager_url=f"{control}:{MANAGER_PORT}",
        control_ca_file=Path(PRIVATE, "control-ca.crt"),
        manager_token_file=Path(PRIVATE, "manager-control.token"),
        control_models_file=Path(PRIVATE, "control-models.json"),
        model_url=site.model.url,
        model_key_file=Path(PRIVATE, "model.key"),
        thinking_replay=site.model.thinking_replay,
        image=site.runtime.image,
        revision=site.runtime.revision,
        # worker_api is required by the target, though the runtime never calls the
        # worker and the pool's NetworkPolicy admits only the app Pod (README).
        policy=runtime_target(
            worker=f"https://{_qualified(site, bundle, 'worker')}:{WORKER_PORT}",
            model=site.model.url,
            control=f"{control}:{CONTROL_PORT}",
            python=RuntimeManagerSettings.model_fields["python"].default,
            relay_tls=True,
        ),
        runtime=site.runtime.profile(),
        recover_infrastructure=site.runtime.recover_infrastructure,
    )


def _check_inputs(site: Site, bundle: BundleConfig) -> dict[str, bytes]:
    """The shared site files, parsed by the policy library before they are copied."""
    try:
        load_policy(site.worker_policy)
        load_ceiling(site.ceiling)
        targets = load_targets(site.targets)
    except PolicyLoadError as exc:
        raise BundleError(f"a site policy file is invalid: {exc}") from None
    if targets.sandbox.middleware != site.openshell.registration:
        raise BundleError("the targets file names another middleware registration")
    cache = (targets.sandbox.package_cache.host, targets.sandbox.package_cache.port)
    if bundle.secrets.cache is not None and cache != (
        _qualified(site, bundle, "cache"),
        CACHE_PORT,
    ):
        raise BundleError(
            f"the targets file must put the package cache at {_cache_url(site, bundle)}"
        )
    if (site.cache is None) != (bundle.secrets.cache is None):
        raise BundleError("give [cache] exactly when the bundle enables the package cache")
    return {
        "ceiling.yaml": _read(site.ceiling),
        "targets.yaml": _read(site.targets),
        "gateway-public.pem": _read(site.openshell.jwt_public_key),
    }


MIN_CLIENT_SECRET = 32
"""The shortest client secret taken, as the workspace's own check (an HMAC key)."""


def _forward_auth_secret(site: Site, bundle: BundleConfig) -> dict[str, bytes]:
    """The workspace Secret's client secret, with the bundle's HS256 forward auth: from the
    site's private file, checked, never printed."""
    ingress = bundle.ingress
    forward = ingress.forward_auth if ingress is not None else None
    wanted = forward is not None and forward.shared_secret
    path = site.forward_auth_secret
    if not wanted:
        if path is not None:
            raise BundleError(
                "forward_auth_secret is for the bundle's HS256 [ingress.forward_auth] only"
            )
        return {}
    if path is None:
        raise BundleError(
            "the bundle's forward auth is HS256: set forward_auth_secret in the site file to "
            "the Authentik provider's client secret, a file of mode 600 such as "
            "secure/authentik-forward-auth-client-secret (docs/KUBERNETES-BUNDLE.md)"
        )
    if not path.exists():
        raise BundleError(
            f"{path} is missing: write the Authentik provider's client secret there "
            "(docs/KUBERNETES-BUNDLE.md#login-through-authentik-opt-in)"
        )
    value = _read(path, private=True).rstrip(b"\r\n")
    if len(value) < MIN_CLIENT_SECRET:
        raise BundleError(f"{path} holds no client secret of at least {MIN_CLIENT_SECRET} bytes")
    return {FORWARD_AUTH_SECRET: value}


def plan(site: Site, bundle: BundleConfig) -> dict[str, bytes]:
    """Every file to write, by its path in the output directory; nothing is written."""
    shared = _check_inputs(site, bundle)
    manager_identity = _identity(site.openshell.manager, "manager")
    worker_identity = _identity(site.openshell.worker, "worker")
    authority = Authority(f"{bundle.name}.{bundle.namespace}")
    certificates = {
        role: authority.issue([_host(bundle, role), _qualified(site, bundle, role)])
        for role in ("control", "worker", "middleware")
    }
    tokens = {name: _token() for name in ("harness", "workspace", "pool", "manager")}
    digests = {name: _digest(tokens[name]) for name in ("harness", "workspace")}
    egress = direct_egress(site, bundle)
    check: dict[str, dict[str, bytes]] = {"control": {}, "workspace": {}}
    if egress is not None:
        tokens[MANAGER_WORKER_TOKEN] = _token()
        digests[MANAGER_CLIENT] = _digest(tokens[MANAGER_WORKER_TOKEN])
        token = _token()
        check = {
            "control": {CHECK_DIGEST: _digest(token).encode()},
            "workspace": {CHECK_TOKEN: token.encode()},
        }
    models = control_models(site.model, site.skills, site.runtime.revision)
    gateway = bundle.runtime_manager.openshell_gateway
    profile = _cli_tree(gateway, site.openshell.endpoint, manager_identity)
    content: dict[str, dict[str, bytes]] = {
        "control": {
            "control.json": _json(control_settings(site, bundle)),
            "runtime-manager-token.sha256": _digest(tokens["manager"]).encode(),
            "worker-token": tokens["harness"].encode(),
            "control.crt": certificates["control"][0],
            "control.key": certificates["control"][1],
            "worker-ca.crt": authority.pem,
            **check["control"],
        },
        "workspace": {
            "worker-token": tokens["workspace"].encode(),
            "worker-ca.crt": authority.pem,
            **check["workspace"],
            **_forward_auth_secret(site, bundle),
        },
        "worker": {
            "worker.toml": toml.dumps(worker_settings(site, bundle, digests)).encode(),
            "worker.crt": certificates["worker"][0],
            "worker.key": certificates["worker"][1],
            POOL_TOKEN: tokens["pool"].encode(),
        },
        "middleware": {
            "middleware.toml": toml.dumps(middleware_settings(site, bundle)).encode(),
            "middleware.crt": certificates["middleware"][0],
            "middleware.key": certificates["middleware"][1],
        },
        MANAGER: {
            "runtime-manager.json": _json(manager_settings(site, bundle).model_dump(mode="json")),
            "control-models.json": _json(models),
            "manager-control.token": tokens["manager"].encode(),
            "model.key": _read(site.model.api_key_file, private=True),
            "control-ca.crt": authority.pem,
            **{key: profile[place] for key, place in GATEWAY_PROFILE.items()},
            **(
                {
                    "manager-worker.token": tokens[MANAGER_WORKER_TOKEN].encode(),
                    "agent-mcp-project-policy.yaml": _read(site.worker_policy),
                    "agent-mcp-ceiling.yaml": _read(site.ceiling),
                    "agent-mcp-targets.yaml": _read(site.targets),
                }
                if egress is not None
                else {}
            ),
        },
    }
    if bundle.secrets.cache is not None:
        content["cache"] = {"cache.toml": toml.dumps(cache_settings(site, bundle)).encode()}
    if (site.checkpoint is None) != (bundle.checkpoint is None):
        raise BundleError("give [checkpoint] exactly when the bundle has [checkpoint]")
    upload: dict[str, bytes] = {}
    restore: dict[str, bytes] = {}
    if site.checkpoint is not None and bundle.checkpoint is not None:
        content["worker"].update(checkpoint_worker_files(site, bundle))
        upload = {
            "credentials": _read(site.checkpoint.upload_credentials, private=True),
            "config": _read(site.checkpoint.upload_config, private=True),
        }
        given = (site.checkpoint.restore_credentials, site.checkpoint.restore_config)
        wanted = bundle.checkpoint.restore_secret is not None
        if any((path is not None) != wanted for path in given):
            raise BundleError(
                "give checkpoint.restore_credentials and restore_config exactly when the"
                " bundle's [checkpoint] names restore_secret"
            )
        if given[0] is not None and given[1] is not None:
            restore = {
                "credentials": _read(given[0], private=True),
                "config": _read(given[1], private=True),
            }
    if site.extra_files.keys() - content.keys():
        raise BundleError("extra_files names a service the bundle does not run")
    files: dict[str, bytes] = {}
    for role, keys in content.items():
        names = _secret(bundle, role).extra_files
        keys.update(_extras(site, role, names, keys, shared))
        if keys.keys() != {*projected_files(bundle, role), *names}:
            raise BundleError(f"secrets.{role}: the generated keys differ from the projected ones")
        files.update({f"secrets/{role}/{key}": value for key, value in keys.items()})
    worker_tree = _cli_tree(gateway, site.openshell.endpoint, worker_identity)
    files.update(
        {
            f"pool/worker-config/openshell/{_tree_path(gateway, name)}": value
            for name, value in worker_tree.items()
        }
    )
    files["gateway/middleware-ca.crt"] = authority.pem
    files[GATEWAY_VALUES] = _gateway_values(site, bundle).encode()
    files["bootstrap/policy.yaml"] = _read(site.worker_policy)
    files.update({f"secrets/checkpoint-upload/{key}": value for key, value in upload.items()})
    files.update({f"secrets/checkpoint-restore/{key}": value for key, value in restore.items()})
    files.update(keeper_files(site, bundle))
    files["secrets.sh"] = _script(bundle, content).encode()
    files["README.md"] = _readme(site, bundle).encode()
    return files


def checkpoint_worker_files(site: Site, bundle: BundleConfig) -> dict[str, bytes]:
    """The worker's checkpoint keys: both buckets' destinations and its read-only key."""
    checkpoint = bundle.checkpoint
    if checkpoint is None or site.checkpoint is None:
        raise BundleError("give [checkpoint] exactly when the bundle has [checkpoint]")
    return {
        "checkpoint-upload.toml": toml.dumps(
            storage_settings(checkpoint, checkpoint.upload_bucket)
        ).encode(),
        "checkpoint-kept.toml": toml.dumps(
            storage_settings(checkpoint, checkpoint.kept_bucket)
        ).encode(),
        "checkpoint-s3-credentials": _read(site.checkpoint.verify_credentials, private=True),
        "checkpoint-s3-config": _read(site.checkpoint.verify_config, private=True),
    }


def keeper_files(site: Site, bundle: BundleConfig) -> dict[str, bytes]:
    """The operator's storage files for promotion and deletion, with the keeper key's
    endpoint: no credential, which stays in the operator's own SDK profile."""
    checkpoint = bundle.checkpoint
    if checkpoint is None or site.checkpoint is None:
        return {}
    endpoint = site.checkpoint.keeper_endpoint or (
        f"http://127.0.0.1:{checkpoint.object_store.ports[0]}"
    )
    files: dict[str, bytes] = {}
    for name, bucket in (("upload", checkpoint.upload_bucket), ("kept", checkpoint.kept_bucket)):
        settings = {**storage_settings(checkpoint, bucket), "endpoint": endpoint}
        files[f"operator/checkpoint-{name}.toml"] = toml.dumps(settings).encode()
    return files


def _secret(bundle: BundleConfig, role: str) -> Secret:
    secret = getattr(bundle.secrets, role.replace("-", "_"))
    if not isinstance(secret, Secret):
        raise BundleError(f"the bundle has no Secret for {role}")
    return secret


def _tree_path(gateway: str, name: str) -> str:
    return name if name == "active_gateway" else f"gateways/{gateway}/{name}"


def _extras(
    site: Site,
    role: str,
    names: tuple[str, ...],
    generated: dict[str, bytes],
    shared: dict[str, bytes],
) -> dict[str, bytes]:
    """The bundle's ``extra_files`` for ``role``: generated, a shared site file, or a
    file the site names; any other is refused, as is the pool token outside the worker."""
    if role == "worker" and POOL_TOKEN not in names:
        raise BundleError(f"secrets.worker.extra_files must name {POOL_TOKEN}")
    if role != "worker" and POOL_TOKEN in names:
        raise BundleError("only the worker holds the pool token")
    given = {str(key): value for key, value in site.extra_files.items()}.get(role, {})
    if given.keys() - set(names):
        raise BundleError(f"extra_files.{role} names keys the bundle does not project")
    result: dict[str, bytes] = {}
    for name in names:
        if name in generated:
            continue
        if name in given:
            result[name] = _read(given[name], private=True)
        elif name in shared:
            result[name] = shared[name]
        else:
            raise BundleError(f"secrets.{role} projects {name}: name its file in extra_files")
    needed = {
        "worker": ("ceiling.yaml", "targets.yaml"),
        "middleware": ("ceiling.yaml", "gateway-public.pem"),
    }
    if missing := [name for name in needed.get(role, ()) if name not in names]:
        raise BundleError(f"secrets.{role}.extra_files must name {', '.join(missing)}")
    return result


def _json(document: object) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _gateway_values(site: Site, bundle: BundleConfig) -> str:
    """The OpenShell chart's values that register the middleware with the gateway. The
    gateway reads ``tls_ca_cert_path`` itself and ships the PEM to the supervisors inside
    their configuration (OpenShell 0.1.5 ``openshell-server/src/config_file.rs``), so only
    the gateway's Pod mounts the CA, from a ConfigMap. The chart takes literal dotted keys
    under ``gatewayConfig``; a nested ``openshell:`` map renders invalid TOML. Every value
    is a validated name, so none needs quoting."""
    volume = f"{bundle.name}-middleware-ca"
    directory = f"/etc/openshell-middleware/{bundle.name}"
    endpoint = f"https://{_host(bundle, 'middleware')}:{MIDDLEWARE_PORT}"
    return f"""# Registers {bundle.name}'s policy middleware with the OpenShell gateway: pass it to
# helm after the site's own values (-f openshell-values.yaml -f <this file>). Helm does
# not merge lists: merge these into any server.extraVolumes or extraVolumeMounts the
# site's values already set.
server:
  extraVolumes:
    - name: {volume}
      configMap: {{name: {volume}}}
  extraVolumeMounts:
    - name: {volume}
      mountPath: {directory}
      readOnly: true
gatewayConfig:
  "openshell.supervisor":
    middleware:
      - name: {site.openshell.registration}
        grpc_endpoint: {endpoint}
        tls_ca_cert_path: {directory}/ca.crt
        max_payload_bytes: {4 * 1024 * 1024}
        timeout: 500ms
"""


def _gateway_namespace(site: Site) -> str:
    """The gateway's namespace when its endpoint is a Service name, else a placeholder."""
    labels = site.openshell.host.split(".")
    return labels[1] if len(labels) >= 3 and labels[2] == "svc" else "OPENSHELL_NAMESPACE"


def _script(bundle: BundleConfig, content: dict[str, dict[str, bytes]]) -> str:
    """The commands that make the Secrets from these files; no value is in them."""
    lines = [
        "#!/bin/sh",
        "# Creates the bundle's Secrets from the files beside this script (sh secrets.sh).",
        "set -eu",
        'cd "$(dirname "$0")"',
    ]
    for role, keys in content.items():
        secret = _secret(bundle, role)
        command = ["kubectl", "-n", bundle.namespace, "create", "secret", "generic", secret.name]
        files = [f"--from-file={key}=secrets/{role}/{key}" for key in sorted(keys)]
        lines.append(
            " \\\n  ".join(
                [" ".join(shlex.quote(part) for part in command), *map(shlex.quote, files)]
            )
        )
    if bundle.checkpoint is not None and bundle.sandboxes is not None:
        # The upload key lives beside the volumes, where only checkpoint Jobs mount it; the
        # restore key, if any, where only restore Jobs do.
        namespace = bundle.sandboxes.namespace
        for name, directory in (
            (bundle.checkpoint.upload_secret, "checkpoint-upload"),
            (bundle.checkpoint.restore_secret, "checkpoint-restore"),
        ):
            if name is None:
                continue
            command = ["kubectl", "-n", namespace, "create", "secret", "generic", name]
            files = [
                f"--from-file={key}=secrets/{directory}/{key}" for key in CHECKPOINT_UPLOAD_KEYS
            ]
            lines.append(
                " \\\n  ".join(
                    [" ".join(shlex.quote(part) for part in command), *map(shlex.quote, files)]
                )
            )
    return "\n".join(lines) + "\n"


def _readme(site: Site, bundle: BundleConfig) -> str:
    """What was generated and what is the operator's, with every ``TODO(operator)``."""
    ns, name = bundle.namespace, bundle.name
    gateway = bundle.runtime_manager.openshell_gateway
    gateway_ns = _gateway_namespace(site)
    projects = "".join(
        f"kubectl -n {ns} exec deploy/{name}-app -c control -- sandbox-science-harness "
        f"project create --state-dir {DATA}/control --project {project.id} "
        f"--profile {project.profile} --policy POLICY_DIGEST\n"
        + "".join(
            f"kubectl -n {ns} exec deploy/{name}-app -c control -- sandbox-science-harness "
            f"session open --state-dir {DATA}/control --project {project.id} "
            f"--session {session.id}{' --dedicated' if session.dedicated else ''}\n"
            for session in project.sessions
        )
        + (
            f"kubectl -n {ns} exec deploy/{name}-app -c control -- sandbox-science-harness "
            f"project budget --state-dir {DATA}/control --project {project.id} "
            f"--daily-cost-limit {limit:g}\n"
            if (limit := site.model.daily_cost_limit) is not None
            else ""
        )
        for project in site.projects
    )
    granted = " ".join(f"--project {project.id}" for project in site.projects)
    fence = "```"
    plain = (
        f"- The model gateway is `{site.model.url}`: over `http` the manager's model key\n"
        "  crosses the Pod network unencrypted.\n"
        if site.model.url.startswith("http:")
        else ""
    )
    checkpoints = (
        "- `secrets/checkpoint-upload/`: the checkpoint Jobs' upload key, for the Secret\n"
        f"  `{bundle.checkpoint.upload_secret}` in the sandbox namespace (`secrets.sh` makes it).\n"
        "- `operator/checkpoint-*.toml`: the buckets as the operator's machine reaches them\n"
        "  (through `kubectl port-forward`), for `checkpoint --apply` and `cleanup --apply`\n"
        "  with the keeper key's SDK profile (docs/KUBERNETES-BUNDLE.md#workspace-checkpoints).\n"
        + (
            "- `secrets/checkpoint-restore/`: the restore Jobs' key (read-only on the kept\n"
            f"  bucket), for the Secret `{bundle.checkpoint.restore_secret}` in the sandbox\n"
            "  namespace (`secrets.sh` makes it; delete it between restores if you prefer).\n"
            if bundle.checkpoint.restore_secret is not None
            else ""
        )
        if bundle.checkpoint is not None
        else ""
    )
    direct = bundle.mcp_direct
    mcp = (
        "The agent's connector calls take the direct path (`[mcp_direct]`, D98) for\n"
        f"  project `{direct.project}`:\n"
        f"  before the bundle's manager starts, create the Secret `{direct.provider_secret}`\n"
        "  from the provider's env file (`CF_ACCESS_CLIENT_ID=` and\n"
        "  `CF_ACCESS_CLIENT_SECRET=` lines), which its init container registers as the\n"
        f"  provider `{direct.provider}`, and import the `cloudflare-access` provider\n"
        "  profile once (docs/KUBERNETES-PREP.md):\n\n"
        f"  {fence}sh\n"
        f"  kubectl -n {ns} create secret generic {direct.provider_secret} \\\n"
        "    --from-env-file=<path>\n"
        f"  {fence}\n\n"
        "  The middleware's `mcp_listings` is on. The site's policy was compiled into the\n"
        "  manager's Secret: after a new policy revision, generate the Secrets anew.\n"
        if direct is not None
        else "No MCP servers are configured (`[mcp_direct]`): the agent has no connectors.\n"
    )
    ingress = bundle.ingress
    forward = ingress.forward_auth if ingress is not None else None
    hs256 = forward is not None and forward.shared_secret
    login = (
        "- Login through Authentik (`[ingress.forward_auth]`, D104): the Traefik middleware\n"
        f"  `{ingress.authentik_middleware}` must list `{forward.header}` in\n"
        f"  `authResponseHeaders`. Users need the group `{forward.user_group}`"
        + (
            f", admins also\n  `{forward.admin_group}` (a direct membership: the token lists"
            " only those).\n"
            if forward.admin_group is not None
            else "; with no\n  `admin_group` every user may take the admin-only actions.\n"
        )
        + (
            "  The workspace's Secret holds the provider's client secret (HS256): whoever reads\n"
            "  that Secret can mint a login, as with the login link. Rotating the secret in\n"
            "  Authentik means writing the site file anew and generating the Secrets again.\n"
            if hs256
            else f"  The workspace believes only tokens signed by a key in `{forward.jwks_url}`;\n"
            f"  the provider whose client ID is `{forward.audience}` needs a Signing Key.\n"
        )
        if ingress is not None and forward is not None and forward.enabled
        else ""
    )
    return f"""# Secrets for {name} in {ns}

Generated by `sandbox-science-kubernetes site`. Every file here is private (mode 600,
directories 700); keep this directory off shared storage and delete it once the Secrets
exist. The private CA's key was never written: to renew a certificate or rotate a
token, generate a new directory, replace every Secret and recreate the Pods.

- `secrets/<service>/`: one directory per Secret, exactly the keys the bundle projects.
- `secrets.sh`: `kubectl create secret generic` for each, from these files only.
- `pool/worker-config/`: the worker's OpenShell CLI tree, for the pool PVC.
- `gateway/`: the CA the gateway must trust for the middleware, and the OpenShell
  chart's values that mount it and register the middleware.
- `bootstrap/policy.yaml`: the project policy, granted to every project below.
{checkpoints}
## Steps

1. `sh secrets.sh`, then render and apply the bundle (docs/KUBERNETES-PREP.md step 8).
   Then register the middleware with the gateway. The gateway restarts, and it connects to
   the middleware at startup and exits if it cannot, so the bundle's pool must be running
   first:

{fence}sh
kubectl -n {gateway_ns} create configmap {name}-middleware-ca \\
  --from-file=ca.crt=gateway/middleware-ca.crt
helm upgrade openshell oci://ghcr.io/nvidia/openshell/helm-chart \\
  --version {OPENSHELL_VERSION} -n {gateway_ns} -f openshell-values.yaml -f {GATEWAY_VALUES}
{fence}

2. Place the CLI tree on the pool PVC (UID 1000, modes kept), then restart the pool:

{fence}sh
tar -C pool -cf - worker-config | \\
  kubectl -n {ns} exec -i deploy/{name}-pool -c worker -- tar -C {DATA} -xf -
kubectl -n {ns} rollout restart deploy/{name}-pool
{fence}

3. Store the policy and grant it, then make the projects and open their sessions with
   the digest it prints:

{fence}sh
kubectl -n {ns} exec -i deploy/{name}-pool -c worker -- sandbox-science-worker policy add \\
  --settings {PRIVATE}/worker.toml {granted} /dev/stdin < bootstrap/policy.yaml
{projects}{fence}

## TODO(operator)

- Step 1's values file is the site's own OpenShell values (`openshell-values.yaml`);
  merge `{GATEWAY_VALUES}` by hand if those already set `server.extraVolumes`,
  `server.extraVolumeMounts` or `"openshell.supervisor"` (Helm does not merge lists). The
  gateway must sign extension tokens with the key whose public half was given as
  `openshell.jwt_public_key` (issuer `openshell-gateway:{site.openshell.gateway_id}`).
- `runtime-manager.json`'s `policy.worker_api` names the worker Service only because
  the target requires one; the runtime never calls it and the pool's NetworkPolicy
  refuses the supervisors there.
{plain}- Step 2 assumes `tar` in the `trusted-worker` image, and step 3 that `policy add` reads
  `/dev/stdin`; neither is verified. Skill packs are not imported.
- {mcp}- The gateway `{gateway}`'s CLI profiles use `{site.openshell.endpoint}`; the gateway's
  certificate must cover that host.
{login}"""  # noqa: S608 - Markdown whose "replace ... values" reads to ruff as SQL


def write(output: Path, files: dict[str, bytes]) -> None:
    """Create ``output`` (or use it if it is an empty directory of this user's) with
    every file mode 600 and directory 700; never write into a non-empty directory."""
    try:
        output.mkdir(mode=0o700)
    except FileExistsError:
        found = output.lstat()
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or any(output.iterdir()):
            raise BundleError(
                f"{output} is not a new or empty directory; nothing is overwritten"
            ) from None
    output.chmod(0o700)
    for relative, content in sorted(files.items()):
        path = output / relative
        for parent in reversed(path.relative_to(output).parents[:-1]):
            directory = output / parent
            if not directory.is_dir():
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        path.chmod(0o600)
