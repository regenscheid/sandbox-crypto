# SPDX-License-Identifier: MIT
"""The project's frontier (docs/specs/ledger.md, LG-18 to LG-24): its categories, and
the best known results in each.

A frontier category names the target and kind of result it compares, its measures, each
with a unit and a direction (lower or higher is better), and its condition keys
(LG-18). Categories are versioned project records: the user or the main agent defines
one, and revises it by naming the revision read; every revision is kept. A result in a
category gives a value for each of its condition keys, and its measures, which must be
the category's, as numbers in its units or as given (LG-19). Condition keys and values are
compared as ``normal`` makes them, so a capital or a no-break space makes no other key.

The frontier is a projection of the entries, read when asked for (LG-34). Published
results that are ``recorded`` and project results that are ``established`` are
eligible, and stay so, marked, when they become ``disputed``: the frontier then shows
the revision before, which was eligible (LG-20). A published result an agent recorded
is eligible only once the user confirmed it, as the revision shown reads (LG-16 and
LG-20 as the coordinator amended them); until then it is an unverified candidate.
Results are compared only under equal conditions: one dominates another when every
measure is a number for both, it is at least as good on each and better on one; the
best known are the eligible results no other dominates, and a result with a missing or
non-numeric measure is listed beside them as not compared (LG-21). Project results not
yet eligible are candidates, with the best known results they would dominate (LG-23).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any, Final

from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)

from . import gate
from .models import (
    PREFIXES,
    Category,
    CategoryDefinition,
    CategoryMeasure,
    CategoryPage,
    CategoryRevised,
    FrontierGroup,
    FrontierResult,
    FrontierView,
)

Json = dict[str, Any]

_SPACES: Final = re.compile(r"\s+")


def normal(text: str) -> str:
    """``text`` as the frontier compares it: in compatibility form (NFKC, which makes a
    no-break space a space), case folded, and each run of white space one space, with
    none at either end."""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    return _SPACES.sub(" ", folded).strip()


MAX_CANDIDATES: Final = 500
"""The most proposed results of a category the frontier reads as candidates; it says
when there are more. It reads every result that is, or was, eligible."""

_ELIGIBLE: Final = {"published": "recorded", "project": "established"}
"""The status that makes a result of each origin eligible (LG-20)."""

_SHOWN: Final = (
    "SELECT e.entry_id,e.type,e.number,e.origin,r.revision,r.title,r.status,r.statement,"
    "r.fields,r.author FROM ledger_entries e JOIN ledger_revisions r ON r.entry_id=e.entry_id "
    "AND r.revision=e.head WHERE e.project=? AND e.type='result' "
)

_TEXTS: Final = TextRevisions("statement")
_PREFIX: Final = PREFIXES

_COLUMNS: Final = (
    "c.category,r.revision,c.head,r.target,r.kind,r.measures,r.conditions,r.author,r.reason,"
    "r.operation_id,r.created"
)


def define(
    db: sqlite3.Connection,
    project: str,
    value: CategoryDefinition,
    author: Json,
    now: str,
) -> Category:
    """Define ``value``'s category, or revise it at the revision it expects, in the open
    change ``db``."""
    found = db.execute(
        "SELECT head FROM frontier_categories WHERE project=? AND category=?",
        (project, value.category),
    ).fetchone()
    if value.expected is None:
        if found is not None:
            raise DocumentError(
                CONFLICT,
                f"frontier category {value.category} exists, at revision {found[0]}: "
                "name the revision you read as expected to revise it",
                {"revision": found[0]},
            )
        position = db.execute(
            "SELECT coalesce(max(position),0)+1 FROM frontier_categories WHERE project=?",
            (project,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO frontier_categories(project,category,head,position) VALUES(?,?,1,?)",
            (project, value.category, position),
        )
        revision = 1
    else:
        if found is None:
            raise DocumentError(NOT_FOUND, f"no frontier category {value.category}")
        if found[0] != value.expected:
            raise DocumentError(
                CONFLICT,
                f"frontier category {value.category} is at revision {found[0]}",
                {"revision": found[0]},
            )
        revision = found[0] + 1
        db.execute(
            "UPDATE frontier_categories SET head=? WHERE project=? AND category=?",
            (revision, project, value.category),
        )
    db.execute(
        "INSERT INTO frontier_category_revisions(project,category,revision,target,kind,"
        "measures,conditions,author,reason,operation_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            project,
            value.category,
            revision,
            value.target,
            value.kind,
            json.dumps([m.model_dump(mode="json") for m in value.measures], ensure_ascii=False),
            json.dumps(value.conditions),
            json.dumps(author, sort_keys=True),
            value.reason,
            value.operation_id,
            now,
        ),
    )
    found_category = read(db, project, value.category, revision)
    assert found_category is not None  # noqa: S101 - written above
    return found_category


def read(
    db: sqlite3.Connection, project: str, category: str, revision: int | None = None
) -> Category | None:
    """``category`` at ``revision``, its current one by default; None when there is none."""
    row = db.execute(
        f"SELECT {_COLUMNS} FROM frontier_categories c JOIN frontier_category_revisions r "  # noqa: S608 - fixed columns
        "ON r.project=c.project AND r.category=c.category "
        "AND r.revision=coalesce(?,c.head) WHERE c.project=? AND c.category=?",
        (revision, project, category),
    ).fetchone()
    return None if row is None else _category(row)


def listing(db: sqlite3.Connection, project: str, after: str | None, limit: int) -> CategoryPage:
    """A page of the project's categories, in the order they were defined, after the one
    named ``after``."""
    start = 0
    if after is not None:
        found = db.execute(
            "SELECT position FROM frontier_categories WHERE project=? AND category=?",
            (project, after),
        ).fetchone()
        if found is None:
            raise DocumentError(NOT_FOUND, f"no frontier category {after}")
        start = found[0]
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM frontier_categories c JOIN frontier_category_revisions r "  # noqa: S608 - fixed columns
        "ON r.project=c.project AND r.category=c.category AND r.revision=c.head "
        "WHERE c.project=? AND c.position>? ORDER BY c.position LIMIT ?",
        (project, start, limit + 1),
    ).fetchall()
    found = [_category(row) for row in rows[:limit]]
    return CategoryPage(
        categories=found, next_after=found[-1].category if len(rows) > limit else None
    )


def check_result(db: sqlite3.Connection, project: str, fields: Json) -> None:
    """A result's fields as its category reads them (LG-19): the category exists, the
    result gives a value for each condition key and no other, each once as ``normal``
    reads keys, and its measures are the category's."""
    name = fields.get("category")
    if name is None:
        return
    category = read(db, project, name)
    if category is None:
        raise DocumentError(
            INVALID_REQUEST, f"no frontier category {name}: define it, or name another"
        )
    given = [normal(key) for key in fields.get("conditions", {})]
    keys = {normal(key) for key in category.conditions}
    twice = sorted({key for key in given if given.count(key) > 1})
    missing = [key for key in category.conditions if normal(key) not in given]
    unknown = sorted(key for key in fields.get("conditions", {}) if normal(key) not in keys)
    measures = {measure.name for measure in category.measures}
    unmeasured = sorted(set(fields.get("measures", {})) - measures)
    problems = [
        *([f"the condition keys {', '.join(twice)} more than once"] if twice else []),
        *([f"no value for the condition keys {', '.join(missing)}"] if missing else []),
        *([f"condition keys it does not have: {', '.join(unknown)}"] if unknown else []),
        *([f"measures it does not have: {', '.join(unmeasured)}"] if unmeasured else []),
    ]
    if problems:
        raise DocumentError(
            INVALID_REQUEST,
            f"a result in frontier category {name} (revision {category.revision}) gives "
            + "; ".join(problems),
        )


