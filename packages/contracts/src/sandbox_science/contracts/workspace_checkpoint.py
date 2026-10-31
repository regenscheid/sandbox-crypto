# SPDX-License-Identifier: MIT
"""Workspace checkpoints are recovery objects, not scientific artifacts.

A receipt's ``capture`` says how consistent its copy is (C3, docs/WORKER.md): an offline
mount the operator declared exclusive, a suspended sandbox's volume read by a checkpoint
Job, a crash-consistent clone, or a stream from a running sandbox. Version 1 receipts,
which carry ``exclusive_offline_mount_declared`` instead, stay readable and serialize to
their original bytes.
"""

from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .model import Counter, Identifier, Sha256, StrictContract

MAX_CHECKPOINT_MEMBERS = 1_000_000
MAX_CHECKPOINT_METADATA = 256 * 1024**2
MAX_CHECKPOINT_CONTENT = 16 * 1024**4
MAX_CHECKPOINT_HEADER = 16 * 1024
MAX_CHECKPOINT_RECEIPT = 128 * 1024
MAX_CHECKPOINT_DEPTH = 256
CHECKPOINT_FORMAT = "sandbox-workspace-checkpoint-v1"
CHECKPOINT_FORMAT_V2 = "sandbox-workspace-checkpoint-v2"
MAX_CHECKPOINT_SKIPPED: Final = 1000
"""Special files (sockets, FIFOs, devices) a capture leaves out and lists; more fail it."""
CaptureMode = Literal["offline_mount", "suspended_pvc", "clone_pvc", "sandbox_stream"]
"""How a checkpoint's bytes were read, which says how consistent they are: ``offline_mount``
(the operator declared the mount exclusive), ``suspended_pvc`` (a checkpoint Job read the
volume of a sandbox the worker confirmed stopped and held), ``clone_pvc`` (a
crash-consistent copy of a running sandbox's volume) or ``sandbox_stream`` (what a running
sandbox presented)."""
SkippedKind = Literal["socket", "fifo", "character_device", "block_device", "other"]


def _literal_path(value: str) -> str:
    if value != "." and (
        value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise ValueError("checkpoint path is not a literal relative path")
    if "\0" in value or len(value.encode("utf-8", "surrogatepass")) > 4096:
        raise ValueError("checkpoint path is invalid or too long")
    if len(value.split("/")) > MAX_CHECKPOINT_DEPTH:
        raise ValueError("checkpoint path is too deep")
    return value


class CheckpointSource(StrictContract):
    """The exact retained binding; native architecture is never inferred from the host."""

    project_id: Identifier
    environment_id: Identifier
    sandbox: Annotated[str, Field(min_length=1, max_length=253)]
    sandbox_id: Annotated[str, Field(min_length=1, max_length=256)]
    profile: Identifier
    image: Annotated[str, Field(min_length=1, max_length=4096)]
    policy: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    launch_settings: Annotated[str, Field(max_length=65536)] | None
    architecture: Literal["amd64", "arm64"]
    require_workspace_prefix: bool
    prefix_identity: Annotated[str, Field(pattern=r"^prefix-[0-9a-f]{32}$")] | None
    package_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None


class CheckpointMember(StrictContract):
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    kind: Literal["directory", "file", "symlink", "hardlink"]
    mode: Annotated[int, Field(strict=True, ge=0, le=0o7777)]
    size: Annotated[int, Field(strict=True, ge=0, le=MAX_CHECKPOINT_CONTENT)] = 0
    target: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _literal_path(value)


class SkippedEntry(StrictContract):
    """A special file a capture left out: it holds no data a restore could recreate."""

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    kind: SkippedKind

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if value == ".":
            raise ValueError("the checkpoint root is never skipped")
        return _literal_path(value)


class CheckpointReceipt(StrictContract):
    """A capture's provenance and counts. Without ``format``, a receipt with ``capture`` is
    version 2 and one without is version 1, as before."""

    format: Literal["sandbox-workspace-checkpoint-v1", "sandbox-workspace-checkpoint-v2"] = (
        CHECKPOINT_FORMAT
    )
    source: CheckpointSource
    created_at: AwareDatetime
    exclusive_offline_mount_declared: Literal[True] | None = None
    capture: CaptureMode | None = None
    skipped: Annotated[tuple[SkippedEntry, ...], Field(max_length=MAX_CHECKPOINT_SKIPPED)] = ()
    archive_sha256: Sha256
    archive_size: Counter
    members: Annotated[int, Field(strict=True, ge=1, le=MAX_CHECKPOINT_MEMBERS)]
    content_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CHECKPOINT_CONTENT)]
    metadata_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CHECKPOINT_METADATA)]

    @model_validator(mode="after")
    def _consistency(self) -> "CheckpointReceipt":
        # A frozen model normalizes its own defaults once, here: ``capture`` without an
        # explicit format makes version 2, and version 1 always declares its mount.
        if "format" not in self.model_fields_set and self.capture is not None:
            object.__setattr__(self, "format", CHECKPOINT_FORMAT_V2)
        if self.format == CHECKPOINT_FORMAT and self.exclusive_offline_mount_declared is None:
            object.__setattr__(self, "exclusive_offline_mount_declared", True)
        if self.format == CHECKPOINT_FORMAT:
            if self.capture is not None or self.skipped:
                raise ValueError("a version 1 receipt declares an exclusive offline mount only")
        elif self.exclusive_offline_mount_declared is not None or self.capture is None:
            raise ValueError("a version 2 receipt states its capture instead")
        if len({entry.path for entry in self.skipped}) != len(self.skipped):
            raise ValueError("a skipped path is listed once")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Version 1 receipts keep their original bytes: their manifests are content-addressed.
        result: dict[str, Any] = handler(self)
        dropped = (
            ("capture", "skipped")
            if self.format == CHECKPOINT_FORMAT
            else ("exclusive_offline_mount_declared",)
        )
        return {key: value for key, value in result.items() if key not in dropped}


