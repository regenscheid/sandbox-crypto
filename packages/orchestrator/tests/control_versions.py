# SPDX-License-Identifier: MIT
"""Older control stores for migration tests, made from a current one.

The code writes only the current schema, so a test cannot make a store in an older
version by writing to it. ``older_store`` makes one: an empty store in that version,
made by the migration steps up to it, filled with a current store's rows in the columns
both have, and with the rows of the tables later steps dropped derived from the
columns and tables that replaced them. A migration test makes its data with the current code,
copies it back, migrates the copy, and reads it as it read the original.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.control import ControlStore, migrations

Json = dict[str, Any]

MACRO_REVISIONS = migrations.BASELINE + 8
"""The version that replaced the macro pin tables with ``macro_revision`` columns."""

RECEIPTS = migrations.BASELINE + 10
"""The version that moved the operation receipts into ``operation_receipts``."""

EVENT_LOG = migrations.BASELINE + 11
"""The version that moved the documents' and the reviewer's events into ``control_events``."""

FEED_CURSORS = migrations.BASELINE + 12
"""The version that moved the reviewer's feed cursors into ``feed_cursors``."""

TEXT_REVISIONS = migrations.BASELINE + 13
"""The version that moved comment and explanation bodies into ``text_revisions``."""

TEXT_MATH = migrations.BASELINE + 14
"""The version that moved the comment and explanation math queues and check links into
``text_math_sources`` and ``math_text_jobs``."""

BUILTIN_SKILLS = migrations.BASELINE + 40
"""The version that let an import be of the built-in skills and tied the name to them."""

EMPTY_SHA = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
"""The SHA-256 of ``{}``, the empty macro setting."""

_PINS = (
    # The pin tables version 10 dropped, from the columns that replaced them.
    "INSERT INTO document_macro_pins SELECT document_id,number,macro_revision "
    "FROM src.revisions WHERE macro_revision IS NOT NULL",
    "INSERT INTO math_macro_pins SELECT j.check_id,s.project,j.macro_revision,"
    "coalesce((SELECT r.sha256 FROM src.project_macro_revisions r WHERE r.project=s.project "
    f"AND r.revision=j.macro_revision),'{EMPTY_SHA}'),coalesce((SELECT a.anchors FROM "
    "response_macro_anchors a JOIN old_response_jobs r ON r.source=a.source "
    "WHERE r.check_id=j.check_id),'[]') FROM src.math_jobs j "
    "JOIN src.math_job_scope s USING(check_id) WHERE j.macro_revision IS NOT NULL",
    "INSERT INTO math_macro_contexts SELECT check_id,'{}','[]',macro_held,"
    "lower(hex(zeroblob(32))) FROM src.math_jobs WHERE macro_held IS NOT NULL",
    "INSERT INTO response_macro_revisions SELECT source,revision,macro_revision "
    "FROM old_response_bodies WHERE macro_revision IS NOT NULL",
    "INSERT INTO review_macro_pins SELECT p.review_id,p.finding_id,p.macro_revision,"
    "coalesce((SELECT r.sha256 FROM src.project_macro_revisions r JOIN src.review_findings f "
    "ON f.project=r.project WHERE f.finding_id=p.finding_id AND r.revision=p.macro_revision),"
    f"'{EMPTY_SHA}') FROM src.review_response_pins p WHERE p.macro_revision IS NOT NULL",
)

_RECEIPTS = (
    # The receipt tables version 12 dropped, from operation_receipts.
    "INSERT INTO requests SELECT project,actor,operation_id,fingerprint,result,"
    "'2026-01-01T00:00:00+00:00' FROM src.operation_receipts WHERE scope='document'",
    "INSERT INTO annotation_operations SELECT project,actor,operation_id,fingerprint,result "
    "FROM src.operation_receipts WHERE scope='comment'",
    "INSERT INTO review_operations SELECT project,actor,operation_id,fingerprint,result "
    "FROM src.operation_receipts WHERE scope='review'",
    "INSERT INTO project_macro_operations SELECT project,actor,operation_id,fingerprint,"
    "json_extract(result,'$.revision') FROM src.operation_receipts WHERE scope='macros'",
)

_EVENTS = (
    # The event tables version 13 dropped, from control_events.
    "INSERT INTO events(seq,time,kind,producer,project_id,session_id,document_id,payload) "
    "SELECT seq,time,kind,producer,project,session,subject_id,payload "
    "FROM src.control_events WHERE subject_type='document' ORDER BY seq",
    "INSERT INTO review_events(project,session,kind,payload,created) "
    "SELECT project,session,kind,payload,(julianday(time)-2440587.5)*86400.0 "
    "FROM src.control_events WHERE subject_type IN ('review','finding') ORDER BY seq",
)