def _category(row: tuple[Any, ...]) -> Category:
    return Category(
        category=row[0],
        revision=row[1],
        head=row[2],
        target=row[3],
        kind=row[4],
        measures=[CategoryMeasure.model_validate(m) for m in json.loads(row[5])],
        conditions=json.loads(row[6]),
        author=json.loads(row[7]),
        reason=row[8],
        operation_id=row[9],
        created=row[10],
    )


def view(db: sqlite3.Connection, project: str, category: Category) -> FrontierView:
    """``category``'s frontier as its results read now: every result that is or was
    eligible, and as many as ``MAX_CANDIDATES`` proposed ones."""
    held = db.execute(
        _SHOWN + "AND (r.status='disputed' OR (json_extract(r.fields,'$.category')=? "
        "AND r.status IN ('recorded','established'))) ORDER BY e.position",
        (project, category.category),
    ).fetchall()
    proposed = db.execute(
        _SHOWN + "AND e.origin='project' AND r.status='proposed' "
        "AND json_extract(r.fields,'$.category')=? ORDER BY e.position LIMIT ?",
        (project, category.category, MAX_CANDIDATES + 1),
    ).fetchall()
    keys = category.conditions
    groups: dict[tuple[str, ...], tuple[dict[str, str], list[_Member]]] = {}
    unplaced: list[FrontierResult] = []
    # Results in the order they were made: their numbers.
    for row in sorted([*held, *proposed[:MAX_CANDIDATES]], key=lambda r: r[2]):
        member = _member(db, row, category.category)
        if member is None:
            continue
        shown, eligible = member[0], member[1]
        given = {normal(key): value for key, value in shown.conditions.items()}
        values = [given.get(normal(key)) for key in keys]
        if any(value is None for value in values):
            if eligible:
                unplaced.append(shown)
            continue
        written = dict(zip(keys, (str(v) for v in values), strict=True))
        found = groups.setdefault(tuple(normal(v) for v in written.values()), (written, []))
        found[1].append(member)
    return FrontierView(
        category=category,
        groups=[_group(category, written, members) for written, members in groups.values()],
        unplaced=unplaced,
        truncated=len(proposed) > MAX_CANDIDATES,
        agent_revised=revised(db, project, category),
    )


