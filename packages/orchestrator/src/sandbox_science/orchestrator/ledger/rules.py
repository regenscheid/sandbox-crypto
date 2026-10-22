# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger rules. The caller owns the transaction."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import NoReturn

from sandbox_science.orchestrator.documents.comment_bodies import sha
from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    DocumentError,
)

from . import gate, preparation, queries
from .models import (
    DECIDED,
    SET_ASIDE,
    EntryLink,
)
from .preparation import fingerprint as fingerprint
from .records import TEXTS, Json, Planned


def check_decisions(db: sqlite3.Connection, planned: list[Planned], role: str) -> set[str]:
    """Statuses only a decision sets (LG-4): an entry becomes ``established``,
    ``obstructed`` or ``refuted``, and a decision ``reversed``, only in a change that
    puts in effect a decision that affects it. The main agent's decision to establish
    a result needs the gate (LG-17), and one that makes an issue obstructed needs an
    accepted assessment (LG-7, ``_check_obstruction``). An entry leaves
    ``refuted``, ``withdrawn`` or ``superseded`` only by such a decision too, which
    for the main agent's on a claim or result passes the gate, whatever status it
    brings the entry back to; and a claim or result that was once established or
    recorded keeps its statement, fields, assumptions and evidence while it is
    ``disputed``, as a decided one does. Returns the entries the gate passed, which
    the flags the change raises must spare."""
    gated: set[str] = set()
    deciding = [
        p
        for p in planned
        if p.row.type == "decision" and p.status == "in_effect" and p.status_before != "in_effect"
    ]
    for p in planned:
        decided = (
            p.status in DECIDED
            or (p.row.type == "decision" and p.status == "reversed")
            # Set aside, an entry comes back only by a decision.
            or (not p.created and p.status_before in SET_ASIDE)
        )
        # An entry stays as decided: while it is established, refuted or obstructed,
        # or disputed after it was established or recorded, what a decision rested on
        # changes only with its status or a new decision.
        rewritten = (
            not p.created
            and p.status == p.status_before
            and (p.status_before in DECIDED or disputed_once_held(db, p))
            and rewrites(db, p)
        )
        if not (decided and "status" in p.changed) and not rewritten:
            continue
        if rewritten and not any(
            d is not p
            and any(
                link.kind == "affects" and link.target_id == p.row.entry_id
                for link in preparation.final_links(db, d)
            )
            for d in deciding
        ):
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} is {p.status}: its statement, fields, assumptions and "
                "evidence change only with its status, or in a change that puts in "
                "effect a decision that affects it",
            )
        if not any(
            d is not p
            and any(
                link.kind == "affects" and link.target_id == p.row.entry_id
                for link in preparation.final_links(db, d)
            )
            for d in deciding
        ):
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} leaves {p.status_before} only in a change that puts in "
                "effect a decision that affects it"
                if p.status_before in SET_ASIDE
                else f"{p.row.handle} becomes {p.status} only in a change that puts in "
                "effect a decision that affects it",
            )
        if role != "main":
            continue
        # Set aside, a claim or result the main agent brings back by any road passes
        # the gate: through disputed or proposed it could otherwise be recorded again
        # with nothing checked.
        if p.status == "established" or (
            p.status_before in SET_ASIDE and p.row.type in ("claim", "result")
        ):
            held = preparation.final_evidence(db, p)
            latest = TEXTS.latest(db, p.row.entry_id)
            assert latest is not None  # noqa: S101 - an existing entry has a statement
            now = gate.material(
                p.fields,
                None if p.statement is not None else latest[0],
                preparation.final_links(db, p),
                held,
                latest[1] if p.statement is None else sha(p.statement),
            )
            gate.establish(db, p.row.entry_id, p.row.handle, held, now, p.row.head)
            gated.add(p.row.entry_id)
        elif p.status == "obstructed":
            check_obstruction(db, p, deciding)
    return gated


