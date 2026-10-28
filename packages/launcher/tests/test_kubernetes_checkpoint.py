# SPDX-License-Identifier: MIT
"""Workspace checkpoint Jobs and cleanup on Kubernetes, offline: the rendered Job (read-only
claim, no token, bounds, namespace), the bundle's rules and the site's keys, and the
operator's commands against a scripted ``kubectl``, refusals included."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.workspace_checkpoint import (
    CheckpointInventory,
    CheckpointPlan,
    CheckpointSource,
    CheckpointState,
    InventoryCheckpoint,
    InventoryEnvironment,
    InventoryHold,
)
from sandbox_science.launcher import kubernetes_cli
from sandbox_science.launcher.kubernetes_bundle import render
from sandbox_science.launcher.kubernetes_checkpoint import (
    PLAN_VARIABLE,
    STORAGE_VARIABLE,
    checkpoint_job,
)
from sandbox_science.launcher.kubernetes_config import BundleConfig, projected_files
from sandbox_science.launcher.kubernetes_operations import (
    OperationError,
    Operator,
    report,
    survey,
)
from sandbox_science.launcher.kubernetes_site import plan as site_plan
from sandbox_science.launcher.kubernetes_site_config import load as load_site
from sandbox_science.worker import checkpoint_jobs_cli
from sandbox_science.worker.checkpoint_s3 import Storage, parse

from .test_kubernetes_bundle import values
from .test_kubernetes_site import site_file, site_values

SANDBOXES = "research-runtimes"
GARAGE = {
    "peer": {
        "namespace_labels": {"kubernetes.io/metadata.name": "garage"},
        "pod_labels": {"app.kubernetes.io/name": "garage"},
    },
    "ports": [3900],
}
CHECKPOINT: dict[str, Any] = {
    "upload_secret": "research-checkpoint-upload",
    "upload_bucket": "research-checkpoint-upload",
    "kept_bucket": "research-checkpoints",
    "endpoint": "http://garage.garage.svc.cluster.local:3900",
    "object_store": GARAGE,
}
NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def with_checkpoint(data: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    result = values() if data is None else data
    result["sandboxes"] = {"namespace": SANDBOXES}
    result["checkpoint"] = {**CHECKPOINT, **changes}
    return result


def configured(**changes: Any) -> BundleConfig:
    return BundleConfig.model_validate_json(json.dumps(with_checkpoint(**changes)))


def source(sandbox: str = "ss-0123456789abcdef") -> CheckpointSource:
    return CheckpointSource(
        project_id="crypto",
        environment_id="crypto-main",
        sandbox=sandbox,
        sandbox_id=f"{sandbox}-id",
        profile="crypto-base",
        image="ghcr.io/test/crypto-base@sha256:" + "1" * 64,
        policy="sha256:" + "2" * 64,
        launch_settings=None,
        architecture="amd64",
        require_workspace_prefix=False,
        prefix_identity=None,
        package_fingerprint=None,
    )


def a_plan(sandbox: str = "ss-0123456789abcdef") -> CheckpointPlan:
    return CheckpointPlan(
        checkpoint_id="ckpt-" + "c" * 24,
        source=source(sandbox),
        source_sha256="d" * 64,
        capture="suspended_pvc",
        hold_expires_at=NOW + timedelta(hours=6),
    )


def job(**changes: Any) -> dict[str, Any]:
    rendered = checkpoint_job(configured(**changes), a_plan(), "workspace-ss-0123", "node-a")
    return json.loads(json.dumps(rendered))


# The Job


def test_the_job_mounts_the_claim_read_only_without_a_token_in_the_sandbox_namespace():
    rendered = job()
    assert rendered["kind"] == "Job"
    assert rendered["metadata"]["namespace"] == SANDBOXES
    assert rendered["metadata"]["generateName"] == "research-checkpoint-"
    assert "name" not in rendered["metadata"]  # kubectl apply refuses it; create makes a new one
    spec = rendered["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 7200
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert "serviceAccountName" not in pod
    assert "fsGroup" not in pod["securityContext"]  # no ownership change on the workspace
    assert pod["securityContext"]["runAsUser"] == 10001
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["workspace"]["persistentVolumeClaim"] == {
        "claimName": "workspace-ss-0123",
        "readOnly": True,
    }
    assert volumes["upload-key"]["secret"]["secretName"] == "research-checkpoint-upload"
    assert [item["key"] for item in volumes["upload-key"]["secret"]["items"]] == [
        "credentials",
        "config",
    ]
    assert set(volumes) == {"workspace", "upload-key", "scratch"}
    assert not any("hostPath" in volume for volume in pod["volumes"])
    (container,) = pod["containers"]
    assert all(
        mount["readOnly"] for mount in container["volumeMounts"] if mount["name"] != "scratch"
    )
    security = container["securityContext"]
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert container["command"][:3] == ["sandbox-science-worker", "checkpoint", "capture-upload"]
    assert container["image"] == values()["images"]["worker"]
    terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms["nodeSelectorTerms"][0]["matchFields"][0]["values"] == ["node-a"]
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_the_job_has_resource_bounds_and_its_scratch_fits_them():
    rendered = job(job={"memory_limit_mib": 4096, "scratch_mib": 1536, "ephemeral_limit_mib": 3072})
    pod = rendered["spec"]["template"]["spec"]
    resources = pod["containers"][0]["resources"]
    assert resources["limits"] == {
        "cpu": "2000m",
        "memory": "4096Mi",
        "ephemeral-storage": "3072Mi",
    }
    assert resources["requests"]["memory"] == "256Mi"
    scratch = next(volume for volume in pod["volumes"] if volume["name"] == "scratch")
    assert scratch["emptyDir"] == {"sizeLimit": "1536Mi"}
    with pytest.raises(ValidationError):
        configured(job={"scratch_mib": 4096})  # larger than its ephemeral limit


def test_the_job_carries_its_plan_and_only_the_upload_destination():
    env = {
        item["name"]: item["value"]
        for item in job()["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert CheckpointPlan.model_validate_json(env[PLAN_VARIABLE]) == a_plan()
    destination = parse(env[STORAGE_VARIABLE])
    assert destination == Storage(
        bucket="research-checkpoint-upload",
        prefix="sandbox-science",
        region="garage",
        endpoint="http://garage.garage.svc.cluster.local:3900",
        addressing_style="path",
    )
    assert env["AWS_PROFILE"] == "checkpoint-upload"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "research-checkpoints" not in json.dumps(env)  # never the kept bucket
    # The worker's command reads exactly these variables.
    assert (PLAN_VARIABLE, STORAGE_VARIABLE) == (
        checkpoint_jobs_cli.PLAN_VARIABLE,
        checkpoint_jobs_cli.STORAGE_VARIABLE,
    )


@pytest.mark.parametrize(
    ("claim", "node", "plan"),
    [
        ("Bad_Claim", None, a_plan()),
        ("workspace-a", "node a", a_plan()),
        ("workspace-a", None, a_plan().model_copy(update={"capture": "clone_pvc"})),
    ],
)
def test_the_job_refuses_unsafe_inputs(claim, node, plan):
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        checkpoint_job(configured(), plan, claim, node)


def test_checkpoint_job_command_writes_one_private_job(tmp_path, capsys):
    config = tmp_path / "bundle.toml"
    from sandbox_science.launcher import toml

    config.write_text(toml.dumps(with_checkpoint()))
    (tmp_path / "plan.json").write_text(a_plan().model_dump_json())
    output = tmp_path / "job.json"
    arguments = ["checkpoint-job", "--config", str(config), "--plan", str(tmp_path / "plan.json")]
    assert kubernetes_cli.main([*arguments, "--claim", "workspace-a", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["kind"] == "Job"
    assert output.stat().st_mode & 0o077 == 0
    assert kubernetes_cli.main([*arguments, "--claim", "workspace-a", "--output", str(output)]) == 1


# The bundle


def test_the_bundle_renders_the_jobs_network_rule_and_the_pools_egress():
    items = json.loads(json.dumps(render(configured())))["items"]
    policy = next(
        item
        for item in items
        if item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "research-checkpoint"
    )
    assert policy["metadata"]["namespace"] == SANDBOXES
    assert policy["spec"]["ingress"] == []
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/part-of": "research",
        "app.kubernetes.io/component": "checkpoint",
    }
    egress = policy["spec"]["egress"]
    assert len(egress) == 2
    assert egress[1]["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "garage"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "garage"}},
        }
    ]
    assert egress[1]["ports"] == [{"protocol": "TCP", "port": 3900}]
    pool = next(
        item
        for item in items
        if item["metadata"]["name"] == "research-pool" and item["kind"] == "NetworkPolicy"
    )
    assert egress[1] in pool["spec"]["egress"]
    # No Job is part of the bundle: the operator creates each.
    assert not any(item["kind"] == "Job" for item in items)


def test_without_the_section_nothing_reaches_the_store():
    plain = values()
    plain["sandboxes"] = {"namespace": SANDBOXES}
    items = json.loads(json.dumps(render(BundleConfig.model_validate_json(json.dumps(plain)))))
    assert "garage" not in json.dumps(items)
    assert "checkpoint" not in json.dumps(items)


def test_the_worker_projects_its_read_only_key_and_the_buckets():
    config = configured()
    assert {
        "checkpoint-upload.toml",
        "checkpoint-kept.toml",
        "checkpoint-s3-credentials",
        "checkpoint-s3-config",
    } <= set(projected_files(config, "worker"))
    assert "checkpoint-s3-credentials" not in projected_files(config, "control")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"kept_bucket": CHECKPOINT["upload_bucket"]}, "separate"),
        ({"hold_seconds": 3600, "deadline_seconds": 7200}, "outlast"),
        ({"endpoint": "http://user:pass@garage:3900"}, "endpoint"),
    ],
)
def test_unsafe_checkpoint_sections_are_refused(change, match):
    with pytest.raises(ValidationError, match=match):
        configured(**change)


def test_the_section_needs_the_sandbox_namespace():
    data = values()
    data["checkpoint"] = CHECKPOINT
    with pytest.raises(ValidationError, match="sandboxes"):
        BundleConfig.model_validate_json(json.dumps(data))


# The site's keys


def checkpoint_site(tmp_path: Path) -> dict[str, Any]:
    site = site_values(tmp_path)
    bundle = Path(tmp_path / "inputs/bundle.toml")
    from sandbox_science.launcher import toml

    data = tomllib.loads(bundle.read_text())
    bundle.write_text(toml.dumps(with_checkpoint(data)))
    keys = {}
    for name in ("upload", "verify"):
        for part in ("credentials", "config"):
            path = tmp_path / "inputs" / f"{name}-{part}"
            path.write_text(f"[checkpoint-{name}]\n# test-secret-{name}-{part}\n")
            path.chmod(0o600)
            keys[f"{name}_{part}"] = str(path)
    site["checkpoint"] = keys
    return site


def test_the_site_puts_each_key_where_it_belongs(tmp_path):
    files = site_plan(*load_site(site_file(tmp_path, checkpoint_site(tmp_path))))
    worker = "secrets/worker/"
    assert files[f"{worker}checkpoint-s3-credentials"].endswith(b"test-secret-verify-credentials\n")
    assert files["secrets/checkpoint-upload/credentials"].endswith(
        b"test-secret-upload-credentials\n"
    )
    # The upload key is never the worker's, and the worker's never the Job's.
    assert not any(
        b"test-secret-upload" in value for key, value in files.items() if key.startswith(worker)
    )
    for name, bucket in (
        ("upload", "research-checkpoint-upload"),
        ("kept", "research-checkpoints"),
    ):
        storage = Storage.model_validate(
            tomllib.loads(files[f"{worker}checkpoint-{name}.toml"].decode())
        )
        assert storage.bucket == bucket
        assert storage.endpoint == "http://garage.garage.svc.cluster.local:3900"
        keeper = Storage.model_validate(
            tomllib.loads(files[f"operator/checkpoint-{name}.toml"].decode())
        )
        assert keeper.bucket == bucket
        assert keeper.endpoint == "http://127.0.0.1:3900"
    script = files["secrets.sh"].decode()
    assert (
        f"kubectl -n {SANDBOXES} create secret generic research-checkpoint-upload \\\n"
        "  --from-file=credentials=secrets/checkpoint-upload/credentials \\\n"
        "  --from-file=config=secrets/checkpoint-upload/config"
    ) in script
    assert "test-secret" not in script


def test_the_site_and_the_bundle_agree_on_checkpoints(tmp_path):
    from sandbox_science.launcher.kubernetes_config import BundleError

    site = checkpoint_site(tmp_path)
    del site["checkpoint"]
    with pytest.raises(BundleError, match="checkpoint"):
        site_plan(*load_site(site_file(tmp_path, site)))


# The operator's commands


class Kubectl:
    """A scripted cluster: answers by the command's words, records every command."""

    def __init__(self, answers: dict[str, bytes | Exception]) -> None:
        self.answers = answers
        self.commands: list[tuple[list[str], bytes | None]] = []

    def __call__(self, arguments: Sequence[str], stdin: bytes | None) -> bytes:
        self.commands.append((list(arguments), stdin))
        line = " ".join(arguments)
        for key, answer in self.answers.items():
            if key in line:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return b""

    def ran(self, words: str) -> list[list[str]]:
        return [command for command, _ in self.commands if words in " ".join(command)]


