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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.pricing import CostSource, source_of

from . import allow
from .builtin import BUILTIN, DELEGATING, PLATFORM, ROLES
from .packs import (
    NAME,
    SHORT,
    TOPIC,
    VARIABLE,
    Snapshot,
    body,
    broken_links,
    hidden,
    plain_text,
    served,
    snapshot,
    topic_name,
)
from .sources import Fetched

if TYPE_CHECKING:
    from sandbox_science.orchestrator.documents import DocumentStore

REPORT_ITEMS: Final = 200
"""Items a report lists of each kind; it counts the rest."""
MAX_LOADED: Final = 100_000
"""Characters of skill text one run is given, those loaded at its start included (SK-39)."""
MAX_OVERRIDDEN: Final = 500
"""Skills a project overrides, and bindings it takes away, at most each (SK-33)."""
OVERRIDES_BYTES: Final = 131072
"""The overrides as JSON, at most as the control store keeps them (version 45)."""
USER_SET_BYTES: Final = 262144
"""The projects' overrides an import's report lists, at most as JSON; it counts the rest."""
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
    changes: dict[str, Any] | None = None
    """When a pin just moved: the revision it left, and the skills added, removed and
    changed since (SK-28)."""
    user_set: dict[str, Any] | None = None
    """The project's overrides of what the pass derived for this source's skills, and the
    bindings it may not have, as the user set them (SK-33)."""


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
    short: str | None = None
    """The import's model pass's short description, for one too long for the index."""
    topic: str | None = None
    """The topic the pass put the skill in."""
    entry: bool = False
    """Whether the pass found that the skill routes work to its pack's others."""
    short_by: str | None = None
    """Who wrote ``short``: ``derived`` by the pass, or ``user`` in the settings."""
    topic_by: str | None = None
    """Who set ``topic``: ``derived`` by the pass, or ``user`` in the settings."""

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
    overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    """The user's overrides of what the import's pass derives (SK-33): ``skills``, each
    qualified ID's ``description``, ``topic`` and ``entry``; ``unbound``, each source's
    derived bindings it may not have; ``approved``, each source's derived bindings set
    though not namespaced to the pack; and ``prefixes``, each source's own namespace."""


@dataclass(frozen=True)
class Bound:
    """A project's derived bindings by source: those its runs set, and those held, each
    with its path and why (SK-31, SK-33)."""

    set: dict[str, dict[str, str]]
    held: dict[str, list[dict[str, str]]]


