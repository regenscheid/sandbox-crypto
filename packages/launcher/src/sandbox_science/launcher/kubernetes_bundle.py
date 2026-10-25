# SPDX-License-Identifier: MIT
"""Pure rendering of trusted services; agents remain in separate OpenShell sandboxes."""

from __future__ import annotations

import json
from typing import cast

import yaml

from . import toml
from .kubernetes_config import (
    GATEWAY_PROFILE,
    REQUIRED_FILES,
    Alerts,
    BundleConfig,
    Capacity,
    Ingress,
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
WORKSPACE_PORT = 8200
PREVIEW_PORT = 8201
LOOPBACK = "127.0.0.1"
POD_NETWORK = "0.0.0.0"  # noqa: S104 - every interface of the Pod, only behind the ingress
CONTROL_PORT = 8443
MANAGER_PORT = 8444
SETTINGS_ENV = "SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS"
APPLICATION = "sandbox-science"
APPLICATION_LABEL = "app.kubernetes.io/name"
LOG_FORMAT = {"SS_LOG_FORMAT": "json"}
"""Every container logs JSON lines for Loki (D103) rather than the local text format."""
JOB_SANDBOX_LABEL = ("sandbox-science.io/job-sandbox", "true")
"""The label the worker's job sandboxes put on their workload Pod through OpenShell's
``allowed_pod_labels`` (``worker.settings.JOB_SANDBOX_LABEL``, which a test compares); the
launcher never imports the worker."""
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


def _object(
    config: BundleConfig,
    kind: str,
    suffix: str,
    annotations: dict[str, str] | None = None,
    namespace: str | None = None,
    **fields: object,
) -> dict[str, object]:
    api = {
        "Deployment": "apps/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
        "Certificate": "cert-manager.io/v1",
        "IngressRoute": "traefik.io/v1alpha1",
        "PodDisruptionBudget": "policy/v1",
    }.get(kind, "v1")
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {
            "name": f"{config.name}-{suffix}",
            "namespace": config.namespace if namespace is None else namespace,
            "labels": {**_identity(config), **_labels(config)},
            **({"annotations": annotations} if annotations else {}),
        },
        **fields,
    }


def _job_budget(config: BundleConfig, namespace: str) -> dict[str, object]:
    """Makes a planned drain wait for job sandboxes (D93, JS-20): ``maxUnavailable: 0``
    allows no voluntary eviction of a Pod with the job label, so ``kubectl drain`` blocks
    until the job ends or the operator decides otherwise. It lives in the sandbox
    namespace, which a PodDisruptionBudget can only select within. A job Pod that is
    running but not Ready may be evicted (``AlwaysAllow``), so a wedged one cannot hold
    a node forever; a Pending one never counts. It protects nothing from crashes,
    power loss or ``--disable-eviction``."""
    key, value = JOB_SANDBOX_LABEL
    return _object(
        config,
        "PodDisruptionBudget",
        "job-sandboxes",
        namespace=namespace,
        spec={
            "maxUnavailable": 0,
            "selector": {"matchLabels": {key: value}},
            "unhealthyPodEvictionPolicy": "AlwaysAllow",
        },
    )


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


def _gateway(config: BundleConfig) -> dict[str, object]:
    """Egress to the OpenShell gateway, for the runtime manager and the pool's worker."""
    gateway = config.network.gateway
    return {"to": [_peer(gateway.peer)], "ports": _ports(*gateway.ports)}


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
            # The gateway connects to each registered middleware at startup and exits if
            # it cannot; the supervisors call it for every request they decide.
            middleware = [*network.supervisors, network.gateway.peer]
            ingress = [
                {
                    "from": [{"podSelector": {"matchLabels": _labels(config, "app")}}],
                    "ports": _ports(8100),
                },
                {"from": [_peer(peer) for peer in middleware], "ports": _ports(50051)},
            ]
            # The worker drives the gateway with its own client identity to create every
            # research sandbox.
            egress.append(_gateway(config))
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
                    _gateway(config),
                ],
            },
        )
    )
    if config.ingress is not None:
        policies.append(_ingress_policy(config, config.ingress))
    return policies


def _ingress_policy(config: BundleConfig, ingress: Ingress) -> dict[str, object]:
    """An additive policy: the ingress controller's Pods may reach the workspace's two
    listeners and nothing else. It adds no egress and no other port; the default-deny and
    the app policy still decide everything else."""
    return _object(
        config,
        "NetworkPolicy",
        "ingress",
        spec={
            "podSelector": {"matchLabels": _labels(config, "app")},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [_peer(peer) for peer in ingress.traefik],
                    "ports": _ports(WORKSPACE_PORT, PREVIEW_PORT),
                }
            ],
        },
    )


