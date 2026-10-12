# SPDX-License-Identifier: MIT
"""Private bounded catalog delivery pins. No network or environment file access."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier, new_id
from sandbox_science.contracts.artifacts import Sha256
from sandbox_science.contracts.documents import (
    MAX_CATALOG_ENTRIES,
    MAX_CATALOG_PAGE_BYTES,
    CatalogDelta,
    CatalogSnapshot,
    Counter,
    PublicationEntry,
    manifest_sha256,
)
from sandbox_science.contracts.model import Contract

from .publication import PublicationPlan


class DeliveryError(Exception):
    """An unsafe, busy or unreadable delivery; the pin remains available for recovery."""


def endpoint_fingerprint(base_url: str) -> str:
    """Bind the exact approved client endpoint; never include credentials in this value."""
    if not 1 <= len(base_url) <= 2048:
        raise DeliveryError("invalid approved worker endpoint")
    try:
        url = urlsplit(base_url)
        _ = url.port
    except ValueError as error:
        raise DeliveryError("invalid approved worker endpoint") from error
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(ord(character) <= 32 or ord(character) >= 127 for character in base_url)
    ):
        raise DeliveryError("invalid approved worker endpoint")
    return hashlib.sha256(base_url.rstrip("/").encode("ascii")).hexdigest()


class DeliveryBinding(Contract):
    """Explicit operator binding. The caller supplies its already-approved client."""

    binding_id: Identifier
    project_id: Identifier
    configuration_revision: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    endpoint_sha256: Sha256


class DeliveryLimits(Contract):
    max_bindings: Annotated[int, Field(strict=True, ge=1, le=10_000)] = 128
    max_pinned_entries: Annotated[int, Field(strict=True, ge=0, le=1_000_000)] = 20_000
    max_pinned_metadata_bytes: Annotated[int, Field(strict=True, ge=2048, le=1024**3)] = (
        32 * 1024 * 1024
    )
    max_pending: Annotated[int, Field(strict=True, ge=1, le=32)] = 8
    lease_seconds: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] = 60.0


Phase = Literal["begin", "metadata", "content", "commit"]


@dataclass(frozen=True)
class Pin:
    target: CatalogSnapshot
    entries: tuple[PublicationEntry, ...]
    mode: Literal["snapshot", "delta"]
    delta: CatalogDelta | None = None
    phase: Phase = "begin"
    next_entry: int = 0
    next_content: int = 0
    offset: int = 0
    uploaded_bytes: int = 0

    @property
    def contents(self) -> tuple[PublicationEntry, ...]:
        # Stable digest order, with one immutable source revision per unique blob.
        unique = {
            entry.content_sha256: entry
            for entry in self.entries
            if entry.content_bytes and entry.content_sha256 is not None
        }
        return tuple(unique[digest] for digest in sorted(unique))


@dataclass(frozen=True)
class Claim:
    binding: DeliveryBinding
    generation: int
    owner: str
    acknowledged: CatalogSnapshot | None
    pin: Pin | None


_ENTRIES = TypeAdapter[tuple[PublicationEntry, ...]](
    Annotated[tuple[PublicationEntry, ...], Field(max_length=MAX_CATALOG_ENTRIES)]
)
_COUNTER = TypeAdapter[int](Counter)
_IDENTIFIER = TypeAdapter[str](Identifier)
_SHA256 = TypeAdapter[str](Sha256)
_DEADLINE = TypeAdapter[float](Annotated[float, Field(strict=True, allow_inf_nan=False)])
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bindings (
      binding TEXT PRIMARY KEY, project TEXT NOT NULL, revision INTEGER NOT NULL,
      endpoint TEXT NOT NULL, acknowledged TEXT, lease_generation INTEGER NOT NULL,
      owner TEXT, lease_until REAL NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS pins (
      binding TEXT PRIMARY KEY REFERENCES bindings(binding), target TEXT NOT NULL,
      entries TEXT NOT NULL, entry_count INTEGER NOT NULL, mode TEXT NOT NULL,
      delta TEXT, input_sha256 TEXT NOT NULL, phase TEXT NOT NULL,
      next_entry INTEGER NOT NULL, next_content INTEGER NOT NULL, offset INTEGER NOT NULL,
      uploaded_bytes INTEGER NOT NULL) STRICT""",
)