class SkillStore:
    """The imported packs and the projects' pins in one control store."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    @classmethod
    def of(cls, documents: DocumentStore) -> SkillStore:
        """The skills kept in the documents' control store, as the workspace reads them."""
        return cls(documents.control)

    def add(
        self,
        source: str,
        snapshot: Snapshot,
        author: Author,
        *,
        derived: dict[str, Any] | None = None,
    ) -> tuple[Imported, bool]:
        """Keep an import of ``source``; ``True`` when its latest import is already this
        one, from the same place at the same revision, and nothing was added. ``derived``
        is what the import's model pass derived, for the report (SK-32)."""
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
            if links := broken_links(got.files):
                # Relative links that name no file of the snapshot (SK-32).
                report["broken_links"] = _capped(links)
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
            if derived is not None:
                # The bindings a project sets only once its user approves them (SK-31).
                given = cast(dict[str, str], derived.get("bindings", {}))
                rows = db.execute("SELECT DISTINCT source FROM skill_imports").fetchall()
                others = {allow.namespace(s) for (s,) in rows}
                names, refused = allow.granted(source, None, others - {allow.namespace(source)})
                held = [
                    {"variable": variable, "path": path, "reason": why}
                    for variable, path in sorted(given.items())
                    if (why := allow.held(variable, source, names=names, refused=refused))
                ]
                report["derived"] = {**derived, "held": held} if held else derived
            user, more = _user_set(db, source, {skill.name for skill in snapshot.skills})
            if user:
                report["user_set"] = user  # by project: what overrides the pass (SK-33)
            if more:
                report["user_set_more"] = more  # projects whose overrides did not fit
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
            before = next(iter(_pins(db, project, source)), None)
            db.execute(
                "INSERT INTO project_skill_pins VALUES(?,?,?,1,?,?) ON CONFLICT(project,source) "
                "DO UPDATE SET import_id=excluded.import_id,revision=revision+1,"
                "author=excluded.author,pinned=excluded.pinned WHERE import_id!=excluded.import_id",
                (project, source, chosen.import_id, author.model_dump_json(), _now()),
            )
            pinned = _pins(db, project, source)[0]
            if before is None or before.pinned.import_id == chosen.import_id:
                return pinned
            moved = _compare(db, before.pinned.snapshot, chosen.snapshot)
            return replace(pinned, changes={"since": _revision(before.pinned), **moved})

    def effective(
        self,
        project: str,
        role: str | None = None,
        *,
        delegates: bool = True,
        excluded: Collection[str] | None = None,
        derived: tuple[str, str] | None = None,
    ) -> SkillSet:
        """The skills a run of ``project`` is given now, or, with ``excluded``, would be
        given with those excluded instead of the settings'. A run of ``role`` gets the
        built-in skills ``ROLES`` gives it, with no role all of them, and a run that cannot
        delegate (``delegates``) none that teach delegating. With ``derived``, an alias and
        a pass version, the entries carry what that pass derived from their snapshots, and
        what another alias's or version's pass derived is never served. Each description is
        served as the bodies are (``served``)."""
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
                revision = _revision(found)
                sources.append(f"{pin.source}@{revision}")
                given = _derived(db, found.snapshot, derived) if derived is not None else {}
                topics = {
                    skill: topic
                    for topic, skills in cast(dict[str, list[str]], given.get("topics", {})).items()
                    for skill in skills
                }
                shorts = cast(dict[str, str], given.get("descriptions", {}))
                entries_of = set(cast(list[str], given.get("entry", [])))
                for name, description, body in db.execute(
                    "SELECT name,description,body_sha256 FROM skill_skills WHERE snapshot=? "
                    "ORDER BY name",
                    (found.snapshot,),
                ):
                    entry = _overridden(
                        Entry(
                            pin.source,
                            name,
                            served(description),
                            body,
                            found.snapshot,
                            revision,
                            served(shorts[name]) if name in shorts else None,
                            served(topics[name]) if name in topics else None,
                            name in entries_of,
                            "derived" if name in shorts else None,
                            "derived" if name in topics else None,
                        ),
                        settings.overrides,
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

    def manifest(self, snapshot: str) -> list[tuple[str, str, int, bool]]:
        """A snapshot's files by path: each with its SHA-256, size and whether it is
        executable, as staging writes and checks them (SK-16)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT f.path,f.blob,length(b.content),f.executable FROM skill_files f "
                "JOIN skill_blobs b ON b.sha256=f.blob WHERE f.snapshot=? ORDER BY f.path",
                (snapshot,),
            ).fetchall()
        return [(path, blob, size, bool(executable)) for path, blob, size, executable in rows]

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

    def metadata(self, snapshot: str, alias: str, version: str) -> dict[str, Any] | None:
        """What the pass of ``version`` by ``alias`` derived from ``snapshot``, with what
        it dropped and the model that answered, or ``None`` if it never ran."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT model,derived,dropped FROM skill_metadata WHERE snapshot=? AND alias=? "
                "AND version=?",
                (snapshot, alias, version),
            ).fetchone()
        if row is None:
            return None
        return {"model": row[0], "derived": json.loads(row[1]), "dropped": json.loads(row[2])}

    def add_metadata(
        self,
        snapshot: str,
        alias: str,
        version: str,
        model: str,
        derived: dict[str, Any],
        dropped: list[dict[str, Any]],
    ) -> None:
        """Keep what a pass derived, unless an earlier one of the same kept it."""
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO skill_metadata VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(snapshot,alias,version) DO NOTHING",
                (snapshot, alias, version, model, json.dumps(derived), json.dumps(dropped), _now()),
            )

    def add_metadata_call(
        self,
        project: str | None,
        snapshot: str,
        model: str,
        tokens: tuple[int | None, int | None],
        cost: float | None,
        cost_source: CostSource | None = None,
    ) -> None:
        """Count one call of the pass, ``project``'s when the import names one (RS-6a),
        with where its cost came from (the gateway, or its model information)."""
        source = source_of(cost, cost_source)
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO skill_metadata_calls(project,created,snapshot,model,input_tokens,"
                "output_tokens,cost,cost_source) VALUES(?,?,?,?,?,?,?,?)",
                (project, datetime.now(UTC).timestamp(), snapshot, model, *tokens, cost, source),
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
                "INSERT INTO project_skill_settings(project,revision,always,omitted,author,"
                "changed) VALUES(?,?,?,?,?,?) ON CONFLICT(project) DO "
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

    def set_overrides(
        self,
        project: str,
        author: Author,
        expected: int,
        *,
        skills: Mapping[str, Mapping[str, object]],
        unbound: Mapping[str, Sequence[str]],
        approved: Mapping[str, Sequence[str]] | None = None,
        prefixes: Mapping[str, str] | None = None,
    ) -> SkillSettings:
        """The user's overrides of what the pass derives, in place of the project's
        current ones, read at ``expected`` (SK-33): for each skill by qualified ID a
        ``description``, ``topic`` or ``entry``; for each source the derived bindings its
        skills may not have, those set though not namespaced to the pack, and a prefix of
        its own (SK-31). They win over what any pass derives, and survive pins."""
        if author.kind != "user":
            raise ControlError(INVALID_REQUEST, "only the user overrides skills")
        kept: dict[str, dict[str, object]] = {}
        for skill, fields in skills.items():
            source, _, name = skill.partition("/")
            if not (NAME.fullmatch(source) and NAME.fullmatch(name)):
                raise ControlError(INVALID_REQUEST, f"{skill[:80]!r} is not a source/name ID")
            if fields:
                kept[skill] = _fields(skill, fields)
        bound, allowed = _variables(unbound), _variables(approved or {})
        if never := sorted(v for vs in allowed.values() for v in vs if allow.forbidden(v)):
            raise ControlError(INVALID_REQUEST, f"{never[0]} is never set, approved or not")
        named: dict[str, str] = {}
        for source, prefix in (prefixes or {}).items():
            _source(source)
            if why := allow.prefix_refused(prefix):
                raise ControlError(INVALID_REQUEST, why)
            named[source] = prefix
        counts = (len(kept), *(sum(map(len, v.values())) for v in (bound, allowed)))
        if max(counts) > MAX_OVERRIDDEN:
            raise ControlError(INVALID_REQUEST, f"at most {MAX_OVERRIDDEN} of each are overridden")
        overrides = {"skills": kept, "unbound": bound, "approved": allowed, "prefixes": named}
        if len(text := json.dumps(overrides)) > OVERRIDES_BYTES:
            raise ControlError(INVALID_REQUEST, f"the overrides pass {OVERRIDES_BYTES:,} bytes")
        with self.control.write() as tx:
            db = tx.sql
            if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project,)).fetchone():
                raise ControlError(NOT_FOUND, f"there is no project {project!r}")
            if (current := _settings(db, project).revision) != expected:
                raise ControlError(CONFLICT, f"the skill settings are at revision {current}")
            db.execute(
                "INSERT INTO project_skill_settings(project,revision,always,omitted,author,"
                "changed,overrides) VALUES(?,?,'[]','[]',?,?,?) ON CONFLICT(project) DO "
                "UPDATE SET revision=excluded.revision,overrides=excluded.overrides,"
                "author=excluded.author,changed=excluded.changed",
                (project, expected + 1, author.model_dump_json(), _now(), text),
            )
            return _settings(db, project)

    def bindings(
        self, project: str, derived: tuple[str, str] | None, tools: Collection[str] = ()
    ) -> Bound:
        """Each pinned source's bindings the pass of ``derived``, an alias and version,
        found: those the project's runs set, namespaced to the pack or approved, and those
        held, with why, as the rest are, and those the project's overrides take away
        (SK-30, SK-31, SK-33). A namespace is refused when it is a tool's (``tools``
        names the environment's commands, when known), or would claim another pinned
        source's; a variable two sources bind is held for both. The deny-list is checked
        again: a pass of an older list may have kept what this one refuses."""
        found = Bound({}, {})
        if self.control.missing or derived is None:
            return found
        with self.control.read() as tx:
            db = tx.sql
            overrides = _settings(db, project).overrides
            prefixes = cast(dict[str, str], overrides.get("prefixes", {}))
            given = {
                pin.source: cast(
                    dict[str, str], _derived(db, pin.pinned.snapshot, derived).get("bindings", {})
                )
                for pin in _pins(db, project)
            }
        spaces = {s: {allow.namespace(s), *([prefixes[s]] if s in prefixes else [])} for s in given}
        named: dict[str, list[str]] = {}
        for source, variables in given.items():
            for variable in variables:
                named.setdefault(variable, []).append(source)
        for source, variables in given.items():
            others = {name for other, names in spaces.items() if other != source for name in names}
            names, refused = allow.granted(source, prefixes.get(source), others, tools)
            for variable, path in sorted(variables.items()):
                why = _held(overrides, source, variable, names, refused)
                if why is None and len(named[variable]) > 1:
                    also = ", ".join(s for s in named[variable] if s != source)
                    why = f"{also} binds it too: held for each"
                if why is None:
                    found.set.setdefault(source, {})[variable] = path
                else:
                    item = {"variable": variable, "path": path, "reason": why}
                    found.held.setdefault(source, []).append(item)
        return found

    def pins(self, project: str) -> list[Pin]:
        """The project's pins, by source."""
        if self.control.missing:
            return []
        with self.control.read() as tx:
            return _pins(tx.sql, project)


