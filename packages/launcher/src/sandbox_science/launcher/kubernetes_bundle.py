# SPDX-License-Identifier: MIT
"""Pure rendering of trusted services; agents remain in separate OpenShell sandboxes."""

from __future__ import annotations

import json

from .kubernetes_config import (
    REQUIRED_FILES,
    BundleConfig,
    Capacity,
    Peer,
    Placement,
    Secret,
    Service,
)

COPY_HELPER = "/usr/local/libexec/sandbox-science/copy-credentials.py"
PRIVATE = "/private"
DATA = "/var/lib/sandbox-science"


def _labels(config: BundleConfig, component: str | None = None) -> dict[str, str]:
    result = {"app.kubernetes.io/part-of": config.name}
    if component is not None:
        result["app.kubernetes.io/component"] = component
    return result


def _object(config: BundleConfig, kind: str, suffix: str, **fields: object) -> dict[str, object]:
    api = {"Deployment": "apps/v1", "NetworkPolicy": "networking.k8s.io/v1"}.get(kind, "v1")
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {
            "name": f"{config.name}-{suffix}",
            "namespace": config.namespace,
            "labels": _labels(config),
        },
        **fields,
    }


def _security() -> dict[str, object]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _resources(capacity: Capacity) -> dict[str, object]:
    return {
        "requests": {
            "cpu": f"{capacity.cpu_request_millis}m",
            "memory": f"{capacity.memory_request_mib}Mi",
            "ephemeral-storage": f"{capacity.ephemeral_request_mib}Mi",
        },
        "limits": {
            "cpu": f"{capacity.cpu_limit_millis}m",
            "memory": f"{capacity.memory_limit_mib}Mi",
            "ephemeral-storage": f"{capacity.ephemeral_limit_mib}Mi",
        },
    }


def _peer(peer: Peer) -> dict[str, object]:
    if peer.cidr is not None:
        return {"ipBlock": {"cidr": peer.cidr}}
    result: dict[str, object] = {"namespaceSelector": {"matchLabels": peer.namespace_labels}}
    if peer.pod_labels:
        result["podSelector"] = {"matchLabels": peer.pod_labels}
    return result


def _ports(*ports: int) -> list[dict[str, object]]:
    return [{"protocol": "TCP", "port": port} for port in ports]


