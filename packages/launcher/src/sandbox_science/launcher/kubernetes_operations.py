# SPDX-License-Identifier: MIT
"""The operator's cluster commands for workspace checkpoints and cleanup (D92,
docs/KUBERNETES-BUNDLE.md#workspace-checkpoints and #cleanup).

They run ``kubectl`` with the operator's own credentials, as the docs' other operator
steps do, and the worker's commands inside its container (``kubectl exec``): the worker
decides what its records allow and journals every deletion, but holds no Kubernetes
right. Promotion and deletion of checkpoint objects run locally with the operator's
keeper key (``sandbox-science-worker checkpoint promote|delete-objects``), which never
enters the cluster. Without ``--apply`` each command only prints what it would run.
"""

from __future__ import annotations

import getpass
import json
import re
import shlex
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sandbox_science.contracts.workspace_checkpoint import (
    CheckpointInventory,
    CheckpointPlan,
    InventoryCheckpoint,
    InventoryEnvironment,
)

from .kubernetes_bundle import PRIVATE
from .kubernetes_checkpoint import checkpoint_job
from .kubernetes_config import BundleConfig, Checkpoint

Runner = Callable[[Sequence[str], bytes | None], bytes]
"""Runs one command with optional standard input; its standard output, or OperationError."""
KEEP_ANNOTATION = "sandbox-science.io/keep"
"""On a workspace's PV or PVC: part of a recovery set, never a cleanup candidate."""
SELECTED_NODE = "volume.kubernetes.io/selected-node"
WORKER_SETTINGS = f"{PRIVATE}/worker.toml"
_MANIFEST = re.compile(r"^checkpoint_manifest=([0-9a-f]{64})\b", re.MULTILINE)
MAX_OUTPUT = 64 * 1024 * 1024


class OperationError(Exception):
    """A step failed or was refused; the message says which and why."""


@dataclass(frozen=True)
class Step:
    """One command of a deletion, and what to journal if it fails."""

    command: list[str]
    on_failure: list[str] | None = None


def run_command(arguments: Sequence[str], stdin: bytes | None) -> bytes:
    """The real runner: no shell, output captured, the command's error in the message."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument vectors, no shell
            list(arguments), input=stdin, capture_output=True, check=False, timeout=86400
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OperationError(f"{arguments[0]} could not run: {error}") from None
    if done.returncode:
        message = done.stderr.decode(errors="replace").strip()[-800:]
        raise OperationError(f"{' '.join(arguments[:4])} … failed: {message}")
    if len(done.stdout) > MAX_OUTPUT:
        raise OperationError(f"{arguments[0]} printed more than expected")
    return done.stdout


def whoami(run: Runner, kubectl: Sequence[str]) -> str:
    """Who runs the command, as the deletion journal names them: the cluster's name for the
    kubeconfig's identity, and the local account."""
    local = f"{getpass.getuser()}@{socket.gethostname()}"
    try:
        user = run([*kubectl, "auth", "whoami", "-o", "jsonpath={.status.userInfo.username}"], None)
    except OperationError:
        return local[:256]
    return f"{user.decode(errors='replace').strip()[:200]} ({local})"[:256]