def _routes(config: BundleConfig, ingress: Ingress) -> list[dict[str, object]]:
    """Per host name: a Service, a certificate and an authenticated Traefik route."""
    items: list[dict[str, object]] = []
    for suffix, host, port in (
        ("workspace", ingress.workspace_host, WORKSPACE_PORT),
        ("preview", ingress.preview_host, PREVIEW_PORT),
    ):
        secret = f"{config.name}-{suffix}-tls"
        items.append(_service(config, "app", suffix, (suffix, port)))
        items.append(
            _object(
                config,
                "Certificate",
                suffix,
                spec={
                    "secretName": secret,
                    "dnsNames": [host],
                    "issuerRef": {
                        "name": ingress.issuer,
                        "kind": ingress.issuer_kind,
                        "group": "cert-manager.io",
                    },
                    "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                },
            )
        )
        route = _object(
            config,
            "IngressRoute",
            suffix,
            None
            if ingress.ingress_class is None
            else {"kubernetes.io/ingress.class": ingress.ingress_class},
            spec={
                "entryPoints": [ingress.entry_point],
                "routes": [
                    {
                        "kind": "Rule",
                        "match": f"Host(`{host}`)",
                        # Authentik answers before any request reaches the workspace.
                        "middlewares": [{"name": ingress.authentik_middleware}],
                        "services": [
                            {
                                "name": f"{config.name}-{suffix}",
                                "port": port,
                                "scheme": "http",
                            }
                        ],
                    }
                ],
                "tls": {"secretName": secret},
            },
        )
        items.append(route)
    return items


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
        ingress = config.ingress
        # Loopback unless the ingress is on (config requires allow_remote then): the
        # workspace speaks plain HTTP, so nothing but the authenticated, TLS-terminating
        # route and its NetworkPolicy may reach the Pod-network listener. It is told the
        # two https:// origins the browser sees, so it answers only to those host names,
        # takes changes only from the workspace's, sets a Secure cookie and announces the
        # preview by its public origin. Traefik passes Host through, so no forwarded
        # header needs trusting (no --trust-proxy-from).
        bind = POD_NETWORK if ingress is not None else LOOPBACK
        remote = (
            [
                "--allow-remote",
                "--public-origin",
                f"https://{ingress.workspace_host}",
                "--preview-public-origin",
                f"https://{ingress.preview_host}",
            ]
            if ingress is not None
            else []
        )
        return [
            "sandbox-science-workspace",
            "serve",
            "--state-dir",
            f"{DATA}/control",
            "--static",
            "/usr/share/sandbox-science/workspace",
            "--listen",
            f"{bind}:{WORKSPACE_PORT}",
            "--preview-listen",
            f"{bind}:{PREVIEW_PORT}",
            *remote,
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
    elif name == "workspace" and config.ingress is not None:
        # Declared for the Services and for people; the listeners are set by the command.
        result["ports"] = [
            {"name": "workspace", "containerPort": WORKSPACE_PORT, "protocol": "TCP"},
            {"name": "preview", "containerPort": PREVIEW_PORT, "protocol": "TCP"},
        ]
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


# Alert rules (D103): an opt-in ``[alerts]`` section, one ConfigMap for Loki's ruler.

ALERT_RULES_FILE = "sandbox-science-alerts.yaml"
"""The rule file's key in the ConfigMap, which Loki's rules sidecar writes as a file."""
ALERTS: tuple[str, ...] = (
    "SandboxScienceServiceErrors",
    "SandboxScienceManagerUnreachable",
    "SandboxScienceCreationUnknown",
    "SandboxScienceMcpCanaryFailing",
    "SandboxSciencePolicyHold",
)
"""The rendered rules' names, in order (docs/KUBERNETES-BUNDLE.md, "Alert rules")."""
NETWORK_DROPS_NOTE = """
# Network-policy drops: not rendered. The services' logs do not carry them: Kubernetes
# NetworkPolicy drops are the CNI's to report, and OpenShell's sandbox network denials
# arrive only as OCSF events, through the sample Alloy configuration's optional OCSF
# pipeline (service "openshell-supervisor", labels class_uid and action), whose values have
# not been seen on a cluster. Once they have, a rule of this shape could be added:
#
#   - alert: SandboxScienceNetworkDenials
#     expr: sum by (namespace) (count_over_time({service="openshell-supervisor",
#       class_uid="4001", action="Denied"}[5m])) > 0
#     labels:
#       severity: warning
#     annotations:
#       summary: OpenShell denied sandbox network connections
"""


def _streams(config: BundleConfig, **matchers: str) -> str:
    """A LogQL stream selector for this instance's lines: the labels the sample Alloy
    configuration sets (``namespace``, ``instance``), with ``matchers`` written as given
    (``'="worker"'``, ``'=~"error|critical"'``)."""
    fields = [f'namespace="{config.namespace}"', f'instance="{config.name}"']
    fields.extend(f"{name}{matcher}" for name, matcher in matchers.items())
    return "{" + ", ".join(fields) + "}"


def _event(name: str) -> str:
    """A line filter for one structured log event, as the services' JSON lines write it
    (``"event":"<name>"``, compact): it finds the event however a pipeline labels it."""
    return f'|= `"event":"{name}"`'


def alert_rules(config: BundleConfig, alerts: Alerts) -> dict[str, object]:
    """The Loki ruler's rule groups (Prometheus rule format) for this instance: LogQL
    over the structured JSON logs. ``project`` comes from the lines' structured metadata,
    as the sample Alloy configuration keeps it; the other fields are parsed from the line."""
    static = {"namespace": config.namespace, "instance": config.name}

    def rule(
        name: str,
        expr: str,
        summary: str,
        description: str,
        *,
        severity: str = "warning",
        hold: str | None = None,
    ) -> dict[str, object]:
        found: dict[str, object] = {"alert": name, "expr": expr}
        if hold is not None:
            found["for"] = hold
        found["labels"] = {"severity": severity, **static}
        found["annotations"] = {"summary": summary, "description": description}
        return found

    errors = _streams(config, level='=~"error|critical"')
    manager = _streams(config, service='="runtime-manager"')
    worker = _streams(config, service='="worker"')
    middleware = _streams(config, service='="middleware"')
    rules = [
        rule(
            ALERTS[0],
            f"sum by (service) (count_over_time({errors}[5m])) > {alerts.error_threshold}",
            f"{{{{ $labels.service }}}} logged over {alerts.error_threshold} errors in 5 minutes",
            "More error (or critical) records than the threshold from one service within "
            "5 minutes.",
        ),
        rule(
            ALERTS[1],
            f"sum by (service, kind) (count_over_time({manager} {_event('runtime_manager_note')}"
            ' | json kind="kind" | kind=~"gateway_unreachable|control_unreachable" [8m])) > 0',
            "the runtime manager has noted {{ $labels.kind }} for over 10 minutes",
            "The runtime manager has noted gateway_unreachable or control_unreachable "
            "continuously for 10 minutes: it logs the note when it starts and every 5 "
            "minutes while it holds. It takes no automatic action meanwhile.",
            severity="critical",
            hold="10m",
        ),
        rule(
            ALERTS[2],
            f"sum by (service, project, environment) (count_over_time({worker} "
            f"{_event('environment_creation_unknown')} [5m])) > 0",
            "environment {{ $labels.environment }} is still creating past the creation grace",
            "The worker's reconciliation found a creation with an unknown outcome past "
            "creation_grace and could not yet confirm the sandbox present or absent.",
        ),
        rule(
            ALERTS[3],
            f"sum by (service, server, status) (count_over_time({_streams(config)} "
            f"{_event('mcp_canary_changed')}"
            ' | json server="server", status="status" | status!="listed" [20m])) > 0',
            "MCP canary of {{ $labels.server }} changed to {{ $labels.status }}",
            "A scheduled MCP canary's listing moved from listed (or no earlier result) to "
            "unavailable, denied or timeout. It resolves 20 minutes after the change; the "
            "workspace's MCP services view shows the latest canary.",
        ),
        rule(
            ALERTS[4],
            f"sum by (service, project, rule) (count_over_time({middleware} "
            f"{_event('detect_finding')}"
            ' | json outcome="outcome", rule="rule" | outcome=~"hold|hold_call" [5m])) > 0',
            "a detect-and-hold rule held calls in project {{ $labels.project }}",
            "The policy middleware's detection rule {{ $labels.rule }} opened a hold: a "
            "session's outward calls wait for the workspace's hold card, or one call waits "
            "for its approval.",
        ),
    ]
    return {"groups": [{"name": f"sandbox-science-{config.name}", "rules": rules}]}


def _alert_config_map(config: BundleConfig, alerts: Alerts) -> dict[str, object]:
    text = yaml.safe_dump(alert_rules(config, alerts), sort_keys=False, width=1000)
    found = _object(
        config,
        "ConfigMap",
        "alert-rules",
        annotations={"k8s-sidecar-target-directory": f"/rules/{alerts.tenant}"},
        namespace=alerts.namespace,
        data={ALERT_RULES_FILE: text + NETWORK_DROPS_NOTE},
    )
    labels = cast("dict[str, str]", cast("dict[str, object]", found["metadata"])["labels"])
    labels.update(alerts.configmap_labels)
    return found


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
    if config.ingress is not None:
        items.extend(_routes(config, config.ingress))
    if config.sandboxes is not None and config.sandboxes.job_budget:
        items.append(_job_budget(config, config.sandboxes.namespace))
    if config.alerts is not None:
        items.append(_alert_config_map(config, config.alerts))
    return {"apiVersion": "v1", "kind": "List", "items": items}