CheckpointId = Annotated[str, Field(pattern=r"^ckpt-[0-9a-f]{24}$")]
"""A checkpoint attempt's ID: what its plan, hold, Job and record share."""
CHECKPOINT_PLAN_FORMAT = "sandbox-workspace-checkpoint-plan-v1"
CHECKPOINT_INVENTORY_FORMAT = "sandbox-workspace-checkpoint-inventory-v1"
MAX_INVENTORY_ROWS: Final = 100_000


class CheckpointPlan(StrictContract):
    """What the worker hands the operator for a checkpoint Job: the retained binding the
    Job writes into the archive and the hold that keeps the sandbox stopped meanwhile. The
    worker later verifies the uploaded checkpoint against its own copy of this binding,
    never against the Job's word."""

    format: Literal["sandbox-workspace-checkpoint-plan-v1"] = CHECKPOINT_PLAN_FORMAT
    checkpoint_id: CheckpointId
    source: CheckpointSource
    source_sha256: Sha256
    capture: CaptureMode
    hold_expires_at: AwareDatetime


CheckpointState = Literal["verified", "kept", "deleting", "deleted"]
"""``verified`` in the upload bucket; ``kept`` once verified again in the kept bucket,
the only state that counts as a checkpoint; ``deleting`` from the moment an operator asked
to remove it (it no longer counts); ``deleted`` once its objects are gone."""


class InventoryEnvironment(StrictContract):
    """An environment the worker has a record of, deleted ones included, so that a volume
    can be traced to its project and session."""

    environment_id: Identifier
    project_id: Identifier
    session_id: Identifier | None = None
    kind: Literal["session", "job", "child"]
    state: Literal["creating", "ready", "stopped", "deleted"]
    sandbox: Annotated[str, Field(min_length=1, max_length=253)]
    sandbox_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    hand_back: Literal["pending", "retained"] | None = None
    """A child environment whose hand-back is under way (``pending``) or was incomplete,
    so the worker keeps it stopped (``retained``): its volume is still referenced."""
    last_started_at: AwareDatetime | None = None
    """When the worker last started its sandbox, if it recorded it: any work after a
    checkpoint needs a start, so a later one means the checkpoint is out of date."""


