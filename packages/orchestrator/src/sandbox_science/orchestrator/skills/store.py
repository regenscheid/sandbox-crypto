# SPDX-License-Identifier: MIT
"""Imported skill packs and the projects' pins of them, in the control store
(docs/specs/skills.md, SK-1 to SK-6, SK-32; docs/SKILLS.md).

An import keeps a snapshot, written once and shared by imports of the same files, and a
record with its report. A project pins one import of each source it uses, and a pin is
refused when one of its skills has the name of a skill of another pinned source (SK-4),
unless the other is the built-in skills, which give way. The user imports and pins packs;
the harness imports and pins the built-in skills it ships, as the platform (SK-40).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents.models import Author

from .builtin import BUILTIN, DELEGATING, PLATFORM, ROLES
from .packs import NAME, Snapshot, body, hidden, served, snapshot
from .sources import Fetched

REPORT_ITEMS: Final = 200
"""Items a report lists of each kind; it counts the rest."""
MAX_LOADED: Final = 100_000
"""Characters of skill text one run is given, those loaded at its start included (SK-39)."""
MAX_ALWAYS: Final = 20
MAX_EXCLUDED: Final = 500
SETTINGS_BYTES: Final = {"always": 16384, "excluded": 65536}
"""The settings' lists as JSON, at most as the control store keeps them (version 41)."""
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
    left_out: tuple[str, ...] = ()
    """For the built-in skills, those a pinned pack's skill of the same name replaces."""


@dataclass(frozen=True)
class Entry:
    """One skill of a run's set."""

    source: str
    name: str
    description: str
    body_sha256: str
    snapshot: str
    revision: str
    """The import's commit or archive digest."""

    @property
    def qualified(self) -> str:
        return f"{self.source}/{self.name}"


@dataclass(frozen=True)
class SkillSet:
    """The skills a run is given (SK-5): the project's pinned skills it does not exclude,
    the sources' revisions, and the skills it loads in full at every run's start."""

    entries: tuple[Entry, ...]
    sources: tuple[str, ...]
    """Each source as ``<source>@<revision>``."""
    always: tuple[str, ...]


@dataclass(frozen=True)
class SkillSettings:
    """A project's skill settings at a revision (0: never changed)."""

    project: str
    revision: int
    always: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