@dataclass
class Operator:
    """One operator's session against one bundle's cluster."""

    config: BundleConfig
    actor: str
    run: Runner = run_command
    kubectl: tuple[str, ...] = ("kubectl",)
    worker: tuple[str, ...] = ("sandbox-science-worker",)
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = print

    @property
    def checkpoint(self) -> Checkpoint:
        if self.config.checkpoint is None:
            raise OperationError("the bundle has no [checkpoint] section")
        return self.config.checkpoint

    @property
    def sandbox_namespace(self) -> str:
        if self.config.sandboxes is None:
            raise OperationError("the bundle has no [sandboxes] section")
        return self.config.sandboxes.namespace

    def exec_worker(self, *arguments: str, key: bool = False) -> list[str]:
        """``sandbox-science-worker checkpoint …`` in the pool's worker container; with
        ``key``, under the worker's read-only checkpoint key."""
        environment = (
            [
                "env",
                f"AWS_SHARED_CREDENTIALS_FILE={PRIVATE}/checkpoint-s3-credentials",
                f"AWS_CONFIG_FILE={PRIVATE}/checkpoint-s3-config",
                f"AWS_PROFILE={self.checkpoint.verify_profile}",
                "AWS_EC2_METADATA_DISABLED=true",
            ]
            if key
            else []
        )
        return [
            *self.kubectl,
            "-n",
            self.config.namespace,
            "exec",
            f"deploy/{self.config.name}-pool",
            "-c",
            "worker",
            "--",
            *environment,
            "sandbox-science-worker",
            "checkpoint",
            *arguments,
            "--settings",
            WORKER_SETTINGS,
        ]

    def get(self, kind: str, *, namespaced: bool = True) -> list[dict[str, object]]:
        namespace = ["-n", self.sandbox_namespace] if namespaced else []
        raw = self.run([*self.kubectl, *namespace, "get", kind, "-o", "json"], None)
        items = _dict(json.loads(raw)).get("items")
        if not isinstance(items, list):
            raise OperationError(f"kubectl get {kind} returned no list")
        return [
            cast(dict[str, object], item)
            for item in cast(list[object], items)
            if isinstance(item, dict)
        ]

    # One checkpoint

    def checkpoint_steps(
        self, environment: str, architecture: str, keeper: tuple[Path, Path] | None
    ) -> list[str]:
        """What ``checkpoint --apply`` runs, for an operator to read or run by hand."""
        sandbox_ns, checkpoint = self.sandbox_namespace, self.checkpoint
        upload, kept = keeper or (Path("UPLOAD.toml"), Path("KEPT.toml"))

        def line(arguments: Sequence[str]) -> str:
            return " ".join(_quote(part) for part in arguments)

        return [
            "# 1. Hold the environment stopped (suspending it if it runs); prints the plan.",
            line(
                self.exec_worker(
                    "plan",
                    "--environment",
                    environment,
                    "--architecture",
                    architecture,
                    "--hold-seconds",
                    str(checkpoint.hold_seconds),
                    "--by",
                    self.actor,
                )
            )
            + " > plan.json",
            "# 2. Find the sandbox's claim and its node; nothing may mount it now.",
            line([*self.kubectl, "-n", sandbox_ns, "get", "pvc,pods", "-o", "wide"]),
            "# 3. Render the Job (it mounts the claim read-only and the Secret "
            f"{checkpoint.upload_secret}) and create it.",
            "sandbox-science-kubernetes checkpoint-job --config CONFIG --plan plan.json "
            "--claim CLAIM --node NODE --output job.json",
            line([*self.kubectl, "create", "-f", "job.json", "-o", "name"]),
            "# 4. Wait for it and read the manifest's SHA-256 from its log.",
            line(
                [
                    *self.kubectl,
                    "-n",
                    sandbox_ns,
                    "wait",
                    "--for=condition=complete",
                    "job/JOB",
                    f"--timeout={checkpoint.deadline_seconds}s",
                ]
            ),
            line([*self.kubectl, "-n", sandbox_ns, "logs", "job/JOB"]),
            "# 5. The worker reads it back in full and records it (upload bucket).",
            line(self._verify("CHECKPOINT_ID", "MANIFEST", "upload")),
            "# 6. Copy it into the kept bucket with the keeper key (through a port-forward).",
            line(self._promote(upload, kept, "MANIFEST")),
            "# 7. The worker verifies the kept copy: only now does the checkpoint count.",
            line(self._verify("CHECKPOINT_ID", "MANIFEST", "kept")),
        ]

    def _verify(self, checkpoint_id: str, manifest: str, stage: str) -> list[str]:
        return self.exec_worker(
            "verify",
            "--checkpoint-id",
            checkpoint_id,
            "--manifest",
            manifest,
            "--storage",
            f"{PRIVATE}/checkpoint-{stage}.toml",
            "--stage",
            stage,
            "--keep",
            str(self.checkpoint.keep),
            "--by",
            self.actor,
            key=True,
        )

    def _promote(self, upload: Path, kept: Path, manifest: str) -> list[str]:
        return [
            *self.worker,
            "checkpoint",
            "promote",
            "--from",
            str(upload),
            "--to",
            str(kept),
            "--manifest",
            manifest,
        ]

    def claim_of(self, sandbox: str) -> tuple[str, str | None]:
        """The workspace claim of ``sandbox`` and its node; refuses while a Pod mounts it."""
        found = [
            pvc
            for pvc in self.get("pvc")
            if _sandbox_owner(pvc) == sandbox or _name(pvc).endswith(f"-{sandbox}")
        ]
        if len(found) != 1:
            raise OperationError(
                f"expected one workspace claim of sandbox {sandbox}, found {len(found)}"
            )
        claim = _name(found[0])
        if claim in _mounted(self.get("pods")):
            raise OperationError(f"a Pod still mounts {claim}: the sandbox is not stopped")
        node = _annotations(found[0]).get(SELECTED_NODE)
        return claim, node

    def run_checkpoint(
        self,
        environment: str,
        architecture: str,
        keeper: tuple[Path, Path] | None,
        *,
        poll: float = 5.0,
    ) -> str:
        """Plan, create the Job, wait, verify, promote and verify again; the manifest."""
        raw = self.run(
            self.exec_worker(
                "plan",
                "--environment",
                environment,
                "--architecture",
                architecture,
                "--hold-seconds",
                str(self.checkpoint.hold_seconds),
                "--by",
                self.actor,
            ),
            None,
        )
        plan = CheckpointPlan.model_validate_json(raw)
        self.out(
            f"planned {plan.checkpoint_id}: {environment} held until"
            f" {plan.hold_expires_at:%Y-%m-%d %H:%M} UTC"
        )
        try:
            claim, node = self.claim_of(plan.source.sandbox)
            job = checkpoint_job(self.config, plan, claim, node)
            created = self.run(
                [*self.kubectl, "create", "-f", "-", "-o", "name"], json.dumps(job).encode()
            )
            name = created.decode().strip().rpartition("/")[2]
            self.out(f"created job {name} reading {claim}" + (f" on {node}" if node else ""))
            self._wait(name, poll)
            logs = self.run(
                [*self.kubectl, "-n", self.sandbox_namespace, "logs", f"job/{name}"], None
            )
            match = _MANIFEST.search(logs.decode(errors="replace"))
            if match is None:
                raise OperationError(f"job {name} printed no checkpoint manifest")
            manifest = match[1]
        except BaseException as error:
            self.run(
                self.exec_worker(
                    "release-hold",
                    "--checkpoint-id",
                    plan.checkpoint_id,
                    "--reason",
                    f"checkpoint failed: {str(error)[:120]}",
                    "--by",
                    self.actor,
                ),
                None,
            )
            raise
        self.out(
            self.run(self._verify(plan.checkpoint_id, manifest, "upload"), None).decode().strip()
        )
        if keeper is None:
            self.out("not promoted (no keeper storage files); run these with the keeper key:")
            self.out(
                " ".join(
                    map(_quote, self._promote(Path("UPLOAD.toml"), Path("KEPT.toml"), manifest))
                )
            )
            self.out(" ".join(map(_quote, self._verify(plan.checkpoint_id, manifest, "kept"))))
            return manifest
        self.out(self.run(self._promote(*keeper, manifest), None).decode().strip())
        self.out(
            self.run(self._verify(plan.checkpoint_id, manifest, "kept"), None).decode().strip()
        )
        return manifest

    def _wait(self, name: str, poll: float) -> None:
        deadline = time.monotonic() + self.checkpoint.deadline_seconds + 300
        while True:
            raw = self.run(
                [*self.kubectl, "-n", self.sandbox_namespace, "get", "job", name, "-o", "json"],
                None,
            )
            status = json.loads(raw).get("status", {})
            if status.get("succeeded"):
                return
            if status.get("failed"):
                logs = self.run(
                    [*self.kubectl, "-n", self.sandbox_namespace, "logs", f"job/{name}"], None
                )
                raise OperationError(f"job {name} failed: {logs.decode(errors='replace')[-600:]}")
            if time.monotonic() > deadline:
                raise OperationError(f"job {name} did not finish within its deadline")
            self.sleep(poll)

    # Cleanup

    def inventory(self) -> CheckpointInventory:
        raw = self.run(self.exec_worker("inventory", "--keep", str(self.checkpoint.keep)), None)
        return CheckpointInventory.model_validate_json(raw)

    def survey(self) -> Survey:
        return survey(
            self.get("pv", namespaced=False),
            self.get("pvc"),
            self.get("pods"),
            self.get("sandboxes.agents.x-k8s.io"),
            self.inventory(),
            self.sandbox_namespace,
            datetime.now(UTC),
        )

    def delete(
        self,
        found: Survey,
        volumes: Sequence[str],
        checkpoints: Sequence[str],
        *,
        without_checkpoint: bool,
        keeper_kept: Path | None,
    ) -> list[Step]:
        """The commands that delete exactly the named candidates, in order; refuses a name
        that is not a candidate or that a rule protects."""
        rows = {row.volume: row for row in found.volumes}
        marks = {row.checkpoint.manifest_sha256: row for row in found.checkpoints}
        refused: list[str] = []
        commands: list[Step] = []
        for name in volumes:
            row = rows.get(name)
            if row is None:
                refused.append(f"{name}: not a cleanup candidate")
            elif row.refusals:
                refused.append(f"{name}: {'; '.join(row.refusals)}")
            elif not row.kept_checkpoints and not without_checkpoint:
                refused.append(f"{name}: no verified checkpoint (pass --without-checkpoint)")
            else:
                commands.extend(self._delete_volume(row, without_checkpoint))
        for manifest in checkpoints:
            mark = marks.get(manifest)
            if mark is None:
                refused.append(f"{manifest}: not a cleanup candidate")
            elif mark.refusals:
                refused.append(f"{manifest}: {'; '.join(mark.refusals)}")
            else:
                commands.extend(self._delete_checkpoint(mark, keeper_kept))
        if refused:
            raise OperationError("refused: " + " | ".join(refused))
        return commands

    def _delete_volume(self, row: VolumeRow, without_checkpoint: bool) -> list[Step]:
        detail = json.dumps({"claim": row.claim, "size": row.size, "phase": row.phase})

        def journal(phase: str) -> list[str]:
            return self.exec_worker(
                "record-volume",
                "--phase",
                phase,
                "--volume",
                row.volume,
                *(["--sandbox", row.sandbox] if row.sandbox else []),
                *(["--without-checkpoint"] if without_checkpoint else []),
                "--detail",
                detail,
                "--by",
                self.actor,
            )

        # The worker journals the intent first, and refuses it if its records forbid it.
        steps = [Step(journal("intent"))]
        if row.claim_exists and row.claim is not None:
            steps.append(
                Step(
                    [*self.kubectl, "-n", self.sandbox_namespace, "delete", "pvc", row.claim],
                    journal("failed"),
                )
            )
        # Released with Retain, the PV keeps its storage; Delete has its provisioner free it.
        steps.append(
            Step(
                [
                    *self.kubectl,
                    "patch",
                    "pv",
                    row.volume,
                    "--type=merge",
                    "-p",
                    '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}',
                ],
                journal("failed"),
            )
        )
        steps.append(Step(journal("done")))
        return steps

    def _delete_checkpoint(self, row: CheckpointRow, keeper_kept: Path | None) -> list[Step]:
        """Journaled and no longer counted first; its objects then go with the keeper key.
        A failure leaves it ``deleting``, which the next cleanup lists again."""
        manifest = row.checkpoint.manifest_sha256
        storage = str(keeper_kept or Path("KEPT.toml"))
        return [
            Step(
                self.exec_worker(
                    "forget",
                    "--manifest",
                    manifest,
                    *([] if row.volume_exists else ["--volume-gone"]),
                    "--by",
                    self.actor,
                )
            ),
            Step(
                [
                    *self.worker,
                    "checkpoint",
                    "delete-objects",
                    "--storage",
                    storage,
                    "--manifest",
                    manifest,
                ]
            ),
            Step(self.exec_worker("forget", "--manifest", manifest, "--done", "--by", self.actor)),
        ]

    def apply(self, steps: list[Step]) -> None:
        """Run the deletion steps in order and stop at the first failure, journaling it."""
        for step in steps:
            self.out("+ " + " ".join(map(shlex.quote, step.command)))
            try:
                output = self.run(step.command, None)
            except OperationError:
                if step.on_failure is not None:
                    self.run(step.on_failure, None)
                raise
            if output.strip():
                self.out(output.decode(errors="replace").strip())


