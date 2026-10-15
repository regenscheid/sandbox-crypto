# SPDX-License-Identifier: MIT
"""Bounded, leased document checks; immutable revision history is the backlog.

SQL work and parsing run in a thread, rendering outside transactions. No renderer
is discovered automatically. Exhausted logical quotas hold the cursor, not saves.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore, dump, identifier
from sandbox_science.orchestrator.documents.engine import attach
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)
from sandbox_science.orchestrator.documents.identity import Mapped
from sandbox_science.orchestrator.documents.operations import unlay
from sandbox_science.orchestrator.documents.project_macros import (
    anchored_macros,
    head,
    job_context,
    pin,
    refresh,
    resolve,
    resolved,
    save_resolution,
)
from sandbox_science.orchestrator.mathcheck import Formula, MathCheck
from sandbox_science.orchestrator.mathchecks.mechanical import MechanicalFindings

from .comments import CommentMath, metadata, reconcile
from .comments import queue as comment_queue
from .responses import ResponseMath
from .responses import metadata as response_metadata
from .responses import queue as response_queue
from .schedule import LEASE
from .statements import StatementMath
from .statements import metadata as statement_metadata
from .statements import queue as statement_queue

Json = dict[str, Any]
MAP_BYTES = 16 * 1024 * 1024
BATCH = 32


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


_EFFECTIVE_CACHED = 256
_EFFECTIVE: OrderedDict[tuple[str, str], tuple[dict[str, str], list[str], bool]] = OrderedDict()
"""Each recent check's effective definitions, problems and held state, by store and check:
the immutable inputs they are derived from are parsed once, not on every read."""
_EFFECTIVE_LOCK = threading.Lock()


class MathJobs:
    def __init__(self, store: ReviewStore, *, limits: MathLimits | None = None) -> None:
        self.store, self.documents, self.limits = store, store.documents, limits or MathLimits()

    def _fence(self, db: sqlite3.Connection, check: str, owner: str) -> tuple[Any, ...]:
        response = response_metadata(db, check)
        if response is not None:
            return ResponseMath(self).fence(db, check, owner, response)

        comment = metadata(db, check)
        if comment is not None:
            return CommentMath(self).fence(db, check, owner, comment)
        statement = statement_metadata(db, check)
        if statement is not None:
            return StatementMath(self).fence(db, check, owner, statement)
        row = db.execute(
            (
                "SELECT j.document,j.revision,j.cursor,j.attribution_cursor FROM math_jobs "
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
        return tuple(row)

    def retry_unavailable(
        self, project: str, configuration: dict[str, object] | None = None
    ) -> None:
        """One durable current-revision retry per explicit live activation.

        Prior attempts remain immutable. This does not spin, start an idle service
        or turn an unavailable observation into a successful check.
        """
        with self.documents.control.write() as tx:
            db = tx.sql
            db.execute(
                (
                    "UPDATE math_sources SET retry_requested=1 WHERE project=? AND "
                    "completed=target AND EXISTS(SELECT 1 FROM math_jobs j WHERE "
                    "j.document=math_sources.document AND j.revision=target AND "
                    "NOT EXISTS(SELECT 1 FROM math_text_jobs x WHERE x.check_id=j.check_id) AND "
                    "j.rowid=(SELECT max(k.rowid) FROM math_jobs k WHERE "
                    "k.document=j.document AND k.revision=j.revision AND NOT EXISTS "
                    "(SELECT 1 FROM math_text_jobs x WHERE x.check_id=k.check_id)) AND "
                    "j.state='finished' AND (j.unavailable>0 OR (? IS NOT NULL AND "
                    "j.configuration!=?)))"
                ),
                (
                    project,
                    dump(configuration) if configuration else None,
                    dump(configuration) if configuration else None,
                ),
            )

            CommentMath(self).retry(db, project, dump(configuration) if configuration else None)

            ResponseMath(self).retry(db, project, dump(configuration) if configuration else None)
            StatementMath(self).retry(db, project, dump(configuration) if configuration else None)

    def claim(self, project: str, owner: str) -> str | None:
        ResponseBodies(self.store).reconcile(project)
        now = self.store.clock()
        if not math.isfinite(now) or abs(now) > 1e18:
            raise DocumentError(INTERNAL, "invalid math clock")
        with self.documents.control.write() as tx:
            db = tx.sql

            reconcile(db, project)

            refresh(db, project)
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
                    "SELECT j.check_id FROM math_jobs j JOIN "
                    "math_job_scope s USING(check_id) WHERE s.project=? AND "
                    "j.state='running' AND (j.lease_until<=? OR j.owner=?) ORDER BY "
                    "j.created LIMIT 1"
                ),
                (project, now, owner),
            ).fetchone()
            if old:
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
                    "SELECT document,target FROM math_sources "
                    "WHERE project=? AND (completed<target OR retry_requested=1) AND "
                    "NOT EXISTS (SELECT 1 FROM math_jobs WHERE "
                    "document=math_sources.document AND state='running') ORDER BY rowid LIMIT 1"
                ),
                (project,),
            ).fetchone()
            if source is None:
                selected = CommentMath(self).claim(db, project, owner, now)
                if selected is not None:
                    return selected

                selected = ResponseMath(self).claim(db, project, owner, now)
                if selected is not None:
                    return selected
                return StatementMath(self).claim(db, project, owner, now)
            source = tuple(source)
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
                "SELECT sha256 FROM revisions WHERE document_id=? AND number=?", source
            ).fetchone()
            if row is None:
                raise DocumentError(INTERNAL, "queued immutable revision is missing")
            check = new_id("math")
            db.execute(
                (
                    "INSERT INTO "
                    "math_jobs(check_id,document,revision,source_sha256,state,owner,lease_u"
                    "ntil,attribution_cursor,created) VALUES(?,?,?,?,'running',?,?,?,?)"
                ),
                (check, *source, row[0], owner, now + LEASE, source[1], now),
            )
            db.execute(
                "UPDATE math_sources SET blocked=NULL,retry_requested=0 WHERE document=?",
                (source[0],),
            )

            pin(db, check, project)
            return check

    def job(self, check: str) -> Json:
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
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT " + ",".join(names) + " FROM math_jobs WHERE check_id=?",  # noqa: S608 - fixed columns
                (check,),
            ).fetchone()
            if row is None:
                raise DocumentError(INTERNAL, "math job disappeared")
            value: Json = dict(zip(names, row, strict=True))
            (value["project"],) = db.execute(
                "SELECT project FROM math_job_scope WHERE check_id=?", (check,)
            ).fetchone()

            comment = metadata(db, check)
            if comment is not None:
                value["comment"] = comment

            response = response_metadata(db, check)
            if response is not None:
                value["response"] = response
            statement = statement_metadata(db, check)
            if statement is not None:
                value["statement"] = statement
            if value["document"] is None and response is None and statement is None:
                raise DocumentError(INTERNAL, "math job names no source")

            value["macro_context"] = job_context(db, check, value["project"])
            return value

    def effective_context(self, job: Json) -> Json | None:
        """The macro context a check resolved, derived again from its setting revision
        and immutable sources; None before the check resolved it. Its inputs never
        change, so each check's is worked out once per process (``_EFFECTIVE``)."""
        with self.documents.control.read() as tx:
            (stored,) = tx.sql.execute(
                "SELECT macro_held FROM math_jobs WHERE check_id=?", (job["check_id"],)
            ).fetchone()
        if stored is None:
            return None
        key = (str(self.documents.path), job["check_id"])
        with _EFFECTIVE_LOCK:
            found = _EFFECTIVE.get(key)
            if found is not None:
                _EFFECTIVE.move_to_end(key)
        if found is None:
            found = self._resolve_macros(job, None)
            with _EFFECTIVE_LOCK:
                _EFFECTIVE[key] = found
                while len(_EFFECTIVE) > _EFFECTIVE_CACHED:
                    _EFFECTIVE.popitem(last=False)
        macros, problems, held = found
        return resolved(job["macro_context"], dict(macros), list(problems), held)

    @staticmethod
    def proof_author(raw: str | None) -> Json | None:
        """A block owner's author, as the owner proof keeps its JSON."""
        return None if raw is None else cast(Json, json.loads(raw))

    def pin(self, check: str, owner: str, identity: dict[str, object]) -> bool:
        value = dump(identity)
        if len(value.encode()) > 4096:
            raise DocumentError(INTERNAL, "math configuration identity exceeds its bound")
        with self.documents.control.write() as tx:
            db = tx.sql
            self._fence(db, check, owner)
            old = db.execute(
                "SELECT configuration FROM math_jobs WHERE check_id=?", (check,)
            ).fetchone()[0]
            if old is not None:
                return old == value
            db.execute("UPDATE math_jobs SET configuration=? WHERE check_id=?", (value, check))
            return True

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json] | None:
        """Prove last change <= pinned revision, resumably; current cache is not authority.

        At most two bounded historical maps are decoded per step. A later USER
        change/removal cannot relabel earlier work. Unknown is recorded only after
        reaching the beginning, never because a page budget ran out.
        """

        with self.documents.control.read() as tx:
            db = tx.sql
            comment = metadata(db, check)
            response = response_metadata(db, check)
            statement = statement_metadata(db, check)
        if response is not None:
            return ResponseMath(self).attribution(check, owner, blocks)
        if statement is not None:
            return StatementMath(self).attribution(check, owner, blocks)
        if comment is not None:
            return CommentMath(self).attribution(check, owner, blocks)
        with self.documents.control.read() as tx:
            db = tx.sql
            document, revision, _, cursor = self._fence(db, check, owner)
            proofs = {
                r[0]: {"revision": r[1], "author": self.proof_author(r[2])}
                for r in db.execute(
                    (
                        "SELECT block,revision,author FROM "
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
                        "SELECT number,block_map,author FROM revisions WHERE document_id=? "
                        "AND number<=? ORDER BY number DESC LIMIT ?"
                    ),
                    (document, cursor, self.limits.history_per_step),
                )
            )
        added: dict[str, Json] = {}
        for number, raw, raw_author in rows:
            author = json.loads(raw_author)
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
        with self.documents.control.write() as tx:
            db = tx.sql
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
        with self.documents.control.write() as tx:
            db = tx.sql
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
                    "UPDATE text_math_sources SET blocked='math observation quota reached' "
                    "WHERE (subject_type,subject_id)=(SELECT subject_type,subject_id "
                    "FROM math_text_jobs WHERE check_id=?)",
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
        job = self.job(check)
        with self.documents.control.write() as tx:
            db = tx.sql
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
                if "response" in job:
                    ResponseMath(self).finish(db, job)
                elif "comment" in job:
                    CommentMath(self).finish(db, job)
                elif "statement" in job:
                    StatementMath(self).finish(db, job)
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
        if "response" in job:
            return ResponseMath(self).load(job)
        if "comment" in job:
            return CommentMath(self).load(job)
        if "statement" in job:
            return StatementMath(self).load(job)
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                (
                    "SELECT r.text,r.block_map,r.sha256 FROM "
                    "revisions r JOIN documents d USING(document_id) WHERE d.project_id=? "
                    "AND r.document_id=? AND r.number=?"
                ),
                (job["project"], job["document"], job["revision"]),
            ).fetchone()
        if row is None:
            raise DocumentError(INTERNAL, "pinned math source is missing")
        if row[2] != job["source_sha256"]:
            raise DocumentError(INTERNAL, "math job digest does not bind the immutable source")
        try:
            return attach(row[0], json.loads(row[1]))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DocumentError(INTERNAL, "pinned math source identity is invalid") from None

    def _resolve_macros(
        self, job: Json, mapped: Mapped | None
    ) -> tuple[dict[str, str], list[str], bool]:
        """A check's effective definitions, problems, and whether its context is held:
        the setting it used with its source's own definitions (a document's front
        matter, a comment's document's, an explanation's anchored documents'); a ledger
        statement has none of its own."""
        project = job["macro_context"]["macros"]
        held = not job["macro_context"]["known"]
        if "response" in job:
            macros, problems, held = anchored_macros(
                self.documents, job["project"], job["macro_context"]
            )
        elif "comment" in job:
            macros, problems = resolve(project, CommentMath(self).macros(job))
        elif "statement" in job:
            macros, problems = resolve(project, {})
        else:
            mapped = self.load(job) if mapped is None else mapped
            macros, problems = resolve(
                project, mapped.parsed.front_matter.macros if mapped.parsed.front_matter else {}
            )
        if held and not problems:
            problems = ["Historical project macro context is unknown; checks are held"]
        return macros, problems, held

    def effective_macros(self, job: Json, mapped: Mapped, owner: str) -> dict[str, str]:
        macros, _problems, held = self._resolve_macros(job, mapped)
        with self.documents.control.write() as tx:
            db = tx.sql
            self._fence(db, job["check_id"], owner)
            save_resolution(db, job["check_id"], held)
            if held:
                db.execute(
                    "UPDATE math_jobs SET reason='macro context unknown or ambiguous' "
                    "WHERE check_id=?",
                    (job["check_id"],),
                )
        job["macro_held"] = held
        return macros

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
        macros = await threaded(self.effective_macros, job, mapped, owner)
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
        context_held = job["macro_held"]
        if (
            context_held
            or checker is None
            or not pinned
            or identity.get("renderer") == "unavailable"
        ):
            answers = [
                MathCheck(
                    id=f.id,
                    status="unavailable",
                    error="macro context unknown or ambiguous"
                    if context_held
                    else "renderer unavailable or its pinned configuration changed",
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

    def listing(
        self, project: str, *, after: str | None = None, session: str | None = None
    ) -> Json:
        if self.documents.control.missing:
            return {"checks": [], "next_after": None, "queue": []}
        with self.documents.control.read() as tx:
            db = tx.sql
            cursor = db.execute("SELECT coalesce(max(rowid),0)+1 FROM math_jobs").fetchone()[0]
            if after is not None:
                identifier(after, "math")
                row = db.execute(
                    (
                        "SELECT j.rowid FROM math_jobs j JOIN math_job_scope"
                        " s USING(check_id) "
                        "WHERE s.project=? AND j.check_id=? AND (s.session IS NULL OR ? IS "
                        "NULL OR s.session=?)"
                    ),
                    (project, after, session, session),
                ).fetchone()
                if row is None:
                    raise DocumentError(NOT_FOUND, "no such scoped math cursor")
                cursor = row[0]
            rows = list(
                db.execute(
                    (
                        "SELECT j.check_id,j.document,j.revision,j.state,j.cursor,j.spans,"
                        "j.available,"
                        "j.rejected,j.unavailable,j.created FROM math_jobs j JOIN math_job_scope"
                        " s USING(check_id) WHERE s.project=? AND j.rowid<? "
                        "AND (s.session IS NULL OR ? IS NULL OR s.session=?) ORDER BY j.rowid "
                        "DESC LIMIT 21"
                    ),
                    (project, cursor, session, session),
                )
            )
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

            for entry in checks:
                comment = metadata(db, entry["check_id"])
                entry["kind"] = "comment" if comment else "document"
                if comment:
                    entry["comment"] = comment
                response = response_metadata(db, entry["check_id"])
                if response:
                    entry["kind"], entry["response"] = "response", response
                statement = statement_metadata(db, entry["check_id"])
                if statement:
                    entry["kind"], entry["statement"] = "statement", statement

                context = job_context(db, entry["check_id"], project)
                (held,) = db.execute(
                    "SELECT macro_held FROM math_jobs WHERE check_id=?", (entry["check_id"],)
                ).fetchone()
                entry["project_macro_revision"] = context["revision"]
                entry["macro_context_status"] = (
                    "unknown"
                    if not context["known"]
                    else "stale"
                    if context["revision"] != head(db, project)
                    else "pending"
                    if held is None
                    else "held"
                    if held
                    else "pinned"
                )
            queue = [
                dict(zip(("document", "target", "completed", "blocked", "running"), r, strict=True))
                for r in db.execute(
                    (
                        "SELECT document,target,completed,blocked,EXISTS(SELECT 1 FROM math_jobs j "
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
            comments = comment_queue(db, project)
            responses = response_queue(db, project, session=session)
            statements = statement_queue(db, project)
            kinds = (queue, comments, responses, statements)
            return {
                "checks": checks,
                "next_after": checks[-1]["check_id"] if len(rows) > 20 else None,
                "queue": [item for kind in kinds for item in kind[:20]],
                "queue_more": any(len(kind) > 20 for kind in kinds),
            }

    def evidence(
        self,
        project: str,
        check: str,
        *,
        after: int = 0,
        span: object = None,
        session: str | None = None,
    ) -> Json:
        identifier(check, "math")
        if type(after) is not int or not 0 <= after <= 20000:
            raise DocumentError(INVALID_REQUEST, "invalid math evidence cursor")
        if span is not None and (
            not isinstance(span, str) or re.fullmatch(r"m[1-9][0-9]{0,8}", span) is None
        ):
            raise DocumentError(INVALID_REQUEST, "invalid math span")
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no such scoped math check")
        with self.documents.control.read() as tx:
            db = tx.sql
            if (
                db.execute(
                    (
                        "SELECT 1 FROM math_jobs j JOIN math_job_scope"
                        " s USING(check_id) WHERE "
                        "s.project=? AND j.check_id=? AND (s.session IS NULL OR ? IS NULL "
                        "OR s.session=?)"
                    ),
                    (project, check, session, session),
                ).fetchone()
                is None
            ):
                raise DocumentError(NOT_FOUND, "no such scoped math check")
            rows = list(
                db.execute(
                    (
                        "SELECT ordinal,payload FROM "
                        "math_observations WHERE check_id=? AND ordinal>=? AND (? IS NULL OR "
                        "span=?) ORDER BY ordinal LIMIT 21"
                    ),
                    (check, after, span, span),
                )
            )
        values: list[Json] = []
        for ordinal, raw in rows[:20]:
            values.append({"ordinal": ordinal, **json.loads(raw)})
        job = self.job(check)
        job["effective_macro_context"] = self.effective_context(job)
        return {
            "check": job,
            "observations": values,
            "next_after": rows[19][0] + 1 if len(rows) > 20 else None,
        }