class InventoryCheckpoint(StrictContract):
    checkpoint_id: CheckpointId
    environment_id: Identifier
    project_id: Identifier
    sandbox: Annotated[str, Field(min_length=1, max_length=253)]
    sandbox_id: Annotated[str, Field(min_length=1, max_length=256)]
    manifest_sha256: Sha256
    archive_size: Counter
    content_bytes: Counter
    capture: CaptureMode
    skipped: Counter
    state: CheckpointState
    created_at: AwareDatetime
    verified_at: AwareDatetime
    kept_at: AwareDatetime | None = None


class InventoryHold(StrictContract):
    checkpoint_id: CheckpointId
    environment_id: Identifier
    sandbox: Annotated[str, Field(min_length=1, max_length=253)]
    expires_at: AwareDatetime


RestoreId = Annotated[str, Field(pattern=r"^rst-[0-9a-f]{24}$")]
"""A restore attempt's ID: what its plan, record, Job and journal rows share."""
RESTORE_PLAN_FORMAT = "sandbox-workspace-restore-plan-v1"
RestoreState = Literal["planned", "failed", "restored", "cancelled"]
"""``planned`` from the plan on and ``failed`` once a Job or verification failed: both keep
the target's sandbox stopped, with no expiry, since its workspace may hold a partial tree;
``restored`` once the worker verified the restored tree; ``cancelled`` when the plan failed
before anything was written, or the operator discarded the target environment."""


class RestorePlan(StrictContract):
    """What the worker hands the operator for a restore Job: the kept checkpoint to write
    (its manifest and the binding recorded in its archive) and the fresh environment it goes
    into, created by the worker for this restore and held stopped. The worker compares the
    tree the Job reports with the one it computed from the kept copy, never trusting the
    Job's word for which checkpoint it wrote."""

    format: Literal["sandbox-workspace-restore-plan-v1"] = RESTORE_PLAN_FORMAT
    restore_id: RestoreId
    checkpoint_id: CheckpointId
    manifest_sha256: Sha256
    source: CheckpointSource
    """The binding recorded in the checkpoint's archive, which the decoder checks."""
    target: CheckpointSource
    """The fresh environment's binding: no prefix identity or package revision yet."""
    planned_at: AwareDatetime

    @model_validator(mode="after")
    def _distinct(self) -> "RestorePlan":
        if self.target.sandbox == self.source.sandbox:
            raise ValueError("a restore never writes into the checkpoint's own sandbox")
        if self.target.architecture != self.source.architecture:
            raise ValueError("a restore keeps the checkpoint's architecture")
        if self.target.project_id != self.source.project_id:
            raise ValueError("a restore stays in the checkpoint's project")
        return self


class InventoryRestore(StrictContract):
    restore_id: RestoreId
    environment_id: Identifier
    sandbox: Annotated[str, Field(min_length=1, max_length=253)]
    manifest_sha256: Sha256
    state: RestoreState


class CheckpointInventory(StrictContract):
    """The worker's records an operator's cleanup decides with (docs/WORKER.md): what
    environments exist and in what state, which hand-backs still reference a volume, which
    checkpoints are kept, and which sandboxes a checkpoint holds stopped."""

    format: Literal["sandbox-workspace-checkpoint-inventory-v1"] = CHECKPOINT_INVENTORY_FORMAT
    generated_at: AwareDatetime
    keep: Annotated[int, Field(strict=True, ge=1, le=100)]
    environments: Annotated[tuple[InventoryEnvironment, ...], Field(max_length=MAX_INVENTORY_ROWS)]
    checkpoints: Annotated[tuple[InventoryCheckpoint, ...], Field(max_length=MAX_INVENTORY_ROWS)]
    holds: Annotated[tuple[InventoryHold, ...], Field(max_length=MAX_INVENTORY_ROWS)]
    restores: Annotated[tuple[InventoryRestore, ...], Field(max_length=MAX_INVENTORY_ROWS)] = ()
    """Restores into fresh environments; an unfinished one keeps its target stopped."""
