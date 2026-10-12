# SPDX-License-Identifier: MIT
"""Bounded, leased document checks; immutable revision history is the backlog.

SQL work and parsing run in a thread, rendering outside transactions. No renderer
is discovered automatically. Exhausted logical quotas hold the cursor, not saves.
"""

# Logical modules share the canonical transaction machinery.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.consistency.store import ReviewStore, dump
from sandbox_science.orchestrator.mathcheck import Formula, MathCheck

from .blocks import parse
from .engine import attach
from .errors import CONFLICT, INTERNAL, DocumentError
from .identity import Mapped
from .models import Author
from .operations import unlay

Json = dict[str, Any]
MAP_BYTES = 16 * 1024 * 1024
BATCH = 32
LEASE = 120


async def owned[T](work: Awaitable[T]) -> T:
    """Retain actual work ownership through repeated caller cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


async def threaded[T](call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await owned(asyncio.to_thread(call, *args, **kwargs))


class Checker(Protocol):
    def identity(self) -> dict[str, object]: ...

    async def check_many(self, formulas: list[Formula]) -> list[MathCheck]: ...


@dataclass(frozen=True)
class MathLimits:
    """Logical data budgets, independent of filesystem/renderer resource controls."""

    jobs: int = 10_000
    observations: int = 100_000
    observation_bytes: int = 256 * 1024 * 1024
    ownerships: int = 100_000
    ownership_bytes: int = 64 * 1024 * 1024
    active: int = 2
    history_per_step: int = 2

    def __post_init__(self) -> None:
        for name, maximum in (
            ("jobs", 100_000),
            ("observations", 1_000_000),
            ("observation_bytes", 1024 * 1024 * 1024),
            ("ownerships", 1_000_000),
            ("ownership_bytes", 1024 * 1024 * 1024),
            ("active", 2),
            ("history_per_step", 2),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"invalid bounded math limit: {name}")


class MathJobs:
    def __init__(self, store: ReviewStore, *, limits: MathLimits | None = None) -> None:
        self.store, self.documents, self.limits = store, store.documents, limits or MathLimits()

    def _fence(self, db: sqlite3.Connection, check: str, owner: str) -> tuple[Any, ...]:
        from .comment_math import CommentMath, metadata

        comment = metadata(db, check)
        if comment is not None:
            return CommentMath(self).fence(db, check, owner, comment)
        row = db.execute(
            (
                "SELECT substr(CAST(j.document AS "
                "BLOB),1,129),j.revision,j.cursor,j.attribution_cursor FROM math_jobs "
                "j JOIN revisions r ON r.document_id=j.document AND "
                "r.number=j.revision JOIN documents d ON d.document_id=j.document JOIN "
                "math_sources s ON s.document=j.document WHERE j.check_id=? AND "
                "j.state='running' AND j.owner=? AND j.lease_until>? AND "
                "j.source_sha256=r.sha256 AND d.project_id=s.project"
            ),
            (check, owner, self.store.clock()),
        ).fetchone()
        if row is None:
            raise DocumentError(CONFLICT, "math check ownership expired or changed")
        if (
            len(row[0]) > 128
            or any(type(value) is not int or value < 0 for value in row[1:3])
            or row[2] > 20000
            or (row[3] is not None and (type(row[3]) is not int or not 0 <= row[3] <= row[1]))
        ):
            raise DocumentError(INTERNAL, "stored math lease/cursor exceeds its bounds")
        return (row[0].decode(), *row[1:])

    def retry_unavailable(
        self, project: str, configuration: dict[str, object] | None = None
    ) -> None:
        """One durable current-revision retry per explicit live activation.

        Prior attempts remain immutable. This does not spin, start an idle service
        or turn an unavailable observation into a successful check.
        """
        with self.documents._write() as db:
            db.execute(
                (
                    "UPDATE math_sources SET retry_requested=1 WHERE project=? AND "
                    "completed=target AND EXISTS(SELECT 1 FROM math_jobs j WHERE "
                    "j.document=math_sources.document AND j.revision=target AND "
                    "NOT EXISTS(SELECT 1 FROM math_comment_jobs c WHERE c.check_id=j.check_id) AND "
                    "j.rowid=(SELECT max(k.rowid) FROM math_jobs k WHERE "
                    "k.document=j.document AND k.revision=j.revision AND NOT EXISTS "
                    "(SELECT 1 FROM math_comment_jobs c WHERE c.check_id=k.check_id)) AND "
                    "j.state='finished' AND (j.unavailable>0 OR (? IS NOT NULL AND "
                    "j.configuration!=?)))"
                ),
                (
                    project,
                    dump(configuration) if configuration else None,
                    dump(configuration) if configuration else None,
                ),
            )
            from .comment_math import CommentMath

            CommentMath(self).retry(db, project, dump(configuration) if configuration else None)

    def claim(self, project: str, owner: str) -> str | None:
        now = self.store.clock()
        if not math.isfinite(now) or abs(now) > 1e18:
            raise DocumentError(INTERNAL, "invalid math clock")
        with self.documents._write() as db:
            from .comment_math import reconcile

            reconcile(db, project)
            # Mixed-version writers may append valid immutable revisions without
            # the new scheduling hook. Reconcile only authoritative head metadata;
            # history still proves each queued revision's individual block owner.
            db.execute(
                "INSERT OR IGNORE INTO math_sources(document,project,target) "
                "SELECT document_id,project_id,head FROM documents WHERE project_id=?",
                (project,),
            )
            db.execute(
                "UPDATE math_sources SET target=max(target,(SELECT head FROM documents d "
                "WHERE d.document_id=math_sources.document)) WHERE project=?",
                (project,),
            )
            if (
                db.execute(
                    "SELECT 1 FROM math_jobs WHERE state='running' AND "
                    "(lease_until IS NULL OR lease_until NOT BETWEEN -1e18 AND 1e18) LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise DocumentError(INTERNAL, "stored math lease is nonfinite or invalid")
            # Recover the same immutable work after restart; a live process is fenced.
            old = db.execute(
                (
                    "SELECT substr(CAST(j.check_id AS BLOB),1,129) FROM math_jobs j JOIN "
                    "math_sources s USING(document) WHERE s.project=? AND "
                    "j.state='running' AND (j.lease_until<=? OR j.owner=?) ORDER BY "
                    "j.created LIMIT 1"
                ),
                (project, now, owner),
            ).fetchone()
            if old:
                if len(old[0]) > 128:
                    raise DocumentError(INTERNAL, "stored math identity exceeds its bound")
                old = (old[0].decode(),)
                db.execute(
                    "UPDATE math_jobs SET owner=?,lease_until=? WHERE check_id=?",
                    (owner, now + LEASE, old[0]),
                )
                return old[0]
            if (
                db.execute("SELECT count(*) FROM math_jobs WHERE state='running'").fetchone()[0]
                >= self.limits.active
            ):
                return None
            source = db.execute(
                (
                    "SELECT substr(CAST(document AS BLOB),1,129),CASE WHEN "
                    "completed<target THEN completed+1 ELSE target END FROM math_sources "
                    "WHERE project=? AND (completed<target OR retry_requested=1) AND "
                    "document NOT IN (SELECT document FROM math_jobs WHERE "
                    "state='running') ORDER BY rowid LIMIT 1"
                ),
                (project,),
            ).fetchone()
            if source is None:
                from .comment_math import CommentMath

                return CommentMath(self).claim(db, project, owner, now)
            if (
                len(source[0]) > 128
                or type(source[1]) is not int
                or not 1 <= source[1] <= 1_000_000_000
            ):
                raise DocumentError(INTERNAL, "stored math source cursor exceeds its bound")
            source = (source[0].decode(), source[1])
            if db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] >= self.limits.jobs:
                db.execute(
                    (
                        "UPDATE math_sources SET blocked='math job history quota reached' "
                        "WHERE document=?"
                    ),
                    (source[0],),
                )
                return None
            row = db.execute(
                (
                    "SELECT substr(CAST(sha256 AS BLOB),1,65) FROM revisions WHERE "
                    "document_id=? AND number=?"
                ),
                source,
            ).fetchone()
            if row is None or len(row[0]) != 64 or not re.fullmatch(b"[0-9a-f]{64}", row[0]):
                raise DocumentError(INTERNAL, "queued immutable revision is missing or invalid")
            check = new_id("math")
            db.execute(
                (
                    "INSERT INTO "
                    "math_jobs(check_id,document,revision,source_sha256,state,owner,lease_u"
                    "ntil,attribution_cursor,created) VALUES(?,?,?,?,'running',?,?,?,?)"
                ),
                (check, *source, row[0].decode(), owner, now + LEASE, source[1], now),
            )
            db.execute(
                "UPDATE math_sources SET blocked=NULL,retry_requested=0 WHERE document=?",
                (source[0],),
            )
            return check

    def job(self, check: str) -> Json:
        # Prefix in SQL before decoding, including corrupted historical records.
        bounds = {
            "check_id": 128,
            "document": 128,
            "source_sha256": 64,
            "state": 32,
            "owner": 128,
            "configuration": 4096,
            "runtime": 32,
            "reason": 1000,
        }
        names = (
            "check_id",
            "document",
            "revision",
            "source_sha256",
            "state",
            "owner",
            "lease_until",
            "cursor",
            "spans",
            "available",
            "rejected",
            "unavailable",
            "configuration",
            "runtime",
            "reason",
            "attribution_cursor",
            "created",
            "ended",
        )
        fields = ",".join(
            f"substr(CAST({name} AS BLOB),1,{bounds[name] + 1})" if name in bounds else name
            for name in names
        )
        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - canonical writer initialized
            row = db.execute(
                "SELECT " + fields + " FROM math_jobs WHERE check_id=?",  # noqa: S608 - fixed columns
                (check,),
            ).fetchone()
            if row is None:
                raise DocumentError(INTERNAL, "math job disappeared")
            value: Json = dict(zip(names, row, strict=True))
            try:
                for name, limit in bounds.items():
                    if value[name] is not None:
                        if len(value[name]) > limit:
                            raise ValueError
                        value[name] = value[name].decode("utf-8")
                project = db.execute(
                    "SELECT substr(CAST(project AS BLOB),1,129) FROM math_sources WHERE document=?",
                    (value["document"],),
                ).fetchone()
                if project is None or len(project[0]) > 128:
                    raise ValueError
                value["project"] = project[0].decode("utf-8")
                if not re.fullmatch("[0-9a-f]{64}", value["source_sha256"]) or value[
                    "state"
                ] not in ("running", "finished"):
                    raise ValueError
                for name in ("revision", "cursor", "available", "rejected", "unavailable"):
                    if type(value[name]) is not int or not 0 <= value[name] <= 1_000_000_000:
                        raise ValueError
                if value["revision"] < 1 or value["cursor"] > 20000:
                    raise ValueError
                for name in ("spans", "attribution_cursor"):
                    if value[name] is not None and (
                        type(value[name]) is not int
                        or not 0 <= value[name] <= (20000 if name == "spans" else value["revision"])
                    ):
                        raise ValueError
                for name in ("created", "ended", "lease_until"):
                    if value[name] is not None and (
                        not isinstance(value[name], int | float) or not math.isfinite(value[name])
                    ):
                        raise ValueError
                from .comment_math import metadata

                comment = metadata(db, check)
                if comment is not None:
                    value["comment"] = comment
                return value
            except (ValueError, TypeError, UnicodeDecodeError):
                raise DocumentError(INTERNAL, "stored math job exceeds its schema bounds") from None

    @staticmethod
    def _proof_author(raw: bytes | None) -> Json | None:
        if raw is None:
            return None
        if len(raw) > 8192:
            raise DocumentError(INTERNAL, "stored math ownership exceeds its bound")
        try:
            return Author.model_validate_json(raw).model_dump(mode="json")
        except ValueError:
            raise DocumentError(INTERNAL, "stored math ownership is invalid") from None

    def pin(self, check: str, owner: str, identity: dict[str, object]) -> bool:
        value = dump(identity)
        if len(value.encode()) > 4096:
            raise DocumentError(INTERNAL, "math configuration identity exceeds its bound")
        with self.documents._write() as db:
            self._fence(db, check, owner)
            old = db.execute(
                "SELECT substr(CAST(configuration AS BLOB),1,4097) FROM math_jobs WHERE check_id=?",
                (check,),
            ).fetchone()[0]
            if old is not None:
                return old == value.encode()
            db.execute("UPDATE math_jobs SET configuration=? WHERE check_id=?", (value, check))
            return True

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json] | None:
        """Prove last change <= pinned revision, resumably; current cache is not authority.

        At most two bounded historical maps are decoded per step. A later USER
        change/removal cannot relabel earlier work. Unknown is recorded only after
        reaching the beginning, never because a page budget ran out.
        """
        from .comment_math import CommentMath, metadata

        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - canonical store initialized
            comment = metadata(db, check)
        if comment is not None:
            return CommentMath(self).attribution(check, owner, blocks)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - writer initialized the canonical store
            document, revision, _, cursor = self._fence(db, check, owner)
            proofs = {
                r[0]: {"revision": r[1], "author": self._proof_author(r[2])}
                for r in db.execute(
                    (
                        "SELECT block,revision,substr(CAST(author AS BLOB),1,8193) FROM "
                        "math_job_owners WHERE check_id=? AND block IN (SELECT value FROM "
                        "json_each(?))"
                    ),
                    (check, dump(sorted(blocks))),
                )
            }
            missing = blocks - proofs.keys()
            if not missing:
                return proofs
            cursor = revision if cursor is None else cursor
            rows = list(
                db.execute(
                    (
                        "SELECT number,substr(CAST(block_map AS BLOB),1,?),substr(CAST(author "
                        "AS BLOB),1,8193) FROM revisions WHERE document_id=? AND number<=? "
                        "ORDER BY number DESC LIMIT ?"
                    ),
                    (MAP_BYTES + 1, document, cursor, self.limits.history_per_step),
                )
            )
        added: dict[str, Json] = {}
        for number, raw, raw_author in rows:
            if len(raw) > MAP_BYTES or len(raw_author) > 8192:
                raise DocumentError(INTERNAL, "historical math provenance exceeds source bounds")
            author = Author.model_validate_json(raw_author).model_dump(mode="json")
            for block in json.loads(raw)["blocks"]:
                if (
                    block["id"] in missing
                    and block["id"] not in added
                    and block["status"] in ("added", "changed")
                ):
                    added[block["id"]] = {"revision": number, "author": author}
        next_cursor = rows[-1][0] - 1 if rows else 0
        if next_cursor == 0:
            added.update(
                {block: {"revision": None, "author": None} for block in missing - added.keys()}
            )
        with self.documents._write() as db:
            self._fence(db, check, owner)
            count, size = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM math_job_owners"
            ).fetchone()
            encoded = {
                block: dump(proof["author"]) if proof["author"] else None
                for block, proof in added.items()
            }
            added_bytes = sum(len(raw.encode()) if raw else 0 for raw in encoded.values())
            if (
                count + len(added) > self.limits.ownerships
                or size + added_bytes > self.limits.ownership_bytes
            ):
                db.execute(
                    "UPDATE math_sources SET blocked='math ownership history quota reached' "
                    "WHERE document=?",
                    (document,),
                )
                return None
            for block, proof in added.items():
                raw = encoded[block]
                db.execute(
                    "INSERT OR IGNORE INTO math_job_owners VALUES(?,?,?,?,?)",
                    (
                        check,
                        block,
                        proof["revision"],
                        raw,
                        len(raw.encode()) if raw else 0,
                    ),
                )
            db.execute(
                "UPDATE math_jobs SET attribution_cursor=?,lease_until=? WHERE check_id=?",
                (next_cursor, self.store.clock() + LEASE, check),
            )
        proofs.update(added)
        return proofs if blocks <= proofs.keys() else None

    def capacity(self, check: str, owner: str) -> bool:
        with self.documents._write() as db:
            document, *_ = self._fence(db, check, owner)
            amount, size = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM math_observations"
            ).fetchone()
            # Conservative reservation prevents calling a renderer whose bounded
            # answers cannot be retained. Serialize commits under the shared writer.
            reserved = (
                db.execute("SELECT count(*) FROM math_jobs WHERE state='running'").fetchone()[0]
                * BATCH
            )
            if (
                amount + reserved > self.limits.observations
                or size + reserved * 32768 > self.limits.observation_bytes
            ):
                db.execute(
                    (
                        "UPDATE math_sources SET blocked='math observation quota reached' "
                        "WHERE document=?"
                    ),
                    (document,),
                )
                db.execute(
                    "UPDATE comment_math_sources SET blocked='math observation quota reached' "
                    "WHERE entry_id=(SELECT entry_id FROM math_comment_jobs WHERE check_id=?)",
                    (check,),
                )
                return False
            db.execute(
                "UPDATE math_jobs SET lease_until=? WHERE check_id=?",
                (self.store.clock() + LEASE, check),
            )
            return True

    def commit(
        self,
        check: str,
        owner: str,
        items: list[Json],
        spans: int,
        *,
        complete: bool,
        live: set[tuple[str, str]],
    ) -> None:
        from sandbox_science.orchestrator.consistency.mechanical import MechanicalFindings

        job = self.job(check)
        with self.documents._write() as db:
            document, revision, cursor, _ = self._fence(db, check, owner)
            if (document, revision, cursor) != (job["document"], job["revision"], job["cursor"]):
                raise DocumentError(CONFLICT, "math pinned work changed before commit")
            if (
                db.execute(
                    "SELECT 1 FROM math_jobs WHERE check_id=? AND source_sha256=?",
                    (check, job["source_sha256"]),
                ).fetchone()
                is None
            ):
                raise DocumentError(CONFLICT, "math pinned digest changed before commit")
            mechanism = MechanicalFindings(self.store)
            runtimes = {
                item["check"]["runtime"]
                for item in items
                if item["check"]["status"] != "unavailable"
            }
            pinned_runtime = job["runtime"]
            if pinned_runtime is None and len(runtimes) == 1:
                pinned_runtime = next(iter(runtimes))
                db.execute(
                    "UPDATE math_jobs SET runtime=? WHERE check_id=?", (pinned_runtime, check)
                )
            if len(runtimes) > 1 or any(runtime != pinned_runtime for runtime in runtimes):
                for item in items:
                    item["check"].update(
                        status="unavailable",
                        error="renderer runtime changed within the pinned attempt",
                        remedy=None,
                    )
            for offset, item in enumerate(items):
                payload = dump(item)
                if len(payload.encode()) > 32768:
                    raise DocumentError(INTERNAL, "math observation exceeds its bound")
                db.execute(
                    "INSERT INTO math_observations VALUES(?,?,?,?,?,?,?,?)",
                    (
                        check,
                        cursor + offset,
                        item["block"],
                        item["span"],
                        item["check"]["input_sha256"],
                        item["check"]["status"],
                        payload,
                        len(payload.encode()),
                    ),
                )
                mechanism.observe(db, job, item)
            unavailable = sum(v["check"]["status"] == "unavailable" for v in items)
            rejected = sum(v["check"]["status"] == "rejected" for v in items)
            db.execute(
                (
                    "UPDATE math_jobs SET "
                    "cursor=cursor+?,spans=?,available=available+?,rejected=rejected+?,unav"
                    "ailable=unavailable+?,attribution_cursor=NULL,lease_until=? WHERE "
                    "check_id=?"
                ),
                (
                    len(items),
                    spans,
                    len(items) - unavailable,
                    rejected,
                    unavailable,
                    self.store.clock() + LEASE,
                    check,
                ),
            )
            if complete:
                mechanism.removed(db, job, live)
                db.execute(
                    (
                        "UPDATE math_jobs SET "
                        "state='finished',ended=?,owner=NULL,lease_until=NULL WHERE check_id=?"
                    ),
                    (self.store.clock(), check),
                )
                if "comment" in job:
                    from .comment_math import CommentMath

                    CommentMath(self).finish(db, job)
                else:
                    db.execute(
                        (
                            "UPDATE math_sources SET completed=max(completed,?),blocked=NULL WHERE "
                            "document=?"
                        ),
                        (revision, document),
                    )

    def load(self, job: Json) -> Mapped:
        """Bound bytes before parsing and bind job/project/hash to actual source."""
        if "comment" in job:
            from .comment_math import CommentMath

            return CommentMath(self).load(job)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - canonical writer initialized
            row = db.execute(
                (
                    "SELECT substr(CAST(r.text AS BLOB),1,1048577),substr(CAST(r.block_map "
                    "AS BLOB),1,16777217),substr(CAST(r.sha256 AS BLOB),1,65) FROM "
                    "revisions r JOIN documents d USING(document_id) WHERE d.project_id=? "
                    "AND r.document_id=? AND r.number=?"
                ),
                (job["project"], job["document"], job["revision"]),
            ).fetchone()
        if row is None or len(row[0]) > 1048576 or len(row[1]) > MAP_BYTES or len(row[2]) != 64:
            raise DocumentError(INTERNAL, "pinned math source is missing or exceeds its bounds")
        if (
            hashlib.sha256(row[0]).hexdigest() != job["source_sha256"]
            or row[2].decode() != job["source_sha256"]
        ):
            raise DocumentError(INTERNAL, "math job digest does not bind the immutable source")
        try:
            return attach(parse(row[0].decode()), json.loads(row[1]))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DocumentError(INTERNAL, "pinned math source identity is invalid") from None

    def effective_macros(self, job: Json, mapped: Mapped) -> dict[str, str]:
        if "comment" in job:
            from .comment_math import CommentMath

            return CommentMath(self).macros(job)
        return mapped.parsed.front_matter.macros if mapped.parsed.front_matter else {}

    async def step(self, check: str, owner: str, checker: Checker | None) -> bool:
        """One bounded provenance page or formula batch; False means durable pending."""
        job = await threaded(self.job, check)
        if job["state"] == "finished":
            return True
        mapped = await threaded(self.load, job)
        if job["cursor"] > len(mapped.parsed.spans):
            raise DocumentError(INTERNAL, "math cursor exceeds the pinned span count")
        selected = list(enumerate(mapped.parsed.spans))[job["cursor"] : job["cursor"] + BATCH]
        blocks = {mapped.block_ids[span.block] for _, span in selected}
        proofs = await threaded(self.attribution, check, owner, blocks)
        if proofs is None or not await threaded(self.capacity, check, owner):
            return False
        macros = await threaded(self.effective_macros, job, mapped)
        formulas: list[Formula] = []
        for index, span in selected:
            block = mapped.parsed.blocks[span.block]
            # Oversized immutable source is retained as unavailable, never sent
            # through the renderer's validated admission boundary.
            constructor = (
                Formula
                if len(unlay(span.tex, block.continuation)) <= 20000
                else Formula.model_construct
            )
            formulas.append(
                constructor(
                    id=mapped.span_ids[index],
                    tex=unlay(span.tex, block.continuation),
                    display=span.kind == "display",
                    macros=macros,
                )
            )
        try:
            identity = await threaded(checker.identity) if checker else {"renderer": "unavailable"}
        except (OSError, ValueError):
            identity = {"renderer": "unavailable", "reason": "invalid operator configuration"}
        identity = json.loads(dump(identity))
        pinned = await threaded(self.pin, check, owner, identity)
        if checker is None or not pinned or identity.get("renderer") == "unavailable":
            answers = [
                MathCheck(
                    id=f.id,
                    status="unavailable",
                    error="renderer unavailable or its pinned configuration changed",
                    input_sha256=hashlib.sha256(f.model_dump_json().encode()).hexdigest(),
                    bundle_sha256=None,
                )
                for f in formulas
            ]
        else:
            eligible = [f for f in formulas if len(f.tex) <= 20000]
            returned = await checker.check_many(eligible)
            if len(returned) != len(eligible) or [v.id for v in returned] != [
                f.id for f in eligible
            ]:
                raise DocumentError(INTERNAL, "renderer returned a mismatched batch")
            by_id = {v.id: v for v in returned}
            answers = [
                by_id[f.id]
                if f.id in by_id
                else MathCheck(
                    id=f.id,
                    status="unavailable",
                    error="formula exceeds renderer input limit",
                    input_sha256=hashlib.sha256(f.model_dump_json().encode()).hexdigest(),
                    bundle_sha256=None,
                )
                for f in formulas
            ]
        if checker is not None:
            try:
                unchanged = dump(await threaded(checker.identity)) == dump(identity)
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                answers = [
                    a.model_copy(
                        update={
                            "status": "unavailable",
                            "error": "operator renderer identity changed during check",
                            "remedy": None,
                        }
                    )
                    for a in answers
                ]
        if len(answers) != len(formulas):
            raise DocumentError(INTERNAL, "renderer returned an incomplete batch")
        items: list[Json] = []
        for (index, span), formula, answer in zip(selected, formulas, answers, strict=True):
            if (
                answer.id != formula.id
                or answer.input_sha256
                != hashlib.sha256(formula.model_dump_json().encode()).hexdigest()
            ):
                raise DocumentError(INTERNAL, "renderer answer does not name the pinned input")
            if answer.status != "unavailable" and any(
                getattr(answer, key) != identity.get(key)
                for key in ("bundle_sha256", "launcher_sha256", "engine")
            ):
                answer = answer.model_copy(
                    update={
                        "status": "unavailable",
                        "error": "renderer configuration changed during the pinned check",
                        "remedy": None,
                    }
                )
            block = mapped.block_ids[span.block]
            items.append(
                {
                    "block": block,
                    "span": mapped.span_ids[index],
                    "owner": proofs[block],
                    "check": answer.model_dump(mode="json"),
                }
            )
        complete = job["cursor"] + len(items) == len(mapped.parsed.spans)
        live = {
            (mapped.block_ids[s.block], mapped.span_ids[i])
            for i, s in enumerate(mapped.parsed.spans)
        }
        await threaded(
            self.commit, check, owner, items, len(mapped.parsed.spans), complete=complete, live=live
        )
        return complete

    def listing(self, project: str, *, after: str | None = None) -> Json:
        from sandbox_science.orchestrator.consistency.store import identifier

        with self.documents._read() as db:
            if (
                db is None
                or db.execute("SELECT 1 FROM sqlite_master WHERE name='math_jobs'").fetchone()
                is None
            ):
                return {"checks": [], "next_after": None, "queue": []}
            cursor = db.execute("SELECT coalesce(max(rowid),0)+1 FROM math_jobs").fetchone()[0]
            if after is not None:
                identifier(after, "math")
                row = db.execute(
                    (
                        "SELECT j.rowid FROM math_jobs j JOIN math_sources s USING(document) "
                        "WHERE s.project=? AND j.check_id=?"
                    ),
                    (project, after),
                ).fetchone()
                if row is None:
                    from .errors import NOT_FOUND

                    raise DocumentError(NOT_FOUND, "no such scoped math cursor")
                cursor = row[0]
            rows = list(
                db.execute(
                    (
                        "SELECT substr(CAST(j.check_id AS BLOB),1,129),"
                        "substr(CAST(j.document AS BLOB),1,129),j.revision,"
                        "substr(CAST(j.state AS BLOB),1,33),j.cursor,j.spans,j.available,"
                        "j.rejected,j.unavailable,j.created FROM math_jobs j JOIN math_sources "
                        "s USING(document) WHERE s.project=? AND j.rowid<? ORDER BY j.rowid "
                        "DESC LIMIT 21"
                    ),
                    (project, cursor),
                )
            )
            for row in rows:
                if len(row[0]) > 128 or len(row[1]) > 128 or len(row[3]) > 32:
                    raise DocumentError(INTERNAL, "stored math listing identity exceeds its bound")
            rows = [(r[0].decode(), r[1].decode(), r[2], r[3].decode(), *r[4:]) for r in rows]
            checks: list[Json] = [
                dict(
                    zip(
                        (
                            "check_id",
                            "document",
                            "revision",
                            "state",
                            "cursor",
                            "spans",
                            "available",
                            "rejected",
                            "unavailable",
                            "created",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in rows[:20]
            ]
            from .comment_math import metadata
            from .comment_math import queue as comment_queue

            for entry in checks:
                comment = metadata(db, entry["check_id"])
                entry["kind"] = "comment" if comment else "document"
                if comment:
                    entry["comment"] = comment
            queue = [
                dict(zip(("document", "target", "completed", "blocked", "running"), r, strict=True))
                for r in db.execute(
                    (
                        "SELECT substr(CAST(document AS BLOB),1,129),target,completed,"
                        "substr(CAST(blocked AS BLOB),1,1001),EXISTS(SELECT 1 FROM math_jobs j "
                        "WHERE j.document=math_sources.document AND j.state='running') FROM "
                        "math_sources WHERE project=? AND "
                        "(completed<target OR retry_requested=1 "
                        "OR EXISTS(SELECT 1 FROM math_jobs j WHERE "
                        "j.document=math_sources.document "
                        "AND j.state='running')) ORDER BY rowid "
                        "LIMIT 21"
                    ),
                    (project,),
                )
            ]
            for entry in queue:
                if len(entry["document"]) > 128 or (
                    entry["blocked"] is not None and len(entry["blocked"]) > 1000
                ):
                    raise DocumentError(INTERNAL, "stored math queue exceeds its bound")
                entry["document"] = entry["document"].decode()
                if entry["blocked"] is not None:
                    entry["blocked"] = entry["blocked"].decode()
            comments = comment_queue(db, project)
            return {
                "checks": checks,
                "next_after": checks[-1]["check_id"] if len(rows) > 20 else None,
                "queue": queue[:20] + comments[:20],
                "queue_more": len(queue) > 20 or len(comments) > 20,
            }

    def evidence(self, project: str, check: str, *, after: int = 0, span: object = None) -> Json:
        from sandbox_science.orchestrator.consistency.store import identifier

        from .errors import INVALID_REQUEST, NOT_FOUND

        identifier(check, "math")
        if type(after) is not int or not 0 <= after <= 20000:
            raise DocumentError(INVALID_REQUEST, "invalid math evidence cursor")
        if span is not None and (
            not isinstance(span, str) or re.fullmatch(r"m[1-9][0-9]{0,8}", span) is None
        ):
            raise DocumentError(INVALID_REQUEST, "invalid math span")
        with self.documents._read() as db:
            if (
                db is None
                or db.execute("SELECT 1 FROM sqlite_master WHERE name='math_jobs'").fetchone()
                is None
                or db.execute(
                    (
                        "SELECT 1 FROM math_jobs j JOIN math_sources s USING(document) WHERE "
                        "s.project=? AND j.check_id=?"
                    ),
                    (project, check),
                ).fetchone()
                is None
            ):
                raise DocumentError(NOT_FOUND, "no such scoped math check")
            rows = list(
                db.execute(
                    (
                        "SELECT ordinal,substr(CAST(payload AS BLOB),1,32769) FROM "
                        "math_observations WHERE check_id=? AND ordinal>=? AND (? IS NULL OR "
                        "span=?) ORDER BY ordinal LIMIT 21"
                    ),
                    (check, after, span, span),
                )
            )
        values: list[Json] = []
        for ordinal, raw in rows[:20]:
            if len(raw) > 32768:
                raise DocumentError(INTERNAL, "stored math evidence exceeds its bound")
            values.append({"ordinal": ordinal, **json.loads(raw)})
        return {
            "check": self.job(check),
            "observations": values,
            "next_after": rows[19][0] + 1 if len(rows) > 20 else None,
        }