@dataclass(frozen=True)
class VolumeRow:
    volume: str
    claim: str | None
    claim_exists: bool
    phase: str
    size: str
    created: datetime | None
    sandbox: str | None
    environments: tuple[InventoryEnvironment, ...]
    kept_checkpoints: int
    refusals: tuple[str, ...]


@dataclass(frozen=True)
class CheckpointRow:
    checkpoint: InventoryCheckpoint
    volume_exists: bool
    refusals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Survey:
    volumes: list[VolumeRow] = field(default_factory=list[VolumeRow])
    checkpoints: list[CheckpointRow] = field(default_factory=list[CheckpointRow])
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _name(item: dict[str, object]) -> str:
    return str(_dict(item.get("metadata")).get("name", ""))


def _annotations(item: dict[str, object]) -> dict[str, str]:
    return {
        str(k): str(v) for k, v in _dict(_dict(item.get("metadata")).get("annotations")).items()
    }


def _sandbox_owner(item: dict[str, object]) -> str | None:
    owners = _dict(item.get("metadata")).get("ownerReferences")
    for owner in cast(list[object], owners) if isinstance(owners, list) else []:
        reference = _dict(owner)
        if reference.get("kind") == "Sandbox" and isinstance(reference.get("name"), str):
            return str(reference["name"])
    return None