def listing(*items: dict[str, Any]) -> bytes:
    return json.dumps({"items": list(items)}).encode()


def pvc(name: str, sandbox: str | None, volume: str, **annotations: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "annotations": annotations}
    if sandbox is not None:
        metadata["ownerReferences"] = [{"kind": "Sandbox", "name": sandbox}]
    return {"metadata": metadata, "spec": {"volumeName": volume}}


def pv(name: str, claim: str, phase: str, **annotations: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "annotations": annotations,
            "creationTimestamp": "2026-10-01T00:00:00Z",
        },
        "spec": {
            "claimRef": {"namespace": SANDBOXES, "name": claim},
            "capacity": {"storage": "20Gi"},
            "persistentVolumeReclaimPolicy": "Retain",
        },
        "status": {"phase": phase},
    }


def pod(claim: str) -> dict[str, Any]:
    return {
        "spec": {"volumes": [{"name": "w", "persistentVolumeClaim": {"claimName": claim}}]},
        "status": {"phase": "Running"},
    }


def operator(kubectl: Kubectl) -> Operator:
    return Operator(
        configured(), "test-operator", run=kubectl, sleep=lambda _s: None, out=lambda _l: None
    )


MANIFEST = "e" * 64


def test_one_checkpoint_command_plans_creates_waits_and_has_the_worker_verify():
    plan = a_plan()
    kubectl = Kubectl(
        {
            "checkpoint plan": plan.model_dump_json().encode(),
            "get pvc": listing(
                {
                    **pvc("workspace-default--ss-0123456789abcdef", "ss-0123456789abcdef", "pv-1"),
                    "metadata": {
                        "name": "workspace-default--ss-0123456789abcdef",
                        "ownerReferences": [{"kind": "Sandbox", "name": "ss-0123456789abcdef"}],
                        "annotations": {"volume.kubernetes.io/selected-node": "node-a"},
                    },
                }
            ),
            "get pods": listing(pod("someone-else")),
            "create -f -": b"job.batch/research-checkpoint-x1\n",
            "get job research-checkpoint-x1": json.dumps({"status": {"succeeded": 1}}).encode(),
            "logs job/research-checkpoint-x1": f"checkpoint_manifest={MANIFEST} id=x\n".encode(),
            "--stage upload": b"verified=...\n",
            "checkpoint promote": b"promoted=...\n",
            "--stage kept": b"kept=...\n",
        }
    )
    keeper = (Path("operator/checkpoint-upload.toml"), Path("operator/checkpoint-kept.toml"))
    assert operator(kubectl).run_checkpoint("crypto-main", "amd64", keeper) == MANIFEST
    order = [
        "checkpoint plan",
        "create -f -",
        "logs job/",
        "--stage upload",
        "checkpoint promote",
        "--stage kept",
    ]
    positions = [
        next(i for i, (c, _) in enumerate(kubectl.commands) if w in " ".join(c)) for w in order
    ]
    assert positions == sorted(positions)
    created = json.loads(next(stdin for c, stdin in kubectl.commands if "create" in c and stdin))
    assert (
        created["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"]
        is True
    )
    plan_command = kubectl.ran("checkpoint plan")[0]
    assert plan_command[:8] == [
        "kubectl",
        "-n",
        "research-test",
        "exec",
        "deploy/research-pool",
        "-c",
        "worker",
        "--",
    ]
    assert "--by" in plan_command
    assert plan_command[plan_command.index("--by") + 1] == "test-operator"
    verify = kubectl.ran("--stage upload")[0]
    assert "AWS_PROFILE=checkpoint-verify" in verify  # the worker's read-only key
    promote = kubectl.ran("checkpoint promote")[0]
    assert promote[0] == "sandbox-science-worker"  # locally, never in the cluster
    assert not kubectl.ran("release-hold")