_Member = tuple[FrontierResult, bool, bool]
"""A result as the frontier shows it, whether it is eligible, and whether a candidate."""


def _member(db: sqlite3.Connection, row: tuple[Any, ...], name: str) -> _Member | None:
    """The result of ``row``, at its current revision, as the frontier holds it: shown,
    eligible or a candidate; None when it is neither, or no longer in category
    ``name``. A disputed result is eligible only when the revision before it became
    disputed was, and is shown as that revision reads (LG-20)."""
    entry, origin, status = row[0], row[3], row[6]
    disputed = False
    if status == "disputed":
        before = db.execute(
            "SELECT revision,title,status,statement,fields,author FROM ledger_revisions "
            "WHERE entry_id=? AND revision<? AND status!='disputed' "
            "ORDER BY revision DESC LIMIT 1",
            (entry, row[4]),
        ).fetchone()
        if before is not None and before[2] == _ELIGIBLE[origin]:
            row = (*row[:4], before[0], before[1], status, before[3], before[4], before[5])
            disputed = True
    fields = json.loads(row[8])
    if fields.get("category") != name:
        return None
    eligible = disputed or status == _ELIGIBLE[origin]
    unverified = eligible and origin == "published" and not _confirmed(db, entry, row[4], row[9])
    eligible = eligible and not unverified
    candidate = not eligible and (
        unverified or (origin == "project" and status in ("proposed", "disputed"))
    )
    if not (eligible or candidate):
        return None
    shown = _result(db, row, fields, disputed=disputed, unverified=unverified)
    return shown, eligible, candidate


def _confirmed(db: sqlite3.Connection, entry: str, revision: int, author: str) -> bool:
    """Whether the user confirmed published result ``entry`` as ``revision`` reads: the
    user wrote it, or a revision since, or one before, with the same statement, fields,
    assumptions and evidence, by recording, revising or checking it (``ledger check``,
    or the workspace's). The consistency reviewer's citation check (CR-4) is not built
    yet."""
    if json.loads(author).get("kind") == "user":
        return True
    examined = gate.examined(db, entry, {revision})
    now = examined.get(revision)
    rows = db.execute(
        "SELECT revision FROM ledger_revisions WHERE entry_id=? AND revision!=? "
        "AND json_extract(author,'$.kind')='user' ORDER BY revision DESC LIMIT ?",
        (entry, revision, gate.KEPT_REVISIONS),
    ).fetchall()
    found = gate.examined(db, entry, {r[0] for r in rows})
    return now is not None and now in found.values()


def revised(db: sqlite3.Connection, project: str, category: Category) -> CategoryRevised | None:
    """The main agent's revision of ``category`` that the user has not revised since, with
    what it changed from the user's latest revision before it; None when the user wrote
    the current one."""
    if category.author.get("kind") == "user":
        return None
    found = db.execute(
        "SELECT max(revision) FROM frontier_category_revisions WHERE project=? AND "
        "category=? AND json_extract(author,'$.kind')='user'",
        (project, category.category),
    ).fetchone()[0]
    before = None if found is None else read(db, project, category.category, found)
    return CategoryRevised(
        revision=category.revision,
        created=category.created,
        author=category.author,
        reason=category.reason,
        since=found,
        changed=[] if before is None else _changes(before, category),
    )


