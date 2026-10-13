# SPDX-License-Identifier: MIT
"""Internal, bounded kernel-observation inbox; transport and run delivery are unwired.

Callers must authenticate MAIN context and prove the original birth/VM pin independently.
All methods are synchronous, own one FULL/WAL transaction and belong in owned thread work.
ACK is a caller's assertion that its exact delivered batch was durably saved first.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from sandbox_science.contracts import Contract, Identifier, new_id

MAX_NOTICE_BYTES = 4096
MAX_PIN_BYTES = 3072
MAX_SCOPE_BYTES = 1536
MAX_CLAIM_BYTES = 4096
MAX_COUNTER = 2**62
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Positive = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
HoldReason = Literal[
    "unknown_lineage", "unknown_revision", "binding_changed", "identity_changed", "capacity"
]


class KernelNoticeError(ValueError):
    """A fixed refusal; no peer body or retained corrupt value is interpolated."""


class NoticeScope(Contract):
    """Trusted control context; lifetime and worker binding never come from the guest."""

    project_id: Identifier
    environment_id: Identifier
    session_id: Identifier
    session_lifetime: Identifier
    worker_binding: Identifier
    sandbox: Identifier
    sandbox_id: Identifier
    incarnation: Annotated[str, Field(pattern=r"^inc-[0-9a-f]{24}$")]


class MainNoticeRun(Contract):
    scope: NoticeScope
    run_id: Identifier
    role: Literal["main"]


class KernelBirthPin(Contract):
    """Exact proved original birth; raw generation differs from worker numbering."""

    project_id: Identifier
    environment_id: Identifier
    session_id: Identifier
    language: Literal["python", "sage"]
    sandbox: Identifier
    sandbox_id: Identifier
    incarnation: Annotated[str, Field(pattern=r"^inc-[0-9a-f]{24}$")]
    generation: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    kernel_id: Identifier
    birth_id: Annotated[str, Field(pattern=r"^birth-[0-9a-f]{24}$")]
    start_revision: Counter


class TrustedKernelNotice(Contract):
    """Input only after exact retained/live birth and original binding proof succeeds."""

    scope: NoticeScope
    birth: KernelBirthPin
    generation: Positive
    current_revision: Counter
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for key in (
            "project_id",
            "environment_id",
            "session_id",
            "sandbox",
            "sandbox_id",
            "incarnation",
        ):
            if getattr(self.scope, key) != getattr(self.birth, key):
                raise ValueError("birth does not bind the trusted notice scope")
        if self.birth.start_revision > self.current_revision:
            raise ValueError("kernel start is ahead of observed packages")
        if self.generation < self.birth.generation:
            raise ValueError("worker generation predates raw birth generation")
        return self


class NoticePin(Contract):
    sequence: Positive
    digest: Digest


class KernelNotice(Contract):
    sequence: Positive
    digest: Digest
    observation: TrustedKernelNotice


class KernelClaim(Contract):
    scope_digest: Digest
    run_id: Identifier
    token: Identifier
    expires_at: float = Field(strict=True)
    pins: tuple[NoticePin, ...] = Field(min_length=1, max_length=16)
    notices: tuple[KernelNotice, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not math.isfinite(self.expires_at):
            raise ValueError("lease deadline must be finite")
        if (
            tuple(NoticePin(sequence=n.sequence, digest=n.digest) for n in self.notices)
            != self.pins
        ):
            raise ValueError("claim must contain the exact notice pins")
        if sorted({p.sequence for p in self.pins}) != [p.sequence for p in self.pins]:
            raise ValueError("claim prefix must be strictly ordered")
        for notice in self.notices:
            if (
                hashlib.sha256(_json(notice.observation.model_dump(mode="json"))).hexdigest()
                != notice.digest
                or _key(notice.observation.scope) != self.scope_digest
            ):
                raise ValueError("claim payload does not match its exact pin")
        return self


class NoticeAdmission(Contract):
    state: Literal["recorded", "duplicate", "not_stale", "held"]
    sequence: Positive | None = None
    reason: HoldReason | None = None


class NoticeStatus(Contract):
    pending: Counter
    acknowledged_through: Counter
    held: HoldReason | None
    global_capacity_held: bool


class KernelNoticeLimits(Contract):
    scopes: Annotated[int, Field(strict=True, ge=1, le=1024)] = 256
    notices: Annotated[int, Field(strict=True, ge=1, le=16384)] = 4096
    per_scope: Annotated[int, Field(strict=True, ge=1, le=1024)] = 64
    bytes: Annotated[int, Field(strict=True, ge=4096, le=64 * 1024 * 1024)] = 8 * 1024 * 1024
    batch_bytes: Annotated[int, Field(strict=True, ge=4096, le=16384)] = 8192
    lease_seconds: Annotated[int, Field(strict=True, ge=1, le=120)] = 30


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS kernel_notice_meta (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),capacity_held INTEGER NOT NULL
      CHECK(capacity_held IN(0,1))) STRICT""",
    "INSERT OR IGNORE INTO kernel_notice_meta VALUES(1,0)",
    """CREATE TABLE IF NOT EXISTS kernel_notice_scopes (
      scope_key TEXT PRIMARY KEY CHECK(length(scope_key)=64),
      payload BLOB NOT NULL CHECK(length(payload)<=1536),
      ack_cursor INTEGER NOT NULL DEFAULT 0 CHECK(ack_cursor>=0),
      next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence>=1),
      held TEXT CHECK(held IN('unknown_lineage','unknown_revision',
        'binding_changed','identity_changed','capacity')),
      lease BLOB CHECK(length(lease)<=4096),until REAL,
      last_ack BLOB CHECK(length(last_ack)<=4096),
      CHECK((lease IS NULL)=(until IS NULL))) STRICT""",
    """CREATE TABLE IF NOT EXISTS kernel_notice_watermarks (
      scope_key TEXT NOT NULL REFERENCES kernel_notice_scopes(scope_key),
      language TEXT NOT NULL CHECK(language IN('python','sage')),
      pin BLOB NOT NULL CHECK(length(pin)<=3072),
      generation INTEGER NOT NULL CHECK(generation>=1),
      revision INTEGER NOT NULL CHECK(revision>=0),
      PRIMARY KEY(scope_key,language)) STRICT""",
    """CREATE TABLE IF NOT EXISTS kernel_notice_items (
      sequence INTEGER NOT NULL CHECK(sequence>=1),
      scope_key TEXT NOT NULL REFERENCES kernel_notice_scopes(scope_key),
      digest TEXT NOT NULL CHECK(length(digest)=64),
      payload BLOB NOT NULL CHECK(length(payload)<=4096),
      PRIMARY KEY(scope_key,sequence)) STRICT""",
    "CREATE INDEX IF NOT EXISTS kernel_notice_pending ON kernel_notice_items(scope_key,sequence)",
    """CREATE TRIGGER IF NOT EXISTS kernel_notice_items_no_update
      BEFORE UPDATE ON kernel_notice_items BEGIN SELECT RAISE(ABORT,'notice is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS kernel_notice_scope_pin_no_update
      BEFORE UPDATE OF payload,scope_key ON kernel_notice_scopes
      BEGIN SELECT RAISE(ABORT,'notice scope is immutable'); END""",
)
_ID = TypeAdapter(Identifier)


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def _bytes(value: object, maximum: int) -> bytes:
    if not isinstance(value, bytes) or not 0 < len(value) <= maximum:
        raise KernelNoticeError("invalid retained kernel notice state")
    return value