def _input_digest(target: CatalogSnapshot, delta: CatalogDelta | None) -> str:
    return hashlib.sha256((delta or target).model_dump_json().encode()).hexdigest()


def _check_pin(pin: Pin) -> None:
    target = pin.target
    if (
        len(pin.entries) != target.entry_count
        or sum(entry.content_bytes for entry in pin.entries) != target.total_content_bytes
        or manifest_sha256(target.project_id, target.catalog_seq, pin.entries)
        != target.manifest_sha256
        or pin.mode not in {"snapshot", "delta"}
        or (pin.mode == "delta") != (pin.delta is not None)
        or (pin.delta is not None and pin.delta.target != target)
        or pin.phase not in {"begin", "metadata", "content", "commit"}
    ):
        raise DeliveryError("unreadable or incomplete delivery pin")
    sizes: dict[str | None, int] = {}
    for entry in pin.entries:
        if entry.content_sha256 in sizes and sizes[entry.content_sha256] != entry.content_bytes:
            raise DeliveryError("one content digest has inconsistent sizes")
        sizes[entry.content_sha256] = entry.content_bytes
    for number in (pin.next_entry, pin.next_content, pin.offset, pin.uploaded_bytes):
        _COUNTER.validate_python(number)
    contents = pin.contents
    if (
        pin.next_entry > len(pin.entries)
        or pin.next_content > len(contents)
        or (pin.next_content == len(contents) and pin.offset)
        or (
            pin.next_content < len(contents)
            and pin.offset >= contents[pin.next_content].content_bytes
        )
        or pin.offset % 262_144
        or (pin.phase == "begin" and pin.next_entry)
        or (
            pin.phase in {"begin", "metadata"}
            and (pin.next_content or pin.offset or pin.uploaded_bytes)
        )
        or pin.uploaded_bytes
        != sum(entry.content_bytes for entry in contents[: pin.next_content]) + pin.offset
        or (pin.phase == "metadata" and pin.mode != "snapshot")
        or (pin.phase in {"content", "commit"} and pin.next_entry != len(pin.entries))
        or (pin.phase == "commit" and pin.next_content != len(contents))
    ):
        raise DeliveryError("unreadable delivery progress")


