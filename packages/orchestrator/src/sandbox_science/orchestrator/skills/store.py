# SPDX-License-Identifier: MIT
"""Imported skill packs and the projects' pins of them, in the control store
(docs/specs/skills.md, SK-1 to SK-6, SK-32; docs/SKILLS.md).

An import keeps a snapshot, written once and shared by imports of the same files, and a
record with its report. A project pins one import of each source it uses, and a pin is
refused when one of its skills has the name of a skill of another pinned source (SK-4).
Only the user imports and pins.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents.models import Author

from .packs import NAME, Snapshot

REPORT_ITEMS: Final = 200
"""Items a report lists of each kind; it counts the rest."""
_IMPORT: Final = (
    "SELECT import_id,source,snapshot,kind,origin,ref,commit_id,archive_sha256,root,report,"
    "created FROM skill_imports"
)


@dataclass(frozen=True)
class Imported:
    import_id: int
    source: str
    snapshot: str
    kind: str
    origin: str
    ref: str | None
    commit: str | None
    archive_sha256: str | None
    root: str
    report: dict[str, Any]
    created: str


@dataclass(frozen=True)
class Pin:
    project: str
    source: str
    revision: int
    pinned: Imported
    skills: tuple[str, ...]


class SkillStore:
    """The imported packs and the projects' pins in one control store."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def add(self, source: str, snapshot: Snapshot, author: Author) -> tuple[Imported, bool]:
        """Keep an import of ``source``; ``True`` when its latest import is already this
        one, from the same place at the same revision, and nothing was added."""
        _source(source)
        got = snapshot.fetched
        with self.control.write() as tx:
            db = tx.sql
            latest = next(iter(_imports(db, source)), None)
            here = (snapshot.sha256, got.kind, got.origin, got.ref, got.commit)
            if latest is not None and here == (
                latest.snapshot, latest.kind, latest.origin, latest.ref, latest.commit
            ):  # fmt: skip
                return latest, True
            _keep(db, snapshot)
            report: dict[str, Any] = {
                "skills": [skill.name for skill in snapshot.skills],
                "excluded": _capped(snapshot.excluded),
                "skipped": _capped(got.skipped),
            }
            if got.default_branch is not None:
                report["default_branch"] = got.default_branch
            if latest is not None:
                report["changes"] = {"since": latest.import_id, **_changes(db, latest, snapshot)}
            row = (*here[:3], got.ref, got.commit, got.archive_sha256, got.root)
            cursor = db.execute(
                "INSERT INTO skill_imports(snapshot,kind,origin,ref,commit_id,archive_sha256,"
                "root,source,report,author,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (*row, source, json.dumps(report), author.model_dump_json(), _now()),
            )
            return _imports(db, import_id=cast(int, cursor.lastrowid))[0], False

    def imports(self, source: str | None = None) -> list[Imported]:
        """Every import, or ``source``'s, the latest first."""
        if self.control.missing:
            return []
        with self.control.read() as tx:
            return _imports(tx.sql, source)

    def pin(self, project: str, source: str, author: Author, revision: str | None = None) -> Pin:
        """Pin ``source``'s latest import in ``project``, or its import whose commit,
        archive or snapshot starts with ``revision`` (at least 7 hexadecimal digits)."""
        _source(source)
        if revision is not None and not re.fullmatch(r"[0-9a-f]{7,64}", revision):
            raise ControlError(INVALID_REQUEST, "name a revision by at least 7 hex digits")
        with self.control.write() as tx:
            db = tx.sql
            if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project,)).fetchone():
                raise ControlError(NOT_FOUND, f"there is no project {project!r}")
            chosen = next(
                (
                    found
                    for found in _imports(db, source)
                    if revision is None
                    or any(
                        name and name.startswith(revision)
                        for name in (found.commit, found.archive_sha256, found.snapshot)
                    )
                ),
                None,
            )
            if chosen is None:
                raise ControlError(NOT_FOUND, f"no import of {source} at {revision or 'all'}")
            names = set(_names(db, chosen.snapshot))
            for other, snapshot in db.execute(
                "SELECT p.source,i.snapshot FROM project_skill_pins p JOIN skill_imports i "
                "USING(import_id) WHERE p.project=? AND p.source!=?",
                (project, source),
            ).fetchall():
                if taken := sorted(names & set(_names(db, snapshot))):
                    raise ControlError(
                        CONFLICT,
                        f"{', '.join(taken)} already defined by {other}, which the project pins",
                        {"source": other, "names": taken},
                    )
            db.execute(
                "INSERT INTO project_skill_pins VALUES(?,?,?,1,?,?) ON CONFLICT(project,source) "
                "DO UPDATE SET import_id=excluded.import_id,revision=revision+1,"
                "author=excluded.author,pinned=excluded.pinned WHERE import_id!=excluded.import_id",
                (project, source, chosen.import_id, author.model_dump_json(), _now()),
            )
            return _pins(db, project, source)[0]

    def pins(self, project: str) -> list[Pin]:
        """The project's pins, by source."""
        if self.control.missing:
            return []
        with self.control.read() as tx:
            return _pins(tx.sql, project)