_CURSORS = (
    # The cursors version 14 moved, from feed_cursors.
    "INSERT INTO review_artifact_cursors SELECT project,session,cursor "
    "FROM src.feed_cursors WHERE feed='artifact'",
    "UPDATE review_windows SET document_cursor=coalesce((SELECT cursor FROM src.feed_cursors c "
    "WHERE c.feed='document' AND c.project=review_windows.project "
    "AND c.session=review_windows.session),0)",
)

_TEXTS = (
    # The body tables version 15 dropped, from text_revisions, as views to copy from.
    "CREATE TEMP VIEW old_response_bodies AS SELECT rowid AS rowid,subject_id AS source,"
    "parent AS finding_id,parent_revision AS decision_revision,revision,body,sha256,block_map,"
    "author,owners,next_block,next_span,identity_floor,held,macro_revision "
    "FROM src.text_revisions WHERE subject_type='response'",
    "CREATE TEMP VIEW old_comment_bodies AS SELECT rowid AS rowid,subject_id AS entry_id,"
    "revision,origin AS history_entry,body,sha256,block_map,author,owners,next_block,next_span "
    "FROM src.text_revisions WHERE subject_type='comment'",
)

_TEXT_MATH = (
    # The queue and link tables version 16 dropped, from the text tables.
    "CREATE TEMP VIEW old_comment_sources AS SELECT rowid AS rowid,subject_id AS entry_id,"
    "parent AS document,project,target,completed,macro_revision,retry_requested,blocked "
    "FROM src.text_math_sources WHERE subject_type='comment'",
    "CREATE TEMP VIEW old_response_sources AS SELECT rowid AS rowid,subject_id AS source,"
    "parent AS finding_id,parent_revision AS decision_revision,project,session,target,"
    "completed,retry_requested,blocked FROM src.text_math_sources "
    "WHERE subject_type='response'",
    "CREATE TEMP VIEW old_comment_jobs AS SELECT rowid AS rowid,check_id,subject_id AS "
    "entry_id,macro_revision,macro_sha256 FROM src.math_text_jobs WHERE subject_type='comment'",
    "CREATE TEMP VIEW old_response_jobs AS SELECT rowid AS rowid,check_id,subject_id AS source "
    "FROM src.math_text_jobs WHERE subject_type='response'",
)


@contextmanager
def at_version(version: int) -> Iterator[None]:
    """The migration list and version as they were at ``version``."""
    saved = migrations.MIGRATIONS, migrations.VERSION
    setattr(migrations, "MIGRATIONS", saved[0][: version - migrations.BASELINE + 1])  # noqa: B010
    setattr(migrations, "VERSION", version)  # noqa: B010
    try:
        yield
    finally:
        setattr(migrations, "MIGRATIONS", saved[0])  # noqa: B010
        setattr(migrations, "VERSION", saved[1])  # noqa: B010


