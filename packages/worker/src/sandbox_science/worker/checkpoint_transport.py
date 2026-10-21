# SPDX-License-Identifier: MIT
"""Stream complete local checkpoints through bounded immutable S3 objects."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from pydantic import Field, TypeAdapter

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.model import Sha256, StrictContract
from sandbox_science.contracts.workspace_checkpoint import (
    MAX_CHECKPOINT_RECEIPT,
    CheckpointReceipt,
    CheckpointSource,
)

from .checkpoint_operator import install, publish_directory, receipt, sync, verify
from .checkpoint_s3 import Objects, Storage
from .checkpoint_stream import CHUNK

CHUNK_BYTES = 16 * 1024**2
MAX_CHUNKS = 65536
MAX_TRANSPORT_BYTES = CHUNK_BYTES * MAX_CHUNKS
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_HEADER_BYTES = 2 * MAX_CHECKPOINT_RECEIPT + 1024
FORMAT = "sandbox-workspace-checkpoint-transport-v1"
_DIGEST = TypeAdapter[str](Sha256)


class Header(StrictContract):
    format: Literal["sandbox-workspace-checkpoint-transport-v1"] = FORMAT
    receipt: CheckpointReceipt


class Chunk(StrictContract):
    sha256: Sha256
    size: Annotated[int, Field(strict=True, ge=1, le=CHUNK_BYTES)]


class End(StrictContract):
    kind: Literal["end"] = "end"
    chunks: Annotated[int, Field(strict=True, ge=1, le=MAX_CHUNKS)]
    archive_size: Annotated[int, Field(strict=True, ge=1, le=MAX_TRANSPORT_BYTES)]


def _line(handle: BinaryIO, maximum: int) -> bytes:
    raw = handle.readline(maximum + 1)
    if not raw.endswith(b"\n") or len(raw) > maximum:
        raise ValueError("checkpoint manifest line is truncated or exceeds its bound")
    strict_loads(raw)
    return raw


def _header(handle: BinaryIO) -> CheckpointReceipt:
    result = Header.model_validate_json(_line(handle, MAX_HEADER_BYTES)).receipt
    if not 1 <= result.archive_size <= MAX_TRANSPORT_BYTES:
        raise ValueError("checkpoint archive exceeds the transport size bound")
    return result


def _chunks(handle: BinaryIO, expected: CheckpointReceipt) -> Iterator[Chunk]:
    left, count = expected.archive_size, 0
    while left:
        entry = Chunk.model_validate_json(_line(handle, 256))
        if entry.size != min(CHUNK_BYTES, left):
            raise ValueError("checkpoint manifest chunk size differs from its position")
        count += 1
        if count > MAX_CHUNKS:
            raise ValueError("checkpoint manifest exceeds its chunk count bound")
        yield entry
        left -= entry.size
    end = End.model_validate_json(_line(handle, 256))
    if end.chunks != count or end.archive_size != expected.archive_size or handle.read(1):
        raise ValueError("checkpoint manifest footer differs or has trailing entries")


def _append(handle: BinaryIO, raw: bytes) -> None:
    if handle.tell() + len(raw) + 1 > MAX_MANIFEST_BYTES:
        raise ValueError("checkpoint manifest exceeds its total bound")
    handle.write(raw + b"\n")


def upload(
    directory: Path,
    source: CheckpointSource,
    settings: Storage,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> str:
    """Commit a manifest only after local verification and every exact chunk readback."""
    with Objects(settings, timeout=timeout, clock=clock) as objects:
        expected = receipt(directory)
        if expected.archive_size > MAX_TRANSPORT_BYTES:
            raise ValueError("checkpoint archive exceeds the transport size bound")
        if verify(directory, source, check=objects.check) != expected:
            raise ValueError("checkpoint local receipt changed during verification")
        with tempfile.TemporaryFile("w+b") as manifest:
            _append(manifest, Header(receipt=expected).model_dump_json().encode())
            descriptor = os.open(directory / "archive.bin", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as archive:
                left, count, whole = expected.archive_size, 0, hashlib.sha256()
                while left:
                    objects.check()
                    size = min(CHUNK_BYTES, left)
                    with tempfile.TemporaryFile("w+b") as chunk:
                        remaining, digest = size, hashlib.sha256()
                        while remaining:
                            data = archive.read(min(CHUNK, remaining))
                            if not data:
                                raise ValueError("checkpoint source archive shrank during upload")
                            chunk.write(data)
                            digest.update(data)
                            whole.update(data)
                            remaining -= len(data)
                        entry = Chunk(sha256=digest.hexdigest(), size=size)
                        objects.retain("chunks", entry.sha256, size, chunk)
                        _append(manifest, entry.model_dump_json().encode())
                    count += 1
                    left -= size
                if archive.read(1) or whole.hexdigest() != expected.archive_sha256:
                    raise ValueError("checkpoint source archive changed during upload")
            _append(
                manifest,
                End(chunks=count, archive_size=expected.archive_size).model_dump_json().encode(),
            )
            size, digest = manifest.tell(), hashlib.sha256()
            manifest.seek(0)
            while data := manifest.read(CHUNK):
                digest.update(data)
            identity = digest.hexdigest()
            objects.retain("manifests", identity, size, manifest)
            return identity


def fetch(
    directory: Path,
    source: CheckpointSource,
    settings: Storage,
    identity: str,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointReceipt:
    """Accept only the pinned manifest and verified complete archive; never extract it."""
    identity = _DIGEST.validate_python(identity)
    with Objects(settings, timeout=timeout, clock=clock) as objects:
        directory = directory.absolute()
        if directory.exists() or directory.is_symlink():
            raise FileExistsError("checkpoint fetch requires a new destination")
        journal = directory.with_name(directory.name + ".checkpoint-fetch")
        journal.mkdir(mode=0o700)
        sync(journal.parent)
        install(
            journal / "intent.json",
            json.dumps(
                {
                    "state": "incomplete",
                    "manifest_sha256": identity,
                    "source": source.model_dump(mode="json"),
                }
            ).encode(),
        )
        stage = journal / "checkpoint"
        stage.mkdir(mode=0o700)
        try:
            with (journal / "manifest.ndjson").open("xb") as manifest:
                objects.copy("manifests", identity, MAX_MANIFEST_BYTES, manifest)
                manifest.flush()
                os.fsync(manifest.fileno())
            with (journal / "manifest.ndjson").open("rb") as manifest:
                expected = _header(manifest)
                if expected.source != source:
                    raise ValueError(
                        "checkpoint manifest differs from the retained original source"
                    )
                with (stage / "archive.bin").open("xb") as archive:
                    for entry in _chunks(manifest, expected):
                        objects.copy(
                            "chunks", entry.sha256, CHUNK_BYTES, archive, expected=entry.size
                        )
                    archive.flush()
                    os.fsync(archive.fileno())
            install(stage / "receipt.json", expected.model_dump_json().encode())
            if verify(stage, source, check=objects.check) != expected:
                raise ValueError("fetched checkpoint receipt differs")
            objects.check()
            publish_directory(stage, directory)
            sync(directory.parent)
            install(journal / "receipt.json", expected.model_dump_json().encode())
            return expected
        except BaseException as error:
            install(
                journal / "failure.json",
                json.dumps(
                    {
                        "state": "incomplete",
                        "error_type": type(error).__name__,
                        "message": str(error)[:512],
                    }
                ).encode(),
            )
            raise
