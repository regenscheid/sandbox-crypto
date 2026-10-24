# SPDX-License-Identifier: MIT
"""Pure rendering of trusted services; agents remain in separate OpenShell sandboxes."""

from __future__ import annotations

import json

from . import toml
from .kubernetes_config import (
    GATEWAY_PROFILE,
    REQUIRED_FILES,
    BundleConfig,
    Capacity,
    Peer,
    Placement,
    Secret,
    Service,
)

COPY_HELPER = "/usr/local/libexec/sandbox-science/copy-credentials.py"
STATE_HELPER = "/usr/local/libexec/sandbox-science/initialize-state.py"
PRIVATE = "/private"
DATA = "/var/lib/sandbox-science"
MANAGER = "runtime-manager"
XDG = f"{PRIVATE}/xdg"
"""The manager's OpenShell CLI tree (``XDG_CONFIG_HOME``), built by its copy init."""
RUNTIME_CREDENTIALS = "/run/sandbox-science/runtime-credentials"
"""Control's memory volume for the runtime token it mints; no other container mounts it."""
CONTROL_PORT = 8443
MANAGER_PORT = 8444
SETTINGS_ENV = "SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS"
APPLICATION = "sandbox-science"
APPLICATION_LABEL = "app.kubernetes.io/name"
LOG_FORMAT = {"SS_LOG_FORMAT": "json"}
"""Every container logs JSON lines for Loki (D103) rather than the local text format."""
UNIT = "sandbox-science-unit"
UNIT_DIR = "/etc/sandbox-science"
"""Where the pool Pod's containers read the unit config (a ConfigMap, never a credential)."""
UNIT_VOLUME = "unit-config"
POOL_SERVICES = ("worker", "middleware", "cache")
_INIT = Capacity(
    cpu_request_millis=50, cpu_limit_millis=100, memory_request_mib=32, memory_limit_mib=128
)


def _labels(config: BundleConfig, component: str | None = None) -> dict[str, str]:
    result = {"app.kubernetes.io/part-of": config.name}
    if component is not None:
        result["app.kubernetes.io/component"] = component
    return result


def _identity(config: BundleConfig) -> dict[str, str]:
    """The labels a log collector (Alloy, docs/KUBERNETES-BUNDLE.md#log-collection) and
    ``kubectl`` select this instance's Pods by: the application and the instance's name.
    Selectors and NetworkPolicies keep to ``_labels``, so these never decide who may talk
    to whom."""
    return {APPLICATION_LABEL: APPLICATION, "app.kubernetes.io/instance": config.name}


