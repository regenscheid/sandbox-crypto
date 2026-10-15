# SPDX-License-Identifier: MIT
"""The project's frontier (docs/specs/ledger.md, LG-18 to LG-24): its categories.

A frontier category names the target and kind of result it compares, its measures, each
with a unit and a direction (lower or higher is better), and its condition keys
(LG-18). Categories are versioned project records: the user or the main agent defines
one, and revises it by naming the revision read; every revision is kept. A result in a
category gives a value for each of its condition keys, and its measures, which must be
the category's, as numbers in its units or as given (LG-19).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)

from .models import Category, CategoryDefinition, CategoryMeasure, CategoryPage

Json = dict[str, Any]

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
            json.dumps([m.model_dump(mode="json") for m in value.measures]),
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
    result gives a value for each condition key and no other, and its measures are the
    category's."""
    name = fields.get("category")
    if name is None:
        return
    category = read(db, project, name)
    if category is None:
        raise DocumentError(
            INVALID_REQUEST, f"no frontier category {name}: define it, or name another"
        )
    conditions = set(fields.get("conditions", {}))
    missing = [key for key in category.conditions if key not in conditions]
    unknown = sorted(conditions - set(category.conditions))
    measures = {measure.name for measure in category.measures}
    unmeasured = sorted(set(fields.get("measures", {})) - measures)
    problems = [
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
