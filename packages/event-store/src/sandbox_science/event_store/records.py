# SPDX-License-Identifier: MIT
"""Records, their hash chain and checkpoints (docs/EVENT-STORE.md).

A record's hash is SHA-256 over seven fields, each written as its length in
bytes (8 bytes, unsigned big-endian) followed by the bytes themselves:

1. the format name ``sandbox-science-event-store/v1``, in ASCII;
2. the previous record's hash, as its 32 raw bytes (32 zero bytes for record 1);
3. the sequence number, as 8 bytes, unsigned big-endian;
4. the time, in ASCII;
5. the kind, in ASCII;
6. the source, in ASCII;
7. the payload's canonical JSON text, in UTF-8.

Every field carries its length, so no two different records encode to the
same bytes, and the format name keeps these hashes apart from any other use
of SHA-256.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

from .canonical import canonical_json, parse_json_object, parse_payload
from .errors import CheckpointError, RecordError

FORMAT: Final = "sandbox-science-event-store/v1"
"""Names the record encoding; the first field of every hash, and part of every checkpoint."""

GENESIS: Final = "0" * 64
"""The previous hash of record 1, and the hash of an empty store's head."""

MAX_SEQ: Final = 2**63 - 1

NAME_RE: Final = re.compile(r"[a-z][a-z0-9._-]{0,63}")
"""Record kinds and sources: lowercase ASCII names."""

HASH_RE: Final = re.compile(r"[0-9a-f]{64}")
TIME_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
"""Record times: UTC, with microseconds."""

_LENGTH_BYTES: Final = 8


def record_hash(
    *, prev_hash: str, seq: int, time: str, kind: str, source: str, payload: str
) -> str:
    """Return a record's hash as lowercase hex (see the module docstring)."""
    if not HASH_RE.fullmatch(prev_hash):
        raise RecordError(f"previous hash {prev_hash!r} is not 64 lowercase hex digits")
    if not 1 <= seq <= MAX_SEQ:
        raise RecordError(f"sequence number {seq} is out of range")
    digest = hashlib.sha256()
    fields = (
        FORMAT.encode(),
        bytes.fromhex(prev_hash),
        seq.to_bytes(8, "big"),
        time.encode(),
        kind.encode(),
        source.encode(),
        payload.encode(),
    )
    for field in fields:
        digest.update(len(field).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(field)
    return digest.hexdigest()


def format_time(moment: datetime) -> str:
    """Write a time as records do: UTC with microseconds, ``2026-10-11T08:15:00.000000Z``."""
    if moment.utcoffset() is None:
        raise ValueError("record times must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def check_name(value: str, what: str) -> str:
    """Return ``value`` if it is a valid record kind or source, else raise RecordError."""
    if not NAME_RE.fullmatch(value):
        raise RecordError(
            f"{what} {value!r} must be 1 to 64 lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter"
        )
    return value


@dataclass(frozen=True, slots=True)
class Record:
    """One stored record. ``payload`` is canonical JSON text; the hashes are lowercase hex."""

    seq: int
    time: str
    kind: str
    source: str
    payload: str
    prev_hash: str
    hash: str

    def data(self) -> dict[str, object]:
        """The payload as a dictionary."""
        return parse_payload(self.payload)

    def to_json(self) -> str:
        """The record as one canonical JSON object, the form ``export`` writes."""
        return canonical_json(
            {
                "seq": self.seq,
                "time": self.time,
                "kind": self.kind,
                "source": self.source,
                "payload": self.data(),
                "prev_hash": self.prev_hash,
                "hash": self.hash,
            }
        )


def record_from_row(row: tuple[object, ...]) -> Record:
    """Check a stored row's types and formats and return it as a record.

    The hashes are not checked here; verification does that.
    """
    if len(row) != 7:
        raise RecordError(f"a row has {len(row)} columns, not 7")
    seq, time, kind, source, payload, prev_hash, hash_ = row
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise RecordError(f"sequence number {seq!r} is not an integer")
    if not 1 <= seq <= MAX_SEQ:
        raise RecordError(f"sequence number {seq} is out of range", seq)
    texts = (time, kind, source, payload, prev_hash, hash_)
    if not all(isinstance(value, str) for value in texts):
        raise RecordError("a text column holds another type", seq)
    time, kind, source, payload, prev_hash, hash_ = cast("tuple[str, ...]", texts)
    if not TIME_RE.fullmatch(time):
        raise RecordError(f"time {time!r} is malformed", seq)
    for value, what in ((kind, "kind"), (source, "source")):
        if not NAME_RE.fullmatch(value):
            raise RecordError(f"{what} {value!r} is malformed", seq)
    for value, what in ((prev_hash, "previous hash"), (hash_, "hash")):
        if not HASH_RE.fullmatch(value):
            raise RecordError(f"{what} {value!r} is not 64 lowercase hex digits", seq)
    try:
        parse_payload(payload)
    except RecordError as exc:
        raise RecordError(str(exc), seq) from exc
    return Record(seq, time, kind, source, payload, prev_hash, hash_)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A store's head: the sequence number and hash of its newest record.

    Kept somewhere the store's writers cannot change, a checkpoint shows later
    whether any record up to it was changed or removed (docs/EVENT-STORE.md).
    An empty store's checkpoint is ``Checkpoint(0, GENESIS)``.
    """

    seq: int
    hash: str

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise CheckpointError(f"sequence number {self.seq!r} is not an integer")
        if not 0 <= self.seq <= MAX_SEQ:
            raise CheckpointError(f"sequence number {self.seq} is out of range")
        if not isinstance(self.hash, str) or not HASH_RE.fullmatch(self.hash):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise CheckpointError(f"hash {self.hash!r} is not 64 lowercase hex digits")
        if self.seq == 0 and self.hash != GENESIS:
            raise CheckpointError("an empty store's checkpoint has the all-zero hash")

    def to_json(self) -> str:
        """The checkpoint as canonical JSON: ``{"format": ..., "hash": ..., "seq": ...}``."""
        return canonical_json({"format": FORMAT, "seq": self.seq, "hash": self.hash})

    @classmethod
    def from_json(cls, text: str) -> Checkpoint:
        """Read a checkpoint as ``to_json`` writes it, in any JSON layout."""
        try:
            data = parse_json_object(text)
        except RecordError as exc:
            raise CheckpointError(f"not a checkpoint: {exc}") from exc
        if set(data) != {"format", "seq", "hash"}:
            raise CheckpointError("a checkpoint has exactly the keys format, seq and hash")
        if data["format"] != FORMAT:
            raise CheckpointError(f"checkpoint format {data['format']!r} is not {FORMAT!r}")
        seq, hash_ = data["seq"], data["hash"]
        if not isinstance(seq, int) or not isinstance(hash_, str):
            raise CheckpointError("a checkpoint's seq is an integer and its hash a string")
        return cls(seq, hash_)