def _source(source: str) -> None:
    if not NAME.fullmatch(source):
        raise ControlError(
            INVALID_REQUEST, "a source's name is lowercase letters, digits and hyphens"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _capped(items: tuple[dict[str, str], ...]) -> list[dict[str, str]]:
    listed = list(items[:REPORT_ITEMS])
    if len(items) > REPORT_ITEMS:
        listed.append({"path": "", "reason": f"{len(items) - REPORT_ITEMS} more"})
    return listed


def _keep(db: sqlite3.Connection, snapshot: Snapshot) -> None:
    """The snapshot's files and skills, unless an earlier import kept them."""
    files, name = snapshot.fetched.files, snapshot.sha256
    if db.execute("SELECT 1 FROM skill_snapshots WHERE snapshot=?", (name,)).fetchone():
        return
    size = sum(len(file.content) for file in files)
    db.execute("INSERT INTO skill_snapshots VALUES(?,?,?)", (name, len(files), size))
    for file in files:
        digest = hashlib.sha256(file.content).hexdigest()
        db.execute("INSERT OR IGNORE INTO skill_blobs VALUES(?,?)", (digest, file.content))
        row = (name, file.path, digest, int(file.executable))
        db.execute("INSERT INTO skill_files VALUES(?,?,?,?)", row)
    for skill in snapshot.skills:
        row = (name, skill.name, skill.description, skill.body_sha256, json.dumps(skill.metadata))
        db.execute("INSERT INTO skill_skills VALUES(?,?,?,?,?)", row)


def _changes(db: sqlite3.Connection, latest: Imported, snapshot: Snapshot) -> dict[str, list[str]]:
    """The skills added, removed and changed since ``latest``: a skill changed when any
    file in its directory did."""
    before = _by_skill(
        db.execute(
            "SELECT path,blob,executable FROM skill_files WHERE snapshot=?", (latest.snapshot,)
        ).fetchall()
    )
    after = _by_skill(
        [
            (f.path, hashlib.sha256(f.content).hexdigest(), int(f.executable))
            for f in snapshot.fetched.files
        ]
    )
    old, new = set(_names(db, latest.snapshot)), {skill.name for skill in snapshot.skills}
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
        "changed": sorted(name for name in new & old if before[name] != after[name]),
    }


def _by_skill(rows: list[tuple[str, str, int]]) -> dict[str, set[tuple[str, str, int]]]:
    found: dict[str, set[tuple[str, str, int]]] = {}
    for row in rows:
        found.setdefault(row[0].split("/", 1)[0], set()).add(row)
    return found


def _names(db: sqlite3.Connection, snapshot: str) -> list[str]:
    rows = db.execute("SELECT name FROM skill_skills WHERE snapshot=? ORDER BY name", (snapshot,))
    return [row[0] for row in rows.fetchall()]


def _imports(
    db: sqlite3.Connection, source: str | None = None, *, import_id: int | None = None
) -> list[Imported]:
    rows = db.execute(
        f"{_IMPORT} WHERE (? IS NULL OR source=?) AND (? IS NULL OR import_id=?) "
        "ORDER BY import_id DESC",
        (source, source, import_id, import_id),
    ).fetchall()
    return [
        Imported(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], json.loads(r[9]), r[10])
        for r in rows
    ]


def _pins(db: sqlite3.Connection, project: str, source: str | None = None) -> list[Pin]:
    rows = db.execute(
        "SELECT source,revision,import_id FROM project_skill_pins WHERE project=? "
        "AND (? IS NULL OR source=?) ORDER BY source",
        (project, source, source),
    ).fetchall()
    pins: list[Pin] = []
    for name, revision, import_id in rows:
        imported = _imports(db, import_id=import_id)[0]
        pins.append(Pin(project, name, revision, imported, tuple(_names(db, imported.snapshot))))
    return pins