def _object(config: BundleConfig, kind: str, suffix: str, **fields: object) -> dict[str, object]:
    api = {"Deployment": "apps/v1", "NetworkPolicy": "networking.k8s.io/v1"}.get(kind, "v1")
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {
            "name": f"{config.name}-{suffix}",
            "namespace": config.namespace,
            "labels": {**_identity(config), **_labels(config)},
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
            ingress = [
                {"from": [_peer(peer) for peer in network.runtimes], "ports": _ports(CONTROL_PORT)},
                {
                    "from": [{"podSelector": {"matchLabels": _labels(config, MANAGER)}}],
                    "ports": _ports(MANAGER_PORT),
                },
            ]
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
    # The manager accepts nothing and reaches only DNS, control's manager port and the
    # OpenShell gateway (KRB-4).
    policies.append(
        _object(
            config,
            "NetworkPolicy",
            MANAGER,
            spec={
                "podSelector": {"matchLabels": _labels(config, MANAGER)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [
                    {
                        "to": [_peer(network.dns)],
                        "ports": [
                            {"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")
                        ],
                    },
                    {
                        "to": [{"podSelector": {"matchLabels": _labels(config, "app")}}],
                        "ports": _ports(MANAGER_PORT),
                    },
                    {"to": [_peer(network.gateway.peer)], "ports": _ports(*network.gateway.ports)},
                ],
            },
        )
    )
    return policies


def _service(
    config: BundleConfig, component: str, suffix: str, *ports: tuple[str, int]
) -> dict[str, object]:
    return _object(
        config,
        "Service",
        suffix,
        spec={
            "type": "ClusterIP",
            "selector": _labels(config, component),
            "ports": [
                {"name": name, "port": port, "targetPort": port, "protocol": "TCP"}
                for name, port in ports
            ],
        },
    )


def _pvc(config: BundleConfig, component: str, placement: Placement) -> dict[str, object]:
    # Only one manager Pod may ever hold its state, even on one node (KRB-9).
    access = "ReadWriteOncePod" if component == MANAGER else "ReadWriteOnce"
    spec: dict[str, object] = {
        "accessModes": [access],
        "volumeMode": "Filesystem",
        "resources": {"requests": {"storage": f"{placement.storage_gib}Gi"}},
    }
    if placement.storage_class is not None:
        spec["storageClassName"] = placement.storage_class
    return _object(config, "PersistentVolumeClaim", f"{component}-data", spec=spec)


def _destination(config: BundleConfig, name: str, file: str) -> str:
    """Where a projected key lands under ``/private``: the manager's gateway profile
    keys form the OpenShell CLI's tree, every other key keeps its name."""
    if name == MANAGER and file in GATEWAY_PROFILE:
        relative = GATEWAY_PROFILE[file]
        if file != "active-gateway":
            relative = f"gateways/{config.runtime_manager.openshell_gateway}/{relative}"
        return f"{XDG.removeprefix(PRIVATE + '/')}/openshell/{relative}"
    return file


def _credentials(
    config: BundleConfig, name: str, secret: Secret, image: str
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
            json.dumps(
                [[file, _destination(config, name, file)] for file in files],
                separators=(",", ":"),
            ),
        ],
        "securityContext": _security(),
        "resources": _resources(_INIT),
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
    if name == MANAGER:
        return ["sandbox-science-runtime-manager", "serve"]
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
            *config.models.workspace_arguments(),
        ]
    # The pool's services run the unit's entry point on the unit's config, one container
    # each so that each keeps its own credentials, limits and probes; ``--only`` picks the
    # container's service from the file all three share (docs/UNIT.md).
    return [UNIT, "serve", "--config", f"{UNIT_DIR}/unit.toml", "--only", name]


def _data_mounts(name: str, capacity: Capacity) -> list[dict[str, object]]:
    """The PVC as a container sees it. In the app Pod each container mounts only the
    directories it uses: control's state, which workspace reads as the harness's state
    directory, and its own managed home/config directories."""
    if name not in ("control", "workspace"):
        return [{"name": "data", "mountPath": DATA}]
    environment = capacity.environment if isinstance(capacity, Service) else {}
    directories = ["control"]
    directories += sorted(
        environment[key].removeprefix(f"{DATA}/")
        for key in ("HOME", "XDG_CONFIG_HOME")
        if key in environment
    )
    return [
        {"name": "data", "mountPath": f"{DATA}/{directory}", "subPath": directory}
        for directory in directories
    ]


def _environment(capacity: Capacity) -> dict[str, str]:
    return {
        "HOME": "/tmp",  # noqa: S108 - existing bounded per-container Pod volume
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        **(capacity.environment if isinstance(capacity, Service) else {}),
    }


def _unit_config(config: BundleConfig) -> str:
    """The unit config of the pool Pod, in the shape ``sandbox-science up`` and the personal
    instance use: one section per service, each naming the settings file the bundle already
    projects from that service's Secret. The unit passes each service the container
    variables it is given (the unit starts its services with a short environment)."""
    services = {
        name: capacity for name, capacity, _ in _roles(config, "pool") if name in POOL_SERVICES
    }
    document: dict[str, object] = {}
    passed = {name: sorted(_environment(capacity)) for name, capacity in services.items()}
    document["unit"] = {"pass_env": passed}
    for name in services:
        document[name] = {"settings_file": f"{PRIVATE}/{name}.toml"}
    return toml.dumps(document)


def _container(
    config: BundleConfig, name: str, image: str, capacity: Capacity
) -> dict[str, object]:
    environment = {**_environment(capacity), **LOG_FORMAT}
    if name == MANAGER:
        # `kubectl exec … sandbox-science-runtime-manager request …` inherits this.
        environment[SETTINGS_ENV] = f"{PRIVATE}/runtime-manager.json"
    mounts = [
        *_data_mounts(name, capacity),
        {"name": f"{name}-scratch", "mountPath": "/tmp"},  # noqa: S108 - bounded Pod volume
        {"name": f"{name}-credentials", "mountPath": PRIVATE, "readOnly": True},
    ]
    if name == "control":
        mounts.append({"name": "runtime-credentials", "mountPath": RUNTIME_CREDENTIALS})
    if name in POOL_SERVICES:
        mounts.append({"name": UNIT_VOLUME, "mountPath": UNIT_DIR, "readOnly": True})
    result: dict[str, object] = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": _command(config, name),
        "securityContext": _security(),
        "resources": _resources(capacity),
        "env": [{"name": key, "value": environment[key]} for key in sorted(environment)],
        "volumeMounts": mounts,
    }
    probe: dict[str, object] | None = None
    if name == "control":
        result["ports"] = [
            {"name": "control", "containerPort": CONTROL_PORT, "protocol": "TCP"},
            {"name": "manager", "containerPort": MANAGER_PORT, "protocol": "TCP"},
        ]
        probe = {"httpGet": {"path": "/ready", "port": CONTROL_PORT, "scheme": "HTTPS"}}
        result["livenessProbe"] = {
            "httpGet": {"path": "/live", "port": CONTROL_PORT, "scheme": "HTTPS"},
            "initialDelaySeconds": 10,
            "periodSeconds": 10,
        }
    elif name in ("worker", "middleware", "cache"):
        probe = {"tcpSocket": {"port": {"worker": 8100, "middleware": 50051, "cache": 8090}[name]}}
    if probe is not None:
        result["readinessProbe"] = {**probe, "periodSeconds": 5}
    return result