@pytest.mark.parametrize("failure", ["mounted", "job-failed", "verify-refused"])
def test_a_failed_checkpoint_lifts_its_hold(failure):
    claim = "workspace-default--ss-0123456789abcdef"
    kubectl = Kubectl(
        {
            "checkpoint plan": a_plan().model_dump_json().encode(),
            "get pvc": listing(pvc(claim, "ss-0123456789abcdef", "pv-1")),
            "get pods": listing(pod(claim if failure == "mounted" else "other")),
            "create -f -": b"job.batch/research-checkpoint-x1\n",
            "get job": json.dumps(
                {"status": {"failed": 1} if failure == "job-failed" else {"succeeded": 1}}
            ).encode(),
            "logs job/": f"checkpoint_manifest={MANIFEST}\n".encode(),
            "--stage upload": OperationError("the checkpoint archive differs"),
        }
    )
    expected = {"mounted": "mounts", "job-failed": "failed", "verify-refused": "differs"}
    with pytest.raises(OperationError, match=expected[failure]):
        operator(kubectl).run_checkpoint("crypto-main", "amd64", None)
    assert len(kubectl.ran("release-hold")) == 1
    assert not kubectl.ran("--stage kept")
    assert not kubectl.ran("checkpoint promote")
    if failure == "mounted":
        assert not kubectl.ran("create")


