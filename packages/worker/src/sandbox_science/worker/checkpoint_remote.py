# SPDX-License-Identifier: MIT
"""Checkpoints in the object store: verify one in place, copy one into the kept bucket,
and remove one the operator chose (docs/WORKER.md#checkpoint-jobs).

``verify`` is the worker's own check (C2): it reads the pinned manifest, then every chunk
in order, each checked against its SHA-256 before the decoder sees a byte of it, and runs
the whole archive through ``decode`` against the binding the worker holds. Nothing is
written locally beyond one chunk in memory. ``promote`` and ``delete`` run with the
operator's keeper key, never in the cluster.
"""

from __future__ import annotations

import io
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Literal

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.workspace_checkpoint import CheckpointReceipt, CheckpointSource

from .checkpoint_s3 import Objects, Storage
from .checkpoint_stream import StreamSummary, decode
from .checkpoint_transport import CHUNK_BYTES, MAX_MANIFEST_BYTES, Chunk, chunks, header
from .checkpoint_tree import TreeIndex

MAX_KEPT_MANIFESTS = 4096
"""Manifests a deletion reads to find which chunks other checkpoints still use."""
MAX_EXPIRED_OBJECTS = 1_000_000
"""Objects of one kind an upload expiry lists; more is an error, never a partial pass."""


class RemoteArchive:
    """The archive as the decoder reads it: one verified chunk at a time, in order."""

    def __init__(self, objects: Objects, entries: Iterator[Chunk]) -> None:
        self.objects, self.entries = objects, entries
        self.current = io.BytesIO()
        self.left = 0

    def read(self, amount: int, /) -> bytes:
        parts: list[bytes] = []
        while amount:
            if not self.left:
                entry = next(self.entries, None)
                if entry is None:
                    break
                # ``copy`` checks the length and the hash before returning, so a replaced
                # or truncated chunk fails here and none of its bytes are decoded.
                buffer = io.BytesIO()
                self.objects.copy("chunks", entry.sha256, CHUNK_BYTES, buffer, expected=entry.size)
                buffer.seek(0)
                self.current, self.left = buffer, entry.size
            data = self.current.read(min(amount, self.left))
            self.left -= len(data)
            amount -= len(data)
            parts.append(data)
        return b"".join(parts)


def _manifest(objects: Objects, identity: str, target: BinaryIO) -> CheckpointReceipt:
    objects.copy("manifests", identity, MAX_MANIFEST_BYTES, target)
    target.seek(0)
    return header(target)


