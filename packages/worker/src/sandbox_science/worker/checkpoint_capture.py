# SPDX-License-Identifier: MIT
"""Stream a mounted workspace straight into checkpoint chunks, with no local archive (C1).

A checkpoint Job (docs/KUBERNETES-BUNDLE.md#workspace-checkpoints) mounts a suspended
sandbox's volume read-only and runs this with the binding the worker planned. The encoder
writes into a sink that cuts every 16 MiB into a content-addressed chunk, retains it with
a full read-back and notes its line; the manifest (receipt header, chunk lines, footer) is
assembled last, once the counts are known, and published after every chunk. Scratch space
stays at one chunk plus the encoder's path index, whatever the workspace's size.

The Job proves nothing by itself: the worker counts a checkpoint only after reading every
chunk back through the decoder against its own binding (``checkpoint_remote.verify``).
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.workspace_checkpoint import (
    MAX_CHECKPOINT_RECEIPT,
    CaptureMode,
    CheckpointReceipt,
    CheckpointSource,
    SkippedEntry,
)

from .checkpoint_operator import check_prefix
from .checkpoint_s3 import Objects, Storage
from .checkpoint_stream import CHUNK, encode
from .checkpoint_transport import (
    CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_MANIFEST_BYTES,
    MAX_TRANSPORT_BYTES,
    Chunk,
    End,
    Header,
)


class ChunkSink:
    """A write-only file for the encoder: every ``CHUNK_BYTES`` become one retained chunk."""

    def __init__(self, objects: Objects, lines: BinaryIO) -> None:
        self.objects, self.lines = objects, lines
        self.buffer = tempfile.TemporaryFile("w+b")  # noqa: SIM115 - closed in close()
        self.digest = hashlib.sha256()
        self.filled = self.total = self.chunks = self.written = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.buffer.close()

    def write(self, data: bytes) -> int:
        self.total += len(data)
        if self.total > MAX_TRANSPORT_BYTES:
            raise ValueError("checkpoint archive exceeds the transport size bound")
        view = memoryview(data)
        while view:
            take = min(CHUNK_BYTES - self.filled, len(view))
            self.buffer.write(view[:take])
            self.digest.update(view[:take])
            self.filled += take
            view = view[take:]
            if self.filled == CHUNK_BYTES:
                self.flush()
        return len(data)

    def flush(self) -> None:
        """Retain the chunk filled so far; the last one is shorter."""
        if not self.filled:
            return
        if self.chunks >= MAX_CHUNKS:
            raise ValueError("checkpoint archive exceeds its chunk count bound")
        entry = Chunk(sha256=self.digest.hexdigest(), size=self.filled)
        self.buffer.flush()
        if self.objects.store("chunks", entry.sha256, entry.size, self.buffer):
            self.written += 1
        line = entry.model_dump_json().encode() + b"\n"
        if self.lines.tell() + len(line) > MAX_MANIFEST_BYTES:
            raise ValueError("checkpoint manifest exceeds its total bound")
        self.lines.write(line)
        self.chunks += 1
        self.buffer.seek(0)
        self.buffer.truncate()
        self.digest = hashlib.sha256()
        self.filled = 0


def capture_upload(
    root: Path,
    source: CheckpointSource,
    settings: Storage,
    capture: CaptureMode,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[str, CheckpointReceipt, int]:
    """Capture ``root`` under ``source`` into the store; the manifest's SHA-256, the
    receipt and how many chunks were written (the rest already read back exactly)."""
    check_prefix(root, source)
    skipped: list[SkippedEntry] = []
    with (
        Objects(settings, timeout=timeout, clock=clock) as objects,
        tempfile.TemporaryFile("w+b") as lines,
        ChunkSink(objects, lines) as sink,
    ):
        summary = encode(root, source, sink, skipped=skipped)
        sink.flush()
        check_prefix(root, source)
        if sink.total != summary.size or not sink.chunks:
            raise ValueError("checkpoint sink lost bytes")
        receipt = CheckpointReceipt(
            source=source,
            created_at=clock.now(),
            capture=capture,
            skipped=tuple(skipped),
            archive_sha256=summary.sha256,
            archive_size=summary.size,
            members=summary.members,
            content_bytes=summary.content_bytes,
            metadata_bytes=summary.metadata_bytes,
        )
        header = Header(receipt=receipt).model_dump_json().encode() + b"\n"
        if len(receipt.model_dump_json().encode()) > MAX_CHECKPOINT_RECEIPT:
            raise ValueError("checkpoint receipt exceeds its metadata bound")
        footer = End(chunks=sink.chunks, archive_size=summary.size).model_dump_json().encode()
        with tempfile.TemporaryFile("w+b") as manifest:
            manifest.write(header)
            lines.seek(0)
            while data := lines.read(CHUNK):
                manifest.write(data)
            manifest.write(footer + b"\n")
            size = manifest.tell()
            if size > MAX_MANIFEST_BYTES:
                raise ValueError("checkpoint manifest exceeds its total bound")
            digest = hashlib.sha256()
            manifest.seek(0)
            while data := manifest.read(CHUNK):
                digest.update(data)
            identity = digest.hexdigest()
            objects.retain("manifests", identity, size, manifest)
    return identity, receipt, sink.written