def _derived(db: sqlite3.Connection, snapshot: str, pass_: tuple[str, str]) -> dict[str, Any]:
    """What the pass of this alias and version derived from ``snapshot``, or nothing."""
    row = db.execute(
        "SELECT derived FROM skill_metadata WHERE snapshot=? AND alias=? AND version=?",
        (snapshot, *pass_),
    ).fetchone()
    return {} if row is None else cast(dict[str, Any], json.loads(row[0]))


def pack(vector: list[float]) -> bytes:
    """A vector as the store keeps it: little-endian float32."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _fields(skill: str, fields: Mapping[str, object]) -> dict[str, object]:
    """A skill's overrides, checked: a description and a topic as derived text is, and
    whether it is an entry skill."""
    unknown = set(fields) - {"description", "topic", "entry"}
    if unknown:
        raise ControlError(INVALID_REQUEST, f"no override {sorted(unknown)[0]!r} for {skill}")
    kept: dict[str, object] = {}
    if "description" in fields:
        text = plain_text(fields["description"])
        if not 0 < len(text) <= SHORT:
            raise ControlError(INVALID_REQUEST, f"{skill}'s description is 1 to {SHORT} characters")
        kept["description"] = text
    if "topic" in fields:
        text = topic_name(fields["topic"])
        if not 0 < len(text) <= TOPIC:
            raise ControlError(INVALID_REQUEST, f"{skill}'s topic is 1 to {TOPIC} characters")
        kept["topic"] = text
    if "entry" in fields:
        if not isinstance(fields["entry"], bool):
            raise ControlError(INVALID_REQUEST, f"{skill}'s entry is true or false")
        kept["entry"] = fields["entry"]
    return kept


def _variables(lists: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Variables by source, checked, without empty lists."""
    found: dict[str, list[str]] = {}
    for source, variables in lists.items():
        _source(source)
        if variables:
            if any(not VARIABLE.fullmatch(v) for v in variables):
                raise ControlError(INVALID_REQUEST, f"{source}'s bindings name no variable")
            found[source] = sorted(set(variables))
    return found