def _policies(config: BundleConfig) -> list[dict[str, object]]:
    network = config.network
    policies = [
        _object(
            config,
            "NetworkPolicy",
            "deny",
            spec={
                "podSelector": {"matchLabels": _labels(config)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        )
    ]
    for component, external in (("app", network.app_egress), ("pool", network.pool_egress)):
        egress: list[dict[str, object]] = [
            {
                "to": [_peer(network.dns)],
                "ports": [{"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")],
            }
        ]
        egress.extend({"to": [_peer(flow.peer)], "ports": _ports(*flow.ports)} for flow in external)
        if component == "app":
            ingress = [{"from": [_peer(peer) for peer in network.runtimes], "ports": _ports(8443)}]
            egress.append(
                {
                    "to": [{"podSelector": {"matchLabels": _labels(config, "pool")}}],
                    "ports": _ports(8100),
                }
            )
        else:
            ingress = [
                {
                    "from": [{"podSelector": {"matchLabels": _labels(config, "app")}}],
                    "ports": _ports(8100),
                },
                {"from": [_peer(peer) for peer in network.supervisors], "ports": _ports(50051)},
            ]
            if config.secrets.cache is not None:
                ingress.append(
                    {"from": [_peer(peer) for peer in network.sandboxes], "ports": _ports(8090)}
                )
        policies.append(
            _object(
                config,
                "NetworkPolicy",
                component,
                spec={
                    "podSelector": {"matchLabels": _labels(config, component)},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": ingress,
                    "egress": egress,
                },
            )
        )
    return policies


def _service(config: BundleConfig, component: str, port: int, suffix: str) -> dict[str, object]:
    return _object(
        config,
        "Service",
        suffix,
        spec={
            "type": "ClusterIP",
            "selector": _labels(config, component),
            "ports": [{"name": suffix, "port": port, "targetPort": port, "protocol": "TCP"}],
        },
    )


def _pvc(config: BundleConfig, component: str, placement: Placement) -> dict[str, object]:
    spec: dict[str, object] = {
        "accessModes": ["ReadWriteOnce"],
        "volumeMode": "Filesystem",
        "resources": {"requests": {"storage": f"{placement.storage_gib}Gi"}},
    }
    if placement.storage_class is not None:
        spec["storageClassName"] = placement.storage_class
    return _object(config, "PersistentVolumeClaim", f"{component}-data", spec=spec)


def _credentials(
    name: str, secret: Secret, image: str
) -> tuple[dict[str, object], list[dict[str, object]]]:
    files = sorted({*REQUIRED_FILES[name], *secret.extra_files})
    source = f"{name}-source"
    destination = f"{name}-credentials"
    init: dict[str, object] = {
        "name": f"copy-{name}",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "-I", COPY_HELPER],
        "args": [
            "--source-root",
            "/source",
            "--destination-root",
            PRIVATE,
            "--files",
            json.dumps([[file, file] for file in files], separators=(",", ":")),
        ],
        "securityContext": _security(),
        "resources": _resources(
            Capacity(
                cpu_request_millis=50,
                cpu_limit_millis=100,
                memory_request_mib=32,
                memory_limit_mib=128,
            )
        ),
        "volumeMounts": [
            {"name": source, "mountPath": "/source", "readOnly": True},
            {"name": destination, "mountPath": PRIVATE},
        ],
    }
    return init, [
        {
            "name": source,
            "secret": {
                "secretName": secret.name,
                "defaultMode": 292,
                "items": [{"key": file, "path": file} for file in files],
            },
        },
        {"name": destination, "emptyDir": {"medium": "Memory", "sizeLimit": "4Mi"}},
    ]


def _command(config: BundleConfig, name: str) -> list[str]:
    if name == "control":
        return ["sandbox-science-control", "--settings", f"{PRIVATE}/control.json"]
    if name == "workspace":
        worker = f"https://{config.name}-worker.{config.namespace}.svc:8100"
        return [
            "sandbox-science-workspace",
            "serve",
            "--state-dir",
            f"{DATA}/control",
            "--static",
            "/usr/share/sandbox-science/workspace",
            "--listen",
            "127.0.0.1:8200",
            "--preview-listen",
            "127.0.0.1:8201",
            "--worker",
            worker,
            "--worker-token-file",
            f"{PRIVATE}/worker-token",
            "--worker-ca-file",
            f"{PRIVATE}/worker-ca.crt",
        ]
    executable = (
        "sandbox-science-policy-middleware" if name == "middleware" else f"sandbox-science-{name}"
    )
    if name == "cache":
        executable = "sandbox-science-package-cache"
    return [executable, "serve", "--settings", f"{PRIVATE}/{name}.toml"]


def _container(config: BundleConfig, name: str, image: str, capacity: Service) -> dict[str, object]:
    environment = {
        "HOME": "/tmp",  # noqa: S108 - existing bounded per-container Pod volume
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        **capacity.environment,
    }
    result: dict[str, object] = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": _command(config, name),
        "securityContext": _security(),
        "resources": _resources(capacity),
        "env": [{"name": key, "value": environment[key]} for key in sorted(environment)],
        "volumeMounts": [
            {"name": "data", "mountPath": DATA},
            {"name": f"{name}-scratch", "mountPath": "/tmp"},  # noqa: S108 - bounded Pod volume
            {"name": f"{name}-credentials", "mountPath": PRIVATE, "readOnly": True},
        ],
    }
    probe: dict[str, object] | None = None
    if name == "control":
        probe = {"httpGet": {"path": "/ready", "port": 8443, "scheme": "HTTPS"}}
        result["livenessProbe"] = {
            "httpGet": {"path": "/live", "port": 8443, "scheme": "HTTPS"},
            "initialDelaySeconds": 10,
            "periodSeconds": 10,
        }
    elif name in ("worker", "middleware", "cache"):
        probe = {"tcpSocket": {"port": {"worker": 8100, "middleware": 50051, "cache": 8090}[name]}}
    if probe is not None:
        result["readinessProbe"] = {**probe, "periodSeconds": 5}
    return result


def _deployment(config: BundleConfig, component: str) -> dict[str, object]:
    placement = config.app if component == "app" else config.pool
    image = config.images.app if component == "app" else config.images.worker
    roles = (
        [
            ("control", config.control, config.secrets.control),
            ("workspace", config.workspace, config.secrets.workspace),
        ]
        if component == "app"
        else [
            ("worker", config.worker, config.secrets.worker),
            ("middleware", config.middleware, config.secrets.middleware),
        ]
    )
    if component == "pool" and config.secrets.cache is not None:
        roles.append(("cache", config.cache, config.secrets.cache))
    inits: list[dict[str, object]] = []
    volumes: list[dict[str, object]] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": f"{config.name}-{component}-data"}}
    ]
    containers: list[dict[str, object]] = []
    for name, capacity, secret in roles:
        init, private = _credentials(name, secret, image)
        inits.append(init)
        volumes.extend(private)
        volumes.append(
            {"name": f"{name}-scratch", "emptyDir": {"sizeLimit": f"{capacity.scratch_mib}Mi"}}
        )
        containers.append(_container(config, name, image, capacity))
    return _object(
        config,
        "Deployment",
        component,
        spec={
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": _labels(config, component)},
            "template": {
                "metadata": {"labels": _labels(config, component)},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 120,
                    "nodeSelector": {"kubernetes.io/arch": placement.architecture},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": inits,
                    "containers": containers,
                    "volumes": volumes,
                },
            },
        },
    )


def render(config: BundleConfig) -> dict[str, object]:
    """Return deterministic API objects, without reading credentials or contacting a cluster."""
    items = [
        *_policies(config),
        _pvc(config, "app", config.app),
        _pvc(config, "pool", config.pool),
        _deployment(config, "app"),
        _deployment(config, "pool"),
        _service(config, "app", 8443, "control"),
        _service(config, "pool", 8100, "worker"),
        _service(config, "pool", 50051, "middleware"),
    ]
    if config.secrets.cache is not None:
        items.append(_service(config, "pool", 8090, "cache"))
    return {"apiVersion": "v1", "kind": "List", "items": items}
