# SPDX-License-Identifier: MIT
"""Check a store's hash chain, and its head against checkpoints kept elsewhere.

From the store alone, verification finds a record that was changed, removed,
inserted or moved, unless whoever did it also recomputed the hashes of every
later record. The hashes are not keyed, so anyone who can write the database
can do that, and can remove the newest records, which leaves a shorter but
valid chain. A checkpoint (the head's sequence number and hash, kept where the
store's writers cannot change it) catches both for every record up to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import RecordError
from .records import GENESIS, HASH_RE, Checkpoint, record_from_row, record_hash
from .store import DEFAULT_TIMEOUT, StoreReader

MAX_PROBLEMS: Final = 1000
"""Problems listed before verification stops listing them."""


@dataclass(frozen=True, slots=True)
class Problem:
    """One way the store differs from an intact chain; ``seq`` names the record, if any."""

    seq: int | None
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class Verification:
    """The outcome: how many records were read, the head, and the problems found."""

    records: int
    head: Checkpoint
    problems: tuple[Problem, ...]
    truncated: bool = False
    """More problems were found than are listed."""

    @property
    def ok(self) -> bool:
        """Whether the chain is intact and agrees with every checkpoint."""
        return not self.problems


def verify_store(
    path: Path, checkpoints: Sequence[Checkpoint] = (), *, timeout: float = DEFAULT_TIMEOUT
) -> Verification:
    """Verify the store at ``path``, and its agreement with ``checkpoints``.

    Raises EventStoreError if the store cannot be read at all.
    """
    with StoreReader(path, timeout=timeout) as reader:
        schema = [Problem(None, problem) for problem in reader.schema_problems()]
        result = verify_rows(reader.rows(), checkpoints)
    if not schema:
        return result
    return Verification(result.records, result.head, (*schema, *result.problems), result.truncated)


def verify_rows(
    rows: Iterable[tuple[object, ...]], checkpoints: Sequence[Checkpoint] = ()
) -> Verification:
    """Verify stored rows ``(seq, time, kind, source, payload, prev_hash, hash)``.

    Rows come in the order of their sequence numbers, as the store keeps them.
    """
    problems = _Problems()
    wanted: dict[int, Checkpoint] = {}
    for checkpoint in checkpoints:
        other = wanted.setdefault(checkpoint.seq, checkpoint)
        if other.hash != checkpoint.hash:
            problems.add(checkpoint.seq, f"two checkpoints for record {checkpoint.seq} disagree")
    expected = 1
    link: str | None = GENESIS  # the stored hash of the record before; None if unknown
    head = Checkpoint(0, GENESIS)
    seen: set[int] = set()
    count = 0
    for row in rows:
        count += 1
        try:
            record = record_from_row(row)
        except RecordError as exc:
            seq = exc.seq
            problems.add(seq, f"record {seq if seq is not None else '?'} is malformed: {exc}")
            if seq is not None:
                seen.add(seq)  # reported; not also as missing
                expected = seq + 1
            stored = row[6] if len(row) == 7 else None
            link = stored if isinstance(stored, str) and HASH_RE.fullmatch(stored) else None
            continue
        seq = record.seq
        if seq > expected:
            if seq == expected + 1:
                problems.add(expected, f"record {expected} is missing")
            else:
                problems.add(expected, f"records {expected} to {seq - 1} are missing")
        elif seq < expected:
            problems.add(seq, f"record {seq} is out of order or repeated")
        elif link is not None and record.prev_hash != link:
            problems.add(
                seq,
                f"record {seq} does not follow record {seq - 1}: records before it were "
                "changed, removed or inserted",
            )
        computed = record_hash(
            prev_hash=record.prev_hash,
            seq=seq,
            time=record.time,
            kind=record.kind,
            source=record.source,
            payload=record.payload,
        )
        if computed != record.hash:
            problems.add(seq, f"record {seq} does not match its hash: it was changed")
        checkpoint = wanted.get(seq)
        if checkpoint is not None and checkpoint.hash != record.hash:
            problems.add(
                seq,
                f"record {seq} differs from its checkpoint: records up to it were changed "
                "and the hashes recomputed",
            )
        seen.add(seq)
        link = record.hash
        expected = seq + 1
        head = Checkpoint(seq, record.hash)
    for checkpoint in sorted(wanted.values(), key=lambda checkpoint: checkpoint.seq):
        if checkpoint.seq == 0 or checkpoint.seq in seen:
            continue
        if checkpoint.seq > head.seq:
            problems.add(
                None,
                f"the store ends at record {head.seq}, but a checkpoint names record "
                f"{checkpoint.seq}: newer records were removed",
            )
        else:
            problems.add(
                checkpoint.seq, f"record {checkpoint.seq}, named by a checkpoint, is missing"
            )
    return Verification(count, head, tuple(problems.items), problems.truncated)


class _Problems:
    """Collects problems, up to MAX_PROBLEMS."""

    def __init__(self) -> None:
        self.items: list[Problem] = []
        self.truncated = False

    def add(self, seq: int | None, message: str) -> None:
        if len(self.items) < MAX_PROBLEMS:
            self.items.append(Problem(seq, message))
        else:
            self.truncated = True
