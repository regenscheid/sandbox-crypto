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
from typing import BinaryIO

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.workspace_checkpoint import CheckpointReceipt, CheckpointSource

from .checkpoint_s3 import Objects, Storage
from .checkpoint_stream import StreamSummary, decode
from .checkpoint_transport import CHUNK_BYTES, MAX_MANIFEST_BYTES, Chunk, chunks, header

MAX_KEPT_MANIFESTS = 4096
"""Manifests a deletion reads to find which chunks other checkpoints still use."""


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
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointReceipt:
    """The receipt of the checkpoint whose manifest has SHA-256 ``identity``, once every
    byte read back matches it and ``source``; any difference raises ValueError."""
    with (
        Objects(settings, timeout=timeout, clock=clock) as objects,
        tempfile.TemporaryFile() as raw,
    ):
        receipt = _manifest(objects, identity, raw)
        if receipt.source != source:
            raise ValueError("checkpoint manifest names another binding than the worker's")
        if receipt.capture is None:
            raise ValueError("checkpoint receipt does not state how it was captured")
        entries = chunks(raw, receipt)
        archive = RemoteArchive(objects, entries)
        summary = decode(archive, source, check=objects.check)
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


def promote(
    source: Storage,
    target: Storage,
    identity: str,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointReceipt:
    """Copy a checkpoint from the upload bucket into the kept one inside the store: every
    chunk, then the manifest last, so a kept manifest never names a missing chunk."""
    with (
        Objects(source, timeout=timeout, clock=clock) as reader,
        Objects(target, timeout=timeout, clock=clock) as writer,
        tempfile.TemporaryFile() as raw,
    ):
        receipt = _manifest(reader, identity, raw)
        for entry in chunks(raw, receipt):
            writer.copy_in(source, "chunks", entry.sha256, entry.size)
        writer.copy_in(source, "manifests", identity, raw.seek(0, io.SEEK_END))
        return receipt


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
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> int:
    """Remove a checkpoint: its manifest first, so it can never be fetched half gone, then
    each of its chunks no other manifest in the bucket names. Returns the chunks removed.
    A manifest that is already gone leaves its chunks for a later pass to find unnamed."""
    with Objects(settings, timeout=timeout, clock=clock) as objects:
        with tempfile.TemporaryFile() as raw:
            receipt = _manifest(objects, identity, raw)
            own = [entry.sha256 for entry in chunks(raw, receipt)]
        others = [
            name for name in objects.names("manifests", MAX_KEPT_MANIFESTS) if name != identity
        ]
        kept = referenced(objects, others)
        objects.delete("manifests", identity)
        removed = 0
        for name in dict.fromkeys(own):
            if name not in kept:
                objects.delete("chunks", name)
                removed += 1
        return removed