def test_an_orphaned_claim_is_released_to_its_provisioner_then_deleted():
    pvs = [pv("pv-orphan", "workspace-default--ss-gone", "Bound")]
    pvcs = [pvc("workspace-default--ss-gone", "ss-gone", "pv-orphan")]
    found = survey(pvs, pvcs, [], [], inventory(), SANDBOXES, NOW)
    steps = operator(Kubectl({})).delete(
        found, ["pv-orphan"], [], without_checkpoint=False, keeper_kept=None
    )
    words = [" ".join(step.command) for step in steps]
    assert "record-volume --phase intent" in words[0]
    assert words[1].startswith("kubectl patch pv pv-orphan")
    assert words[2] == f"kubectl -n {SANDBOXES} delete pvc workspace-default--ss-gone"
    assert "record-volume --phase done" in words[3]


def test_without_apply_the_checkpoint_steps_are_printed(tmp_path, capsys, monkeypatch):
    from sandbox_science.launcher import toml

    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint()))

    def never(_arguments: Sequence[str], _stdin: bytes | None) -> bytes:
        raise AssertionError("nothing runs without --apply")

    monkeypatch.setattr(kubernetes_cli, "run_command", never)
    assert (
        kubernetes_cli.main(["checkpoint", "--config", str(config), "--environment", "crypto-main"])
        == 0
    )
    printed = capsys.readouterr().out
    assert "checkpoint plan --environment crypto-main" in printed
    assert "research-checkpoint-upload" in printed  # the Secret it references
    assert "--stage kept" in printed