def check_obstruction(db: sqlite3.Connection, p: Planned, deciding: list[Planned]) -> None:
    """The main agent's decision makes or keeps an issue ``obstructed`` (LG-7, LG-F8)
    only when it names the excluded scope, its evidence (an evidence link) and the
    assessment that accepted the claimed obstruction: the user's, the latest of the
    user's on that claim, both on the issue; and only for the content they were made
    on: the issue's statement and fields at the claim's and the assessment's revisions
    are those it has, and the change leaves them as they are. So a reworded issue, or
    an obstructed one rewritten, needs a fresh acceptance (as #277 and #281 hold for
    decided entries). A delegation started to assess the claim will count too once
    delegations can be briefed to assess one."""
    issue = p.row

    def refuse(why: str) -> NoReturn:
        raise gate.refused(
            issue.entry_id,
            "assessment",
            f"{issue.handle} is obstructed by the main agent's decision only on the "
            f"user's accepted assessment of an obstruction claimed on it: {why}",
        )

    naming = [
        d
        for d in deciding
        if d is not p
        and any(
            link.kind == "affects" and link.target_id == issue.entry_id
            for link in preparation.final_links(db, d)
        )
    ]
    named = [
        d
        for d in naming
        if d.fields.get("assessment")
        and d.fields.get("excluded_scope")
        and preparation.final_evidence(db, d)
    ]
    if not named:
        refuse(
            "the decision names the assessment (fields.assessment), the scope it "
            "excludes (fields.excluded_scope) and its evidence (an evidence link)"
        )
    if p.created or "statement" in p.changed or "fields" in p.changed:
        refuse("the change keeps the issue's statement and fields as they were assessed")
    current = queries.content(db, issue.entry_id, issue.head)
    for d in named:
        found = db.execute(
            "SELECT a.entry_id,a.verdict,json_extract(a.author,'$.kind'),"
            "a.entry_revision,c.record_id,c.entry_id,c.ending,c.entry_revision,a.created "
            "FROM ledger_work a JOIN ledger_work c ON c.record_id=a.assesses "
            "WHERE a.record_id=? AND a.project=?",
            (d.fields["assessment"], issue.project),
        ).fetchone()
        if found is None:
            refuse(f"no assessment {d.fields['assessment']} of a claim on it")
        on, verdict, kind, at, claim, claimed_on, ending, claimed_at, made = found
        # The owner's rule (2026-10-15): the acceptance is recorded after the issue was
        # last weakened, reopened among others, or obstructed.
        moved = [
            datetime.fromisoformat(created).timestamp()
            for (created,) in db.execute(
                "SELECT r.created FROM ledger_revisions r JOIN ledger_revisions b "
                "ON b.entry_id=r.entry_id AND b.revision=r.revision-1 WHERE r.entry_id=? "
                "AND r.status='obstructed' AND b.status!='obstructed'",
                (issue.entry_id,),
            )
        ]
        since = max([*moved, gate.weakened(db, issue.entry_id) or 0.0], default=0.0)
        if datetime.fromisoformat(made).timestamp() <= since:
            refuse(
                "the acceptance it names was recorded before the issue was last obstructed, "
                "reopened or flagged: an obstruction needs an acceptance recorded since"
            )
        if (on, claimed_on, ending) != (issue.entry_id, issue.entry_id, "obstruction_claimed"):
            refuse("the assessment it names is not of an obstruction claimed on it")
        if (verdict, kind) != ("accepted", "user"):
            refuse("the assessment it names is not the user's acceptance")
        (latest,) = db.execute(
            "SELECT verdict FROM ledger_work WHERE assesses=? "
            "AND json_extract(author,'$.kind')='user' ORDER BY position DESC LIMIT 1",
            (claim,),
        ).fetchone()
        if latest != "accepted":
            refuse("the user's latest assessment of the claim does not accept it")
        if db.execute(
            "SELECT 1 FROM ledger_revisions r JOIN ledger_entries e USING(entry_id) "
            "WHERE e.type='decision' AND r.entry_id!=? "
            "AND json_extract(r.fields,'$.assessment')=? LIMIT 1",
            (d.row.entry_id, d.fields["assessment"]),
        ).fetchone():
            refuse(
                "the acceptance it names settled an earlier obstruction: each obstruction "
                "needs a fresh acceptance of the user's"
            )
        if not (
            queries.content(db, issue.entry_id, at)
            == current
            == queries.content(db, issue.entry_id, claimed_at)
        ):
            refuse(
                "its statement or fields changed since the claim or its acceptance: a "
                "reworded issue needs a fresh acceptance"
            )


def disputed_once_held(db: sqlite3.Connection, p: Planned) -> bool:
    """Whether ``p`` is a claim or result, disputed, that was once established or
    recorded: the frontier still shows it as it was then (LG-20)."""
    return (
        p.status_before == "disputed"
        and p.row.type in ("claim", "result")
        and db.execute(
            "SELECT 1 FROM ledger_revisions WHERE entry_id=? "
            "AND status IN ('established','recorded') LIMIT 1",
            (p.row.entry_id,),
        ).fetchone()
        is not None
    )


def rewrites(db: sqlite3.Connection, p: Planned) -> bool:
    """Whether the change alters what a decision on ``p``'s entry rested on: its
    statement, fields, ``assumes`` links or evidence."""
    if {"statement", "evidence"} & set(p.changed):
        return True
    if "fields" in p.changed and unsettles(p.fields_before, p.fields):
        return True
    if "links" not in p.changed:
        return False

    def assumed(links: list[EntryLink]) -> set[tuple[str, int | None]]:
        return {(link.target_id, link.revision) for link in links if link.kind == "assumes"}

    return assumed(p.previous) != assumed(preparation.final_links(db, p))


def unsettles(before: Json, after: Json) -> bool:
    """Whether fields ``after`` change what a decision on the entry rested on: anything
    but caveats added. A caveat qualifies a settled entry; dropping one strengthens it."""
    kept = [{k: v for k, v in f.items() if k != "caveats"} for f in (before, after)]
    return kept[0] != kept[1] or not set(before.get("caveats", ())) <= set(after.get("caveats", ()))
