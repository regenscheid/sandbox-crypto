# SPDX-License-Identifier: MIT
"""Figure checks of the current revisions of documents and ledger statements (FG-19 to
FG-21).

``subscribe`` records, in each save's transaction and with SQL only, that a document or
a ledger entry changed (``figure_sources``); it never checks, and never refuses a save.
``FigureChecks.drain``, which the run service calls as it looks at the control store,
checks each such text at its current revision: each ``vega-lite`` fence through the
figure checker (``figurecheck``), with its pinned data's bytes, and each reference to a
recorded file by its pin. The results replace the text's earlier ones in
``figure_checks``, by the block (or the statement) and the figure's place in it, so only
the current revision has results. A figure whose fence, data and checker are unchanged
keeps its result. Without a configured checker, charts are ``unavailable``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sandbox_science.contracts.artifact_inventory import ArtifactLink

from .aio import threaded
from .artifacts import ArtifactInventory
from .control import ControlEvent, Tx
from .control.texts import TextRevisions
from .documents.blocks import Parsed, normalize_newlines, parse
from .documents.engine import attach
from .documents.errors import DocumentError
from .documents.pins import CHART, chart_data, fence_body
from .documents.store import DocumentStore
from .figurecheck import (
    MAX_BATCH,
    MAX_DATA,
    Chart,
    FigureCheck,
    FigureChecker,
    Problem,
    identity,
)

_log = logging.getLogger(__name__)

Json = dict[str, Any]

SAVES: Final = ("document.revision_created", "ledger.entry_created", "ledger.entry_revised")
DRAWN: Final = frozenset(
    {"image/png", "image/jpeg", "image/svg+xml", "application/pdf", "text/html"}
)
"""The types the GUI draws (FG-12); any other shows as a link."""
MAX_IMAGE: Final = 8 * 1024 * 1024
TEXTS_PER_DRAIN: Final = 4
CHARTS_PER_TEXT: Final = 32
"""Charts one text's check sends to the checker; the rest are unavailable."""
TEXT_BYTES: Final = 64 * 1024 * 1024
"""Data one text's check reads, each SHA-256 once a batch (security review of #514)."""
DRAIN_BYTES: Final = 64 * 1024 * 1024
"""Data after which a drain takes no further text."""
MAX_PATH_SHOWN: Final = 200
RETRIES: Final = 4
BACKOFF: Final = 60.0
"""Seconds before the first retry of a check the checker left unavailable."""
TRANSIENT: Final = "figure checker"
"""How the reasons of the checker's own trouble begin: a deadline, a crash, a bad answer."""
UNREADABLE: Final = "figure checker build unreadable"
TOO_MANY: Final = f"the text has more than {CHARTS_PER_TEXT} charts to check; not checked"
TOO_MUCH_DATA: Final = "the text's charts read more data than one check reads; not checked"
UNREAD: Final = "its data could not be read"
FAILED: Final = "the figure check failed"
_REASONS: Final = {
    "no_file": "no recorded file at that path",
    "deleted": "the file was deleted",
    "content_not_kept": "its bytes were not kept",
    "needs_session": "a relative path needs a session",
}
STATEMENTS: Final = TextRevisions("statement")


def subscribe(documents: DocumentStore) -> None:
    """Schedule a figure check of every document and ledger entry saved through
    ``documents``."""
    for kind in SAVES:
        documents.control.subscribe(kind, _record)


def _record(tx: Tx, event: ControlEvent) -> None:
    if event.document is not None:
        subject = ("document", event.document)
    elif isinstance(entry := event.payload.get("entry"), str):
        subject = ("statement", entry)
    else:
        return
    tx.sql.execute(
        "INSERT INTO figure_sources(subject_type,subject_id,project,seq) VALUES(?,?,?,?) "
        "ON CONFLICT(subject_type,subject_id) DO UPDATE SET seq=excluded.seq,tries=0,after=0",
        (*subject, event.project, event.seq),
    )


@dataclass
class _Figure:
    place: str
    """The block's ID, or ``statement``."""
    figure: str
    """``chart:<n>`` or ``file:<n>``: its kind and its place among them there."""
    digest: str
    path: str | None = None
    chart: Chart | None = None
    pins: list[Json] = field(default_factory=list[Json])
    result: FigureCheck | None = None


@dataclass
class _Text:
    kind: str
    subject: str
    project: str
    revision: int
    seq: int
    figures: list[_Figure]