class SkillStore:
    """The imported packs and the projects' pins in one control store."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def add(self, source: str, snapshot: Snapshot, author: Author) -> tuple[Imported, bool]:
        """Keep an import of ``source``; ``True`` when its latest import is already this
        one, from the same place at the same revision, and nothing was added."""
        _source(source)
        got = snapshot.fetched
        if (source == BUILTIN) != (got.kind == BUILTIN):
            raise ControlError(INVALID_REQUEST, f"{BUILTIN!r} names the built-in skills only")
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
            if marked := hidden(got.files):
                # Shown as code points wherever a run is given them (SK-14).
                report["hidden_characters"] = _capped(marked)
            warnings = list(got.warnings)
            if latest is not None and latest.origin != got.origin:
                warnings.append(f"{source} was imported before from {latest.origin}")
            if warnings:
                report["warnings"] = warnings[:REPORT_ITEMS]
            if latest is not None:
                report["changes"] = {"since": latest.import_id, **_changes(db, latest, snapshot)}
            if source == BUILTIN:
                # Each project's built-in skills that a pinned pack's of the same name
                # leaves out of its runs (SK-4).
                names = {skill.name for skill in snapshot.skills}
                left = {
                    project: found
                    for (project,) in db.execute(
                        "SELECT DISTINCT project FROM project_skill_pins WHERE source!=? "
                        "ORDER BY project",
                        (BUILTIN,),
                    ).fetchall()
                    if (found := _displaced(db, project, names))
                }
                if left:
                    report["left_out"] = left
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
        archive or snapshot starts with ``revision`` (at least 12 hexadecimal digits); a
        prefix that names imports of different snapshots is refused."""
        _source(source)
        if revision is not None and not re.fullmatch(r"[0-9a-f]{12,64}", revision):
            raise ControlError(INVALID_REQUEST, "name a revision by at least 12 hex digits")
        with self.control.write() as tx:
            db = tx.sql
            if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project,)).fetchone():
                raise ControlError(NOT_FOUND, f"there is no project {project!r}")
            matches = [
                found
                for found in _imports(db, source)
                # The built-in source pins built-in imports only (a K1-era pack of that
                # name was renamed by version 42).
                if (found.kind == BUILTIN) == (source == BUILTIN)
                and (
                    revision is None
                    or any(
                        name and name.startswith(revision)
                        for name in (found.commit, found.archive_sha256, found.snapshot)
                    )
                )
            ]
            if not matches:
                raise ControlError(NOT_FOUND, f"no import of {source} at {revision or 'all'}")
            if revision is not None and len({found.snapshot for found in matches}) > 1:
                raise ControlError(
                    CONFLICT, f"{revision} names more than one import of {source}: give more digits"
                )
            chosen = matches[0]
            names = set(_names(db, chosen.snapshot))
            settings = _settings(db, project)
            excluded = set(settings.excluded)
            for other, snapshot in db.execute(
                "SELECT p.source,i.snapshot FROM project_skill_pins p JOIN skill_imports i "
                "USING(import_id) WHERE p.project=? AND p.source!=?",
                (project, source),
            ).fetchall():
                taken = sorted(names & set(_names(db, snapshot)))
                if source == BUILTIN:
                    continue  # a built-in skill gives way to a pinned pack's (SK-4)
                if other == BUILTIN:
                    taken = [name for name in taken if f"{BUILTIN}/{name}" not in excluded]
                    if taken:
                        hint = " ".join(f"--exclude {BUILTIN}/{name}" for name in taken)
                        then = sorted(excluded | {f"{BUILTIN}/{name}" for name in taken})
                        raise ControlError(
                            CONFLICT,
                            f"{', '.join(taken)} already defined by the built-in skills: "
                            f"exclude them first: skills settings --project {project} "
                            f"--expected {settings.revision} {hint} (the project then "
                            f"excludes {', '.join(then)})",
                            {"source": BUILTIN, "names": taken},
                        )
                if taken:
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

    def effective(
        self,
        project: str,
        role: str | None = None,
        *,
        delegates: bool = True,
        excluded: Collection[str] | None = None,
    ) -> SkillSet:
        """The skills a run of ``project`` is given now, or, with ``excluded``, would be
        given with those excluded instead of the settings'. A run of ``role`` gets the
        built-in skills ``ROLES`` gives it, with no role all of them, and a run that cannot
        delegate (``delegates``) none that teach delegating. Each description is served as
        the bodies are (``served``)."""
        if self.control.missing:
            return SkillSet((), (), ())
        with self.control.read() as tx:
            db = tx.sql
            settings = _settings(db, project)
            if excluded is not None:
                settings = replace(settings, excluded=tuple(excluded))
            entries: list[Entry] = []
            sources: list[str] = []
            for pin in _pins(db, project):
                found = pin.pinned
                revision = found.commit or found.archive_sha256 or found.ref or found.snapshot
                sources.append(f"{pin.source}@{revision}")
                for name, description, body in db.execute(
                    "SELECT name,description,body_sha256 FROM skill_skills WHERE snapshot=? "
                    "ORDER BY name",
                    (found.snapshot,),
                ):
                    entry = Entry(
                        pin.source, name, served(description), body, found.snapshot, revision
                    )
                    roles = ROLES.get(name, ("main",)) if pin.source == BUILTIN else None
                    if entry.qualified in settings.excluded or (
                        roles is not None
                        and (
                            (role is not None and role not in roles)
                            or (not delegates and name in DELEGATING)
                        )
                    ):
                        continue
                    entries.append(entry)
        packs = {entry.name for entry in entries if entry.source != BUILTIN}
        entries = [e for e in entries if e.source != BUILTIN or e.name not in packs]
        given = {entry.qualified for entry in entries}
        always = tuple(skill for skill in settings.always if skill in given)
        return SkillSet(tuple(entries), tuple(sources), always)

    def ensure(self, project: str, fetched: Fetched | None) -> None:
        """Pin in ``project`` the built-in skills the harness ships (``fetched``), and
        import them first when they changed (SK-40)."""
        if fetched is None:
            return
        shipped = snapshot(fetched)
        latest = next(iter(self.imports(BUILTIN)), None)
        if latest is None or (latest.snapshot, latest.ref) != (shipped.sha256, fetched.ref):
            latest, _ = self.add(BUILTIN, shipped, PLATFORM)
        pinned = {pin.source: pin.pinned.import_id for pin in self.pins(project)}
        if pinned.get(BUILTIN) != latest.import_id:
            self.pin(project, BUILTIN, PLATFORM)

    def file(self, snapshot: str, path: str) -> bytes | None:
        """A snapshot's file, by its path in the snapshot; ``None`` if it has none."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT b.content FROM skill_files f JOIN skill_blobs b ON b.sha256=f.blob "
                "WHERE f.snapshot=? AND f.path=?",
                (snapshot, path),
            ).fetchone()
        return None if row is None else bytes(row[0])

    def served_body(self, entry: Entry) -> str:
        """A skill's body as a run is given it (``served``)."""
        content = self.file(entry.snapshot, f"{entry.name}/SKILL.md") or b""
        return served(body(content.decode(errors="replace")))

    def skills(self, snapshot: str) -> list[tuple[str, str]]:
        """A snapshot's skills' names and descriptions."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT name,description FROM skill_skills WHERE snapshot=? ORDER BY name",
                (snapshot,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def vectors(self, snapshot: str, model: str, dimensions: int) -> dict[str, list[float]]:
        """A snapshot's skills' vectors by ``model`` in ``dimensions`` (SK-29)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT name,vector FROM skill_vectors WHERE snapshot=? AND model=? "
                "AND dimensions=?",
                (snapshot, model, dimensions),
            ).fetchall()
        return {row[0]: unpack(bytes(row[1])) for row in rows}

    def add_vectors(
        self, snapshot: str, model: str, dimensions: int, named: dict[str, list[float]]
    ) -> None:
        """Keep the vectors, unless an earlier call kept them; a vector the store refuses
        is an error, never passed over."""
        with self.control.write() as tx:
            for name, vector in named.items():
                tx.sql.execute(
                    "INSERT INTO skill_vectors VALUES(?,?,?,?,?) "
                    "ON CONFLICT(snapshot,model,dimensions,name) DO NOTHING",
                    (snapshot, model, dimensions, name, pack(vector)),
                )

    def saw_space(self, snapshot: str, alias: str, model: str, dimensions: int) -> None:
        """``alias`` answered in this space for ``snapshot``, whose vectors there are kept."""
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO skill_vector_spaces VALUES(?,?,?,?,?) ON CONFLICT(snapshot,alias,"
                "model,dimensions) DO UPDATE SET seen=excluded.seen",
                (snapshot, alias, model, dimensions, datetime.now(UTC).timestamp()),
            )

    def space_for(self, snapshot: str, alias: str) -> tuple[str, int, bool] | None:
        """The space ``alias`` last answered in for ``snapshot``, and whether its vectors
        there cover every skill of it; ``None`` if it never answered."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT s.model,s.dimensions,(SELECT count(*) FROM skill_vectors v WHERE "
                "v.snapshot=s.snapshot AND v.model=s.model AND v.dimensions=s.dimensions) = "
                "(SELECT count(*) FROM skill_skills k WHERE k.snapshot=s.snapshot) "
                "FROM skill_vector_spaces s WHERE s.snapshot=? AND s.alias=? "
                "ORDER BY s.seen DESC LIMIT 1",
                (snapshot, alias),
            ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]), bool(row[2]))

    def add_embedding_call(self, project: str, model: str, texts: int, cost: float | None) -> None:
        """Count one embedding call of ``project``'s, priced as the gateway reported."""
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO skill_embedding_calls VALUES(?,?,?,?,?)",
                (project, datetime.now(UTC).timestamp(), model, texts, cost),
            )

    def settings(self, project: str) -> SkillSettings:
        if self.control.missing:
            return SkillSettings(project, 0)
        with self.control.read() as tx:
            return _settings(tx.sql, project)

    def set_settings(
        self,
        project: str,
        author: Author,
        expected: int,
        *,
        always: tuple[str, ...],
        excluded: tuple[str, ...],
    ) -> SkillSettings:
        """The user's change of ``project``'s skill settings, read at ``expected``."""
        if author.kind != "user":
            raise ControlError(INVALID_REQUEST, "only the user changes skill settings")
        for skill in (*always, *excluded):
            source, _, name = skill.partition("/")
            if not (NAME.fullmatch(source) and NAME.fullmatch(name)):
                raise ControlError(INVALID_REQUEST, f"{skill[:80]!r} is not a source/name ID")
        if len(always) > MAX_ALWAYS or len(excluded) > MAX_EXCLUDED:
            raise ControlError(
                INVALID_REQUEST, f"at most {MAX_ALWAYS} always loaded and {MAX_EXCLUDED} excluded"
            )
        lists = {"always": list(dict.fromkeys(always)), "excluded": sorted(set(excluded))}
        for key, items in lists.items():
            if len(json.dumps(items).encode()) > SETTINGS_BYTES[key]:
                raise ControlError(
                    INVALID_REQUEST, f"the {key} list passes {SETTINGS_BYTES[key]:,} bytes"
                )
        # As the new lists would make the set: a skill the change includes again counts.
        given = {
            entry.qualified: entry
            for entry in self.effective(project, excluded=lists["excluded"]).entries
        }
        chars = sum(
            len(self.served_body(entry))
            for skill in lists["always"]
            if skill not in lists["excluded"] and (entry := given.get(skill)) is not None
        )
        if chars > MAX_LOADED:
            raise ControlError(
                INVALID_REQUEST,
                f"the skills loaded at every run's start come to {chars:,} characters, past "
                f"the run's {MAX_LOADED:,}",
            )
        with self.control.write() as tx:
            db = tx.sql
            if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project,)).fetchone():
                raise ControlError(NOT_FOUND, f"there is no project {project!r}")
            current = _settings(db, project)
            if current.revision != expected:
                raise ControlError(
                    CONFLICT, f"the skill settings are at revision {current.revision}"
                )
            db.execute(
                "INSERT INTO project_skill_settings VALUES(?,?,?,?,?,?) ON CONFLICT(project) DO "
                "UPDATE SET revision=excluded.revision,always=excluded.always,"
                "omitted=excluded.omitted,author=excluded.author,changed=excluded.changed",
                (
                    project,
                    expected + 1,
                    json.dumps(lists["always"]),
                    json.dumps(lists["excluded"]),
                    author.model_dump_json(),
                    _now(),
                ),
            )
            return _settings(db, project)

    def pins(self, project: str) -> list[Pin]:
        """The project's pins, by source."""
        if self.control.missing:
            return []
        with self.control.read() as tx:
            return _pins(tx.sql, project)


