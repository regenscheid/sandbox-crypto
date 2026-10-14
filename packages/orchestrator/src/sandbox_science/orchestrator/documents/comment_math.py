# SPDX-License-Identifier: MIT
"""Comment source adapter for the shared bounded mechanical math worker.

Only source admission/loading/ownership differs. Renderer, observation limits,
actual-work cancellation ownership, lifecycle and closing delivery are shared.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from sandbox_science.contracts import new_id

from . import comment_bodies as bodies
from .engine import attach
from .errors import CONFLICT, INTERNAL, DocumentError
from .identity import Mapped
from .mathjobs import LEASE

if TYPE_CHECKING:
    from .mathjobs import MathJobs

Json = dict[str, Any]


def metadata(db: sqlite3.Connection, check: str) -> Json | None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='math_comment_jobs'").fetchone() is None:
        return None
    row = db.execute(
        "SELECT substr(CAST(c.entry_id AS BLOB),1,129),c.macro_revision,"
        "substr(CAST(c.macro_sha256 AS BLOB),1,65),CASE WHEN "
        "json_type(b.block_map,'$.identity_floor')='integer' THEN "
        "json_extract(b.block_map,'$.identity_floor') ELSE NULL END,j.revision,"
        "t.document_id=j.document AND original.kind IN ('create','reply') "
        "FROM math_comment_jobs c JOIN math_jobs j USING(check_id) LEFT JOIN "
        "annotation_body_revisions b "
        "ON b.entry_id=c.entry_id AND b.revision=j.revision LEFT JOIN annotation_entries "
        "original ON original.entry_id=c.entry_id LEFT JOIN annotation_threads t "
        "ON t.thread_id=original.thread_id WHERE c.check_id=?",
        (check,),
    ).fetchone()
    if row is None:
        return None
    if (
        len(row[0]) > 128
        or re.fullmatch(b"cmt-[0-9a-f]{24}", row[0]) is None
        or type(row[1]) is not int
        or not 1 <= row[1] <= 999999999
        or re.fullmatch(b"[0-9a-f]{64}", row[2]) is None
        or type(row[3]) is not int
        or type(row[4]) is not int
        or not 1 <= row[3] <= row[4] <= 999999999
        or row[5] != 1
    ):
        raise DocumentError(INTERNAL, "retained comment math identity exceeds its bounds")
    return {
        "entry_id": row[0].decode(),
        "macro_revision": row[1],
        "macro_sha256": row[2].decode(),
        "identity_floor": row[3],
    }


class CommentMath:
    def __init__(self, jobs: MathJobs) -> None:
        self.jobs, self.store, self.documents = jobs, jobs.store, jobs.documents

    def claim(self, db: sqlite3.Connection, project: str, owner: str, now: float) -> str | None:
        validate_sources(db, project)
        row = db.execute(
            "SELECT substr(CAST(s.entry_id AS BLOB),1,129),"
            "substr(CAST(s.document AS BLOB),1,129),CASE WHEN s.completed<s.target "
            "THEN s.completed+1 ELSE s.target END,d.head FROM comment_math_sources s "
            "JOIN documents d ON d.document_id=s.document JOIN annotation_entries original "
            "ON original.entry_id=s.entry_id JOIN annotation_threads t "
            "ON t.thread_id=original.thread_id AND t.document_id=s.document "
            "WHERE s.project=? AND d.project_id=s.project "
            "AND s.target>0 AND (s.completed<s.target OR s.retry_requested=1 OR "
            "s.macro_revision<d.head) "
            "AND (s.blocked IS NULL OR s.blocked NOT LIKE 'Legacy writer%') "
            "AND (s.blocked IS NULL OR s.blocked NOT LIKE 'Derived body identity%') "
            "AND NOT EXISTS(SELECT 1 FROM math_jobs j WHERE j.document=s.document "
            "AND j.state='running') ORDER BY s.rowid LIMIT 1",
            (project,),
        ).fetchone()
        if row is None:
            return None
        if (
            len(row[0]) > 128
            or len(row[1]) > 128
            or any(type(n) is not int or not 1 <= n <= 999999999 for n in row[2:])
        ):
            raise DocumentError(INTERNAL, "retained comment math cursor exceeds its bounds")
        entry, document, revision, macro_revision = row[0].decode(), row[1].decode(), row[2], row[3]
        if db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] >= self.jobs.limits.jobs:
            db.execute(
                "UPDATE comment_math_sources SET blocked='math job history quota reached' "
                "WHERE entry_id=?",
                (entry,),
            )
            return None
        hashes = db.execute(
            "SELECT substr(CAST(b.sha256 AS BLOB),1,65),substr(CAST(r.sha256 AS BLOB),1,65),"
            "CASE WHEN json_type(b.block_map,'$.held')='text' THEN substr(CAST("
            "json_extract(b.block_map,'$.held') AS BLOB),1,1001) ELSE NULL END "
            "FROM annotation_body_revisions b JOIN revisions r ON r.document_id=? "
            "AND r.number=? WHERE b.entry_id=? AND b.revision=?",
            (document, macro_revision, entry, revision),
        ).fetchone()
        if (
            hashes is None
            or any(re.fullmatch(b"[0-9a-f]{64}", h) is None for h in hashes[:2])
            or (hashes[2] is not None and len(hashes[2]) > 1000)
        ):
            raise DocumentError(INTERNAL, "queued comment revision or macro context is invalid")
        check = new_id("math")
        db.execute(
            "INSERT INTO math_jobs(check_id,document,revision,source_sha256,state,owner,"
            "lease_until,created) VALUES(?,?,?,?,'running',?,?,?)",
            (check, document, revision, hashes[0].decode(), owner, now + LEASE, now),
        )
        db.execute(
            "INSERT INTO math_comment_jobs VALUES(?,?,?,?)",
            (check, entry, macro_revision, hashes[1].decode()),
        )
        from .project_macros import pin

        pin(db, check, project)
        db.execute(
            "UPDATE comment_math_sources SET retry_requested=0,blocked=NULL WHERE entry_id=?",
            (entry,),
        )
        if hashes[2] is not None:
            # A formerly held body can be followed by a parseable revision. Retain
            # its missing coverage as an ended unavailable attempt, never an empty
            # successful parse/removal, then let the next claim service the head.
            db.execute(
                "UPDATE math_jobs SET state='finished',unavailable=1,reason=?,ended=?,"
                "owner=NULL,lease_until=NULL "
                "WHERE check_id=?",
                (hashes[2].decode(), now, check),
            )
            db.execute(
                "UPDATE comment_math_sources SET completed=max(completed,?) WHERE entry_id=?",
                (revision, entry),
            )
        return check

    def fence(self, db: sqlite3.Connection, check: str, owner: str, meta: Json) -> tuple[Any, ...]:
        row = db.execute(
            "SELECT substr(CAST(j.document AS BLOB),1,129),j.revision,j.cursor "
            "FROM math_jobs j JOIN math_comment_jobs c USING(check_id) "
            "JOIN annotation_body_revisions b ON b.entry_id=c.entry_id AND b.revision=j.revision "
            "JOIN comment_math_sources s ON s.entry_id=c.entry_id "
            "JOIN annotation_entries original ON original.entry_id=c.entry_id "
            "JOIN annotation_threads t ON t.thread_id=original.thread_id "
            "JOIN documents d ON d.document_id=j.document JOIN revisions r ON "
            "r.document_id=j.document AND r.number=c.macro_revision WHERE j.check_id=? "
            "AND j.state='running' AND j.owner=? AND j.lease_until>? "
            "AND j.source_sha256=b.sha256 AND c.macro_sha256=r.sha256 "
            "AND s.document=j.document AND s.project=d.project_id",
            (check, owner, self.store.clock()),
        ).fetchone()
        if row is None:
            raise DocumentError(CONFLICT, "comment math ownership or source pin changed")
        bodies.parent(db, meta["entry_id"], row[0].decode())
        if (
            len(row[0]) > 128
            or type(row[1]) is not int
            or not 1 <= row[1] <= 999999999
            or type(row[2]) is not int
            or not 0 <= row[2] <= 20000
            or metadata(db, check) != meta
        ):
            raise DocumentError(INTERNAL, "retained comment math fence exceeds its bounds")
        return row[0].decode(), row[1], row[2], None

    def load(self, job: Json) -> Mapped:
        with self.documents.control.read() as tx:
            db = tx.sql
            bodies.parent(db, job["comment"]["entry_id"], job["document"], job["project"])
            saved = bodies.read(db, job["comment"]["entry_id"], job["revision"])
        if saved["sha256"] != job["source_sha256"]:
            raise DocumentError(INTERNAL, "comment math digest does not bind its body")
        return attach(saved["body"], saved["block_map"], documents=False)

    def macros(self, job: Json) -> dict[str, str]:
        # Use the ordinary immutable document loader and integrity check. Comment
        # front matter is display text, never its own macro authority.
        parent = {
            **job,
            "revision": job["comment"]["macro_revision"],
            "source_sha256": job["comment"]["macro_sha256"],
        }
        parent.pop("comment")
        mapped = self.jobs.load(parent)
        return mapped.parsed.front_matter.macros if mapped.parsed.front_matter else {}

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json]:
        with self.documents.control.read() as tx:
            db = tx.sql
            meta = metadata(db, check)
            assert meta is not None  # noqa: S101 - dispatcher selected comment adapter
            _, revision, _, _ = self.fence(db, check, owner, meta)
            saved = bodies.read(db, meta["entry_id"], revision)
        proofs: dict[str, Json] = {}
        for block in blocks:
            value = saved["owners"].get(block)
            if not isinstance(value, dict):
                raise DocumentError(INTERNAL, "comment block ownership proof is missing")
            value = cast(Json, value)
            if set(value) != {"revision", "author"}:
                raise DocumentError(INTERNAL, "comment block ownership proof is missing")
            raw = bodies.dump(value["author"]).encode() if value["author"] else None
            actor = self.jobs.proof_author(raw)
            if value["revision"] is not None and (
                type(value["revision"]) is not int or not 1 <= value["revision"] <= revision
            ):
                raise DocumentError(INTERNAL, "comment owner revision is invalid")
            proofs[block] = {"revision": value["revision"], "author": actor}
        return proofs

    def finish(self, db: sqlite3.Connection, job: Json) -> None:
        db.execute(
            "UPDATE comment_math_sources SET completed=max(completed,?),"
            "macro_revision=max(macro_revision,?),blocked=NULL WHERE entry_id=?",
            (job["revision"], job["comment"]["macro_revision"], job["comment"]["entry_id"]),
        )

    def retry(self, db: sqlite3.Connection, project: str, configuration: str | None) -> None:
        db.execute(
            "UPDATE comment_math_sources SET retry_requested=1 WHERE project=? "
            "AND completed=target AND EXISTS(SELECT 1 FROM math_jobs j JOIN math_comment_jobs c "
            "USING(check_id) WHERE c.entry_id=comment_math_sources.entry_id AND j.revision=target "
            "AND c.macro_revision=comment_math_sources.macro_revision AND j.rowid=(SELECT "
            "max(k.rowid) FROM math_jobs k JOIN math_comment_jobs x USING(check_id) "
            "WHERE x.entry_id=c.entry_id AND k.revision=j.revision AND "
            "x.macro_revision=c.macro_revision) AND j.state='finished' AND "
            "(j.unavailable>0 OR (? IS NOT NULL AND j.configuration!=?)))",
            (project, configuration, configuration),
        )


def remaining(db: sqlite3.Connection, project: str) -> bool:
    validate_sources(db, project)
    return (
        db.execute(
            "SELECT 1 FROM comment_math_sources s JOIN documents d ON d.document_id=s.document "
            "WHERE s.project=? AND s.target>0 AND (s.completed<s.target OR s.retry_requested=1 "
            "OR s.macro_revision<d.head OR EXISTS(SELECT 1 FROM math_jobs j "
            "JOIN math_comment_jobs c USING(check_id) WHERE c.entry_id=s.entry_id "
            "AND j.state='running')) LIMIT 1",
            (project,),
        ).fetchone()
        is not None
    )


def reconcile(db: sqlite3.Connection, project: str) -> None:
    validate_sources(db, project)
    db.execute(
        "UPDATE comment_math_sources AS s SET blocked='Legacy writer changed the canonical "
        "body; automatic checks are held' WHERE s.project=? AND s.target>0 AND EXISTS("
        "SELECT 1 FROM annotation_body_revisions b JOIN annotation_entries original "
        "ON original.entry_id=s.entry_id WHERE b.entry_id=s.entry_id AND b.revision=s.target "
        "AND b.history_entry!=(SELECT e.entry_id FROM annotation_entries e "
        "WHERE e.thread_id=original.thread_id AND (e.entry_id=original.entry_id OR "
        "(e.kind='edit' AND original.number=1) OR (e.kind='edit_body' "
        "AND json_extract(e.payload,'$.entry')=original.entry_id)) ORDER BY "
        "e.number DESC LIMIT 1))",
        (project,),
    )


def queue(db: sqlite3.Connection, project: str) -> list[Json]:
    if (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='comment_math_sources'").fetchone()
        is None
    ):
        return []
    validate_sources(db, project)
    rows = db.execute(
        "SELECT substr(CAST(s.document AS BLOB),1,129),substr(CAST(s.entry_id AS BLOB),1,129),"
        "s.target,s.completed,substr(CAST(s.blocked AS BLOB),1,1001),EXISTS(SELECT "
        "1 FROM math_jobs j "
        "JOIN math_comment_jobs c USING(check_id) WHERE c.entry_id=s.entry_id AND "
        "j.state='running'),"
        "s.macro_revision,d.head FROM comment_math_sources s JOIN documents d ON "
        "d.document_id=s.document "
        "WHERE s.project=? AND (s.blocked IS NOT NULL OR s.completed<s.target OR "
        "s.retry_requested=1 "
        "OR s.macro_revision<d.head OR EXISTS(SELECT 1 FROM math_jobs j JOIN math_comment_jobs c "
        "USING(check_id) WHERE c.entry_id=s.entry_id AND j.state='running')) ORDER "
        "BY s.rowid LIMIT 21",
        (project,),
    )
    values: list[Json] = []
    for document, entry, target, completed, blocked, running, macro, head in rows:
        if (
            len(document) > 128
            or len(entry) > 128
            or (blocked is not None and len(blocked) > 1000)
            or any(
                type(n) is not int or not 0 <= n <= 999999999
                for n in (target, completed, macro, head)
            )
        ):
            raise DocumentError(INTERNAL, "retained comment math queue exceeds its bounds")
        values.append(
            {
                "document": document.decode(),
                "entry": entry.decode(),
                "kind": "comment",
                "target": target,
                "completed": completed,
                "blocked": blocked.decode() if blocked else None,
                "running": running,
                "macro_revision": macro,
                "macro_target": head,
            }
        )
    return values


def validate_sources(db: sqlite3.Connection, project: str) -> None:
    if (
        db.execute(
            "SELECT 1 FROM comment_math_sources s LEFT JOIN annotation_entries e "
            "ON e.entry_id=s.entry_id LEFT JOIN annotation_threads t ON t.thread_id=e.thread_id "
            "LEFT JOIN documents d ON d.document_id=t.document_id WHERE s.project=? AND "
            "(e.kind IS NULL OR e.kind NOT IN ('create','reply') OR t.document_id IS NULL "
            "OR s.document IS NOT t.document_id OR s.project IS NOT d.project_id) LIMIT 1",
            (project,),
        ).fetchone()
        is not None
    ):
        raise DocumentError(INTERNAL, "comment queue does not bind its canonical parent")