class FigureChecks:
    """The figure checks of one control store; ``checker`` is the configured one."""

    def __init__(
        self,
        documents: DocumentStore,
        checker: FigureChecker | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.documents, self.checker, self.clock = documents, checker, clock
        self.inventory = ArtifactInventory(documents)

    async def drain(self) -> int:
        """Check the figures of up to ``TEXTS_PER_DRAIN`` saved texts that are due, while
        the drain has read less than ``DRAIN_BYTES`` of data; how many. A text whose check
        fails is logged and dropped, never retried each look (security review of #514)."""
        if self.documents.control.missing or not (rows := await threaded(self._due)):
            return 0
        checker, version = self.checker, "none"
        if checker is not None:
            try:
                version = await threaded(checker.version)
            except (OSError, ValueError):
                checker = None
        why = "no figure checker is configured" if self.checker is None else UNREADABLE
        read = done = 0
        for row in rows:
            if read >= DRAIN_BYTES:
                break
            text: _Text | None = None
            try:
                text = await threaded(self._load, version, *row)
                read += await self._check(text, checker, why)
                await threaded(self._save, text)
            except Exception:  # one text's failure must not hold up the others
                _log.exception("the figure check of %s %s failed", row[0], row[1])
                await threaded(self._failed, row, text)
            done += 1
        return done

    async def _check(self, text: _Text, checker: FigureChecker | None, why: str) -> int:
        """Check a text's charts that need it, at most ``CHARTS_PER_TEXT``, a batch at a
        time, each batch's data read just before it is sent, once for each SHA-256, and
        dropped after; the bytes read."""
        waiting = [f for f in text.figures if f.result is None and f.chart is not None]
        for item in waiting[CHARTS_PER_TEXT:]:
            item.result = FigureCheck(status="unavailable", reason=TOO_MANY)
        read = 0
        for start in range(0, min(len(waiting), CHARTS_PER_TEXT), MAX_BATCH):
            batch = waiting[start : start + MAX_BATCH]
            if checker is None:
                for item in batch:
                    item.result = FigureCheck(status="unavailable", reason=why)
                continue
            shared: dict[str, str] = {}
            for item in batch:
                read += await threaded(self._data, text.project, item, shared, TEXT_BYTES - read)
            ready = [f for f in batch if f.result is None and f.chart is not None]
            charts = [f.chart for f in ready if f.chart is not None]
            for item, result in zip(ready, await checker.check_many(charts), strict=True):
                item.result = result
            for item in batch:
                item.chart = None  # its data go with it
        return read

    def _due(self) -> list[tuple[str, str, str, int]]:
        with self.documents.control.read() as tx:
            return tx.sql.execute(
                "SELECT subject_type,subject_id,project,seq FROM figure_sources "
                "WHERE after<=? ORDER BY seq LIMIT ?",
                (self.clock(), TEXTS_PER_DRAIN),
            ).fetchall()

    def _load(self, version: str, kind: str, subject: str, project: str, seq: int) -> _Text:
        with self.documents.control.read() as tx:
            return self._text(tx.sql, version, kind, subject, project, seq)

    def backfill(self) -> int:
        """Record the documents and ledger entries whose current revision may hold
        figures and has no checks, as those saved before figure checks were built
        (control store version 57 has no backfill); how many. The run service does
        this as it starts."""
        if self.documents.control.missing:
            return 0
        with self.documents.control.write() as tx:
            documents = tx.sql.execute(
                "INSERT INTO figure_sources(subject_type,subject_id,project,seq) "
                "SELECT 'document',d.document_id,d.project_id,0 FROM documents d "
                "JOIN revisions r ON r.document_id=d.document_id AND r.number=d.head "
                "WHERE r.tombstone=0 AND (r.pins IS NOT NULL OR instr(r.text,'vega-lite')) "
                "AND NOT EXISTS(SELECT 1 FROM figure_checks c WHERE c.subject_type='document' "
                "AND c.subject_id=d.document_id AND c.revision=d.head) "
                "ON CONFLICT DO NOTHING"
            ).rowcount
            statements = tx.sql.execute(
                "INSERT INTO figure_sources(subject_type,subject_id,project,seq) "
                "SELECT 'statement',e.entry_id,e.project,0 FROM ledger_entries e "
                "JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                "JOIN text_revisions t ON t.subject_type='statement' "
                "AND t.subject_id=e.entry_id AND t.revision=r.statement "
                "WHERE (r.pins IS NOT NULL OR instr(t.body,'vega-lite')) "
                "AND NOT EXISTS(SELECT 1 FROM figure_checks c WHERE c.subject_type='statement' "
                "AND c.subject_id=e.entry_id AND c.revision=e.head) "
                "ON CONFLICT DO NOTHING"
            ).rowcount
        return documents + statements

    def _text(
        self, db: Any, version: str, kind: str, subject: str, project: str, seq: int
    ) -> _Text:
        figures: list[_Figure] = []
        found = (
            db.execute(
                "SELECT r.number,r.text,r.block_map,r.pins,r.tombstone FROM documents d "
                "JOIN revisions r ON r.document_id=d.document_id AND r.number=d.head "
                "WHERE d.document_id=?",
                (subject,),
            ).fetchone()
            if kind == "document"
            else db.execute(
                "SELECT r.revision,r.statement,NULL,r.pins,0 FROM ledger_entries e "
                "JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                "WHERE e.entry_id=?",
                (subject,),
            ).fetchone()
        )
        revision = 0 if found is None else found[0]
        if found is not None and not found[4]:
            pins: list[Json] = json.loads(found[3] or "[]")
            try:
                if kind == "document":
                    mapped = attach(found[1], json.loads(found[2]))
                    parsed, places = mapped.parsed, list(mapped.block_ids)
                else:
                    saved = STATEMENTS.read(db, subject, found[1], full=False)
                    text = "" if saved is None else str(saved["body"])
                    parsed = parse(normalize_newlines(text), documents=False)
                    places = ["statement"] * len(parsed.blocks)
            except DocumentError:
                parsed, places = None, []
            if parsed is not None:
                figures = _figures(parsed, places, pins, version)
        known = {
            (place, figure, digest): FigureCheck.model_validate_json(result)
            for place, figure, digest, result in db.execute(
                "SELECT place,figure,input_sha256,result FROM figure_checks "
                "WHERE subject_type=? AND subject_id=? AND status!='unavailable'",
                (kind, subject),
            )
        }
        for item in figures:
            item.result = item.result or known.get((item.place, item.figure, item.digest))
        return _Text(kind, subject, project, revision, seq, figures)

    def _data(self, project: str, item: _Figure, shared: dict[str, str], room: int) -> int:
        """Read a chart's pinned data, each file once for its SHA-256 in ``shared``, within
        ``room`` bytes, or give its result without the checker; the bytes read."""
        assert item.chart is not None  # noqa: S101 - only charts are read
        new = {str(entry["sha256"]): int(entry.get("size", 0)) for entry in item.pins}
        needed = sum(size for sha, size in new.items() if sha not in shared)
        if needed > room:
            item.result = FigureCheck(status="unavailable", reason=TOO_MUCH_DATA)
            return 0
        read = 0
        for entry in item.pins:
            if (known := shared.get(str(entry["sha256"]))) is not None:
                item.chart.files[str(entry["path"])] = known
                continue
            try:
                revision = self.inventory.resolve(
                    project,
                    ArtifactLink(
                        artifact_id=entry["artifact"],
                        project_id=project,
                        mode="pinned",
                        revision=entry["revision"],
                        sha256=entry["sha256"],
                    ),
                )
                data = self.inventory.content(project, revision.reference, max_bytes=MAX_DATA)
            except (DocumentError, ValueError):
                item.result = FigureCheck(status="unavailable", reason=UNREAD)
                return read
            read += len(data)
            text = shared[str(entry["sha256"])] = data.decode("utf-8", "replace")
            item.chart.files[str(entry["path"])] = text
        return read

    def _save(self, text: _Text) -> None:
        now = datetime.now(UTC).isoformat()
        with self.documents.control.write() as tx:
            db = tx.sql
            head = db.execute(
                "SELECT head FROM documents WHERE document_id=?"
                if text.kind == "document"
                else "SELECT head FROM ledger_entries WHERE entry_id=?",
                (text.subject,),
            ).fetchone()
            if head is not None and head[0] != text.revision:
                return  # a newer revision was saved since: its own source checks it
            db.execute(
                "DELETE FROM figure_checks WHERE subject_type=? AND subject_id=?",
                (text.kind, text.subject),
            )
            for item in text.figures:
                assert item.result is not None  # noqa: S101 - every figure has one by now
                db.execute(
                    "INSERT INTO figure_checks(subject_type,subject_id,project,revision,place,"
                    "figure,path,input_sha256,status,result,checked) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        text.kind,
                        text.subject,
                        text.project,
                        text.revision,
                        item.place,
                        item.figure,
                        item.path,
                        item.digest,
                        item.result.status,
                        item.result.model_dump_json(exclude={"id"}),
                        now,
                    ),
                )
            # A check the checker's own trouble left unavailable is tried again later,
            # at most RETRIES times, each wait four times the last; else the text is done.
            again = any(
                item.result is not None and (item.result.reason or "").startswith(TRANSIENT)
                for item in text.figures
            )
            source = (text.kind, text.subject, text.seq)
            if (
                not again
                or not db.execute(
                    "UPDATE figure_sources SET tries=tries+1,after=?*(1<<(2*tries))+? "
                    "WHERE subject_type=? AND subject_id=? AND seq=? AND tries<?",
                    (BACKOFF, self.clock(), *source, RETRIES),
                ).rowcount
            ):
                db.execute(
                    "DELETE FROM figure_sources WHERE subject_type=? AND subject_id=? AND seq=?",
                    source,
                )

    def _failed(self, row: tuple[str, str, str, int], text: _Text | None) -> None:
        """Record a text whose check failed: its figures without a result unavailable,
        when it was read, and its source removed, so it is not tried at every look."""
        if text is not None:
            for item in text.figures:
                if item.result is None:
                    item.result = FigureCheck(status="unavailable", reason=FAILED)
            try:
                self._save(text)
            except Exception:  # its record failed too: drop the source alone
                _log.exception("the figure check of %s %s was not recorded", row[0], row[1])
        with self.documents.control.write() as tx:
            tx.sql.execute(
                "DELETE FROM figure_sources WHERE subject_type=? AND subject_id=? AND seq=?",
                (row[0], row[1], row[3]),
            )


