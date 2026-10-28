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
from .kubernetes_checkpoint import CHECKPOINT, CHECKPOINT_LABEL, checkpoint_job
from .kubernetes_config import BundleConfig, Checkpoint

Runner = Callable[[Sequence[str], bytes | None], bytes]
"""Runs one command with optional standard input; its standard output, or OperationError."""
KEEP_ANNOTATION = "sandbox-science.io/keep"
"""On a workspace's PV or PVC: part of a recovery set, never a cleanup candidate."""
SELECTED_NODE = "volume.kubernetes.io/selected-node"
OPENSHELL_SEPARATOR = "--"
"""Between the segments of OpenShell's routable names: a sandbox's Kubernetes objects are
``<OpenShell workspace>--<sandbox>`` (the ``Sandbox``, and its claim
``workspace-<workspace>--<sandbox>``). The worker's sandbox names never contain it
(``worker.environments.OPENSHELL_SEPARATOR``; a test compares them, the launcher never
imports the worker), so the sandbox is what follows its last occurrence."""
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
    stdin: bytes | None = None
    output: Path | None = None
    """Where the command's output is kept, for a later step to read."""


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
            if _sandbox_owner(pvc) == sandbox or claim_sandbox(_name(pvc)) == sandbox
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
        name: str | None = None
        claim = ""
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
            # The worker's own check; a failure lifts the hold too, so research goes on.
            self.out(
                self.run(self._verify(plan.checkpoint_id, manifest, "upload"), None)
                .decode()
                .strip()
            )
        except BaseException as error:
            # Stop the Job and lift the hold so research goes on; the run's own error comes
            # first, and a failure to clean up is reported beside it, never instead of it.
            problems: list[str] = []
            if name is not None:
                try:
                    self.run(self._delete_job(name), None)
                except OperationError as cleanup:
                    problems.append(f"the Job {name} was not deleted: {cleanup}")
            try:
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
            except OperationError as cleanup:
                problems.append(
                    f"the hold was not lifted ({cleanup}); it expires at"
                    f" {plan.hold_expires_at:%Y-%m-%d %H:%M} UTC"
                )
            if problems:
                raise OperationError(f"{error}; and {'; '.join(problems)}") from error
            raise
        self._finish_job(name, logs, claim)
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

    def _delete_job(self, name: str) -> list[str]:
        return [
            *self.kubectl,
            "-n",
            self.sandbox_namespace,
            "delete",
            "job",
            name,
            "--cascade=background",
            "--wait=false",
        ]

    def _finish_job(self, name: str, logs: bytes, claim: str) -> None:
        """After the worker verified what the Job wrote: its log goes to the operator's
        output and the Job goes, since its finished Pod still names the claim and would keep
        the claim ``Terminating`` once the environment is deleted. A failed deletion is
        reported, not raised: the checkpoint is good, and the Job's TTL removes it later."""
        for line in logs.decode(errors="replace").splitlines():
            self.out(f"job {name}: {line}")
        try:
            self.run(self._delete_job(name), None)
        except OperationError as error:
            self.out(
                f"the Job {name} was not deleted ({error}); its Pod keeps {claim} in use until"
                f" it is, or until {self.checkpoint.job_ttl_seconds}s after it finished"
            )
            return
        self.out(f"deleted job {name}")

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
            jobs=self.get("jobs.batch"),
        )

    def delete(
        self,
        found: Survey,
        volumes: Sequence[str],
        checkpoints: Sequence[str],
        *,
        without_checkpoint: Sequence[str],
        keeper_kept: Path | None,
        workdir: Path = Path("."),
        retire: Sequence[str] = (),
    ) -> list[Step]:
        """The commands that delete exactly the named candidates, in order; refuses a name
        that is not a candidate or that a rule protects. ``without_checkpoint`` names the
        volumes that may go without a current verified checkpoint, and must name only
        volumes being deleted. ``retire`` names deleted environments whose remaining
        (latest kept) checkpoints go too, all of them or none."""
        rows = {row.volume: row for row in found.volumes}
        marks = {row.checkpoint.manifest_sha256: row for row in found.checkpoints}
        refused: list[str] = []
        commands: list[Step] = []
        if stray := sorted(set(without_checkpoint) - set(volumes)):
            refused.append(f"--without-checkpoint names no --volume: {', '.join(stray)}")
        for name in volumes:
            row = rows.get(name)
            if row is None:
                refused.append(f"{name}: not a cleanup candidate")
            elif row.refusals:
                refused.append(f"{name}: {'; '.join(row.refusals)}")
            elif row.uncovered is not None and name not in without_checkpoint:
                refused.append(f"{name}: {row.uncovered} (pass --without-checkpoint {name})")
            elif row.claim_uid is None:
                refused.append(f"{name}: its claim reference carries no UID to check against")
            else:
                commands.extend(self._delete_volume(row, name in without_checkpoint))
        for manifest in checkpoints:
            mark = marks.get(manifest)
            if mark is None:
                refused.append(f"{manifest}: not a cleanup candidate")
            elif mark.retire:
                environment = mark.checkpoint.environment_id
                refused.append(
                    f"{manifest}: one of the latest checkpoints of deleted environment"
                    f" {environment} (pass --retire-checkpoints {environment})"
                )
            elif mark.refusals:
                refused.append(f"{manifest}: {'; '.join(mark.refusals)}")
            else:
                commands.extend(self._delete_checkpoint(mark, keeper_kept, workdir))
        for environment in dict.fromkeys(retire):
            retiring = [
                mark
                for mark in found.checkpoints
                if mark.retire and mark.checkpoint.environment_id == environment
            ]
            info = next((e for e in found.environments if e.environment_id == environment), None)
            if info is None:
                refused.append(f"{environment}: the worker has no record of this environment")
            elif info.state != "deleted":
                refused.append(f"{environment}: environment {environment} is {info.state}")
            elif not retiring:
                refused.append(f"{environment}: no kept checkpoints left to retire")
            elif blocked := [mark for mark in retiring if mark.refusals]:
                refused.extend(
                    f"{environment}: checkpoint {mark.checkpoint.manifest_sha256}:"
                    f" {'; '.join(mark.refusals)}"
                    for mark in blocked
                )
            else:
                for mark in retiring:
                    commands.extend(
                        self._delete_checkpoint(mark, keeper_kept, workdir, retire=environment)
                    )
        if refused:
            raise OperationError("refused: " + " | ".join(refused))
        return commands

    def _delete_volume(self, row: VolumeRow, without_checkpoint: bool) -> list[Step]:
        detail = json.dumps(
            {"claim": row.claim, "claim_uid": row.claim_uid, "size": row.size, "phase": row.phase}
        )

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
        # Then the PV's reclaim policy becomes Delete, so its provisioner frees the storage
        # once no claim holds it (at once for a Released PV); then the claim, if any, goes.
        # Both act only on the objects surveyed: the patch tests the PV's claim UID and
        # phase, and the claim's deletion carries its UID as a precondition.
        patch = [
            {"op": "test", "path": "/spec/claimRef/uid", "value": row.claim_uid},
            {"op": "test", "path": "/status/phase", "value": row.phase},
            {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Delete"},
        ]
        steps = [Step(journal("intent"))]
        # A finished checkpoint Job's Pod still names the claim; the claim would stay
        # Terminating until it went. Each goes with the UID the survey saw.
        steps.extend(
            Step(
                [
                    *self.kubectl,
                    "delete",
                    "--raw",
                    f"/apis/batch/v1/namespaces/{self.sandbox_namespace}/jobs/{job.name}",
                    "-f",
                    "-",
                ],
                journal("failed"),
                stdin=json.dumps(
                    {
                        "kind": "DeleteOptions",
                        "apiVersion": "v1",
                        "preconditions": {"uid": job.uid},
                        "propagationPolicy": "Background",
                    }
                ).encode(),
            )
            for job in row.jobs
        )
        steps.append(
            Step(
                [
                    *self.kubectl,
                    "patch",
                    "pv",
                    row.volume,
                    "--type=json",
                    "-p",
                    json.dumps(patch, separators=(",", ":")),
                ],
                journal("failed"),
            )
        )
        if row.claim_exists and row.claim is not None:
            options = {
                "kind": "DeleteOptions",
                "apiVersion": "v1",
                "preconditions": {"uid": row.claim_uid},
            }
            steps.append(
                Step(
                    [
                        *self.kubectl,
                        "delete",
                        "--raw",
                        f"/api/v1/namespaces/{self.sandbox_namespace}"
                        f"/persistentvolumeclaims/{row.claim}",
                        "-f",
                        "-",
                    ],
                    journal("failed"),
                    stdin=json.dumps(options).encode(),
                )
            )
        steps.append(Step(journal("done")))
        return steps

    def _delete_checkpoint(
        self,
        row: CheckpointRow,
        keeper_kept: Path | None,
        workdir: Path,
        *,
        retire: str | None = None,
    ) -> list[Step]:
        """Journaled and no longer counted first; the worker's recorded chunk list then
        lets the keeper key remove its chunks and, last, its manifest. A failure leaves it
        ``deleting``, which the next cleanup lists again and finishes. ``retire`` names the
        deleted environment whose last checkpoints these are; the worker checks it again."""
        manifest = row.checkpoint.manifest_sha256
        storage = str(keeper_kept or Path("KEPT.toml"))
        listing = workdir / f"chunks-{manifest}.txt"
        return [
            Step(
                self.exec_worker(
                    "forget",
                    "--manifest",
                    manifest,
                    *([] if row.volume_exists else ["--volume-gone"]),
                    *(["--retire", retire] if retire is not None else []),
                    "--by",
                    self.actor,
                )
            ),
            Step(self.exec_worker("chunks", "--manifest", manifest), output=listing),
            Step(
                [
                    *self.worker,
                    "checkpoint",
                    "delete-objects",
                    "--storage",
                    storage,
                    "--manifest",
                    manifest,
                    "--chunks-file",
                    str(listing),
                ]
            ),
            Step(self.exec_worker("forget", "--manifest", manifest, "--done", "--by", self.actor)),
        ]

    def apply(self, steps: list[Step]) -> None:
        """Run the deletion steps in order and stop at the first failure, journaling it."""
        for step in steps:
            self.out("+ " + " ".join(map(shlex.quote, step.command)))
            try:
                output = self.run(step.command, step.stdin)
            except OperationError as error:
                if step.on_failure is not None:
                    try:
                        self.run(step.on_failure, None)
                    except OperationError as journal:
                        raise OperationError(
                            f"{error}; and its failure was not journaled: {journal}"
                        ) from error
                raise
            if step.output is not None:
                step.output.write_bytes(output)
                continue
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
    claim_uid: str | None = None
    newest_checkpoint: datetime | None = None
    last_started: datetime | None = None
    uncovered: str | None = None
    """Why no verified checkpoint covers it: none, or the newest predates its last start.
    Only ``--without-checkpoint`` for this volume lets it go then."""
    jobs: tuple[JobRef, ...] = ()
    """Finished checkpoint Jobs whose Pods still name its claim: deleted before it."""


@dataclass(frozen=True)
class JobRef:
    """A finished checkpoint Job, by the UID the survey saw."""

    name: str
    uid: str


@dataclass(frozen=True)
class CheckpointRow:
    checkpoint: InventoryCheckpoint
    volume_exists: bool
    refusals: tuple[str, ...] = ()
    retire: bool = False
    """One of the latest kept checkpoints of an environment the worker records as deleted:
    never a candidate, removed only with ``--retire-checkpoints`` for its environment."""


@dataclass(frozen=True)
class Survey:
    volumes: list[VolumeRow] = field(default_factory=list[VolumeRow])
    checkpoints: list[CheckpointRow] = field(default_factory=list[CheckpointRow])
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    environments: tuple[InventoryEnvironment, ...] = ()


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _name(item: dict[str, object]) -> str:
    return str(_dict(item.get("metadata")).get("name", ""))


def _annotations(item: dict[str, object]) -> dict[str, str]:
    return {
        str(k): str(v) for k, v in _dict(_dict(item.get("metadata")).get("annotations")).items()
    }


def worker_sandbox(name: str) -> str:
    """The worker's name of the sandbox an OpenShell Kubernetes name carries:
    ``default--ss-…`` and ``team-a--ss-…`` are both ``ss-…``, the name the worker records.
    A name without the separator is the sandbox's own."""
    return name.rpartition(OPENSHELL_SEPARATOR)[2]


def claim_sandbox(claim: str) -> str | None:
    """The worker's name of the sandbox a workspace claim's name carries
    (``workspace-<workspace>--<sandbox>``), if it has OpenShell's shape."""
    return worker_sandbox(claim) if OPENSHELL_SEPARATOR in claim else None


def _sandbox_owner(item: dict[str, object]) -> str | None:
    """The worker's name of the ``Sandbox`` that owns ``item``, if one does."""
    owners = _dict(item.get("metadata")).get("ownerReferences")
    for owner in cast(list[object], owners) if isinstance(owners, list) else []:
        reference = _dict(owner)
        if reference.get("kind") == "Sandbox" and isinstance(reference.get("name"), str):
            return worker_sandbox(str(reference["name"]))
    return None


def _items(value: object) -> list[dict[str, object]]:
    return [_dict(item) for item in cast(list[object], value)] if isinstance(value, list) else []


def _job_claims(job: dict[str, object]) -> set[str]:
    """The claims a Job's Pods name."""
    pod = _dict(_dict(_dict(job.get("spec")).get("template")).get("spec"))
    claims = (
        _dict(volume.get("persistentVolumeClaim")).get("claimName")
        for volume in _items(pod.get("volumes"))
    )
    return {claim for claim in claims if isinstance(claim, str)}


def _job_finished(job: dict[str, object]) -> bool:
    return any(
        condition.get("type") in ("Complete", "Failed") and condition.get("status") == "True"
        for condition in _items(_dict(job.get("status")).get("conditions"))
    )


def _checkpoint_job(job: dict[str, object]) -> bool:
    labels = _dict(_dict(job.get("metadata")).get("labels"))
    return labels.get("app.kubernetes.io/component") == CHECKPOINT and CHECKPOINT_LABEL in labels


def _being_deleted(pv: dict[str, object]) -> bool:
    """A PV already being deleted with its storage: it counts as gone."""
    return bool(_dict(pv.get("metadata")).get("deletionTimestamp")) and (
        _dict(pv.get("spec")).get("persistentVolumeReclaimPolicy") == "Delete"
    )


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
    *,
    jobs: Sequence[dict[str, object]] = (),
) -> Survey:
    """Cleanup candidates and what protects each (pure, so tests decide it offline).

    Sandboxes are matched by the worker's names, exactly: OpenShell's names carry its
    workspace before them (``worker_sandbox``). A volume is a candidate when it held a
    workspace in the sandbox namespace and its sandbox is gone: a Released PV, or a claim
    whose owning Sandbox object no longer exists. It is refused while its sandbox exists, a
    Pod or an unfinished Job mounts it, it is marked ``sandbox-science.io/keep`` (a recovery
    set), the worker records a live or paused environment, a hand-back or a checkpoint hold
    for it; finished checkpoint Jobs that still name its claim are deleted with it. A
    checkpoint is a candidate when newer kept ones of its workspace fill the kept count, or
    when its deletion began; the latest kept ones of an environment the worker records as
    deleted are listed for retirement, refused while their volume exists or the worker's
    records still reference their workspace."""
    live = {worker_sandbox(_name(item)) for item in sandboxes}
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
        claim_uid = str(reference.get("uid", "")) or None
        pvc = claims.get(claim_name or "")
        if pvc is not None and (
            _dict(pvc.get("spec")).get("volumeName") != _name(pv)
            or _dict(pvc.get("metadata")).get("uid") != claim_uid
        ):
            pvc = None
        named = claim_sandbox(claim_name) if claim_name is not None else None
        sandbox = _sandbox_owner(pvc) if pvc is not None else None
        if sandbox is None and named in known:
            sandbox = named
        if not _being_deleted(pv):
            # Any trace of a volume counts, so a checkpoint is never taken for its last.
            volume_sandboxes.update(s for s in (sandbox, named) if s is not None)
        phase = str(status.get("phase", ""))
        orphaned = pvc is not None and _sandbox_owner(pvc) is not None and sandbox not in live
        if phase != "Released" and not orphaned:
            continue
        refusals: list[str] = []
        if sandbox is not None and sandbox in live:
            refusals.append(f"its sandbox {sandbox} exists")
        if claim_name in mounted and pvc is not None:
            refusals.append("a Pod mounts it")
        finished: list[JobRef] = []
        for job in jobs if pvc is not None else ():
            if claim_name not in _job_claims(job):
                continue
            uid = str(_dict(job.get("metadata")).get("uid", ""))
            if not _checkpoint_job(job):
                refusals.append(f"Job {_name(job)} mounts it")
            elif not _job_finished(job):
                refusals.append(f"checkpoint Job {_name(job)} still runs and mounts it")
            elif not uid:
                refusals.append(f"checkpoint Job {_name(job)} mounts it and carries no UID")
            else:
                finished.append(JobRef(_name(job), uid))
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
        own = [c.created_at for c in kept if sandbox is not None and c.sandbox == sandbox]
        newest = max(own, default=None)
        started = max(
            (e.last_started_at for e in environments if e.last_started_at is not None),
            default=None,
        )
        uncovered = None
        if newest is None:
            uncovered = "no verified checkpoint"
        elif started is not None and started > newest:
            uncovered = (
                "its newest verified checkpoint predates its last start"
                f" ({started:%Y-%m-%d %H:%M} UTC)"
            )
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
                claim_uid=claim_uid,
                newest_checkpoint=newest,
                last_started=started,
                uncovered=uncovered,
                jobs=tuple(finished),
            )
        )
    for pvc in pvcs:
        volume_sandboxes.update(
            s for s in (_sandbox_owner(pvc), claim_sandbox(_name(pvc))) if s is not None
        )
    marks: list[CheckpointRow] = []
    by_sandbox: dict[str, list[InventoryCheckpoint]] = {}
    for checkpoint in kept:
        by_sandbox.setdefault(checkpoint.sandbox, []).append(checkpoint)
    recorded = {info.environment_id: info for info in found.environments}
    for sandbox, listed in by_sandbox.items():
        listed.sort(key=lambda c: c.created_at, reverse=True)
        marks.extend(CheckpointRow(c, sandbox in volume_sandboxes) for c in listed[found.keep :])
        for c in listed[: found.keep]:
            info = recorded.get(c.environment_id)
            if info is not None and info.state == "deleted":
                exists = sandbox in volume_sandboxes
                marks.append(
                    CheckpointRow(c, exists, tuple(_retire_refusals(c, found, exists)), retire=True)
                )
    marks.extend(
        CheckpointRow(c, c.sandbox in volume_sandboxes)
        for c in found.checkpoints
        if c.state == "deleting"
    )
    return Survey(rows, marks, now, found.environments)


