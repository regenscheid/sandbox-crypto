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
Where the rule cannot compare two eligible results of a category, the user or the main
agent records that one dominates the other with a ``dominates`` link and its reason,
which takes the dominated one from the best known, or from those not compared (LG-22).
A link between two results the rule now compares counts for nothing, and so does one on a
cycle of results that dominate one another, by links or by the rule, as the frontier reads
them; a change that adds a link on such a cycle is refused, and the frontier lists the
links that count for nothing, with why.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any, Final

from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)

from . import evidence, gate
from .models import (
    PREFIXES,
    Category,
    CategoryDefinition,
    CategoryMeasure,
    CategoryPage,
    CategoryRevised,
    DominatesLink,
    EvidenceLink,
    FrontierChange,
    FrontierGroup,
    FrontierHistory,
    FrontierMember,
    FrontierPast,
    FrontierPastGroup,
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
"""The most proposed results of a category the frontier reads as candidates, newest
first; it says where the next ones begin. It reads every result that is, or was,
eligible."""

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


def view(
    db: sqlite3.Connection,
    project: str,
    category: Category,
    *,
    candidates_before: int | None = None,
    full: bool = True,
) -> FrontierView:
    """``category``'s frontier as its results read now: every result that is or was
    eligible, and as many as ``MAX_CANDIDATES`` proposed ones, newest first, made before
    the result numbered ``candidates_before``; without ``full``, the results without what
    only shows them (their statements, evidence, findings and flags), for the history."""
    held = db.execute(
        _SHOWN + "AND (r.status='disputed' OR (json_extract(r.fields,'$.category')=? "
        "AND r.status IN ('recorded','established'))) ORDER BY e.position",
        (project, category.category),
    ).fetchall()
    proposed = db.execute(
        _SHOWN + "AND e.origin='project' AND r.status='proposed' "
        "AND json_extract(r.fields,'$.category')=? AND e.number<? "
        "ORDER BY e.number DESC LIMIT ?",
        (project, category.category, candidates_before or 2**62, MAX_CANDIDATES + 1),
    ).fetchall()
    keys = category.conditions
    groups: dict[tuple[str, ...], tuple[dict[str, str], list[_Member]]] = {}
    unplaced: list[FrontierResult] = []
    # Results in the order they were made: their numbers.
    for row in sorted([*held, *proposed[:MAX_CANDIDATES]], key=lambda r: r[2]):
        member = _member(db, row, category.category, full=full)
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
    found = [_group(category, written, members) for written, members in groups.values()]
    above, voided = _linked(
        db,
        category,
        [*(r for g in found for r in (*g.best, *g.dominated, *g.not_compared)), *unplaced],
    )
    more = len(proposed) > MAX_CANDIDATES
    return FrontierView(
        category=category,
        groups=[_dominated(group, above) for group in found],
        unplaced=[_above(r, above.get(r.entry_id, [])) for r in unplaced],
        truncated=more,
        next_candidates_before=proposed[MAX_CANDIDATES - 1][2] if more else None,
        agent_revised=revised(db, project, category),
        void_links=voided,
        lookalike=_lookalike([written for written, _ in groups.values()]),
    )


_LOOKALIKE: Final = str.maketrans(
    {
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d", "-"),
        **dict.fromkeys("\u2044\u2215\u29f8\uff0f", "/"),
        **dict(
            zip(
                "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0458\u0455\u04bb\u0501",
                "aeopcyxijshd",
                strict=True,
            )
        ),
        **dict(
            zip(
                "\u03b1\u03bf\u03c1\u03bd\u03ba\u03b9\u03c4\u03c5\u03c7\u03b5",
                "aopvkituxe",
                strict=True,
            )
        ),
    }
)
"""A partial table of characters that look like ASCII ones: Unicode dashes and slashes,
and Cyrillic and Greek letters that look Latin, after case folding."""


def _skeleton(text: str) -> str:
    """``text`` as it looks, near enough: as ``normal`` reads it, with the lookalikes of
    ``_LOOKALIKE`` and every other script's digits made ASCII."""
    folded = normal(text).translate(_LOOKALIKE)
    return "".join(
        str(digit) if (digit := unicodedata.decimal(c, None)) is not None else c for c in folded
    )


def _lookalike(groups: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """The sets of groups whose conditions differ only in characters that look alike:
    the user's spellings, which the frontier keeps apart, so it warns."""
    found: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for conditions in groups:
        found.setdefault(tuple(_skeleton(v) for v in conditions.values()), []).append(conditions)
    return [same for same in found.values() if len(same) > 1]


_Member = tuple[FrontierResult, bool, bool]
"""A result as the frontier shows it, whether it is eligible, and whether a candidate."""


def _member(
    db: sqlite3.Connection, row: tuple[Any, ...], name: str, *, full: bool = True
) -> _Member | None:
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
    by = confirmed(db, entry, row[4]) if eligible and origin == "published" else None
    unverified = eligible and origin == "published" and by is None
    eligible = eligible and not unverified
    candidate = not eligible and (
        unverified or (origin == "project" and status in ("proposed", "disputed"))
    )
    if not (eligible or candidate):
        return None
    shown = _result(db, row, fields, disputed=disputed, unverified=unverified, full=full)
    if by is not None:
        shown = shown.model_copy(update={"confirmed_by": by})
    return shown, eligible, candidate


def confirmed(db: sqlite3.Connection, entry: str, revision: int) -> str | None:
    """Who confirmed published result ``entry`` as ``revision`` reads (LG-16 as amended):
    ``user`` when the user's latest revision holds that material and the user wrote it,
    by recording the entry or by a revision that brought it in, or checked it (``ledger
    check``, or the workspace's: a check of the entry itself, never another target such
    as a delegation's outcome); else ``citation check`` when the consistency reviewer's
    checks confirm each of its quotes (CR-4, ``_cited``); None when neither did. The
    user's word comes first, so a result both confirm says ``user``. A user's other
    edits, such as a title or a dropped link, confirm nothing, and material an agent
    writes after the user's latest revision is not what the user confirmed."""
    # The owner's rule (2026-10-15): only checks and confirmations recorded since the
    # entry was last weakened confirm it, as of the revision that brought in the status and
    # material ``revision`` holds; a later revision that keeps both, such as one adding a
    # caveat, weakens nothing.
    rows = db.execute(
        "SELECT revision,status,created,json_extract(author,'$.kind') FROM ledger_revisions "
        "WHERE entry_id=? AND revision<=? ORDER BY revision DESC LIMIT ?",
        (entry, revision, gate.KEPT_REVISIONS),
    ).fetchall()
    held = gate.examined(db, entry, {row[0] for row in rows})
    start = 0
    while (
        start + 1 < len(rows)
        and rows[start + 1][1] == rows[0][1]
        and (held.get(rows[start + 1][0]) == held.get(revision))
    ):
        start += 1
    _, status, created, author = rows[start]
    if author == "user" and start + 1 < len(rows):
        # The user moved it back into this status: the user's authority restores what
        # confirmed the same material in that status before (the coordinator, 2026-10-16).
        before = next(
            (
                row[0]
                for row in rows[start + 1 :]
                if row[1] == status and held.get(row[0]) == held.get(revision)
            ),
            None,
        )
        if before is not None:
            return confirmed(db, entry, before)
    since = gate.weakened(db, entry, created)
    if _by_user(db, entry, revision, since):
        return "user"
    return "citation check" if _cited(db, entry, revision, since) else None


def _cited(db: sqlite3.Connection, entry: str, revision: int, since: float | None) -> bool:
    """Whether the consistency reviewer's checks confirm revision ``revision`` of
    ``entry`` (CR-4; LG-16 as amended): it holds at least one quote, and each
    - cites a reference that is not refuted, withdrawn or superseded, in a link that counts;
    - of a PDF, is of a PDF the user vouches for (``_vouched``);
    - has a ``supported`` check of the revision's statement, fields, assumptions and
      evidence, its latest of them, and no ``unsupported`` check of any revision since,
      so that a revision that returns to checked material does not return to its check."""
    quotes = [link for link in evidence.stored(db, entry, revision) if link.target == "quote"]
    material = gate.examined(db, entry, {revision}).get(revision)
    if not quotes or material is None:
        return False
    digest = gate.fingerprint(material)
    vouched: dict[str, set[str]] = {}
    for link in quotes:
        if not gate.counts(db, link):
            return False
        if (link.locator or {}).get("source") == "pdf":
            if link.target_id not in vouched:
                vouched[link.target_id] = _vouched(db, link.target_id)
            if link.sha256 not in vouched[link.target_id]:
                return False
        latest = db.execute(
            "SELECT verdict,created,rowid FROM citation_checks WHERE entry_id=? AND "
            "evidence_id=? AND material=? ORDER BY created DESC, rowid DESC LIMIT 1",
            (entry, link.evidence_id, digest),
        ).fetchone()
        if latest is None or latest[0] != "supported" or (since or 0.0) >= latest[1]:
            return False
        if db.execute(
            "SELECT 1 FROM citation_checks WHERE entry_id=? AND evidence_id=? AND "
            "verdict='unsupported' AND (created>? OR (created=? AND rowid>?)) LIMIT 1",
            (entry, link.evidence_id, latest[1], latest[1], latest[2]),
        ).fetchone():
            return False
    return True


def _vouched(db: sqlite3.Connection, reference: str) -> set[str]:
    """The SHA-256s of the PDFs the user vouches for as ``reference``'s (LG-16 as
    amended): those named by the revisions of it that the user checked (``ledger check``,
    or the workspace's), whose PDF field, hash included, the revision pins. Vouching is
    explicit: no other revision of the user's vouches, any more than a user's other edits
    confirm a result, and an agent may name any artifact as a reference's PDF, one it
    made included. The e-print server vouches for none yet: no e-print result names a
    hash."""
    head = db.execute("SELECT head FROM ledger_entries WHERE entry_id=?", (reference,)).fetchone()
    if head is None:
        return set()
    checked = {
        link.checked
        for link in evidence.stored(db, reference, head[0])
        if link.checked is not None
        and _own_check(link, reference)
        and gate.added_by_user(db, reference, link, head[0])
    }
    found: set[str] = set()
    for revision in checked:
        row = db.execute(
            "SELECT json_extract(fields,'$.pdf.sha256') FROM ledger_revisions "
            "WHERE entry_id=? AND revision=?",
            (reference, revision),
        ).fetchone()
        if row is not None and row[0] is not None:
            found.add(row[0])
    return found


def _own_check(link: EvidenceLink, entry: str) -> bool:
    """Whether ``link`` is a check of ``entry`` itself, as ``ledger check`` records one:
    an ``independent_check`` whose target is the entry, added by the revision after the
    one it checked. Another target, such as a delegation's outcome, is not the user's
    check, whoever added it."""
    return (
        link.role == "independent_check"
        and link.target == "entry"
        and link.target_id == entry
        and link.checked is not None
        and link.added == link.checked + 1
    )


def _by_user(db: sqlite3.Connection, entry: str, revision: int, since: float | None) -> bool:
    """Whether the user confirmed ``entry`` as ``revision`` reads, as ``confirmed`` says."""
    latest = db.execute(
        "SELECT max(revision) FROM ledger_revisions WHERE entry_id=? "
        "AND json_extract(author,'$.kind')='user'",
        (entry,),
    ).fetchone()[0]
    if latest is None:
        return False
    found = gate.examined(db, entry, {revision, latest})
    now = found.get(revision)
    if now is None or found.get(latest) != now:
        return False
    # The revision that brought the material in: did the user write it?
    at = latest
    while (
        at > max(1, latest - gate.KEPT_REVISIONS)
        and gate.examined(db, entry, {at - 1}).get(at - 1) == now
    ):
        at -= 1
    wrote = db.execute(
        "SELECT created FROM ledger_revisions WHERE entry_id=? AND revision=? "
        "AND json_extract(author,'$.kind')='user'",
        (entry, at),
    ).fetchone()
    if wrote is not None and datetime.fromisoformat(wrote[0]).timestamp() > (since or 0.0):
        return True
    checked = {
        link.checked
        for link in evidence.stored(db, entry, latest)
        if link.checked is not None
        and _own_check(link, entry)
        and gate.added_by_user(db, entry, link, latest)
        and gate.after(db, entry, link, since)
    }
    return now in gate.examined(db, entry, checked).values()


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


def is_eligible(db: sqlite3.Connection, entry: str, origin: str | None, status: str) -> bool:
    """Whether a result of ``origin`` that a change leaves in ``status`` is eligible by
    its status (LG-20): a recorded published result or an established project one, or
    one now disputed whose revision before it became disputed was either. Whether the
    user confirmed an agent's published result, the frontier reads apart."""
    wanted = _ELIGIBLE.get(origin or "")
    if status != "disputed":
        return status == wanted
    before = db.execute(
        "SELECT status FROM ledger_revisions WHERE entry_id=? AND status!='disputed' "
        "ORDER BY revision DESC LIMIT 1",
        (entry,),
    ).fetchone()
    return before is not None and before[0] == wanted


def comparable(category: Category, a: Json, b: Json) -> bool:
    """Whether the rule compares two results' fields (LG-21): their conditions are equal
    on every condition key, as ``normal`` reads them, and every measure is a number for
    both."""
    first, second = _key(category, a), _key(category, b)
    return first is not None and first == second and _numbers(category, a) and _numbers(category, b)


def _key(category: Category, fields: Json) -> tuple[str, ...] | None:
    """A result's conditions as the frontier groups them; None when one is missing."""
    given = {normal(k): v for k, v in fields.get("conditions", {}).items()}
    values = [given.get(normal(key)) for key in category.conditions]
    return None if any(v is None for v in values) else tuple(normal(str(v)) for v in values)


def _numbers(category: Category, fields: Json) -> bool:
    measures = fields.get("measures", {})
    return all(isinstance(measures.get(m.name), (int, float)) for m in category.measures)


CYCLE: Final = (
    "it closes a cycle of results that dominate one another, by dominates links or by the "
    "rule: it counts for nothing until a link of the cycle is dropped"
)
RULED: Final = "the frontier compares the two results itself now: the rule decides"


def _void(
    category: Category, shown: dict[str, FrontierResult], links: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """The links of ``links`` (holders to targets, among ``shown``) that count for nothing,
    each with why: one between two results the rule compares (M1), or one on a cycle of
    links and the rule's dominance (M2). Within a group the rule's dominance is a strict
    order, so a cycle through it passes from link to link, and reading the links' own
    ends, with the rule between them, finds every one."""
    void: dict[tuple[str, str], str] = {}
    fields = {e: {"conditions": r.conditions, "measures": r.measures} for e, r in shown.items()}
    edges: dict[str, set[str]] = {}
    for holder, target in links:
        if comparable(category, fields[holder], fields[target]):
            void[(holder, target)] = RULED
        else:
            edges.setdefault(holder, set()).add(target)
    ends = {e for pair in links if pair not in void for e in pair}
    for a in ends:
        for b in ends:
            if (
                a != b
                and comparable(category, fields[a], fields[b])
                and beats(category, fields[a]["measures"], fields[b]["measures"])
            ):
                edges.setdefault(a, set()).add(b)

    def reaches(start: str, goal: str) -> bool:
        seen, todo = {start}, [start]
        while todo:
            at = todo.pop()
            if at == goal:
                return True
            for nxt in edges.get(at, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    for holder, target in links:
        if (holder, target) not in void and reaches(target, holder):
            void[(holder, target)] = CYCLE
    return void


def _linked(
    db: sqlite3.Connection, category: Category, eligible: list[FrontierResult]
) -> tuple[dict[str, list[DominatesLink]], list[DominatesLink]]:
    """The eligible results the ``dominates`` links of other eligible ones name, each
    with those links: who holds it, its reason and who recorded it; and apart, the links
    that count for nothing, each with why (M1, M2)."""
    shown = {r.entry_id: r for r in eligible}
    above: dict[str, list[DominatesLink]] = {}
    if not shown:
        return above, []
    marks = ",".join("?" * len(shown))
    rows = [
        (holder, target, note, head)
        for holder, target, note, head in db.execute(
            "SELECT x.entry_id,x.target_id,x.note,e.head FROM ledger_links x "  # noqa: S608 - placeholders
            "JOIN ledger_entries e ON e.entry_id=x.entry_id AND x.revision=e.head "
            f"WHERE x.kind='dominates' "
            f"AND x.target='entry' AND x.entry_id IN ({marks}) ORDER BY e.position,x.ordinal",
            list(shown),
        )
        if target in shown and target != holder
    ]
    void = _void(category, shown, [(h, t) for h, t, _, _ in rows])
    voided: list[DominatesLink] = []
    for holder, target, note, head in rows:
        # The holder's revision that added the link, which its author recorded.
        added = db.execute(
            "SELECT coalesce(max(r.revision),0)+1 FROM ledger_revisions r WHERE r.entry_id=? "
            "AND r.revision<=? AND NOT EXISTS (SELECT 1 FROM ledger_links x WHERE "
            "x.entry_id=r.entry_id AND x.revision=r.revision AND x.kind='dominates' "
            "AND x.target='entry' AND x.target_id=?)",
            (holder, head, target),
        ).fetchone()[0]
        author = db.execute(
            "SELECT author FROM ledger_revisions WHERE entry_id=? AND revision=?",
            (holder, added),
        ).fetchone()[0]
        link = DominatesLink(
            entry_id=holder,
            handle=shown[holder].handle,
            target=shown[target].handle,
            reason=note or "",
            author=json.loads(author),
            revision=added,
            void=void.get((holder, target)),
        )
        if link.void is None:
            above.setdefault(target, []).append(link)
        else:
            voided.append(link)
    return above, voided


def _above(result: FrontierResult, links: list[DominatesLink]) -> FrontierResult:
    if not links:
        return result
    return result.model_copy(
        update={
            "dominated_by": [*result.dominated_by, *(link.handle for link in links)],
            "linked_by": links,
        }
    )


def _dominated(group: FrontierGroup, above: dict[str, list[DominatesLink]]) -> FrontierGroup:
    """``group`` once the ``dominates`` links have taken the results they name from the
    best known and from those not compared (LG-22)."""
    if not any(r.entry_id in above for r in (*group.best, *group.dominated, *group.not_compared)):
        return group
    moved = [r for r in (*group.best, *group.not_compared) if r.entry_id in above]
    dominated = [_above(r, above.get(r.entry_id, [])) for r in (*group.dominated, *moved)]
    return group.model_copy(
        update={
            "best": [r for r in group.best if r.entry_id not in above],
            "not_compared": [r for r in group.not_compared if r.entry_id not in above],
            "dominated": dominated,
        }
    )


def _group(category: Category, conditions: dict[str, str], members: list[_Member]) -> FrontierGroup:
    """One set of conditions' best known, dominated, not compared and candidate results,
    each in the order they were made. Read from the best on, a result's dominators all
    come before it, and one is best known exactly when no best known result before it
    dominates it, so a group of g results with s best known takes g times s comparisons,
    not g squared, in the history's write lock too; a dominated result names the best
    known results that dominate it."""
    compared = [r for r, eligible, _ in members if eligible and _numeric(category, r)]
    place = {r.entry_id: n for n, r in enumerate(compared)}
    signed = {r.entry_id: _goodness(category, r) for r in compared}
    found: list[tuple[FrontierResult, tuple[float, ...]]] = []
    above: dict[str, list[FrontierResult]] = {}
    for result in sorted(compared, key=lambda r: signed[r.entry_id]):
        mine = signed[result.entry_id]
        over = [b for b, theirs in found if _ahead(theirs, mine)]
        if over:
            above[result.entry_id] = over
        else:
            found.append((result, mine))
    best = [r for r in compared if r.entry_id not in above]
    dominated = [
        r.model_copy(
            update={
                "dominated_by": [
                    b.handle for b in sorted(above[r.entry_id], key=lambda b: place[b.entry_id])
                ]
            }
        )
        for r in compared
        if r.entry_id in above
    ]
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
        key=_slot(conditions),
        best=best,
        dominated=dominated,
        not_compared=[r for r, eligible, _ in members if eligible and not _numeric(category, r)],
        candidates=candidates,
    )


def _goodness(category: Category, result: FrontierResult) -> tuple[float, ...]:
    """``result``'s measures, each signed so that less is better: a result that dominates
    another comes before it in this order."""
    return tuple(
        _number(result.measures, m.name) * (1 if m.direction == "lower" else -1)
        for m in category.measures
    )


def _ahead(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """Whether signed measures ``a`` dominate ``b``: no worse on any, and not the same."""
    return a != b and all(x <= y for x, y in zip(a, b, strict=True))


def _numeric(category: Category, result: FrontierResult) -> bool:
    """Whether every measure of the category is a number for ``result``."""
    return all(isinstance(result.measures.get(m.name), (int, float)) for m in category.measures)


def dominates(category: Category, a: FrontierResult, b: FrontierResult) -> bool:
    """Whether ``a`` dominates ``b`` (LG-21): at least as good on every measure of the
    category, and better on one; both give every measure as a number."""
    return beats(category, a.measures, b.measures)


def beats(category: Category, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether measures ``a`` dominate measures ``b`` (LG-21), every one a number."""
    better = False
    for measure in category.measures:
        x, y = _number(a, measure.name), _number(b, measure.name)
        if x == y:
            continue
        if (x < y) != (measure.direction == "lower"):
            return False
        better = True
    return better


def _number(measures: dict[str, Any], name: str) -> float:
    value = measures[name]
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} is {value!r}, not a number")
    return value


def _result(
    db: sqlite3.Connection,
    row: tuple[Any, ...],
    fields: Json,
    *,
    disputed: bool,
    unverified: bool,
    full: bool = True,
) -> FrontierResult:
    """The entry of ``row`` as the frontier shows it, at the revision ``row`` names."""
    entry = row[0]
    if not full:
        return FrontierResult(
            entry_id=entry,
            handle=f"{_PREFIX[row[1]]}-{row[2]}",
            revision=row[4],
            title=row[5],
            statement="",
            status=row[6],
            origin=row[3],
            disputed=disputed,
            unverified=unverified,
            author=json.loads(row[9]),
            conditions=fields.get("conditions", {}),
            measures=fields.get("measures", {}),
            evidence_types=[],
            sources=[],
            artifacts=[],
            open_findings=0,
            open_flags=0,
        )
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
            "AND f.status='open'",
            (entry,),
        ).fetchone()[0],
        open_flags=db.execute(
            "SELECT count(*) FROM ledger_flags WHERE entry_id=? AND cleared IS NULL", (entry,)
        ).fetchone()[0],
    )


def record(
    db: sqlite3.Connection, project: str, categories: set[str], cause: Json, now: str
) -> list[tuple[str, list[FrontierChange]]]:
    """Record each change the open change ``db`` made to the best known results of
    ``categories`` (LG-24): for every set of conditions whose best known results are not
    the ones last recorded, the set as it is now, what it added and removed, and
    ``cause``. Returns the changes, by category."""
    found: list[tuple[str, list[FrontierChange]]] = []
    for name in sorted(categories):
        category = read(db, project, name)
        if category is None:
            continue
        now_best: dict[str, list[FrontierMember]] = {
            _slot(group.conditions): [
                FrontierMember(entry_id=r.entry_id, handle=r.handle, revision=r.revision)
                for r in group.best
            ]
            for group in view(db, project, category, full=False).groups
        }
        before: dict[str, list[str]] = {}
        handles: dict[str, str] = {}
        for conditions, best in db.execute(
            "SELECT c.conditions,c.best FROM frontier_changes c WHERE c.project=? "
            "AND c.category=? AND c.seq=(SELECT max(d.seq) FROM frontier_changes d "
            "WHERE d.project=c.project AND d.category=c.category "
            "AND d.conditions=c.conditions)",
            (project, name),
        ):
            members = json.loads(best)
            before[conditions] = [m["entry_id"] for m in members]
            handles |= {m["entry_id"]: m["handle"] for m in members}
        changes: list[FrontierChange] = []
        for key in sorted(set(now_best) | set(before)):
            members = now_best.get(key, [])
            ids = [m.entry_id for m in members]
            if set(ids) == set(before.get(key, [])):
                continue
            handles |= {m.entry_id: m.handle for m in members}
            added = [m.handle for m in members if m.entry_id not in before.get(key, [])]
            removed = [handles[i] for i in before.get(key, []) if i not in ids]
            seq = db.execute(
                "INSERT INTO frontier_changes(project,category,conditions,best,added,removed,"
                "cause,created) VALUES(?,?,?,?,?,?,?,?)",
                (
                    project,
                    name,
                    key,
                    json.dumps([m.model_dump(mode="json") for m in members]),
                    json.dumps(added),
                    json.dumps(removed),
                    _bounded(cause),
                    now,
                ),
            ).lastrowid
            assert seq is not None  # noqa: S101 - inserted above
            changes.append(
                FrontierChange(
                    seq=seq,
                    conditions=json.loads(key),
                    best=members,
                    added=added,
                    removed=removed,
                    cause=json.loads(_bounded(cause)),
                    created=now,
                )
            )
        if changes:
            found.append((name, changes))
    return found


def history(
    db: sqlite3.Connection, project: str, category: str, before: int | None, limit: int
) -> FrontierHistory:
    """``category``'s frontier changes, newest first, before ``before``."""
    rows = db.execute(
        "SELECT seq,conditions,best,added,removed,cause,created FROM frontier_changes "
        "WHERE project=? AND category=? AND seq<? ORDER BY seq DESC LIMIT ?",
        (project, category, before or 2**62, limit + 1),
    ).fetchall()
    changes = [
        FrontierChange(
            seq=row[0],
            conditions=json.loads(row[1]),
            best=[FrontierMember.model_validate(m) for m in json.loads(row[2])],
            added=json.loads(row[3]),
            removed=json.loads(row[4]),
            cause=json.loads(row[5]),
            created=row[6],
        )
        for row in rows[:limit]
    ]
    return FrontierHistory(
        category=category,
        changes=changes,
        next_before=changes[-1].seq if len(rows) > limit else None,
    )


def past(db: sqlite3.Connection, project: str, category: str, at: str) -> FrontierPast:
    """``category``'s best known results as the last change recorded at or before ``at``
    left them, under each set of conditions (LG-24)."""
    groups = [
        FrontierPastGroup(
            conditions=json.loads(conditions),
            best=[FrontierMember.model_validate(m) for m in json.loads(best)],
        )
        for conditions, best in db.execute(
            "SELECT c.conditions,c.best FROM frontier_changes c WHERE c.project=? "
            "AND c.category=? AND c.seq=(SELECT max(d.seq) FROM frontier_changes d "
            "WHERE d.project=c.project AND d.category=c.category "
            "AND d.conditions=c.conditions AND d.created<=?) ORDER BY c.seq",
            (project, category, at),
        )
    ]
    return FrontierPast(category=category, at=at, groups=[g for g in groups if g.best])


SLOT_BYTES: Final = 8192
"""The most bytes of a set of conditions the history keeps (its table's CHECK)."""

CAUSE_BYTES: Final = 16384
"""The most bytes of a change's cause the history keeps (its table's CHECK)."""


def _slot(conditions: dict[str, str]) -> str:
    """A set of conditions as the history keys it: as ``normal`` reads them, so a group
    keeps its key whichever result's spelling it shows, and within ``SLOT_BYTES``: past
    them, each value is cut, and ``#`` holds a digest of the whole set, which keeps two
    sets apart (the review's L1 and M4)."""
    read = {key: normal(value) for key, value in conditions.items()}
    text = json.dumps(read, sort_keys=True, ensure_ascii=False)
    if len(text.encode()) <= SLOT_BYTES:
        return text
    cut: dict[str, str] = {key: value[:24] + "…" for key, value in read.items()}
    cut["#"] = hashlib.sha256(text.encode()).hexdigest()
    return json.dumps(cut, sort_keys=True, ensure_ascii=False)


def _bounded(cause: Json) -> str:
    """``cause`` as the history keeps it, within ``CAUSE_BYTES``: past them, its reason
    and links are cut short, and then left out, so recording never refuses a change."""
    text = json.dumps(cause, sort_keys=True, ensure_ascii=False)
    for shorter in (
        {
            **cause,
            "reason": str(cause.get("reason", ""))[:200],
            "dominates": cause.get("dominates", [])[:4],
        },
        {key: value for key, value in cause.items() if key not in ("reason", "dominates")},
    ):
        if len(text.encode()) <= CAUSE_BYTES:
            break
        text = json.dumps(shorter, sort_keys=True, ensure_ascii=False)
    return text