def _parse[T: Contract](model: type[T], value: object, maximum: int) -> T:
    try:
        return model.model_validate_json(_bytes(value, maximum))
    except (ValidationError, ValueError, RecursionError):
        raise KernelNoticeError("invalid retained kernel notice state") from None


def _validate[T: Contract](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump(warnings=False))
    except (ValidationError, ValueError, AttributeError):
        raise KernelNoticeError("invalid trusted kernel notice input") from None


def _counter(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise KernelNoticeError("invalid retained kernel notice counter")
    return value


def _key(scope: NoticeScope) -> str:
    return hashlib.sha256(
        _json(
            {
                k: getattr(scope, k)
                for k in ("project_id", "environment_id", "session_id", "session_lifetime")
            }
        )
    ).hexdigest()


def _pin(value: TrustedKernelNotice) -> bytes:
    return _json({"birth": value.birth.model_dump(mode="json"), "generation": value.generation})


def _lease(claim: KernelClaim) -> bytes:
    return _json(
        {
            "scope_digest": claim.scope_digest,
            "run_id": claim.run_id,
            "token": claim.token,
            "pins": [p.model_dump() for p in claim.pins],
        }
    )


class KernelNotices:
    """Internal MAIN inbox. No worker transport, scheduler, run save or ACK wiring."""

    def __init__(
        self,
        path: Path,
        *,
        limits: KernelNoticeLimits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path, self.clock = path, clock
        self.limits = _validate(KernelNoticeLimits, limits or KernelNoticeLimits())
        with self._db() as db:
            for statement in _SCHEMA:
                db.execute(statement)
            self._budget(db)

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def _now(self) -> float:
        value = self.clock()
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 2**53 - 120
        ):
            raise KernelNoticeError("invalid kernel notice clock")
        return float(value)

    def _budget(self, db: sqlite3.Connection) -> int:
        # Fixed logical scalar/hold reservations also count; SQLite file overhead is separate.
        total = 8
        for table, maximum, columns in (
            ("kernel_notice_scopes", self.limits.scopes, "scope_key,payload,lease,last_ack"),
            ("kernel_notice_watermarks", 2 * self.limits.scopes, "scope_key,language,pin"),
            ("kernel_notice_items", self.limits.notices, "scope_key,digest,payload"),
        ):
            count = db.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT ?)",  # noqa: S608
                (maximum + 1,),
            ).fetchone()[0]
            if count > maximum:
                raise KernelNoticeError("retained kernel notice row quota exceeded")
            expression = "+".join(
                f"coalesce(length(CAST({c} AS BLOB)),0)" for c in columns.split(",")
            )
            total += db.execute(
                f"SELECT coalesce(sum({expression}),0) FROM {table}"  # noqa: S608
            ).fetchone()[0]
            total += count * (64 if table == "kernel_notice_scopes" else 16)
        if db.execute(
            "SELECT EXISTS(SELECT 1 FROM kernel_notice_scopes WHERE held IS NOT NULL AND"
            " (typeof(held)!='text' OR length(CAST(held AS BLOB))>32 OR held NOT IN"
            " ('unknown_lineage','unknown_revision','binding_changed',"
            "'identity_changed','capacity')))"
        ).fetchone()[0]:
            raise KernelNoticeError("invalid retained kernel notice hold")
        if db.execute(
            "SELECT EXISTS(SELECT 1 FROM kernel_notice_items GROUP BY scope_key HAVING count(*)>?)",
            (self.limits.per_scope,),
        ).fetchone()[0]:
            raise KernelNoticeError("retained kernel notice per-scope quota exceeded")
        if total > self.limits.bytes:
            raise KernelNoticeError("retained kernel notice byte quota exceeded")
        row = db.execute(
            "SELECT capacity_held FROM kernel_notice_meta WHERE singleton=1"
        ).fetchone()
        if (
            db.execute("SELECT count(*) FROM kernel_notice_meta").fetchone()[0] != 1
            or row is None
            or type(row[0]) is not int
            or row[0] not in (0, 1)
        ):
            raise KernelNoticeError("invalid retained kernel notice capacity state")
        return total

    def _scope(self, db: sqlite3.Connection, scope: NoticeScope) -> tuple[object, ...] | None:
        row = db.execute(
            "SELECT substr(payload,1,1537),ack_cursor,"
            "CASE WHEN held IS NULL THEN NULL WHEN length(CAST(held AS BLOB))>0"
            " THEN substr(CAST(held AS BLOB),1,33) ELSE x'00' END,"
            "substr(lease,1,4097),until,substr(last_ack,1,4097),next_sequence"
            " FROM kernel_notice_scopes WHERE scope_key=?",
            (_key(scope),),
        ).fetchone()
        if row is None:
            return None
        saved = _parse(NoticeScope, row[0], MAX_SCOPE_BYTES)
        cursor, next_sequence = _counter(row[1]), _counter(row[6])
        if not cursor < next_sequence:
            raise KernelNoticeError("invalid retained kernel notice sequence")
        held = row[2]
        if held is not None and held not in (
            b"unknown_lineage",
            b"unknown_revision",
            b"binding_changed",
            b"identity_changed",
            b"capacity",
        ):
            raise KernelNoticeError("invalid retained kernel notice hold")
        if (row[3] is None) != (row[4] is None) or (
            row[4] is not None
            and (
                type(row[4]) not in (int, float)
                or not math.isfinite(row[4])
                or not 0 <= row[4] <= 2**53
            )
        ):
            raise KernelNoticeError("invalid retained kernel notice lease")
        for receipt in (row[3], row[5]):
            if receipt is not None:
                self._receipt(receipt, _key(scope))
        if saved != scope:
            raise KernelNoticeError("kernel notice original binding changed")
        return cast(tuple[object, ...], row)

    def _receipt(self, raw: object, key: str) -> dict[str, object]:
        try:
            value: object = json.loads(_bytes(raw, MAX_CLAIM_BYTES))
            if not isinstance(value, dict):
                raise ValueError
            value = cast(dict[str, object], value)
            if (
                set(value) != {"scope_digest", "run_id", "token", "pins"}
                or value["scope_digest"] != key
            ):
                raise ValueError
            if (
                not isinstance(value["pins"], list)
                or not 1 <= len(cast(list[object], value["pins"])) <= 16
            ):
                raise ValueError
            pins = tuple(NoticePin.model_validate(p) for p in cast(list[object], value["pins"]))
            _ID.validate_python(value["run_id"])
            if (
                not isinstance(value["token"], str)
                or re.fullmatch(r"knl-[0-9a-f]{24}", value["token"]) is None
            ):
                raise ValueError
            if sorted({p.sequence for p in pins}) != [p.sequence for p in pins]:
                raise ValueError
            return value
        except (ValueError, ValidationError, RecursionError, UnicodeDecodeError):
            raise KernelNoticeError("invalid retained kernel notice receipt") from None

    def _enroll(self, db: sqlite3.Connection, scope: NoticeScope) -> bool:
        if self._scope(db, scope) is not None:
            return True
        raw = _json(scope.model_dump(mode="json"))
        if len(raw) > MAX_SCOPE_BYTES:
            raise KernelNoticeError("trusted kernel notice scope is too large")
        if (
            db.execute("SELECT count(*) FROM kernel_notice_scopes").fetchone()[0]
            >= self.limits.scopes
            or self._budget(db) + len(raw) + 64 + 64 > self.limits.bytes
        ):
            db.execute("UPDATE kernel_notice_meta SET capacity_held=1 WHERE singleton=1")
            return False
        db.execute(
            "INSERT INTO kernel_notice_scopes(scope_key,payload) VALUES(?,?)", (_key(scope), raw)
        )
        return True

    def hold(
        self, actor: MainNoticeRun, reason: Literal["unknown_lineage", "unknown_revision"]
    ) -> NoticeAdmission:
        """Retain an explicit unknown; never erase birth history or infer disappearance."""
        actor = _validate(MainNoticeRun, actor)
        if reason not in ("unknown_lineage", "unknown_revision"):
            raise KernelNoticeError("invalid kernel notice hold reason")
        with self._db() as db:
            self._budget(db)
            if not self._enroll(db, actor.scope):
                return NoticeAdmission(state="held", reason="capacity")
            db.execute(
                "UPDATE kernel_notice_scopes SET held=? WHERE scope_key=?",
                (reason, _key(actor.scope)),
            )
            return NoticeAdmission(state="held", reason=reason)

    def record(self, actor: MainNoticeRun, observation: TrustedKernelNotice) -> NoticeAdmission:
        """Append a new stale assessment; old/duplicate input cannot revive delivery."""
        actor, observation = (
            _validate(MainNoticeRun, actor),
            _validate(TrustedKernelNotice, observation),
        )
        if actor.scope != observation.scope:
            raise KernelNoticeError("kernel notice input is outside this MAIN scope")
        raw, pin = _json(observation.model_dump(mode="json")), _pin(observation)
        if len(raw) > MAX_NOTICE_BYTES or len(pin) > MAX_PIN_BYTES:
            raise KernelNoticeError("trusted kernel notice input is too large")
        scope, key, language = actor.scope, _key(actor.scope), observation.birth.language
        with self._db() as db:
            self._budget(db)
            if not self._enroll(db, scope):
                return NoticeAdmission(state="held", reason="capacity")
            old = db.execute(
                "SELECT substr(pin,1,3073),generation,revision FROM kernel_notice_watermarks"
                " WHERE scope_key=? AND language=?",
                (key, language),
            ).fetchone()
            previous_size = 0
            if old is not None:
                old_pin, generation, revision = (
                    _bytes(old[0], MAX_PIN_BYTES),
                    _counter(old[1]),
                    _counter(old[2]),
                )
                previous_size = len(old_pin) + 64 + len(language) + 16
                # Validate the old pin before comparing scalar order.
                self._watermark(old_pin, scope, language, generation, revision)
                if observation.generation < generation or observation.current_revision < revision:
                    return NoticeAdmission(state="duplicate")
                if observation.generation == generation and pin != old_pin:
                    db.execute(
                        "UPDATE kernel_notice_scopes SET held='identity_changed' WHERE scope_key=?",
                        (key,),
                    )
                    return NoticeAdmission(state="held", reason="identity_changed")
                if (
                    observation.generation == generation
                    and observation.current_revision == revision
                ):
                    db.execute(
                        "UPDATE kernel_notice_scopes SET held=NULL WHERE scope_key=? AND"
                        " held IN('unknown_lineage','unknown_revision')",
                        (key,),
                    )
                    return NoticeAdmission(state="duplicate")
            stale = observation.birth.start_revision < observation.current_revision
            needed = len(pin) + 64 + len(language) + 16 - previous_size
            if stale:
                needed += len(raw) + 128 + 16
            count = db.execute("SELECT count(*) FROM kernel_notice_items").fetchone()[0]
            local = db.execute(
                "SELECT count(*) FROM kernel_notice_items WHERE scope_key=?", (key,)
            ).fetchone()[0]
            total = self._budget(db)
            if (
                stale and (count >= self.limits.notices or local >= self.limits.per_scope)
            ) or total + needed > self.limits.bytes:
                db.execute(
                    "UPDATE kernel_notice_scopes SET held='capacity' WHERE scope_key=?", (key,)
                )
                return NoticeAdmission(state="held", reason="capacity")
            db.execute(
                "INSERT INTO kernel_notice_watermarks VALUES(?,?,?,?,?)"
                " ON CONFLICT(scope_key,language) DO UPDATE SET pin=excluded.pin,"
                "generation=excluded.generation,revision=excluded.revision",
                (key, language, pin, observation.generation, observation.current_revision),
            )
            db.execute("UPDATE kernel_notice_scopes SET held=NULL WHERE scope_key=?", (key,))
            if not stale:
                return NoticeAdmission(state="not_stale")
            digest = hashlib.sha256(raw).hexdigest()
            row = self._scope(db, scope)
            if row is None:
                raise KernelNoticeError("kernel notice scope disappeared")
            sequence = _counter(row[6])
            if sequence >= MAX_COUNTER:
                raise KernelNoticeError("kernel notice sequence exhausted")
            db.execute(
                "UPDATE kernel_notice_scopes SET next_sequence=? WHERE scope_key=?",
                (sequence + 1, key),
            )
            db.execute(
                "INSERT INTO kernel_notice_items(scope_key,sequence,digest,payload)"
                " VALUES(?,?,?,?)",
                (key, sequence, digest, raw),
            )
            return NoticeAdmission(state="recorded", sequence=sequence)

    def _watermark(
        self, raw: bytes, scope: NoticeScope, language: str, generation: int, revision: int
    ) -> None:
        try:
            value: object = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            value = cast(dict[str, object], value)
            if set(value) != {"birth", "generation"}:
                raise ValueError
            proof = TrustedKernelNotice.model_validate(
                {
                    "scope": scope,
                    "birth": value["birth"],
                    "generation": value["generation"],
                    "current_revision": revision,
                    "observed_at": "2000-01-01T00:00:00Z",
                }
            )
            if proof.birth.language != language or proof.generation != generation:
                raise ValueError
        except (ValueError, ValidationError, RecursionError, UnicodeDecodeError):
            raise KernelNoticeError("invalid retained kernel notice watermark") from None

    def _items(self, db: sqlite3.Connection, scope: NoticeScope) -> tuple[KernelNotice, ...]:
        saved = self._scope(db, scope)
        if saved is None:
            return ()
        cursor, next_sequence = _counter(saved[1]), _counter(saved[6])
        rows = db.execute(
            "SELECT sequence,substr(CAST(digest AS BLOB),1,65),substr(payload,1,4097)"
            " FROM kernel_notice_items WHERE scope_key=? ORDER BY sequence LIMIT 17",
            (_key(scope),),
        ).fetchall()
        items: list[KernelNotice] = []
        size = 0
        for sequence, digest, raw in rows[:16]:
            sequence = _counter(sequence)
            if not cursor < sequence < next_sequence:
                raise KernelNoticeError("invalid retained kernel notice sequence")
            observation = _parse(TrustedKernelNotice, raw, MAX_NOTICE_BYTES)
            if (
                observation.scope != scope
                or not isinstance(digest, bytes)
                or digest != hashlib.sha256(raw).hexdigest().encode()
            ):
                raise KernelNoticeError("invalid retained kernel notice pin")
            item = KernelNotice(sequence=sequence, digest=digest.decode(), observation=observation)
            size += len(_json(item.model_dump(mode="json")))
            if size > self.limits.batch_bytes:
                break
            items.append(item)
        return tuple(items)

    def claim(self, actor: MainNoticeRun) -> KernelClaim | None:
        """Lease one exact bounded prefix; an expired owner never retains authority."""
        actor = _validate(MainNoticeRun, actor)
        now = self._now()
        with self._db() as db:
            total = self._budget(db)
            row = self._scope(db, actor.scope)
            if row is None or row[2] not in (None, b"capacity"):
                return None
            if row[3] is not None and cast(float, row[4]) > now:
                return None
            items = self._items(db, actor.scope)
            if not items:
                return None
            token = new_id("knl")
            while items:
                claim = KernelClaim(
                    scope_digest=_key(actor.scope),
                    run_id=actor.run_id,
                    token=token,
                    expires_at=now + self.limits.lease_seconds,
                    pins=tuple(NoticePin(sequence=n.sequence, digest=n.digest) for n in items),
                    notices=items,
                )
                if len(_json(claim.model_dump(mode="json"))) <= self.limits.batch_bytes:
                    break
                items = items[:-1]
            else:
                return None
            receipt = _lease(claim)
            if (
                len(receipt) > MAX_CLAIM_BYTES
                or total + len(receipt) - (len(cast(bytes, row[3])) if row[3] else 0)
                > self.limits.bytes
            ):
                return None
            db.execute(
                "UPDATE kernel_notice_scopes SET lease=?,until=? WHERE scope_key=?",
                (receipt, claim.expires_at, claim.scope_digest),
            )
            return claim

    def _claimed(
        self,
        db: sqlite3.Connection,
        actor: MainNoticeRun,
        claim: KernelClaim,
        *,
        replay: bool = False,
    ) -> bool:
        if claim.scope_digest != _key(actor.scope) or claim.run_id != actor.run_id:
            raise KernelNoticeError("kernel notice claim is outside this MAIN run")
        row = self._scope(db, actor.scope)
        receipt = _lease(claim)
        if row is not None and replay and row[5] == receipt:
            return True
        if row is None or row[3] != receipt or cast(float, row[4]) <= self._now():
            raise KernelNoticeError("kernel notice lease expired or was displaced")
        items = self._items(db, actor.scope)
        if (
            len(items) < len(claim.pins)
            or tuple(
                NoticePin(sequence=n.sequence, digest=n.digest) for n in items[: len(claim.pins)]
            )
            != claim.pins
        ):
            raise KernelNoticeError("kernel notice claimed prefix changed")
        for original, current in zip(claim.notices, items, strict=False):
            if original != current:
                raise KernelNoticeError("kernel notice claimed payload changed")
        return False

    def renew(self, actor: MainNoticeRun, claim: KernelClaim) -> KernelClaim:
        actor, claim = _validate(MainNoticeRun, actor), _validate(KernelClaim, claim)
        with self._db() as db:
            self._budget(db)
            self._claimed(db, actor, claim)
            deadline = self._now() + self.limits.lease_seconds
            renewed = claim.model_copy(update={"expires_at": deadline})
            if len(_json(renewed.model_dump(mode="json"))) > self.limits.batch_bytes:
                raise KernelNoticeError("renewed kernel notice claim exceeds batch bound")
            db.execute(
                "UPDATE kernel_notice_scopes SET until=? WHERE scope_key=?",
                (deadline, claim.scope_digest),
            )
            return renewed

    def release(self, actor: MainNoticeRun, claim: KernelClaim) -> None:
        """Return an exact live claim; never release a later run's lease."""
        actor, claim = _validate(MainNoticeRun, actor), _validate(KernelClaim, claim)
        with self._db() as db:
            self._budget(db)
            self._claimed(db, actor, claim)
            db.execute(
                "UPDATE kernel_notice_scopes SET lease=NULL,until=NULL WHERE scope_key=?",
                (claim.scope_digest,),
            )

    def acknowledge(self, actor: MainNoticeRun, claim: KernelClaim) -> bool:
        """After durable save only: compact the exact batch, retaining dedup and receipt.

        Returns True for the last exact ACK replay. Older displaced ACK receipts refuse.
        This internal prerequisite does not verify a RunRecord or call this method itself.
        """
        actor, claim = _validate(MainNoticeRun, actor), _validate(KernelClaim, claim)
        with self._db() as db:
            self._budget(db)
            if self._claimed(db, actor, claim, replay=True):
                return True
            receipt = _lease(claim)
            db.execute(
                "UPDATE kernel_notice_scopes SET ack_cursor=?,last_ack=?,lease=NULL,until=NULL"
                " WHERE scope_key=?",
                (claim.pins[-1].sequence, receipt, claim.scope_digest),
            )
            db.executemany(
                "DELETE FROM kernel_notice_items WHERE scope_key=? AND sequence=? AND digest=?",
                [(claim.scope_digest, p.sequence, p.digest) for p in claim.pins],
            )
            return False

    def status(self, actor: MainNoticeRun) -> NoticeStatus:
        actor = _validate(MainNoticeRun, actor)
        with self._db() as db:
            self._budget(db)
            row = self._scope(db, actor.scope)
            global_held = bool(
                db.execute(
                    "SELECT capacity_held FROM kernel_notice_meta WHERE singleton=1"
                ).fetchone()[0]
            )
            pending = db.execute(
                "SELECT count(*) FROM kernel_notice_items WHERE scope_key=?", (_key(actor.scope),)
            ).fetchone()[0]
            return NoticeStatus(
                pending=pending,
                acknowledged_through=cast(int, row[1]) if row else 0,
                held=cast(HoldReason, cast(bytes, row[2]).decode()) if row and row[2] else None,
                global_capacity_held=global_held,
            )
