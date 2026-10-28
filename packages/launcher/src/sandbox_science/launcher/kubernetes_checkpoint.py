# SPDX-License-Identifier: MIT
"""Pure rendering of workspace checkpoint Jobs and their network rules (D92,
docs/KUBERNETES-BUNDLE.md#workspace-checkpoints).

A checkpoint Job runs in the sandbox namespace, beside the volume it reads: the trusted
worker image, ``sandbox-science-worker checkpoint capture-upload``, the workspace's claim
mounted read-only, the upload key's Secret and nothing else, no service-account token,
the sandbox user's uid, pinned to the volume's node. Its plan (the binding to write into
the archive, from the worker) and its destination arrive as environment variables. The
operator creates it (``kubectl create``); no service in the bundle can.
"""

from __future__ import annotations

import json
import re

from sandbox_science.contracts.workspace_checkpoint import CheckpointPlan

from .kubernetes_config import CHECKPOINT_UPLOAD_KEYS, BundleConfig, Checkpoint, Egress

CHECKPOINT = "checkpoint"
WORKSPACE = "/mnt/workspace"
CREDENTIALS = "/etc/sandbox-science/checkpoint-s3"
PLAN_VARIABLE = "SS_CHECKPOINT_PLAN"
STORAGE_VARIABLE = "SS_CHECKPOINT_STORAGE"
"""The variables ``checkpoint_jobs_cli`` reads (a test compares them; the launcher never
imports the worker)."""
CHECKPOINT_LABEL = "sandbox-science.io/checkpoint-id"
_DNS_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")


def _labels(config: BundleConfig) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "sandbox-science",
        "app.kubernetes.io/instance": config.name,
        "app.kubernetes.io/part-of": config.name,
        "app.kubernetes.io/component": CHECKPOINT,
    }


def _checkpoint(config: BundleConfig) -> tuple[Checkpoint, str]:
    if config.checkpoint is None or config.sandboxes is None:
        raise ValueError("the bundle has no [checkpoint] section")
    return config.checkpoint, config.sandboxes.namespace


def _peer(egress: Egress) -> dict[str, object]:
    peer = egress.peer
    if peer.cidr is not None:
        to: dict[str, object] = {"ipBlock": {"cidr": peer.cidr}}
    else:
        to = {"namespaceSelector": {"matchLabels": peer.namespace_labels}}
        if peer.pod_labels:
            to["podSelector"] = {"matchLabels": peer.pod_labels}
    return {"to": [to], "ports": [{"protocol": "TCP", "port": port} for port in egress.ports]}


def object_store_egress(config: BundleConfig) -> dict[str, object]:
    """The pool's egress rule to the object store, where the worker verifies checkpoints."""
    checkpoint, _ = _checkpoint(config)
    return _peer(checkpoint.object_store)


def network_policy(config: BundleConfig) -> dict[str, object]:
    """In the sandbox namespace: nothing connects to a checkpoint Job's Pod; it reaches DNS
    and the object store only, never the gateway, a sandbox or the internet. NetworkPolicies
    add up, so a Pod there carrying these labels would gain the same egress: the worker
    never sets ``app.kubernetes.io`` labels on sandboxes."""
    checkpoint, namespace = _checkpoint(config)
    dns = config.network.dns
    selector: dict[str, object] = (
        {"ipBlock": {"cidr": dns.cidr}}
        if dns.cidr is not None
        else {
            "namespaceSelector": {"matchLabels": dns.namespace_labels},
            **({"podSelector": {"matchLabels": dns.pod_labels}} if dns.pod_labels else {}),
        }
    )
    labels = _labels(config)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{config.name}-{CHECKPOINT}",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "podSelector": {
                "matchLabels": {
                    key: labels[key]
                    for key in ("app.kubernetes.io/part-of", "app.kubernetes.io/component")
                }
            },
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [selector],
                    "ports": [{"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")],
                },
                _peer(checkpoint.object_store),
            ],
        },
    }


def storage_settings(checkpoint: Checkpoint, bucket: str) -> dict[str, str]:
    """A bucket's destination settings in ``checkpoint_s3.Storage``'s shape; no credential."""
    return {
        "bucket": bucket,
        "prefix": checkpoint.prefix,
        "region": checkpoint.region,
        "endpoint": checkpoint.endpoint,
        "addressing_style": checkpoint.addressing_style,
    }


