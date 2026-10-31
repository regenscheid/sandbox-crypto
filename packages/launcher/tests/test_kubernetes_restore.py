# SPDX-License-Identifier: MIT
"""Restoring a kept checkpoint on Kubernetes, offline: the rendered restore Job, the site's
restore key, the operator's ``restore`` command against a scripted ``kubectl`` (order,
failure, resume, discard), and cleanup's upload expiry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts.workspace_checkpoint import RestorePlan, RestoreState
from sandbox_science.launcher import kubernetes_cli, toml
from sandbox_science.launcher.kubernetes_checkpoint import (
    RESTORE_VARIABLE,
    STORAGE_VARIABLE,
    restore_job,
)
from sandbox_science.launcher.kubernetes_operations import OperationError, Operator
from sandbox_science.launcher.kubernetes_site import plan as site_plan
from sandbox_science.launcher.kubernetes_site_config import load as load_site
from sandbox_science.worker import checkpoint_jobs_cli
from sandbox_science.worker.checkpoint_s3 import parse

from .test_kubernetes_checkpoint import (
    NOW,
    SANDBOXES,
    Kubectl,
    checkpoint_site,
    configured,
    listing,
    pod,
    pvc,
    source,
    with_checkpoint,
)
from .test_kubernetes_site import site_file

RESTORE_SECRET = "research-checkpoint-restore"
TARGET_SANDBOX = "ss-fedcba9876543210"
MANIFEST = "e" * 64
TREE = "7" * 64
ATTEMPT = "a1" * 16


def a_restore() -> RestorePlan:
    target = source(TARGET_SANDBOX).model_copy(update={"environment_id": "crypto-restored"})
    return RestorePlan(
        restore_id="rst-" + "a" * 24,
        checkpoint_id="ckpt-" + "c" * 24,
        manifest_sha256=MANIFEST,
        source=source(),
        target=target,
        tree_sha256=TREE,
        planned_at=NOW,
    )


def restoring(**changes: Any) -> Any:
    return configured(restore_secret=RESTORE_SECRET, **changes)


def rendered() -> dict[str, Any]:
    return json.loads(
        json.dumps(restore_job(restoring(), a_restore(), "workspace-t", "node-b", ATTEMPT))
    )


# The Job


def test_the_restore_job_writes_the_targets_claim_with_a_read_only_key_and_no_token():
    job = rendered()
    assert job["metadata"]["generateName"] == "research-restore-"
    assert job["metadata"]["namespace"] == SANDBOXES
    labels = job["spec"]["template"]["metadata"]["labels"]
    # The checkpoint Jobs' NetworkPolicy selects these: DNS and the store, nothing else.
    assert labels["app.kubernetes.io/component"] == "checkpoint"
    assert labels["sandbox-science.io/restore-id"] == "rst-" + "a" * 24
    assert "sandbox-science.io/checkpoint-id" not in labels
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert "fsGroup" not in pod_spec["securityContext"]
    assert pod_spec["securityContext"]["runAsUser"] == 1000  # the research images' sandbox user
    assert pod_spec["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    terms = pod_spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms["nodeSelectorTerms"][0]["matchFields"][0]["values"] == ["node-b"]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert volumes["workspace"]["persistentVolumeClaim"] == {
        "claimName": "workspace-t",
        "readOnly": False,
    }
    assert volumes["restore-key"]["secret"]["secretName"] == RESTORE_SECRET
    container = pod_spec["containers"][0]
    assert container["command"][:3] == ["sandbox-science-worker", "checkpoint", "restore-write"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    env = {item["name"]: item["value"] for item in container["env"]}
    assert RestorePlan.model_validate_json(env[RESTORE_VARIABLE]) == a_restore()
    assert parse(env[STORAGE_VARIABLE]).bucket == "research-checkpoints"  # the kept bucket
    assert "research-checkpoint-upload" not in json.dumps(env)
    assert env["AWS_PROFILE"] == "checkpoint-restore"
    assert env["SS_RESTORE_ATTEMPT"] == ATTEMPT
    assert RESTORE_VARIABLE == checkpoint_jobs_cli.RESTORE_VARIABLE


def test_without_a_restore_secret_no_restore_job_is_rendered():
    with pytest.raises(ValueError, match="restore_secret"):
        restore_job(configured(), a_restore(), "workspace-t", None, ATTEMPT)
    with pytest.raises(ValueError, match="nonce"):
        restore_job(restoring(), a_restore(), "workspace-t", None, "not-a-nonce")
    with pytest.raises(ValueError, match="separate"):
        configured(restore_secret="research-checkpoint-upload")


def test_the_restore_job_command_writes_one_private_job(tmp_path):
    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint(restore_secret=RESTORE_SECRET)))
    (tmp_path / "restore.json").write_text(a_restore().model_dump_json())
    output = tmp_path / "job.json"
    arguments = ["restore-job", "--config", str(config), "--plan", str(tmp_path / "restore.json")]
    arguments += ["--attempt", ATTEMPT]
    assert kubernetes_cli.main([*arguments, "--claim", "workspace-t", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["metadata"]["generateName"] == "research-restore-"
    assert output.stat().st_mode & 0o077 == 0


# The site's restore key


def test_the_site_puts_the_restore_key_in_its_own_secret(tmp_path):
    site = checkpoint_site(tmp_path)
    bundle = Path(tmp_path / "inputs/bundle.toml")
    import tomllib

    data = tomllib.loads(bundle.read_text())
    data["checkpoint"]["restore_secret"] = RESTORE_SECRET
    bundle.write_text(toml.dumps(data))
    with pytest.raises(Exception, match="restore_credentials"):
        site_plan(*load_site(site_file(tmp_path, site)))
    for part in ("credentials", "config"):
        path = tmp_path / "inputs" / f"restore-{part}"
        path.write_text(f"[checkpoint-restore]\n# test-secret-restore-{part}\n")
        path.chmod(0o600)
        site["checkpoint"][f"restore_{part}"] = str(path)
    files = site_plan(*load_site(site_file(tmp_path, site)))
    assert files["secrets/checkpoint-restore/credentials"].endswith(
        b"test-secret-restore-credentials\n"
    )
    assert not any(
        b"test-secret-restore" in value
        for key, value in files.items()
        if not key.startswith("secrets/checkpoint-restore/")
    )
    script = files["secrets.sh"].decode()
    assert (
        f"kubectl -n {SANDBOXES} create secret generic {RESTORE_SECRET} \\\n"
        "  --from-file=credentials=secrets/checkpoint-restore/credentials"
    ) in script


# The operator's command


def target_claim() -> dict[str, Any]:
    claim = f"workspace-default--{TARGET_SANDBOX}"
    return {
        **pvc(claim, f"default--{TARGET_SANDBOX}", "pv-t"),
        "metadata": {
            "name": claim,
            "ownerReferences": [{"kind": "Sandbox", "name": f"default--{TARGET_SANDBOX}"}],
            "annotations": {"volume.kubernetes.io/selected-node": "node-b"},
        },
    }


def restore_run(**answers: bytes | Exception) -> tuple[Kubectl, list[str], Operator]:
    kubectl = Kubectl(
        {
            "checkpoint restore-plan": a_restore().model_dump_json().encode(),
            "get pvc": listing(target_claim()),
            "get pods": listing(pod("someone-else")),
            "restore-attempt": f"restore_attempt={ATTEMPT}\n".encode(),
            "create -f -": b"job.batch/research-restore-r1\n",
            "get job research-restore-r1": json.dumps({"status": {"succeeded": 1}}).encode(),
            "logs job/research-restore-r1": (
                f"restore_tree={TREE} restore_attempt={ATTEMPT} restore_id=x\n".encode()
            ),
            "restore-verify": b"restored=...\n",
            **answers,
        }
    )
    printed: list[str] = []
    operator = Operator(
        restoring(), "test-operator", run=kubectl, sleep=lambda _s: None, out=printed.append
    )
    return kubectl, printed, operator


def test_one_restore_command_plans_creates_journals_waits_and_has_the_worker_verify():
    kubectl, printed, operator = restore_run()
    key = Path("secrets/checkpoint-restore")
    assert operator.run_restore(MANIFEST, "crypto-restored", key=key) == a_restore()
    order = [
        "checkpoint restore-plan",
        "restore-attempt",
        f"create secret generic {RESTORE_SECRET}",
        "create -f -",
        "restore-record --restore-id",
        "logs job/",
        "restore-verify",
        "delete job research-restore-r1",
        f"delete secret {RESTORE_SECRET}",
    ]
    positions = [
        next(i for i, (c, _) in enumerate(kubectl.commands) if w in " ".join(c)) for w in order
    ]
    assert positions == sorted(positions)
    planning = kubectl.ran("checkpoint restore-plan")[0]
    assert "AWS_PROFILE=checkpoint-verify" in planning  # the worker reads the kept copy itself
    assert planning[planning.index("--target") + 1] == "crypto-restored"
    assert planning[planning.index("--by") + 1] == "test-operator"
    journal = kubectl.ran("restore-record")[0]
    assert json.loads(journal[journal.index("--detail") + 1]) == {
        "claim": f"workspace-default--{TARGET_SANDBOX}",
        "job": "research-restore-r1",
        "node": "node-b",
        "attempt": ATTEMPT,
    }
    verify = kubectl.ran("restore-verify")[0]
    assert verify[verify.index("--tree") + 1] == TREE
    assert verify[verify.index("--attempt") + 1] == ATTEMPT
    secret = kubectl.ran("create secret generic")[0]
    assert f"--from-file=credentials={key / 'credentials'}" in secret
    created = json.loads(next(stdin for c, stdin in kubectl.commands if "create" in c and stdin))
    claim = created["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]
    assert claim["readOnly"] is False
    assert (
        f"job research-restore-r1: restore_tree={TREE} restore_attempt={ATTEMPT} restore_id=x"
        in (printed)
    )
    assert not kubectl.ran("--phase failed")


@pytest.mark.parametrize("failure", ["mounted", "job-failed", "verify-refused"])
def test_a_failed_restore_is_journaled_and_its_target_stays_held(failure):
    answers: dict[str, bytes | Exception] = {}
    if failure == "mounted":
        answers["get pods"] = listing(pod(f"workspace-default--{TARGET_SANDBOX}"))
    if failure == "job-failed":
        answers["get job research-restore-r1"] = json.dumps({"status": {"failed": 1}}).encode()
    if failure == "verify-refused":
        answers["restore-verify"] = OperationError("the restored tree differs")
    kubectl, printed, operator = restore_run(**answers)
    with pytest.raises(OperationError, match="--resume rst-a") as raised:
        operator.run_restore(MANIFEST, "crypto-restored", key=Path("keys"))
    assert "held stopped" in str(raised.value)
    assert len(kubectl.ran("--phase failed")) == 1
    assert not kubectl.ran("release-hold")  # a restore's hold has no early release
    if failure == "mounted":
        assert not kubectl.ran("create")
        assert not kubectl.ran("delete secret")
    else:
        assert kubectl.ran(f"-n {SANDBOXES} delete job research-restore-r1")
        assert kubectl.ran(f"-n {SANDBOXES} delete secret {RESTORE_SECRET}")
        # The failed Job's log stays in the operator's output.
        assert any(line.startswith("job research-restore-r1: restore_tree=") for line in printed)


def test_a_report_from_another_attempt_is_not_passed_on():
    stale = f"restore_tree={TREE} restore_attempt={'b2' * 16}\n".encode()
    kubectl, _, operator = restore_run(**{"logs job/research-restore-r1": stale})
    with pytest.raises(OperationError, match="for its attempt"):
        operator.run_restore(MANIFEST, "crypto-restored")
    assert not kubectl.ran("restore-verify")


def test_a_secret_that_cannot_be_deleted_is_reported_loudly():
    _, printed, operator = restore_run(**{"delete secret": OperationError("forbidden")})
    operator.run_restore(MANIFEST, "crypto-restored", key=Path("keys"))
    assert any("delete it by hand" in line for line in printed)


def test_a_restore_resumes_by_its_id():
    kubectl, _, operator = restore_run()
    operator.run_restore(None, None, resume="rst-" + "a" * 24)
    assert not kubectl.ran("create secret")  # a standing Secret
    planning = kubectl.ran("checkpoint restore-plan")[0]
    assert planning[planning.index("--resume") + 1] == "rst-" + "a" * 24
    assert "--manifest" not in planning


def test_without_a_restore_secret_nothing_runs():
    kubectl = Kubectl({})
    operator = Operator(configured(), "op", run=kubectl, sleep=lambda _s: None, out=print)
    with pytest.raises(OperationError, match="restore_secret"):
        operator.run_restore(MANIFEST, "crypto-restored")
    assert kubectl.commands == []


def test_the_restore_command_prints_its_steps_and_asks_before_a_discard(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint(restore_secret=RESTORE_SECRET)))
    kubectl = Kubectl({"restore-discard": b"discarded=rst-x\n"})
    monkeypatch.setattr(kubernetes_cli, "run_command", kubectl)
    base = ["restore", "--config", str(config), "--operator", "test-operator"]
    assert kubernetes_cli.main([*base, "--checkpoint", MANIFEST, "--target", "e-new"]) == 0
    printed = capsys.readouterr().out
    assert "restore-plan --manifest " + MANIFEST in printed
    assert RESTORE_SECRET in printed
    assert "restore-verify" in printed
    assert f"delete secret {RESTORE_SECRET}" in printed
    assert kubectl.commands == []  # nothing runs without --apply
    apply = [*base, "--checkpoint", MANIFEST, "--target", "e-new", "--apply"]
    assert kubernetes_cli.main(apply) == 1
    assert "--restore-key" in capsys.readouterr().err
    assert kubernetes_cli.main([*base, "--checkpoint", MANIFEST]) == 1
    assert kubernetes_cli.main([*base, "--resume", "rst-x", "--discard", "rst-x"]) == 1
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert kubernetes_cli.main([*base, "--discard", "rst-x", "--apply"]) == 1
    assert not kubectl.ran("restore-discard")
    assert kubernetes_cli.main([*base, "--discard", "rst-x", "--apply", "--yes"]) == 0
    discard = kubectl.ran("restore-discard")[0]
    assert discard[discard.index("--by") + 1] == "test-operator"


# Upload expiry


def test_upload_expiry_journals_lists_protected_objects_and_removes_the_rest(tmp_path):
    kubectl = Kubectl({"upload-expiry --phase intent": b"a" * 64 + b"\n"})
    operator = Operator(configured(), "test-operator", run=kubectl, out=lambda _l: None)
    steps = operator.expire_uploads(168, Path("operator/checkpoint-upload.toml"), tmp_path)
    words = [" ".join(step.command) for step in steps]
    assert "upload-expiry --phase intent --older-than-hours 168" in words[0]
    assert words[1].startswith("sandbox-science-worker checkpoint expire-objects")
    assert "--protected-file " + str(tmp_path / "protected-uploads.txt") in words[1]
    assert "upload-expiry --phase done" in words[2]
    assert steps[1].on_failure is not None
    assert "--phase failed" in " ".join(steps[1].on_failure)
    kubectl.answers["checkpoint expire-objects"] = b"expired=3 kept=2 protected_names=1\n"
    operator.apply(steps)
    assert (tmp_path / "protected-uploads.txt").read_bytes() == b"a" * 64 + b"\n"
    done = kubectl.ran("upload-expiry --phase done")[0]
    assert json.loads(done[done.index("--detail") + 1]) == {
        "expired": 3,
        "kept": 2,
        "protected_names": 1,
    }
    with pytest.raises(OperationError, match="8760"):
        operator.expire_uploads(0, None, tmp_path)


def test_the_cleanup_command_expires_uploads_only_with_the_keeper_key_and_its_sdk(
    tmp_path, monkeypatch, capsys
):
    from .test_kubernetes_checkpoint import inventory

    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint()))
    kubectl = Kubectl(
        {
            "get pv ": listing(),
            "get pvc": listing(),
            "get pods": listing(),
            "get sandboxes": listing(),
            "get jobs": listing(),
            "checkpoint inventory": inventory().model_dump_json().encode(),
        }
    )
    monkeypatch.setattr(kubernetes_cli, "run_command", kubectl)
    base = ["cleanup", "--config", str(config), "--operator", "test-operator"]
    assert kubernetes_cli.main([*base, "--expire-uploads", "168"]) == 0
    assert "expire-objects" in capsys.readouterr().out
    assert kubernetes_cli.main([*base, "--expire-uploads", "168", "--apply", "--yes"]) == 1
    assert "--keeper-upload" in capsys.readouterr().err
    kubectl.answers["checkpoint sdk-check"] = OperationError("No module named 'boto3'")
    keeper = ["--keeper-upload", str(tmp_path / "upload.toml")]
    assert kubernetes_cli.main([*base, "--expire-uploads", "168", *keeper, "--apply"]) == 1
    assert "uv sync --extra s3" in capsys.readouterr().err
    assert not kubectl.ran("upload-expiry")
    # The check asks the very program the expiry step runs.
    assert kubectl.ran("checkpoint sdk-check")[0][0] == "sandbox-science-worker"
    del kubectl.answers["checkpoint sdk-check"]
    assert kubernetes_cli.main([*base, "--expire-uploads", "168", *keeper, "--apply", "--yes"]) == 0
    assert kubectl.ran("upload-expiry --phase intent")
    assert kubectl.ran("checkpoint expire-objects")
    assert kubectl.ran("upload-expiry --phase done")


def test_a_checkpoint_with_keeper_files_needs_the_sdk_before_anything_runs(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "bundle.toml"
    config.write_text(toml.dumps(with_checkpoint()))
    kubectl = Kubectl({"checkpoint sdk-check": OperationError("No module named 'boto3'")})
    monkeypatch.setattr(kubernetes_cli, "run_command", kubectl)
    arguments = [
        "checkpoint",
        "--config",
        str(config),
        "--environment",
        "crypto-main",
        "--operator",
        "op",
        "--apply",
        "--keeper-upload",
        "u.toml",
        "--keeper-kept",
        "k.toml",
    ]
    assert kubernetes_cli.main(arguments) == 1
    assert "boto3" in capsys.readouterr().err
    assert [" ".join(c) for c, _ in kubectl.commands] == [
        "sandbox-science-worker checkpoint sdk-check"
    ]


# Cleanup beside restores


def test_after_a_restore_the_old_sandboxs_checkpoints_are_retirable_unless_one_is_read():
    from sandbox_science.contracts.workspace_checkpoint import (
        CheckpointInventory,
        InventoryRestore,
    )
    from sandbox_science.launcher.kubernetes_operations import survey

    from .test_kubernetes_checkpoint import environment, kept

    def found(restore_state: RestoreState | None) -> Any:
        restores = (
            ()
            if restore_state is None
            else (
                InventoryRestore(
                    restore_id="rst-" + "a" * 24,
                    environment_id="e-old",
                    sandbox="ss-new",
                    manifest_sha256=f"{2:064x}",
                    state=restore_state,
                ),
            )
        )
        inventory_ = CheckpointInventory(
            generated_at=NOW,
            keep=2,
            # The ID was created again by the restore: its sandbox is another one now.
            environments=(environment("e-old", "ss-new", "ready"),),
            checkpoints=tuple(
                kept("ss-old", n).model_copy(update={"environment_id": "e-old"}) for n in (1, 2)
            ),
            holds=(),
            restores=restores,
        )
        return survey([], [], [], [], inventory_, SANDBOXES, NOW)

    marks = found(None).checkpoints
    assert {mark.retire for mark in marks} == {True}
    assert not any(mark.refusals for mark in marks)
    operator = Operator(configured(), "op", run=Kubectl({}), out=lambda _l: None)
    steps = operator.delete(
        found(None), [], [], without_checkpoint=[], keeper_kept=None, retire=["e-old"]
    )
    assert sum("forget" in " ".join(step.command) for step in steps) == 4  # intent and done
    reading = {m.checkpoint.manifest_sha256: m.refusals for m in found("failed").checkpoints}
    assert reading[f"{2:064x}"] == (f"restore rst-{'a' * 24} reads it",)
    with pytest.raises(OperationError, match="reads it"):
        operator.delete(
            found("planned"), [], [], without_checkpoint=[], keeper_kept=None, retire=["e-old"]
        )
    assert not any(m.refusals for m in found("restored").checkpoints)