def pack(vector: list[float]) -> bytes:
    """A vector as the store keeps it: little-endian float32."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _settings(db: sqlite3.Connection, project: str) -> SkillSettings:
    row = db.execute(
        "SELECT revision,always,omitted FROM project_skill_settings WHERE project=?", (project,)
    ).fetchone()
    if row is None:
        return SkillSettings(project, 0)
    return SkillSettings(project, row[0], tuple(json.loads(row[1])), tuple(json.loads(row[2])))


def _source(source: str) -> None:
    if not NAME.fullmatch(source):
        raise ControlError(
            INVALID_REQUEST, "a source's name is lowercase letters, digits and hyphens"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _capped(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    listed = [
        {**item, "path": item["path"][:200] + ("…" if len(item["path"]) > 200 else "")}
        for item in items[:REPORT_ITEMS]
    ]
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
    if source is None:
        pins = [
            replace(pin, left_out=tuple(_displaced(db, project, set(pin.skills))))
            if pin.source == BUILTIN
            else pin
            for pin in pins
        ]
    return pins


def _displaced(db: sqlite3.Connection, project: str, names: set[str]) -> list[str]:
    """The built-in skills of ``names`` that a pinned pack's skill of the same name leaves
    out of ``project``'s runs: neither of them excluded, as ``effective`` has it."""
    excluded = set(_settings(db, project).excluded)
    packs = {
        name
        for source, name in db.execute(
            "SELECT p.source,s.name FROM project_skill_pins p JOIN skill_imports i "
            "USING(import_id) JOIN skill_skills s ON s.snapshot=i.snapshot "
            "WHERE p.project=? AND p.source!=?",
            (project, BUILTIN),
        ).fetchall()
        if f"{source}/{name}" not in excluded
    }
    return sorted(name for name in names if name in packs and f"{BUILTIN}/{name}" not in excluded)