def _mounted(pods: list[dict[str, object]]) -> set[str]:
    """Claims a Pod that has not ended mounts."""
    claims: set[str] = set()
    for pod in pods:
        if _dict(pod.get("status")).get("phase") in ("Succeeded", "Failed"):
            continue
        volumes = _dict(pod.get("spec")).get("volumes")
        for volume in cast(list[object], volumes) if isinstance(volumes, list) else []:
            claim = _dict(_dict(volume).get("persistentVolumeClaim")).get("claimName")
            if isinstance(claim, str):
                claims.add(claim)
    return claims


def _created(item: dict[str, object]) -> datetime | None:
    stamp = _dict(item.get("metadata")).get("creationTimestamp")
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return None


def survey(
    pvs: list[dict[str, object]],
    pvcs: list[dict[str, object]],
    pods: list[dict[str, object]],
    sandboxes: list[dict[str, object]],
    found: CheckpointInventory,
    namespace: str,
    now: datetime,
) -> Survey:
    """Cleanup candidates and what protects each (pure, so tests decide it offline).

    A volume is a candidate when it held a workspace in the sandbox namespace and its
    sandbox is gone: a Released PV, or a claim whose owning Sandbox object no longer
    exists. It is refused while its sandbox exists, a Pod mounts it, it is marked
    ``sandbox-science.io/keep`` (a recovery set), the worker records a live or paused
    environment, a hand-back or a checkpoint hold for it. A checkpoint is a candidate when
    newer kept ones of its workspace fill the kept count, or when its deletion began."""
    live = {_name(item) for item in sandboxes}
    claims = {_name(item): item for item in pvcs}
    mounted = _mounted(pods)
    known = {info.sandbox for info in found.environments} | live
    kept = [c for c in found.checkpoints if c.state == "kept"]
    rows: list[VolumeRow] = []
    volume_sandboxes: set[str] = set()
    for pv in pvs:
        spec, status = _dict(pv.get("spec")), _dict(pv.get("status"))
        reference = _dict(spec.get("claimRef"))
        if reference.get("namespace") != namespace:
            continue
        claim_name = str(reference.get("name", "")) or None
        pvc = claims.get(claim_name or "")
        if pvc is not None and _dict(pvc.get("spec")).get("volumeName") != _name(pv):
            pvc = None
        sandbox = _sandbox_owner(pvc) if pvc is not None else None
        if sandbox is None and claim_name is not None:
            matches = [s for s in known if claim_name.endswith(f"-{s}")]
            sandbox = matches[0] if len(matches) == 1 else None
        if sandbox is not None:
            volume_sandboxes.add(sandbox)
        phase = str(status.get("phase", ""))
        orphaned = pvc is not None and _sandbox_owner(pvc) is not None and sandbox not in live
        if phase != "Released" and not orphaned:
            continue
        refusals: list[str] = []
        if sandbox is not None and sandbox in live:
            refusals.append(f"its sandbox {sandbox} exists")
        if claim_name in mounted and pvc is not None:
            refusals.append("a Pod mounts it")
        noted = {**_annotations(pv), **(_annotations(pvc) if pvc is not None else {})}
        if KEEP_ANNOTATION in noted:
            refusals.append(f"marked to keep ({noted[KEEP_ANNOTATION][:80]})")
        environments = tuple(
            e for e in found.environments if sandbox is not None and e.sandbox == sandbox
        )
        for info in environments:
            if info.state != "deleted":
                refusals.append(f"environment {info.environment_id} is {info.state}")
            if info.hand_back is not None:
                refusals.append(
                    f"environment {info.environment_id}'s hand-back is {info.hand_back}"
                )
        if sandbox is not None and any(hold.sandbox == sandbox for hold in found.holds):
            refusals.append("a checkpoint holds it")
        capacity = _dict(spec.get("capacity")).get("storage", "")
        rows.append(
            VolumeRow(
                volume=_name(pv),
                claim=claim_name,
                claim_exists=pvc is not None,
                phase=phase,
                size=str(capacity),
                created=_created(pv),
                sandbox=sandbox,
                environments=environments,
                kept_checkpoints=sum(
                    1 for c in kept if sandbox is not None and c.sandbox == sandbox
                ),
                refusals=tuple(refusals),
            )
        )
    for pvc in pvcs:
        owner = _sandbox_owner(pvc)
        if owner is not None:
            volume_sandboxes.add(owner)
    marks: list[CheckpointRow] = []
    by_sandbox: dict[str, list[InventoryCheckpoint]] = {}
    for checkpoint in kept:
        by_sandbox.setdefault(checkpoint.sandbox, []).append(checkpoint)
    for sandbox, listed in by_sandbox.items():
        listed.sort(key=lambda c: c.created_at, reverse=True)
        marks.extend(CheckpointRow(c, sandbox in volume_sandboxes) for c in listed[found.keep :])
    marks.extend(
        CheckpointRow(c, c.sandbox in volume_sandboxes)
        for c in found.checkpoints
        if c.state == "deleting"
    )
    return Survey(rows, marks, now)


