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
eligible, and stay so, marked, when they become ``disputed`` (LG-20). Results are
compared only under equal conditions: one dominates another when every measure is a
number for both, it is at least as good on each and better on one; the best known are
the eligible results no other dominates, and a result with a missing or non-numeric
measure is listed beside them as not compared (LG-21). Project results not yet eligible
are candidates, with the best known results they would dominate (LG-23).
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

from .models import (
    PREFIXES,
    Category,
    CategoryDefinition,
    CategoryMeasure,
    CategoryPage,
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


MAX_RESULTS: Final = 500
"""The most results of a category the frontier reads; it says when there are more."""

_ELIGIBLE: Final = {"published": "recorded", "project": "established"}
"""The status that makes a result of each origin eligible (LG-20)."""

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
    """``category``'s frontier as its results' current revisions read now."""
    rows = db.execute(
        "SELECT e.entry_id,e.type,e.number,e.origin,r.revision,r.title,r.status,r.statement,"
        "r.fields FROM ledger_entries e JOIN ledger_revisions r ON r.entry_id=e.entry_id "
        "AND r.revision=e.head WHERE e.project=? AND e.type='result' "
        "AND json_extract(r.fields,'$.category')=? ORDER BY e.position LIMIT ?",
        (project, category.category, MAX_RESULTS + 1),
    ).fetchall()
    keys = category.conditions
    groups: dict[tuple[str, ...], list[tuple[FrontierResult, bool, bool]]] = {}
    unplaced: list[FrontierResult] = []
    for row in rows[:MAX_RESULTS]:
        entry, origin, status = row[0], row[3], row[6]
        fields = json.loads(row[8])
        eligible = status == _ELIGIBLE.get(origin) or (
            status == "disputed"
            and db.execute(
                "SELECT 1 FROM ledger_revisions WHERE entry_id=? AND status=? LIMIT 1",
                (entry, _ELIGIBLE.get(origin)),
            ).fetchone()
            is not None
        )
        candidate = not eligible and origin == "project" and status in ("proposed", "disputed")
        if not (eligible or candidate):
            continue
        shown = _result(db, row, fields, eligible and status == "disputed")
        conditions = fields.get("conditions", {})
        if any(key not in conditions for key in keys):
            if eligible:
                unplaced.append(shown)
            continue
        groups.setdefault(tuple(conditions[key] for key in keys), []).append(
            (shown, eligible, candidate)
        )
    return FrontierView(
        category=category,
        groups=[
            _group(category, dict(zip(keys, values, strict=True)), members)
            for values, members in groups.items()
        ],
        unplaced=unplaced,
        truncated=len(rows) > MAX_RESULTS,
    )


def _group(
    category: Category,
    conditions: dict[str, str],
    members: list[tuple[FrontierResult, bool, bool]],
) -> FrontierGroup:
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
    db: sqlite3.Connection, row: tuple[Any, ...], fields: Json, disputed: bool
) -> FrontierResult:
    """The entry of ``row`` as the frontier shows it."""
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