def _roles(config: BundleConfig, component: str) -> list[tuple[str, Capacity, Secret]]:
    if component == "app":
        return [
            ("control", config.control, config.secrets.control),
            ("workspace", config.workspace, config.secrets.workspace),
        ]
    if component == MANAGER:
        return [(MANAGER, config.runtime_manager, config.secrets.runtime_manager)]
    roles: list[tuple[str, Capacity, Secret]] = [
        ("worker", config.worker, config.secrets.worker),
        ("middleware", config.middleware, config.secrets.middleware),
    ]
    if config.secrets.cache is not None:
        roles.append(("cache", config.cache, config.secrets.cache))
    return roles


def _deployment(config: BundleConfig, component: str) -> dict[str, object]:
    placement: Placement = {
        "app": config.app,
        "pool": config.pool,
        MANAGER: config.runtime_manager,
    }[component]
    # The manager runs from trusted-app, which carries the launcher and the OpenShell CLI.
    image = config.images.worker if component == "pool" else config.images.app
    initialize: dict[str, object] = {
        "name": "initialize-state",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "-I", STATE_HELPER],
        "args": [
            "--root",
            DATA,
            "--component",
            component,
            *(
                ["--package-cache"]
                if component == "pool" and config.secrets.cache is not None
                else []
            ),
        ],
        "securityContext": _security(),
        "resources": _resources(_INIT),
        "volumeMounts": [{"name": "data", "mountPath": DATA}],
    }
    inits: list[dict[str, object]] = [initialize]
    volumes: list[dict[str, object]] = [
        {"name": "data", "persistentVolumeClaim": {"claimName": f"{config.name}-{component}-data"}}
    ]
    containers: list[dict[str, object]] = []
    for name, capacity, secret in _roles(config, component):
        init, private = _credentials(config, name, secret, image)
        inits.append(init)
        volumes.extend(private)
        scratch: dict[str, object] = {"sizeLimit": f"{capacity.scratch_mib}Mi"}
        if name == MANAGER:
            # The runtime token passes through a private file in /tmp before upload.
            scratch = {"medium": "Memory", **scratch}
        volumes.append({"name": f"{name}-scratch", "emptyDir": scratch})
        containers.append(_container(config, name, image, capacity))
    if component == "app":
        volumes.append(
            {"name": "runtime-credentials", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}
        )
    if component == "pool":
        volumes.append(
            {
                "name": UNIT_VOLUME,
                "configMap": {
                    "name": f"{config.name}-pool-unit",
                    "defaultMode": 292,
                    "items": [{"key": "unit.toml", "path": "unit.toml"}],
                },
            }
        )
    return _object(
        config,
        "Deployment",
        component,
        spec={
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": _labels(config, component)},
            "template": {
                "metadata": {"labels": {**_identity(config), **_labels(config, component)}},
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
        _pvc(config, MANAGER, config.runtime_manager),
        _object(config, "ConfigMap", "pool-unit", data={"unit.toml": _unit_config(config)}),
        _deployment(config, "app"),
        _deployment(config, "pool"),
        _deployment(config, MANAGER),
        _service(config, "app", "control", ("control", CONTROL_PORT), ("manager", MANAGER_PORT)),
        _service(config, "pool", "worker", ("worker", 8100)),
        _service(config, "pool", "middleware", ("middleware", 50051)),
    ]
    if config.secrets.cache is not None:
        items.append(_service(config, "pool", "cache", ("cache", 8090)))
    return {"apiVersion": "v1", "kind": "List", "items": items}