def verify(
    settings: Storage,
    identity: str,
    source: CheckpointSource,
    *,
    chunk_names: list[str] | None = None,
    tree: TreeIndex | None = None,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointReceipt:
    """The receipt of the checkpoint whose manifest has SHA-256 ``identity``, once every
    byte read back matches it and ``source``; any difference raises ValueError. With
    ``chunk_names``, the manifest's chunks are appended there in order; with ``tree``, the
    archive's tree is described there."""
    return extract(
        settings,
        identity,
        source,
        None,
        chunk_names=chunk_names,
        tree=tree,
        timeout=timeout,
        clock=clock,
    )


def extract(
    settings: Storage,
    identity: str,
    source: CheckpointSource,
    target: Path | None,
    *,
    chunk_names: list[str] | None = None,
    tree: TreeIndex | None = None,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointReceipt:
    """``verify``, writing the tree into the empty directory ``target`` as it is read (a
    restore Job); a chunk is checked against its hash before the decoder sees a byte of it,
    so a failure leaves a partial tree of verified bytes only."""
    with (
        Objects(settings, timeout=timeout, clock=clock) as objects,
        tempfile.TemporaryFile() as raw,
    ):
        receipt = _manifest(objects, identity, raw)
        if receipt.source != source:
            raise ValueError("checkpoint manifest names another binding than the worker's")
        if receipt.capture is None:
            raise ValueError("checkpoint receipt does not state how it was captured")
        entries = _noted(chunks(raw, receipt), chunk_names)
        archive = RemoteArchive(objects, entries)
        summary = decode(archive, source, target, check=objects.check, tree=tree)
        if next(entries, None) is not None:
            raise ValueError("checkpoint manifest lists chunks past the archive's end")
        if summary != StreamSummary(
            receipt.archive_sha256,
            receipt.archive_size,
            receipt.members,
            receipt.content_bytes,
            receipt.metadata_bytes,
        ):
            raise ValueError("checkpoint archive differs from its manifest's receipt")
        return receipt


def _noted(entries: Iterator[Chunk], names: list[str] | None) -> Iterator[Chunk]:
    for entry in entries:
        if names is not None:
            names.append(entry.sha256)
        yield entry


def _promote_one(
    reader: Objects,
    writer: Objects,
    kind: Literal["chunks", "manifests"],
    digest: str,
    size: int,
) -> bool:
    """One object into the kept bucket; whether it was copied.

    Chunks are shared between checkpoints of a workspace (an unchanged leading part of the
    archive gives the same chunks), so a kept object that already reads back exactly is
    never overwritten: anything holding the upload key could have replaced the upload's
    copy since the worker verified it. Otherwise the upload's copy is read and hashed, and
    only that version (its ETag) is copied; the copy is then read back and hashed too."""
    if writer.verified(kind, digest, size):
        return False
    reader.copy(kind, digest, size, expected=size)
    if not reader.etag:
        raise ValueError("the object store gave no ETag to pin the copy to")
    writer.copy_in(reader.settings, kind, digest, size, reader.etag)
    return True


def promote(
    source: Storage,
    target: Storage,
    identity: str,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[CheckpointReceipt, int]:
    """Copy a checkpoint from the upload bucket into the kept one inside the store: every
    chunk, then the manifest last, so a kept manifest never names a missing chunk. Returns
    the receipt and how many objects were copied (the rest were already kept, exactly)."""
    with (
        Objects(source, timeout=timeout, clock=clock) as reader,
        Objects(target, timeout=timeout, clock=clock) as writer,
        tempfile.TemporaryFile() as raw,
    ):
        receipt = _manifest(reader, identity, raw)
        copied = 0
        for entry in chunks(raw, receipt):
            copied += _promote_one(reader, writer, "chunks", entry.sha256, entry.size)
        size = raw.seek(0, io.SEEK_END)
        copied += _promote_one(reader, writer, "manifests", identity, size)
        return receipt, copied


def referenced(objects: Objects, identities: list[str]) -> set[str]:
    """The chunks the given manifests name."""
    names: set[str] = set()
    for identity in identities:
        with tempfile.TemporaryFile() as raw:
            receipt = _manifest(objects, identity, raw)
            names.update(entry.sha256 for entry in chunks(raw, receipt))
    return names


def delete(
    settings: Storage,
    identity: str,
    *,
    chunk_names: list[str] | None = None,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> int:
    """Remove a checkpoint: each of its chunks no other manifest in the bucket names, then
    its manifest last. ``chunk_names`` is the list the worker recorded when it verified the
    checkpoint, so a run can resume after a failure even once the manifest is gone; without
    it the manifest is read. A missing object counts as removed (S3 deletes are idempotent).
    Returns how many chunks were asked to go."""
    with Objects(settings, timeout=timeout, clock=clock) as objects:
        if chunk_names is None:
            with tempfile.TemporaryFile() as raw:
                receipt = _manifest(objects, identity, raw)
                chunk_names = [entry.sha256 for entry in chunks(raw, receipt)]
        others = [
            name for name in objects.names("manifests", MAX_KEPT_MANIFESTS) if name != identity
        ]
        kept = referenced(objects, others)
        removed = 0
        for name in dict.fromkeys(chunk_names):
            if name not in kept:
                objects.delete("chunks", name)
                removed += 1
        objects.delete("manifests", identity)
        return removed


def expire(
    settings: Storage,
    older_than: timedelta,
    protected: set[str],
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[int, int]:
    """Remove the upload bucket's objects older than ``older_than`` that ``protected`` does
    not name (the worker's list of what unpromoted checkpoints still need there): manifests
    first, so no manifest is left naming a removed chunk. An object whose time the store
    does not give is kept. Returns how many were removed and how many kept."""
    removed = kept = 0
    with Objects(settings, timeout=timeout, clock=clock) as objects:
        cutoff = clock.now() - older_than
        kinds: tuple[Literal["chunks", "manifests"], ...] = ("manifests", "chunks")
        for kind in kinds:
            for name, written in objects.listing(kind, MAX_EXPIRED_OBJECTS):
                if name in protected or written is None or written >= cutoff:
                    kept += 1
                    continue
                objects.delete(kind, name)
                removed += 1
    return removed, kept