def environment(
    environment_id: str,
    sandbox: str,
    state: Literal["creating", "ready", "stopped", "deleted"],
    **fields: Any,
) -> InventoryEnvironment:
    return InventoryEnvironment(
        environment_id=environment_id,
        project_id="crypto",
        kind="session",
        state=state,
        sandbox=sandbox,
        **fields,
    )


def kept(sandbox: str, n: int, state: CheckpointState = "kept") -> InventoryCheckpoint:
    return InventoryCheckpoint(
        checkpoint_id="ckpt-" + f"{n:024x}",
        environment_id=f"env-{sandbox}",
        project_id="crypto",
        sandbox=sandbox,
        sandbox_id=f"{sandbox}-id",
        manifest_sha256=f"{n:064x}",
        archive_size=1000,
        content_bytes=900,
        capture="suspended_pvc",
        skipped=0,
        state=state,
        created_at=NOW - timedelta(days=10 - n),
        verified_at=NOW,
        kept_at=NOW,
    )


def cluster() -> tuple[list[dict[str, Any]], ...]:
    pvs = [
        pv("pv-gone", "workspace-default--ss-gone", "Released"),
        pv("pv-nockpt", "workspace-default--ss-nockpt", "Released"),
        pv("pv-live", "workspace-default--ss-live", "Bound"),
        pv("pv-paused", "workspace-default--ss-paused", "Released"),
        pv("pv-child", "workspace-default--ss-child", "Released"),
        pv(
            "pv-kept",
            "workspace-default--ss-gone2",
            "Released",
            **{"sandbox-science.io/keep": "recovery set 7"},
        ),
        pv("pv-orphan", "workspace-default--ss-orphan", "Bound"),
        pv("pv-held", "workspace-default--ss-held", "Released"),
    ]
    pvcs = [
        pvc("workspace-default--ss-live", "ss-live", "pv-live"),
        pvc("workspace-default--ss-orphan", "ss-orphan", "pv-orphan"),
    ]
    pods = [pod("workspace-default--ss-orphan")]
    sandboxes = [{"metadata": {"name": "ss-live"}}]
    return pvs, pvcs, pods, sandboxes