def older_store(source: Path, target: Path, version: int) -> None:
    """Make ``target`` a store in ``version`` holding the rows of the store ``source``."""
    with at_version(version):
        store = ControlStore(target)
        store.initialize()
        store.close()
    db = sqlite3.connect(target, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        db.execute("BEGIN")
        tables = [
            name
            for (name,) in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        for table in tables:
            _copy(db, table)
        if version < TEXT_MATH:
            for statement in _TEXT_MATH:
                db.execute(statement)
            for table, view in (
                ("comment_math_sources", "old_comment_sources"),
                ("response_math_sources", "old_response_sources"),
                ("math_comment_jobs", "old_comment_jobs"),
                ("math_response_jobs", "old_response_jobs"),
            ):
                _copy(db, table, view)
        if version < TEXT_REVISIONS:
            for statement in _TEXTS:
                db.execute(statement)
            _copy(db, "review_response_bodies", "old_response_bodies")
            _copy(db, "annotation_body_revisions", "old_comment_bodies")
        if version < MACRO_REVISIONS:
            # An explanation's anchors as main pinned them at its first save, which is
            # what the current code derives for it; a legacy writer pinned none.
            from sandbox_science.orchestrator.documents.project_macros import response_anchors

            reader = sqlite3.connect(source)
            try:
                for (key,) in reader.execute(
                    "SELECT b.subject_id FROM text_revisions b WHERE b.subject_type='response' "
                    "AND b.revision=1 AND b.macro_revision IS NOT NULL ORDER BY b.rowid"
                ).fetchall():
                    db.execute(
                        "INSERT INTO response_macro_anchors VALUES(?,?)",
                        (key, json.dumps(response_anchors(reader, key), separators=(",", ":"))),
                    )
            finally:
                reader.close()
            for statement in _PINS:
                db.execute(statement)
        if version < RECEIPTS:
            for statement in _RECEIPTS:
                db.execute(statement)
        if version < EVENT_LOG:
            for statement in _EVENTS:
                db.execute(statement)
        if version < FEED_CURSORS:
            for statement in _CURSORS:
                db.execute(statement)
        db.execute("COMMIT")
    finally:
        db.close()


def _copy(db: sqlite3.Connection, table: str, source: str | None = None) -> None:
    """Copy the rows of ``source`` (``src``'s table of the same name, or a temporary view)
    into ``table``, in the columns both have."""
    origin = f'src."{table}"' if source is None else f'temp."{source}"'
    info = f'src.table_info("{table}")' if source is None else f'temp.table_info("{source}")'
    mine = db.execute(f'PRAGMA main.table_info("{table}")').fetchall()
    theirs = {row[1] for row in db.execute(f"PRAGMA {info}")}
    if not theirs:
        return
    columns = [row[1] for row in mine if row[1] in theirs]
    keys = [row for row in mine if row[5]]
    # A table whose key is its rowid copies it with the key; any other keeps its rowids,
    # but a table without rowids has none to keep or to order by. Such a table is an index
    # a trigger fills as the rows it indexes are copied, so what it already holds stays.
    alias = len(keys) == 1 and keys[0][2].upper() == "INTEGER"
    (sql,) = db.execute(
        "SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    without = "WITHOUT ROWID" in str(sql).upper()
    names = ",".join(f'"{column}"' for column in columns)
    rowid = "" if alias or without else "rowid,"
    order = "" if without else " ORDER BY rowid"
    db.execute(
        f'INSERT {"OR IGNORE " if without else ""}INTO main."{table}" ({rowid}{names}) '
        f"SELECT {rowid}{names} FROM {origin}{order}"
    )


def macro_answers(docs: Any, reviews: Any, effective: Callable[[Any, Json], Any]) -> Json:
    """Every macro-related answer a store gives, as JSON: each check's context and
    effective context (``effective(jobs, job)``, since main and B-12 give it differently),
    the check listing, every document revision's and explanation body's views, every
    finding with its contexts, the confirmation targets, the runs and the pending math
    notices. A version-9 fixture's answers from main are compared with B-12's."""
    from sandbox_science.orchestrator.consistency.findings import Findings
    from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
    from sandbox_science.orchestrator.documents.project_macros import response_view, view
    from sandbox_science.orchestrator.mathchecks.contexts import finding_macro_contexts
    from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
    from sandbox_science.orchestrator.mathchecks.notices import MathNotices

    def safe(read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}"}

    out: Json = {}
    db = sqlite3.connect(docs.path)
    try:
        jobs = MathJobs(reviews)
        out["jobs"] = {}
        for (check,) in db.execute("SELECT check_id FROM math_jobs ORDER BY rowid").fetchall():
            job = safe(lambda c=check: jobs.job(c))
            if "error" in job:
                out["jobs"][check] = job
                continue
            out["jobs"][check] = {
                "macro_context": job["macro_context"],
                "effective": safe(lambda j=job: effective(jobs, j)),
            }
        out["listing"] = safe(lambda: jobs.listing("p"))
        revisions = db.execute("SELECT document_id,number FROM revisions ORDER BY rowid").fetchall()
        out["views"] = {
            f"{d}@{n}": {
                "historical": safe(
                    lambda d=d, n=n: view(docs, "p", document=d, revision=n, historical=True)
                ),
                "current": safe(lambda d=d, n=n: view(docs, "p", document=d, revision=n)),
            }
            for d, n in revisions
        }
        bodies = db.execute(
            "SELECT subject_id,revision,parent,parent_revision FROM text_revisions "
            "WHERE subject_type='response' ORDER BY rowid"
        ).fetchall()
        out["responses"] = {
            f"{source}#{revision}": {
                "historical": safe(
                    lambda s=source, r=revision: response_view(docs, "p", s, body_revision=r)
                ),
                "current": safe(lambda s=source: response_view(docs, "p", s)),
                "api": safe(
                    lambda f=finding, d=decision, r=revision: ResponseBodies(reviews).get(
                        "p", "s", f, d, body_revision=r
                    )
                ),
            }
            for source, revision, finding, decision in bodies
        }
        out["findings"] = {}
        for listed in Findings(reviews).listing("p", "s")["findings"]:
            full = Findings(reviews).get("p", "s", listed["finding_id"])
            contexts = safe(
                lambda full=full: finding_macro_contexts(reviews, "p", full).model_dump(mode="json")
            )
            out["findings"][listed["finding_id"]] = {"finding": full, "contexts": contexts}
        out["confirmation_targets"] = safe(lambda: reviews.confirmation_targets("p", "s"))
        out["runs"] = {
            review: safe(lambda r=review: reviews.run("p", "s", r))
            for (review,) in db.execute(
                "SELECT review_id FROM review_runs ORDER BY rowid"
            ).fetchall()
        }
        out["notices"] = safe(
            lambda: sorted(
                n["notice_id"] for n in MathNotices(reviews).pending("p", "s", "answers", set())
            )
        )
    finally:
        db.close()
    return json.loads(json.dumps(out, default=str))
