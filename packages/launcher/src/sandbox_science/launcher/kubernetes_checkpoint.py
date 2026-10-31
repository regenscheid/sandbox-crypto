# SPDX-License-Identifier: MIT
"""Pure rendering of workspace checkpoint Jobs and their network rules (D92,
docs/KUBERNETES-BUNDLE.md#workspace-checkpoints).

A checkpoint Job runs in the sandbox namespace, beside the volume it reads: the trusted
worker image, ``sandbox-science-worker checkpoint capture-upload``, the workspace's claim
mounted read-only, the upload key's Secret and nothing else, no service-account token,
the sandbox user's uid, pinned to the volume's node. Its plan (the binding to write into
the archive, from the worker) and its destination arrive as environment variables. The
operator creates it (``kubectl create``); no service in the bundle can. A restore Job is
the same Pod with ``restore-write``, the fresh target's claim read-write and the restore
key, read-only on the kept bucket, instead of the upload key.
"""

from __future__ import annotations

import json
import re

from sandbox_science.contracts.workspace_checkpoint import CheckpointPlan, RestorePlan

from .kubernetes_config import CHECKPOINT_UPLOAD_KEYS, BundleConfig, Checkpoint, Egress

CHECKPOINT = "checkpoint"
RESTORE = "restore"
WORKSPACE = "/mnt/workspace"
CREDENTIALS = "/etc/sandbox-science/checkpoint-s3"
PLAN_VARIABLE = "SS_CHECKPOINT_PLAN"
STORAGE_VARIABLE = "SS_CHECKPOINT_STORAGE"
RESTORE_VARIABLE = "SS_RESTORE_PLAN"
ATTEMPT_VARIABLE = "SS_RESTORE_ATTEMPT"
"""The variables ``checkpoint_jobs_cli`` reads (a test compares them; the launcher never
imports the worker)."""
CHECKPOINT_LABEL = "sandbox-science.io/checkpoint-id"
RESTORE_LABEL = "sandbox-science.io/restore-id"
_ATTEMPT = re.compile(r"^[0-9a-f]{32}$")
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
    checkpoint, _ = _checkpoint(config)
    if plan.capture != "suspended_pvc":
        raise ValueError("a checkpoint Job reads a suspended sandbox's volume")
    return _job(
        config,
        name=CHECKPOINT,
        labels={CHECKPOINT_LABEL: plan.checkpoint_id},
        variables={
            PLAN_VARIABLE: plan.model_dump_json(),
            STORAGE_VARIABLE: json.dumps(
                storage_settings(checkpoint, checkpoint.upload_bucket), sort_keys=True
            ),
        },
        command=["capture-upload"],
        claim=claim,
        read_only=True,
        secret=("upload-key", checkpoint.upload_secret),
        profile=checkpoint.upload_profile,
        node=node,
        architecture=plan.source.architecture,
        user=(checkpoint.run_as_user, checkpoint.run_as_group),
    )


def restore_job(
    config: BundleConfig, plan: RestorePlan, claim: str, node: str | None, attempt: str
) -> dict[str, object]:
    """One restore Job for ``plan``: the fresh target's claim mounted read-write, the
    restore key's Secret (read-only on the kept bucket), everything else as a checkpoint
    Job's, including its labels, so the same NetworkPolicy gives it DNS and the store only."""
    checkpoint, _ = _checkpoint(config)
    if checkpoint.restore_secret is None:
        raise ValueError("the bundle's [checkpoint] names no restore_secret")
    if _ATTEMPT.fullmatch(attempt) is None:
        raise ValueError("the attempt is the worker's nonce for this Job")
    return _job(
        config,
        name=RESTORE,
        labels={RESTORE_LABEL: plan.restore_id},
        variables={
            RESTORE_VARIABLE: plan.model_dump_json(),
            ATTEMPT_VARIABLE: attempt,
            STORAGE_VARIABLE: json.dumps(
                storage_settings(checkpoint, checkpoint.kept_bucket), sort_keys=True
            ),
        },
        command=["restore-write"],
        claim=claim,
        read_only=False,
        secret=("restore-key", checkpoint.restore_secret),
        profile=checkpoint.restore_profile,
        node=node,
        architecture=plan.target.architecture,
        user=(checkpoint.restore_run_as_user, checkpoint.restore_run_as_group),
    )


def _job(
    config: BundleConfig,
    *,
    name: str,
    labels: dict[str, str],
    variables: dict[str, str],
    command: list[str],
    claim: str,
    read_only: bool,
    secret: tuple[str, str],
    profile: str,
    node: str | None,
    architecture: str,
    user: tuple[int, int],
) -> dict[str, object]:
    checkpoint, namespace = _checkpoint(config)
    if _DNS_NAME.fullmatch(claim) is None or len(claim) > 253:
        raise ValueError("the workspace claim is not a Kubernetes object name")
    if node is not None and (_DNS_NAME.fullmatch(node) is None or len(node) > 253):
        raise ValueError("the node is not a Kubernetes node name")
    labels = {**_labels(config), **labels}
    environment = {
        **variables,
        "AWS_SHARED_CREDENTIALS_FILE": f"{CREDENTIALS}/credentials",
        "AWS_CONFIG_FILE": f"{CREDENTIALS}/config",
        "AWS_PROFILE": profile,
        "AWS_EC2_METADATA_DISABLED": "true",
        "HOME": "/tmp",  # noqa: S108 - the Pod's bounded scratch volume
        "TMPDIR": "/tmp",  # noqa: S108 - the Pod's bounded scratch volume
        "SS_LOG_FORMAT": "json",
    }
    container: dict[str, object] = {
        "name": name,
        "image": config.images.worker,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "sandbox-science-worker",
            "checkpoint",
            *command,
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
            "runAsUser": user[0],
            "runAsGroup": user[1],
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": _resources(checkpoint),
        "terminationMessagePolicy": "File",
        "volumeMounts": [
            {"name": "workspace", "mountPath": WORKSPACE, "readOnly": read_only},
            {"name": secret[0], "mountPath": CREDENTIALS, "readOnly": True},
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
            "runAsUser": user[0],
            "runAsGroup": user[1],
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "nodeSelector": {"kubernetes.io/arch": architecture},
        "containers": [container],
        "volumes": [
            {
                "name": "workspace",
                "persistentVolumeClaim": {"claimName": claim, "readOnly": read_only},
            },
            {
                "name": secret[0],
                "secret": {
                    "secretName": secret[1],
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
            "generateName": f"{config.name}-{name}-",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": checkpoint.deadline_seconds,
            # A finished Job's Pod still names the claim, which keeps a deleted claim
            # Terminating; ``checkpoint`` and ``restore`` delete their Job, this the rest.
            "ttlSecondsAfterFinished": checkpoint.job_ttl_seconds,
            "template": {"metadata": {"labels": labels}, "spec": spec},
        },
    }