def results(db: sqlite3.Connection, kind: str, subject: str, revision: int) -> list[Json]:
    """The results of a text revision's figure checks, as the GUI shows them beside each
    figure (FG-23): its place, figure and, for a file, its path, with the result. A
    revision not checked, or no longer current, has none."""
    return [
        {"place": place, "figure": figure, "path": path, **json.loads(result)}
        for place, figure, path, result in db.execute(
            "SELECT place,figure,path,result FROM figure_checks WHERE subject_type=? "
            "AND subject_id=? AND revision=? ORDER BY place,figure",
            (kind, subject, revision),
        )
    ]


def shown(documents: DocumentStore, kind: str, subject: str, revision: int) -> list[Json]:
    """``results`` in a read of its own, for a reader with no transaction open."""
    with documents.control.read() as tx:
        return results(tx.sql, kind, subject, revision)


def _shown(path: object) -> str:
    """A path as a message names it: at most ``MAX_PATH_SHOWN`` characters."""
    text = str(path)
    return text if len(text) <= MAX_PATH_SHOWN else text[:MAX_PATH_SHOWN] + "…"


def _figures(parsed: Parsed, places: list[str], pins: list[Json], version: str) -> list[_Figure]:
    """A text's charts and file references, with the results that need no checker."""
    figures: list[_Figure] = []
    count: Counter[tuple[str, str]] = Counter()

    def numbered(place: str, kind: str) -> str:
        count[(place, kind)] += 1
        return f"{kind}:{count[(place, kind)] - 1}"

    for block in parsed.blocks:
        place = places[block.index]
        if block.kind != "code" or str(block.attrs.get("info") or "").split(" ", 1)[0] != CHART:
            continue
        source = fence_body(block.text)
        paths = set(chart_data(block.text))
        held = [p for p in pins if p["block"] == place and p["kind"] == "chart_data"]
        held = [p for p in held if p["path"] in paths]
        data = {str(p["path"]): str(p.get("sha256", p.get("unresolved"))) for p in held}
        figure = numbered(place, "chart")
        item = _Figure(place, figure, identity(source, data, version), pins=held)
        item.chart = Chart(id=f"c{len(figures)}", source=source[:65_536])
        errors = [
            Problem(
                kind="unresolved_reference",
                message=f"{_shown(p['path'])} is not pinned: "
                f"{_REASONS.get(p['unresolved'], 'unknown')}",
            )
            for p in held
            if "unresolved" in p
        ]
        if not errors and sum(int(p.get("size", 0)) for p in held) > MAX_DATA:
            errors.append(Problem(kind="size", message="the chart's data exceed 8 MiB in all"))
        if errors:
            item.result = FigureCheck(id=item.chart.id, status="rejected", errors=errors)
        figures.append(item)
    for entry in pins:
        if entry["kind"] != "image":
            continue
        place = str(entry["block"])
        digest = identity(json.dumps(entry, sort_keys=True), {}, "file")
        item = _Figure(place, numbered(place, "file"), digest, path=str(entry["path"]))
        if "unresolved" in entry:
            reason = _REASONS.get(entry["unresolved"], "unknown")
            problem = Problem(
                kind="unresolved_reference",
                message=f"{_shown(item.path)} is not pinned: {reason}",
            )
            item.result = FigureCheck(id="file", status="rejected", errors=[problem])
        else:
            notes: list[Problem] = []
            if entry.get("media_type") not in DRAWN or int(entry.get("size", 0)) > MAX_IMAGE:
                notes.append(
                    Problem(kind="link", message=f"{_shown(item.path)} shows as a link, not drawn")
                )
            item.result = FigureCheck(id="file", status="accepted", notes=notes)
        figures.append(item)
    return figures