def _retire_refusals(
    checkpoint: InventoryCheckpoint, found: CheckpointInventory, volume_exists: bool
) -> list[str]:
    """Why one of a deleted environment's latest checkpoints may not be retired: its volume
    exists, or the worker's records still reference its workspace (as the worker's own
    ``checkpoint_jobs.retire_refusals`` decides again)."""
    environment = checkpoint.environment_id
    reasons = ["its volume still exists"] if volume_exists else []
    for info in found.environments:
        if info.environment_id == environment and info.hand_back is not None:
            reasons.append(f"environment {environment}'s hand-back is {info.hand_back}")
    if any(
        hold.environment_id == environment or hold.sandbox == checkpoint.sandbox
        for hold in found.holds
    ):
        reasons.append("a checkpoint holds it")
    return reasons


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
            f"{row.kept_checkpoints} verified checkpoint(s), newest"
            f" {_age(row.newest_checkpoint, found.now)} old"
            if row.kept_checkpoints
            else "no verified checkpoint"
        )
        checkpoint += (
            f"; last started {_age(row.last_started, found.now)} ago" if row.last_started else ""
        )
        if row.uncovered is not None and row.kept_checkpoints:
            checkpoint += " (after the newest checkpoint: not covered)"
        if row.jobs:
            checkpoint += (
                f"; finished checkpoint Job(s) {', '.join(job.name for job in row.jobs)}"
                " still name its claim (deleted with it)"
            )
        verdict = "REFUSED: " + "; ".join(row.refusals) if row.refusals else "candidate"
        lines.append(
            f"volume {row.volume} {row.size} age {_age(row.created, found.now)} {row.phase}"
            f" claim {row.claim or '-'} sandbox {row.sandbox or '?'}: {owners};"
            f" {checkpoint}; {verdict}"
        )
    for mark in found.checkpoints:
        c = mark.checkpoint
        if mark.refusals:
            verdict = ("REFUSED retirement: " if mark.retire else "REFUSED: ") + "; ".join(
                mark.refusals
            )
        elif mark.retire:
            verdict = f"retirable (--retire-checkpoints {c.environment_id})"
        else:
            verdict = "candidate"
        lines.append(
            f"checkpoint {c.manifest_sha256} {c.archive_size} bytes"
            f" age {_age(c.created_at, found.now)}"
            f" {c.state} sandbox {c.sandbox}: project {c.project_id} environment"
            f" {c.environment_id}; {'volume exists' if mark.volume_exists else 'volume gone'};"
            f" {verdict}"
        )
    if not lines:
        lines.append("no cleanup candidates")
    return lines


def _quote(part: str) -> str:
    return shlex.quote(part)