def inventory() -> CheckpointInventory:
    return CheckpointInventory(
        generated_at=NOW,
        keep=2,
        environments=(
            environment("e-gone", "ss-gone", "deleted", session_id="main"),
            environment("e-nockpt", "ss-nockpt", "deleted"),
            environment("e-live", "ss-live", "ready"),
            environment("e-paused", "ss-paused", "stopped"),
            environment("e-child", "ss-child", "deleted", hand_back="retained"),
            environment("e-held", "ss-held", "deleted"),
        ),
        checkpoints=(
            kept("ss-gone", 1),
            kept("ss-gone", 2),
            kept("ss-gone", 3),
            kept("ss-paused", 4),
            kept("ss-live", 5, "deleting"),
        ),
        holds=(
            InventoryHold(
                checkpoint_id="ckpt-" + "f" * 24,
                environment_id="e-held",
                sandbox="ss-held",
                expires_at=NOW + timedelta(hours=1),
            ),
        ),
    )


def surveyed():
    pvs, pvcs, pods, sandboxes = cluster()
    return survey(pvs, pvcs, pods, sandboxes, inventory(), SANDBOXES, NOW)


def test_cleanup_lists_candidates_with_owner_size_age_and_checkpoint():
    found = surveyed()
    rows = {row.volume: row for row in found.volumes}
    assert set(rows) == {
        "pv-gone",
        "pv-nockpt",
        "pv-paused",
        "pv-child",
        "pv-kept",
        "pv-orphan",
        "pv-held",
    }
    assert "pv-live" not in rows  # bound to a sandbox that exists: not left behind
    assert rows["pv-gone"].refusals == ()
    assert rows["pv-gone"].kept_checkpoints == 3
    assert rows["pv-nockpt"].kept_checkpoints == 0
    text = "\n".join(report(found))
    assert "volume pv-gone 20Gi age 28d Released" in text
    assert "project crypto session main environment e-gone (deleted)" in text
    assert "3 verified checkpoint(s); candidate" in text
    assert "no verified checkpoint" in text


@pytest.mark.parametrize(
    ("volume", "reason"),
    [
        ("pv-paused", "environment e-paused is stopped"),
        ("pv-child", "hand-back is retained"),
        ("pv-kept", "marked to keep (recovery set 7)"),
        ("pv-orphan", "a Pod mounts it"),
        ("pv-held", "a checkpoint holds it"),
    ],
)
def test_cleanup_refuses_protected_volumes(volume, reason):
    found = surveyed()
    row = next(row for row in found.volumes if row.volume == volume)
    assert any(reason in refusal for refusal in row.refusals)
    with pytest.raises(OperationError, match="refused"):
        operator(Kubectl({})).delete(found, [volume], [], without_checkpoint=True, keeper_kept=None)


