# SPDX-License-Identifier: MIT
"""Opt-in bounded cache passes, retaining the original metadata and remote content."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.model import StrictContract

from .artifact_blobs import ArtifactBlobs, Blob
from .artifact_references import reference_page
from .documents import DocumentError


class MaintenancePolicy(StrictContract):
    """Physical cache policy, independent of scientific retention or logical quotas."""

    max_references: Annotated[int, Field(ge=1, le=100)] = 25
    max_bytes: Annotated[int, Field(ge=1, le=64 * 1024**3)] = 1024**3
    min_age_seconds: Annotated[int, Field(ge=0, le=3650 * 86400)] = 86400
    archive_before_evict: bool = False


def load_policy(path: Path) -> MaintenancePolicy:
    """Read bounded operator configuration; reject unknown fields and type coercion."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("maintenance policy must be a regular file")
        content = source.read(8193)
    if len(content) > 8192:
        raise ValueError("maintenance policy is too large")
    return MaintenancePolicy.model_validate(tomllib.loads(content.decode("utf-8")))


@dataclass(frozen=True)
class MaintenanceEntry:
    sha256: str
    bytes: int
    state: Literal["not_cached", "young", "over_budget", "would_evict", "evicted", "unavailable"]


@dataclass(frozen=True)
class MaintenanceResult:
    entries: tuple[MaintenanceEntry, ...]
    next_after: str
    selected_bytes: int
    evicted_bytes: int
    failed: int


def maintain(
    database: Path,
    policy: MaintenancePolicy,
    *,
    apply: bool = False,
    after: str = "",
    clock: Clock = SYSTEM_CLOCK,
) -> MaintenanceResult:
    """One bounded page; no background retry, model call, remote delete or PVC action.

    ``max_bytes`` bounds selected content, not network traffic: full verification can
    read it several times. An individual SDK operation retains its existing timeout.
    The cursor can advance past failures; wrapping scans them again on a later pass.
    """
    store = ArtifactBlobs(database)
    if store.backing is None:
        raise ValueError("configure artifact S3 backing first")
    page = reference_page(database, after=after, limit=policy.max_references + 1)
    entries: list[MaintenanceEntry] = []
    selected = evicted = failed = 0
    now = clock.now().timestamp()
    for blob in page[: policy.max_references]:
        state: MaintenanceEntry | None = None
        try:
            info = store.path(blob.sha256).lstat()
        except FileNotFoundError:
            # Cold copies are not downloaded merely to evict them again.
            state = MaintenanceEntry(blob.sha256, blob.size, "not_cached")
        except OSError:
            state = MaintenanceEntry(blob.sha256, blob.size, "unavailable")
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_size != blob.size:
                state = MaintenanceEntry(blob.sha256, blob.size, "unavailable")
            elif now - info.st_mtime < policy.min_age_seconds:
                state = MaintenanceEntry(blob.sha256, blob.size, "young")
            elif selected + blob.size > policy.max_bytes:
                state = MaintenanceEntry(blob.sha256, blob.size, "over_budget")
            else:
                selected += blob.size
                state = _evict(store, blob, policy, apply=apply)
                if state.state == "evicted":
                    evicted += blob.size
        entries.append(state)
        failed += state.state == "unavailable"
    # Empty cursor means this cycle completed. A scheduled caller restarts from
    # the beginning next time, including new low hashes and previously failed rows.
    next_after = entries[-1].sha256 if len(page) > len(entries) else ""
    return MaintenanceResult(tuple(entries), next_after, selected, evicted, failed)


def _evict(
    store: ArtifactBlobs, blob: Blob, policy: MaintenancePolicy, *, apply: bool
) -> MaintenanceEntry:
    try:
        if apply and policy.archive_before_evict:
            store.archive(blob)
        local = store.evict(blob, apply=apply)
    except (DocumentError, OSError, ValueError):
        # Underlying SDK errors may contain private destinations or credentials.
        return MaintenanceEntry(blob.sha256, blob.size, "unavailable")
    return MaintenanceEntry(
        blob.sha256, blob.size, ("evicted" if apply else "would_evict") if local else "not_cached"
    )