def _held(
    overrides: dict[str, Any],
    source: str,
    variable: str,
    names: Sequence[str],
    refused: Sequence[str],
) -> str | None:
    """Why the project does not set ``source``'s derived binding of ``variable``."""
    if variable in cast(dict[str, list[str]], overrides.get("unbound", {})).get(source, []):
        return "taken away by the project's overrides"
    approved = cast(dict[str, list[str]], overrides.get("approved", {})).get(source, [])
    return allow.held(variable, source, names=names, refused=refused, approved=approved)


def _settings(db: sqlite3.Connection, project: str) -> SkillSettings:
    row = db.execute(
        "SELECT revision,always,omitted,overrides FROM project_skill_settings WHERE project=?",
        (project,),
    ).fetchone()
    if row is None:
        return SkillSettings(project, 0)
    always, omitted = tuple(json.loads(row[1])), tuple(json.loads(row[2]))
    return SkillSettings(project, row[0], always, omitted, json.loads(row[3]))


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
    after = [
        (f.path, hashlib.sha256(f.content).hexdigest(), int(f.executable))
        for f in snapshot.fetched.files
    ]
    return _changed(
        _files(db, latest.snapshot),
        after,
        set(_names(db, latest.snapshot)),
        {skill.name for skill in snapshot.skills},
    )


