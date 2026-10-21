# SPDX-License-Identifier: MIT
"""Offline workspace checkpoints are recovery objects, not scientific artifacts."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from .model import Counter, Identifier, Sha256, StrictContract

MAX_CHECKPOINT_MEMBERS = 1_000_000
MAX_CHECKPOINT_METADATA = 256 * 1024**2
MAX_CHECKPOINT_CONTENT = 16 * 1024**4
MAX_CHECKPOINT_HEADER = 16 * 1024
MAX_CHECKPOINT_RECEIPT = 128 * 1024
MAX_CHECKPOINT_DEPTH = 256
CHECKPOINT_FORMAT = "sandbox-workspace-checkpoint-v1"


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
        if value != "." and (
            value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))
        ):
            raise ValueError("checkpoint path is not a literal relative path")
        if "\0" in value or len(value.encode("utf-8", "surrogatepass")) > 4096:
            raise ValueError("checkpoint path is invalid or too long")
        if len(value.split("/")) > MAX_CHECKPOINT_DEPTH:
            raise ValueError("checkpoint path is too deep")
        return value


class CheckpointReceipt(StrictContract):
    format: Literal["sandbox-workspace-checkpoint-v1"] = CHECKPOINT_FORMAT
    source: CheckpointSource
    created_at: AwareDatetime
    exclusive_offline_mount_declared: Literal[True] = True
    archive_sha256: Sha256
    archive_size: Counter
    members: Annotated[int, Field(strict=True, ge=1, le=MAX_CHECKPOINT_MEMBERS)]
    content_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CHECKPOINT_CONTENT)]
    metadata_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CHECKPOINT_METADATA)]