def _resources(checkpoint: Checkpoint) -> dict[str, object]:
    job = checkpoint.job
    return {
        "requests": {
            "cpu": f"{job.cpu_request_millis}m",
            "memory": f"{job.memory_request_mib}Mi",
            "ephemeral-storage": f"{job.ephemeral_request_mib}Mi",
        },
        "limits": {
            "cpu": f"{job.cpu_limit_millis}m",
            "memory": f"{job.memory_limit_mib}Mi",
            "ephemeral-storage": f"{job.ephemeral_limit_mib}Mi",
        },
    }


def checkpoint_job(
    config: BundleConfig, plan: CheckpointPlan, claim: str, node: str | None
) -> dict[str, object]:
    """One checkpoint Job for ``plan``, reading the claim ``claim`` (in the sandbox
    namespace) on ``node``, the volume's node; without a node the scheduler follows the
    volume's own affinity. ``generateName`` makes ``kubectl apply`` refuse it."""
    checkpoint, namespace = _checkpoint(config)
    if _DNS_NAME.fullmatch(claim) is None or len(claim) > 253:
        raise ValueError("the workspace claim is not a Kubernetes object name")
    if node is not None and (_DNS_NAME.fullmatch(node) is None or len(node) > 253):
        raise ValueError("the node is not a Kubernetes node name")
    if plan.capture != "suspended_pvc":
        raise ValueError("a checkpoint Job reads a suspended sandbox's volume")
    labels = {**_labels(config), CHECKPOINT_LABEL: plan.checkpoint_id}
    environment = {
        PLAN_VARIABLE: plan.model_dump_json(),
        STORAGE_VARIABLE: json.dumps(
            storage_settings(checkpoint, checkpoint.upload_bucket), sort_keys=True
        ),
        "AWS_SHARED_CREDENTIALS_FILE": f"{CREDENTIALS}/credentials",
        "AWS_CONFIG_FILE": f"{CREDENTIALS}/config",
        "AWS_PROFILE": checkpoint.upload_profile,
        "AWS_EC2_METADATA_DISABLED": "true",
        "HOME": "/tmp",  # noqa: S108 - the Pod's bounded scratch volume
        "TMPDIR": "/tmp",  # noqa: S108 - the Pod's bounded scratch volume
        "SS_LOG_FORMAT": "json",
    }
    container: dict[str, object] = {
        "name": CHECKPOINT,
        "image": config.images.worker,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "sandbox-science-worker",
            "checkpoint",
            "capture-upload",
            "--workspace",
            WORKSPACE,
            "--timeout",
            str(checkpoint.deadline_seconds),
        ],
        "env": [{"name": key, "value": environment[key]} for key in sorted(environment)],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": checkpoint.run_as_user,
            "runAsGroup": checkpoint.run_as_group,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": _resources(checkpoint),
        "terminationMessagePolicy": "File",
        "volumeMounts": [
            {"name": "workspace", "mountPath": WORKSPACE, "readOnly": True},
            {"name": "upload-key", "mountPath": CREDENTIALS, "readOnly": True},
            {"name": "scratch", "mountPath": "/tmp"},  # noqa: S108 - bounded Pod volume
        ],
    }
    spec: dict[str, object] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "imagePullSecrets": [{"name": config.image_pull_secret}],
        "terminationGracePeriodSeconds": 30,
        # No fsGroup: the kubelet would otherwise change the workspace's group ownership.
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": checkpoint.run_as_user,
            "runAsGroup": checkpoint.run_as_group,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "nodeSelector": {"kubernetes.io/arch": plan.source.architecture},
        "containers": [container],
        "volumes": [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}},
            {
                "name": "upload-key",
                "secret": {
                    "secretName": checkpoint.upload_secret,
                    "defaultMode": 0o444,
                    "items": [{"key": key, "path": key} for key in CHECKPOINT_UPLOAD_KEYS],
                },
            },
            {"name": "scratch", "emptyDir": {"sizeLimit": f"{checkpoint.job.scratch_mib}Mi"}},
        ],
    }
    if node is not None:
        spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {"key": "metadata.name", "operator": "In", "values": [node]}
                            ]
                        }
                    ]
                }
            }
        }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "generateName": f"{config.name}-{CHECKPOINT}-",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": checkpoint.deadline_seconds,
            # A finished Job's Pod still names the claim, which keeps a deleted claim
            # Terminating; ``checkpoint`` deletes its Job once verified, this the rest.
            "ttlSecondsAfterFinished": checkpoint.job_ttl_seconds,
            "template": {"metadata": {"labels": labels}, "spec": spec},
        },
    }