def _compare(db: sqlite3.Connection, old: str, new: str) -> dict[str, list[str]]:
    """The skills added, removed and changed from the kept snapshot ``old`` to ``new``."""
    names = set(_names(db, old)), set(_names(db, new))
    return _changed(_files(db, old), _files(db, new), *names)


def _files(db: sqlite3.Connection, snapshot: str) -> list[tuple[str, str, int]]:
    rows = db.execute("SELECT path,blob,executable FROM skill_files WHERE snapshot=?", (snapshot,))
    return [(row[0], row[1], row[2]) for row in rows.fetchall()]


def _revision(found: Imported) -> str:
    """How an import is named to the user: its commit, archive digest, ref or snapshot."""
    return found.commit or found.archive_sha256 or found.ref or found.snapshot


def _changed(
    old_files: list[tuple[str, str, int]],
    new_files: list[tuple[str, str, int]],
    old: set[str],
    new: set[str],
) -> dict[str, list[str]]:
    before, after = _by_skill(old_files), _by_skill(new_files)
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
        overrides = _settings(db, project).overrides
        pins = [
            replace(pin, user_set=found)
            if (found := _of_source(overrides, pin.source, set(pin.skills)))
            else pin
            for pin in pins
        ]
    return pins


def _of_source(overrides: dict[str, Any], source: str, names: set[str]) -> dict[str, Any]:
    """``source``'s overrides: its skills' and its bindings', with those of skills its
    snapshot lacks named apart (kept for a later import that has them)."""
    skills = {
        skill: fields
        for skill, fields in cast(dict[str, Any], overrides.get("skills", {})).items()
        if skill.partition("/")[0] == source
    }
    found: dict[str, Any] = {}
    if present := {k: v for k, v in skills.items() if k.partition("/")[2] in names}:
        found["skills"] = present
    if absent := sorted(k for k in skills if k.partition("/")[2] not in names):
        found["kept_for_absent_skills"] = absent
    for key in ("unbound", "approved", "prefixes"):
        if mine := cast(dict[str, Any], overrides.get(key, {})).get(source):
            found["prefix" if key == "prefixes" else key] = mine
    return found


def _user_set(db: sqlite3.Connection, source: str, names: set[str]) -> tuple[dict[str, Any], int]:
    """For an import's report: each project's overrides of ``source``'s skills, as many
    as fit in ``USER_SET_BYTES``, and how many projects' more there are."""
    found: dict[str, Any] = {}
    size = more = 0
    for project, text in db.execute(
        "SELECT project,overrides FROM project_skill_settings ORDER BY project"
    ).fetchall():
        if of := _of_source(json.loads(text), source, names):
            size += len(json.dumps(of))
            if size > USER_SET_BYTES:
                more += 1
            else:
                found[project] = of
    return found, more


def _overridden(entry: Entry, overrides: dict[str, Any]) -> Entry:
    """``entry`` with the user's overrides, which win over what the pass derived."""
    fields = cast(dict[str, Any], overrides.get("skills", {})).get(entry.qualified)
    if not fields:
        return entry
    changed: dict[str, Any] = {}
    if "description" in fields:
        changed |= {"short": served(fields["description"]), "short_by": "user"}
    if "topic" in fields:
        changed |= {"topic": served(fields["topic"]), "topic_by": "user"}
    if "entry" in fields:
        changed["entry"] = bool(fields["entry"])
    return replace(entry, **changed)


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