def _age(created: datetime | None, now: datetime) -> str:
    if created is None:
        return "?"
    days = (now - created).total_seconds() / 86400
    return f"{days:.0f}d" if days >= 1 else f"{days * 24:.0f}h"


def report(found: Survey) -> list[str]:
    """The candidates as lines an operator reads before naming what to delete."""
    lines: list[str] = []
    for row in found.volumes:
        owners = (
            ", ".join(
                f"project {e.project_id}"
                + (f" session {e.session_id}" if e.session_id else "")
                + f" environment {e.environment_id} ({e.state})"
                for e in row.environments
            )
            or "owner unknown"
        )
        checkpoint = (
            f"{row.kept_checkpoints} verified checkpoint(s)"
            if row.kept_checkpoints
            else "no verified checkpoint"
        )
        verdict = "REFUSED: " + "; ".join(row.refusals) if row.refusals else "candidate"
        lines.append(
            f"volume {row.volume} {row.size} age {_age(row.created, found.now)} {row.phase}"
            f" claim {row.claim or '-'} sandbox {row.sandbox or '?'}: {owners};"
            f" {checkpoint}; {verdict}"
        )
    for mark in found.checkpoints:
        c = mark.checkpoint
        lines.append(
            f"checkpoint {c.manifest_sha256} {c.archive_size} bytes"
            f" age {_age(c.created_at, found.now)}"
            f" {c.state} sandbox {c.sandbox}: project {c.project_id} environment"
            f" {c.environment_id}; {'volume exists' if mark.volume_exists else 'volume gone'};"
            f" {'REFUSED: ' + '; '.join(mark.refusals) if mark.refusals else 'candidate'}"
        )
    if not lines:
        lines.append("no cleanup candidates")
    return lines


def _quote(part: str) -> str:
    return shlex.quote(part)