class Journal:
    """Only called on its owner's dedicated SQL thread, including close."""

    def __init__(self, path: Path, limits: DeliveryLimits, clock: Callable[[], float]) -> None:
        self.path = path
        self.limits = limits
        self.clock = clock
        self._db: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                objects = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' LIMIT 3"
                    )
                }
                if (
                    version not in (0, 1)
                    or (version == 0 and objects)
                    or (version == 1 and objects != {"bindings", "pins"})
                ):
                    raise DeliveryError("unsupported delivery journal version")
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
                db.commit()
            except BaseException:
                db.close()
                raise
            self._db = db
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _now(self) -> float:
        value = self.clock()
        if isinstance(value, bool) or not math.isfinite(value):
            raise DeliveryError("invalid delivery clock")
        return value

    def _quotas(self, db: sqlite3.Connection) -> None:
        count = db.execute("SELECT count(*) FROM bindings").fetchone()[0]
        entries, metadata, largest, smallest = db.execute(
            "SELECT coalesce(sum(entry_count),0),"
            "coalesce(sum(length(cast(target AS BLOB))+length(cast(entries AS BLOB))"
            "+coalesce(length(cast(delta AS BLOB)),0)),0),coalesce(max(entry_count),0),"
            "coalesce(min(entry_count),0) FROM pins"
        ).fetchone()
        if (
            count > self.limits.max_bindings
            or entries > self.limits.max_pinned_entries
            or largest > MAX_CATALOG_ENTRIES
            or smallest < 0
            or metadata > self.limits.max_pinned_metadata_bytes
        ):
            raise DeliveryError("delivery journal exceeds configured quota")

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        db = self._connect()
        db.execute("BEGIN IMMEDIATE")
        try:
            self._quotas(db)
            yield db
            self._quotas(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _binding_values(
        project: object,
        revision: object,
        endpoint: object,
        generation: object,
        owner: object,
        deadline: object,
    ) -> None:
        _IDENTIFIER.validate_python(project)
        if not _COUNTER.validate_python(revision) or not _COUNTER.validate_python(generation):
            raise DeliveryError("unreadable delivery binding counters")
        _SHA256.validate_python(endpoint)
        if owner is not None:
            _IDENTIFIER.validate_python(owner)
        _DEADLINE.validate_python(deadline)

    @staticmethod
    def _snapshot(raw: str | None, project: str) -> CatalogSnapshot | None:
        if raw is None:
            return None
        if len(raw.encode()) > 2048:
            raise DeliveryError("unreadable delivery target")
        result = CatalogSnapshot.model_validate_json(raw)
        if result.project_id != project:
            raise DeliveryError("delivery target belongs to another project")
        return result

    def _pin(self, db: sqlite3.Connection, binding: DeliveryBinding) -> Pin | None:
        row = db.execute(
            "SELECT substr(target,1,2049),substr(cast(entries AS BLOB),1,?),entry_count,"
            "substr(mode,1,17),substr(cast(delta AS BLOB),1,65537),substr(input_sha256,1,65),"
            "substr(phase,1,17),next_entry,next_content,offset,uploaded_bytes FROM pins"
            " WHERE binding=?",
            (self.limits.max_pinned_metadata_bytes + 1, binding.binding_id),
        ).fetchone()
        if row is None:
            return None
        target = self._snapshot(row[0], binding.project_id)
        if target is None or len(row[1]) > self.limits.max_pinned_metadata_bytes:
            raise DeliveryError("unreadable delivery pin metadata")
        if row[4] is not None and len(row[4]) > MAX_CATALOG_PAGE_BYTES:
            raise DeliveryError("unreadable delivery delta")
        entries = _ENTRIES.validate_json(row[1])
        delta = CatalogDelta.model_validate_json(row[4]) if row[4] is not None else None
        pin = Pin(target, entries, row[3], delta, row[6], *row[7:])
        _check_pin(pin)
        if row[2] != len(entries) or row[5] != _input_digest(target, delta):
            raise DeliveryError("delivery pin payload binding mismatches")
        return pin

    def claim(self, binding: DeliveryBinding) -> Claim | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT substr(project,1,129),revision,substr(endpoint,1,65),"
                "substr(acknowledged,1,2049),lease_generation,substr(owner,1,129),lease_until"
                " FROM bindings WHERE binding=?",
                (binding.binding_id,),
            ).fetchone()
            now = self._now()
            generation = 1
            acknowledged = None
            if row is not None:
                self._binding_values(row[0], row[1], row[2], row[4], row[5], row[6])
                if row[0] != binding.project_id or binding.configuration_revision < row[1]:
                    raise DeliveryError("binding project or configuration revision mismatches")
                if row[1] == binding.configuration_revision:
                    if row[2] != binding.endpoint_sha256:
                        raise DeliveryError(
                            "binding endpoint changed without a configuration revision"
                        )
                    if row[5] is not None and row[6] > now:
                        return None
                generation = _COUNTER.validate_python(row[4] + 1)
                acknowledged = self._snapshot(row[3], binding.project_id)
            owner = new_id("delivery")
            db.execute(
                "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(binding) DO UPDATE SET"
                " revision=excluded.revision,endpoint=excluded.endpoint,"
                "lease_generation=excluded.lease_generation,owner=excluded.owner,"
                "lease_until=excluded.lease_until",
                (
                    binding.binding_id,
                    binding.project_id,
                    binding.configuration_revision,
                    binding.endpoint_sha256,
                    acknowledged.model_dump_json() if acknowledged is not None else None,
                    generation,
                    owner,
                    now + self.limits.lease_seconds,
                ),
            )
            return Claim(binding, generation, owner, acknowledged, self._pin(db, binding))

    def _guard(self, db: sqlite3.Connection, claim: Claim) -> None:
        row = db.execute(
            "SELECT substr(project,1,129),revision,substr(endpoint,1,65),lease_generation,"
            "substr(owner,1,129),lease_until FROM bindings"
            " WHERE binding=?",
            (claim.binding.binding_id,),
        ).fetchone()
        expected = (
            claim.binding.project_id,
            claim.binding.configuration_revision,
            claim.binding.endpoint_sha256,
            claim.generation,
            claim.owner,
        )
        if row is not None:
            self._binding_values(*row)
        if row is None or row[:5] != expected or row[5] <= self._now():
            raise DeliveryError("delivery lease was displaced or expired")

    def active(self, claim: Claim) -> None:
        with self._transaction() as db:
            self._guard(db, claim)

    def pin(self, claim: Claim, plan: PublicationPlan, *, delta: bool) -> Pin:
        pin = Pin(
            plan.snapshot,
            plan.entries,
            "delta" if delta else "snapshot",
            plan.delta if delta else None,
        )
        _check_pin(pin)
        if pin.target.project_id != claim.binding.project_id:
            raise DeliveryError("publication plan belongs to another project")
        if claim.acknowledged is not None and (
            claim.acknowledged.catalog_seq > pin.target.catalog_seq
            or (
                claim.acknowledged.catalog_seq == pin.target.catalog_seq
                and claim.acknowledged != pin.target
            )
        ):
            raise DeliveryError("source cursor moved behind or forked from an acknowledged target")
        with self._transaction() as db:
            self._guard(db, claim)
            if self._pin(db, claim.binding) is not None:
                raise DeliveryError("an unresolved delivery pin already exists")
            db.execute(
                "INSERT INTO pins VALUES (?,?,?,?,?,?,?,?,0,0,0,0)",
                (
                    claim.binding.binding_id,
                    pin.target.model_dump_json(),
                    json.dumps(
                        [entry.model_dump(mode="json") for entry in pin.entries],
                        separators=(",", ":"),
                    ),
                    len(pin.entries),
                    pin.mode,
                    pin.delta.model_dump_json() if pin.delta is not None else None,
                    _input_digest(pin.target, pin.delta),
                    pin.phase,
                ),
            )
        return pin

    def progress(self, claim: Claim, pin: Pin) -> None:
        _check_pin(pin)
        with self._transaction() as db:
            self._guard(db, claim)
            saved = self._pin(db, claim.binding)
            if saved is None or (saved.target, saved.mode, saved.delta) != (
                pin.target,
                pin.mode,
                pin.delta,
            ):
                raise DeliveryError("delivery progress targets a different pin")
            db.execute(
                "UPDATE pins SET phase=?,next_entry=?,next_content=?,offset=?,uploaded_bytes=?"
                " WHERE binding=?",
                (
                    pin.phase,
                    pin.next_entry,
                    pin.next_content,
                    pin.offset,
                    pin.uploaded_bytes,
                    claim.binding.binding_id,
                ),
            )

    def acknowledge(self, claim: Claim, pin: Pin) -> None:
        with self._transaction() as db:
            self._guard(db, claim)
            saved = self._pin(db, claim.binding)
            if saved is None or saved.target != pin.target:
                raise DeliveryError("acknowledgement targets a different pin")
            db.execute(
                "UPDATE bindings SET acknowledged=? WHERE binding=?",
                (pin.target.model_dump_json(), claim.binding.binding_id),
            )
            db.execute("DELETE FROM pins WHERE binding=?", (claim.binding.binding_id,))

    def release(self, claim: Claim) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE bindings SET owner=NULL,lease_until=0 WHERE binding=? AND revision=?"
                " AND endpoint=? AND lease_generation=? AND owner=?",
                (
                    claim.binding.binding_id,
                    claim.binding.configuration_revision,
                    claim.binding.endpoint_sha256,
                    claim.generation,
                    claim.owner,
                ),
            )


def reset_progress(pin: Pin) -> Pin:
    return replace(pin, phase="begin", next_entry=0, next_content=0, offset=0, uploaded_bytes=0)


JOURNAL_ERRORS = (
    DeliveryError,
    sqlite3.DatabaseError,
    ValidationError,
    ValueError,
    UnicodeError,
    RecursionError,
)