def _changes(before: Category, after: Category) -> list[str]:
    """What ``after`` changed from ``before``, in words."""
    changed = [part for part in ("target", "kind") if getattr(before, part) != getattr(after, part)]
    old = {m.name: m for m in before.measures}
    new = {m.name: m for m in after.measures}
    for name, measure in new.items():
        if name not in old:
            changed.append(f"measure {name} added, {measure.direction} is better")
            continue
        if old[name].unit != measure.unit:
            changed.append(f"measure {name}'s unit")
        if old[name].direction != measure.direction:
            changed.append(f"measure {name}'s direction: {measure.direction} is better")
    changed += [f"measure {name} removed" for name in old if name not in new]
    changed += [f"condition key {k} added" for k in after.conditions if k not in before.conditions]
    changed += [
        f"condition key {k} removed" for k in before.conditions if k not in after.conditions
    ]
    return changed


def _group(category: Category, conditions: dict[str, str], members: list[_Member]) -> FrontierGroup:
    """One set of conditions' best known, dominated, not compared and candidate results."""
    compared = [r for r, eligible, _ in members if eligible and _numeric(category, r)]
    best: list[FrontierResult] = []
    dominated: list[FrontierResult] = []
    for result in compared:
        above = [o.handle for o in compared if o is not result and dominates(category, o, result)]
        if above:
            dominated.append(result.model_copy(update={"dominated_by": above}))
        else:
            best.append(result)
    candidates = [
        result.model_copy(
            update={
                "would_dominate": [
                    b.handle
                    for b in best
                    if _numeric(category, result) and dominates(category, result, b)
                ]
            }
        )
        for result, _, candidate in members
        if candidate
    ]
    return FrontierGroup(
        conditions=conditions,
        best=best,
        dominated=dominated,
        not_compared=[r for r, eligible, _ in members if eligible and not _numeric(category, r)],
        candidates=candidates,
    )


def _numeric(category: Category, result: FrontierResult) -> bool:
    """Whether every measure of the category is a number for ``result``."""
    return all(isinstance(result.measures.get(m.name), (int, float)) for m in category.measures)


def dominates(category: Category, a: FrontierResult, b: FrontierResult) -> bool:
    """Whether ``a`` dominates ``b`` (LG-21): at least as good on every measure of the
    category, and better on one; both give every measure as a number."""
    better = False
    for measure in category.measures:
        x, y = _number(a, measure.name), _number(b, measure.name)
        if x == y:
            continue
        if (x < y) != (measure.direction == "lower"):
            return False
        better = True
    return better


def _number(result: FrontierResult, name: str) -> float:
    value = result.measures[name]
    if isinstance(value, str):
        raise ValueError(f"{result.handle} gives {name} as {value!r}, not a number")
    return value


def _result(
    db: sqlite3.Connection, row: tuple[Any, ...], fields: Json, *, disputed: bool, unverified: bool
) -> FrontierResult:
    """The entry of ``row`` as the frontier shows it, at the revision ``row`` names."""
    entry = row[0]
    text = _TEXTS.read(db, entry, row[7], full=False)
    held = db.execute(
        "SELECT v.type,v.target,v.target_id,v.target_revision,v.role,t.type,t.number "
        "FROM ledger_evidence v LEFT JOIN ledger_entries t ON v.target='entry' "
        "AND t.entry_id=v.target_id WHERE v.entry_id=? AND v.revision=? ORDER BY v.ordinal",
        (entry, row[4]),
    ).fetchall()
    return FrontierResult(
        entry_id=entry,
        handle=f"{_PREFIX[row[1]]}-{row[2]}",
        revision=row[4],
        title=row[5],
        statement="" if text is None else text["body"],
        status=row[6],
        origin=row[3],
        disputed=disputed,
        unverified=unverified,
        author=json.loads(row[9]),
        conditions=fields.get("conditions", {}),
        measures=fields.get("measures", {}),
        evidence_types=list(dict.fromkeys(h[0] for h in held)),
        sources=list(
            dict.fromkeys(
                f"{_PREFIX[h[5]]}-{h[6]}"
                for h in held
                if h[5] == "reference" and h[4] == "supports" and h[0] == "source_claim"
            )
        ),
        artifacts=[{"artifact_id": h[2], "revision": h[3]} for h in held if h[1] == "artifact"],
        open_findings=db.execute(
            "SELECT count(DISTINCT x.finding_id) FROM review_finding_entries x "
            "JOIN review_findings f USING(finding_id) WHERE x.entry_id=? "
            "AND f.status NOT IN ('confirmed fixed','withdrawn','superseded')",
            (entry,),
        ).fetchone()[0],
        open_flags=db.execute(
            "SELECT count(*) FROM ledger_flags WHERE entry_id=? AND cleared IS NULL", (entry,)
        ).fetchone()[0],
    )