def test_a_volume_without_a_verified_checkpoint_needs_the_flag():
    found = surveyed()
    run = operator(Kubectl({}))
    with pytest.raises(OperationError, match="--without-checkpoint"):
        run.delete(found, ["pv-nockpt"], [], without_checkpoint=False, keeper_kept=None)
    steps = run.delete(found, ["pv-nockpt"], [], without_checkpoint=True, keeper_kept=None)
    assert "--without-checkpoint" in steps[0].command
    with pytest.raises(OperationError, match="not a cleanup candidate"):
        run.delete(found, ["pv-live"], [], without_checkpoint=True, keeper_kept=None)


def test_only_checkpoints_beyond_the_kept_count_are_candidates():
    found = surveyed()
    marks = {mark.checkpoint.manifest_sha256: mark for mark in found.checkpoints}
    # ss-gone has three kept: the oldest is a candidate; ss-paused's only one is not.
    assert set(marks) == {f"{1:064x}", f"{5:064x}"}  # and a deletion to finish
    assert marks[f"{1:064x}"].volume_exists  # its Released PV still exists
    run = operator(Kubectl({}))
    with pytest.raises(OperationError, match="not a cleanup candidate"):
        run.delete(found, [], [f"{4:064x}"], without_checkpoint=False, keeper_kept=None)
    with pytest.raises(OperationError, match="not a cleanup candidate"):
        run.delete(found, [], [f"{3:064x}"], without_checkpoint=False, keeper_kept=None)


def test_deletions_journal_intent_first_and_name_the_operator():
    found = surveyed()
    kubectl = Kubectl({})
    run = operator(kubectl)
    steps = run.delete(
        found, ["pv-gone"], [f"{1:064x}"], without_checkpoint=False, keeper_kept=Path("kept.toml")
    )
    words = [" ".join(step.command) for step in steps]
    assert "record-volume --phase intent --volume pv-gone --sandbox ss-gone" in words[0]
    assert "--by test-operator" in words[0]
    assert words[1].startswith("kubectl patch pv pv-gone --type=merge")
    assert "record-volume --phase done" in words[2]
    assert "forget --manifest " + f"{1:064x}" + " --by test-operator" in words[3]
    assert "--volume-gone" not in words[3]  # its volume exists
    assert words[4].startswith(
        "sandbox-science-worker checkpoint delete-objects --storage kept.toml"
    )
    assert "forget --manifest " + f"{1:064x}" + " --done" in words[5]
    run.apply(steps)
    assert [" ".join(c) for c, _ in kubectl.commands] == words


def test_a_failed_volume_deletion_is_journaled_and_stops():
    found = surveyed()
    kubectl = Kubectl(
        {"delete pvc": OperationError("forbidden"), "patch pv": OperationError("forbidden")}
    )
    run = operator(kubectl)
    steps = run.delete(found, ["pv-gone"], [], without_checkpoint=False, keeper_kept=None)
    with pytest.raises(OperationError, match="forbidden"):
        run.apply(steps)
    assert kubectl.ran("--phase failed")
    assert not kubectl.ran("--phase done")


def test_the_cleanup_command_asks_before_deleting(tmp_path, monkeypatch, capsys):
    from sandbox_science.launcher import toml

    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint()))
    pvs, pvcs, pods, sandboxes = cluster()
    kubectl = Kubectl(
        {
            "get pv ": listing(*pvs),
            "get pvc": listing(*pvcs),
            "get pods": listing(*pods),
            "get sandboxes": listing(*sandboxes),
            "checkpoint inventory": inventory().model_dump_json().encode(),
        }
    )
    monkeypatch.setattr(kubernetes_cli, "run_command", kubectl)
    base = ["cleanup", "--config", str(config), "--operator", "test-operator"]
    assert kubernetes_cli.main(base) == 0
    assert "volume pv-gone" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert kubernetes_cli.main([*base, "--volume", "pv-gone", "--apply"]) == 1
    assert not kubectl.ran("record-volume")
    monkeypatch.setattr("builtins.input", lambda _prompt: "delete")
    assert kubernetes_cli.main([*base, "--volume", "pv-gone", "--apply"]) == 0
    assert kubectl.ran("patch pv pv-gone")
    assert kubernetes_cli.main([*base, "--volume", "pv-paused", "--apply", "--yes"]) == 1
    assert "environment e-paused is stopped" in capsys.readouterr().err
