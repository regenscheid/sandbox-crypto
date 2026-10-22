# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger rules. The caller owns the transaction."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import NoReturn, cast

from sandbox_science.orchestrator.documents.comment_bodies import sha
from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author

from . import evidence, frontier, gate, preparation, queries, records
from .models import (
    DECIDED,
    NO_CONDITION,
    PREFIXES,
    SET_ASIDE,
    EntryLink,
    EvidenceLink,
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


def check_quotes(db: sqlite3.Connection, planned: list[Planned]) -> None:
    """A quote names what it quotes as the reference reads at the revision it pins
    (LG-8, CR-4): a PDF quote, the reference's PDF, pinned by its SHA-256; an ePrint
    quote, the reference's ePrint identifier. The reference may be one the same
    change records or revises."""
    by_id = {p.row.entry_id: p for p in planned}
    for p in planned:
        if p.evidence is None:
            continue
        kept: list[EvidenceLink] = []
        for link in p.evidence:
            if link.target != "quote" or link.locator is None or "named" in link.locator:
                kept.append(link)
                continue
            kept.append(quoted(db, by_id, p, link))
        p.evidence = kept


def quoted(
    db: sqlite3.Connection, by_id: dict[str, Planned], p: Planned, link: EvidenceLink
) -> EvidenceLink:
    """``link``, a new quote, with what it quotes named from its reference's fields."""
    assert link.locator is not None  # noqa: S101 - a quote has one
    assert link.revision is not None  # noqa: S101 - and pins its reference
    reference = by_id.get(link.target_id)
    stored = db.execute(
        "SELECT head FROM ledger_entries WHERE entry_id=?", (link.target_id,)
    ).fetchone()
    if reference is not None and (stored is None or link.revision > stored[0]):
        fields: Json = reference.fields  # the revision the change makes
    else:
        row = db.execute(
            "SELECT fields FROM ledger_revisions WHERE entry_id=? AND revision=?",
            (link.target_id, link.revision),
        ).fetchone()
        fields = {} if row is None else json.loads(row[0])
    locator = dict(link.locator)
    handle = link.handle or link.target_id
    if locator["source"] == "pdf":
        pdf = fields.get("pdf")
        if not isinstance(pdf, dict):
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} quotes a PDF of {handle}, which names none: give the "
                "reference its pdf, or quote the ePrint",
            )
        locator["named"] = {"artifact_id": pdf["artifact_id"], "revision": pdf["revision"]}
        return link.model_copy(update={"locator": locator, "sha256": pdf["sha256"]})
    identifiers = cast(Json, fields.get("identifiers") or {})
    eprint = identifiers.get("eprint")
    if not eprint:
        raise DocumentError(
            INVALID_REQUEST,
            f"{p.row.handle} quotes an ePrint of {handle}, which names none: give the "
            "reference its eprint identifier, or quote its PDF",
        )
    locator["named"] = {"eprint": eprint}
    return link.model_copy(update={"locator": locator})


def check_published(db: sqlite3.Connection, planned: list[Planned]) -> None:
    """A published claim or result cites its reference through a ``supports`` link of
    type ``source_claim`` (LG-15), which may be to a reference the same change
    records."""
    made = {p.row.entry_id for p in planned if p.row.type == "reference"}
    for p in planned:
        if p.row.origin != "published" or (not p.created and p.evidence is None):
            continue
        targets = {link.target_id for link in p.evidence or () if link.target in ("entry", "quote")}
        references = made | {
            entry
            for (entry,) in db.execute(
                "SELECT entry_id FROM ledger_entries WHERE type='reference' AND entry_id IN "  # noqa: S608 - placeholders only
                f"({','.join('?' * len(targets))})",
                tuple(targets),
            )
        }
        if not evidence.cites_reference(p.evidence or [], references):
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} is published: it cites its reference with a supports "
                "link of type source_claim",
            )


def check_dominates(
    db: sqlite3.Connection, project: str, planned: list[Planned], role: str
) -> None:
    """A ``dominates`` link a change adds (LG-22): the user's or the main agent's,
    between two results of one category, both eligible as the change leaves them,
    where the rule cannot compare them."""
    by_id = {p.row.entry_id: p for p in planned}
    for p in planned:
        for link in records.links_added(p, "dominates"):
            if role == "delegated":
                raise records.refused("a delegated agent records no dominates link")
            other = by_id.get(link.target_id)
            if other is not None:
                target = (other.row.handle, other.row.origin, other.status, other.fields)
            else:
                row = db.execute(
                    "SELECT e.type,e.number,e.origin,r.status,r.fields FROM ledger_entries e "
                    "JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                    "WHERE e.entry_id=?",
                    (link.target_id,),
                ).fetchone()
                target = (f"{PREFIXES[row[0]]}-{row[1]}", row[2], row[3], json.loads(row[4]))
            handle, origin, status, fields = target
            for name, entry, of, now in (
                (p.row.handle, p.row.entry_id, p.row.origin, p.status),
                (handle, link.target_id, origin, status),
            ):
                if not frontier.is_eligible(db, entry, of, now):
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{name} is not eligible for the frontier: a dominates link is "
                        "between recorded published or established project results",
                    )
            name = p.fields.get("category")
            category = None if name is None else frontier.read(db, project, name)
            if category is None or fields.get("category") != name:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} and {handle} are not results of one frontier category",
                )
            if frontier.comparable(category, p.fields, fields):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"the frontier compares {p.row.handle} and {handle} itself: their "
                    "conditions are equal and their measures numbers",
                )


def check_cycles(db: sqlite3.Connection, project: str, planned: list[Planned]) -> None:
    """Once a change is written, no ``dominates`` link it added lies on a cycle of
    results that dominate one another, by links or by the rule (the review's M2), as
    the frontier reads them: a disputed result as the revision before, and only the
    results it holds eligible."""
    added = {
        (p.row.entry_id, link.target_id): p
        for p in planned
        for link in records.links_added(p, "dominates")
    }
    if not added:
        return
    names = {p.fields.get("category") for p in added.values()} - {None}
    for name in sorted(str(n) for n in names):
        category = frontier.read(db, project, name)
        if category is None:
            continue
        for link in frontier.view(db, project, category).void_links:
            p = added.get((link.entry_id, queries.entry_id(db, project, link.target)))
            if p is not None and link.void == frontier.CYCLE:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{link.handle} dominating {link.target} closes a cycle: "
                    f"{link.target} already dominates {link.handle}, by dominates links "
                    "or by the rule, through the results between, as the frontier "
                    "reads them",
                )


def check_links(db: sqlite3.Connection, planned: list[Planned]) -> None:
    """What holds once the change commits (LG-5, LG-30). ``supersedes`` and
    ``split_into`` links never lead back to their holder. An entry is superseded
    exactly while a current revision supersedes it, so a supersedes link and its
    target becoming superseded commit together, and so do the link's removal and the
    target leaving superseded; an entry that is superseded supersedes nothing new. An
    issue is split exactly while it holds split_into links."""
    by_id = {p.row.entry_id: p for p in planned}
    final = {
        p.row.entry_id: p.links
        if p.links is not None
        else queries.links(db, p.row.entry_id, p.row.head)
        for p in planned
    }

    def targets(entry: str, kind: str) -> list[str]:
        if entry in final:
            return [
                link.target_id
                for link in final[entry]
                if link.kind == kind and link.target == "entry"
            ]
        return [row[0] for row in db.execute(queries.TARGETS, (entry, kind))]

    def holders(entry: str, kind: str) -> list[str]:
        stored = [row[0] for row in db.execute(queries.HOLDERS, (entry, kind))]
        return [holder for holder in stored if holder not in final] + [
            holder for holder in final if entry in targets(holder, kind)
        ]

    def status(entry: str) -> str:
        if entry in by_id:
            return by_id[entry].status
        return db.execute(
            "SELECT r.status FROM ledger_entries e JOIN ledger_revisions r ON "
            "r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
            (entry,),
        ).fetchone()[0]

    def handle(entry: str) -> str:
        if entry in by_id:
            return by_id[entry].row.handle
        kind, number = db.execute(
            "SELECT type,number FROM ledger_entries WHERE entry_id=?", (entry,)
        ).fetchone()
        return f"{PREFIXES[kind]}-{number}"

    superseding: list[str] = []
    for p in planned:
        for link in [
            *records.links_added(p, "supersedes"),
            *records.links_added(p, "split_into"),
        ]:
            seen: set[str] = set()
            reached = [link.target_id]
            while reached:
                entry = reached.pop()
                if entry == p.row.entry_id:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{p.row.handle} {link.kind} {link.handle}, "
                        f"which already leads back to {p.row.handle}",
                    )
                if entry not in seen:
                    seen.add(entry)
                    reached.extend(targets(entry, link.kind))
        for link in records.links_added(p, "supersedes"):
            if p.status == "superseded":
                raise DocumentError(
                    INVALID_REQUEST, f"{p.row.handle} is superseded and supersedes nothing new"
                )
            if status(link.target_id) != "superseded":
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{link.handle} becomes superseded in the change that supersedes it",
                )
        superseding += [link.target_id for link in records.links_dropped(p, "supersedes")]
    moved = [p.row.entry_id for p in planned if "status" in p.changed]
    for entry in dict.fromkeys([*moved, *superseding]):
        if by_id.get(entry) is not None and by_id[entry].row.type not in ("claim", "result"):
            continue
        held = holders(entry, "supersedes")
        if status(entry) == "superseded" and not held:
            raise DocumentError(
                INVALID_REQUEST,
                f"{handle(entry)} is superseded only while an entry supersedes it",
            )
        if status(entry) != "superseded" and held:
            raise DocumentError(
                INVALID_REQUEST,
                f"{handle(entry)} stays superseded while {handle(held[0])} supersedes it",
            )
    for p in planned:
        if p.row.type != "issue" or ("status" not in p.changed and p.change.links is None):
            continue
        split = bool(targets(p.row.entry_id, "split_into"))
        if p.status == "split" and not split:
            raise DocumentError(
                INVALID_REQUEST, f"{p.row.handle} names the issues it is split into"
            )
        if p.status != "split" and split:
            raise DocumentError(
                INVALID_REQUEST, f"{p.row.handle} holds split_into links only while split"
            )


def check_research(
    db: sqlite3.Connection, planned: list[Planned], author: Author, sessions: frozenset[str]
) -> None:
    """The research state's rules (CM-10), as the change leaves its entries: a
    contradiction concerns the two sides, at least, that disagree; a decision's reopen
    condition, its ``reopen_on`` links and its session follow ``_check_condition``,
    ``_check_kept`` and ``_check_session``; and only a decision that the change puts
    in effect to make an issue obstructed names an excluded scope and an assessment
    (LG-7), so that no later decision widens a settled impossibility."""
    by_id = {p.row.entry_id: p for p in planned}
    deciding = [
        d
        for d in planned
        if d.row.type == "decision" and d.status == "in_effect" and d.status_before != d.status
    ]
    for p in planned:
        links = preparation.final_links(db, p)
        if p.row.type == "issue" and p.fields.get("kind") == "contradiction":
            sides = {(k.target, k.target_id, k.block) for k in links if k.kind == "concerns"}
            if len(sides) < 2:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} is a contradiction: it concerns the entries, findings "
                    "or document blocks that disagree, at least two",
                )
        if p.row.type == "issue" and p.status == "obstructed" != p.status_before:
            deciders = [
                d
                for d in deciding
                if any(
                    k.kind == "affects" and k.target_id == p.row.entry_id
                    for k in preparation.final_links(db, d)
                )
            ]
            if len(deciders) > 1:
                raise DocumentError(
                    INVALID_REQUEST, f"one decision makes {p.row.handle} obstructed"
                )
            if author.kind == "user" and deciders and not deciders[0].fields.get("excluded_scope"):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"the decision that makes {p.row.handle} obstructed names the scope it "
                    "excludes (fields.excluded_scope)",
                )
        dropped = set(p.fields_before.get("caveats", ())) - set(p.fields.get("caveats", ()))
        if author.kind != "user" and dropped:
            raise records.refused(
                f"an agent adds caveats to {p.row.handle} and removes none: the user may remove one"
            )
        if p.row.type != "decision":
            continue
        check_condition(db, p, links, by_id)
        check_kept(db, p, author, deciding)
        check_session(sessions, p, author)
        named = [p.fields.get(k) for k in ("excluded_scope", "assessment")]
        before = [p.fields_before.get(k) for k in ("excluded_scope", "assessment")]
        if named != before and any(named):
            obstructs = any(d is p for d in deciding) and any(
                k.kind == "affects"
                and (target := by_id.get(k.target_id)) is not None
                and target.row.type == "issue"
                and target.status == "obstructed" != target.status_before
                for k in links
            )
            if not obstructs:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} names an excluded scope or an assessment only in the "
                    "change that puts it in effect and makes the issue it affects obstructed",
                )


def check_condition(
    db: sqlite3.Connection,
    p: Planned,
    links: list[EntryLink],
    by_id: dict[str, Planned],
) -> None:
    """A decision's reopen condition and its ``reopen_on`` links come together, a
    condition the ledger does not hold yet being stated as an open issue of kind
    question that it links to; one that becomes rejected or reversed states its
    condition, ``none`` when nothing would reopen it (CM-10)."""
    condition = p.fields.get("reopen_condition")
    watched = [k for k in links if k.kind == "reopen_on"]
    if condition is None and watched:
        raise DocumentError(
            INVALID_REQUEST,
            f"{p.row.handle}'s reopen_on links watch for its reopen condition: give "
            "fields.reopen_condition",
        )
    if condition is not None and condition.strip().casefold() == NO_CONDITION:
        if watched:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle}'s reopen condition is none, so it watches nothing",
            )
    elif condition is not None and not watched:
        raise DocumentError(
            INVALID_REQUEST,
            f"{p.row.handle}'s reopen condition names what it watches with reopen_on "
            "links: an open issue of kind question that states the condition, or the "
            "assumption, claim or result whose change would matter",
        )
    moved = p.created or "status" in p.changed
    if p.status in ("rejected", "reversed") and moved and condition is None:
        raise DocumentError(
            INVALID_REQUEST,
            f"{p.row.handle} becomes {p.status}: state what would justify reopening "
            f"it (fields.reopen_condition), or {NO_CONDITION}",
        )
    for link in records.links_added(p, "reopen_on"):
        target = by_id.get(link.target_id)
        kind, status, asked = (
            (target.row.type, target.status, target.fields.get("kind"))
            if target is not None
            else db.execute(
                "SELECT e.type,r.status,json_extract(r.fields,'$.kind') FROM ledger_entries "
                "e JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                "WHERE e.entry_id=?",
                (link.target_id,),
            ).fetchone()
        )
        if kind == "issue" and (status != "open" or (asked or "question") != "question"):
            raise DocumentError(
                INVALID_REQUEST,
                f"{link.handle} is not an open question: a reopen condition watches an "
                "open issue of kind question",
            )


def check_kept(db: sqlite3.Connection, p: Planned, author: Author, deciding: list[Planned]) -> None:
    """A decision in effect, rejected or reversed keeps its statement, fields, links
    and evidence unless it changes its status in the same change or a decision put in
    effect by it affects it (LG-30). The user edits a decision of the user's freely; an
    agent never changes one's title or content, but may state the reopen condition of
    one it rejects or reverses, if it has none (the coordinator, 2026-10-15)."""
    content = {"statement", "fields", "links", "evidence"} & set(p.changed)
    if p.created or not (content or {"title", "pins"} & set(p.changed)):
        return
    users = db.execute(
        "SELECT 1 FROM ledger_revisions WHERE entry_id=? "
        "AND json_extract(author,'$.kind')='user' LIMIT 1",
        (p.row.entry_id,),
    ).fetchone()
    if users and author.kind == "user":
        return
    if users:
        others = {k: v for k, v in p.fields.items() if k != "reopen_condition"}
        final = [k for k in preparation.final_links(db, p) if k.kind != "reopen_on"]
        stated = (
            not {"statement", "title", "evidence", "pins"} & set(p.changed)
            and "status" in p.changed
            and p.status in ("rejected", "reversed")
            and "reopen_condition" not in p.fields_before
            and others == p.fields_before
            and ("links" not in p.changed or final == p.previous)
        )
        if not stated:
            raise records.refused(
                f"{p.row.handle} is the user's decision: an agent changes its status by a "
                "decision, and never its title, statement (its pins included), fields, "
                "links or evidence"
            )
    if (
        content
        and p.status_before in ("in_effect", "rejected", "reversed")
        and "status" not in p.changed
        and not any(
            d is not p
            and any(
                k.kind == "affects" and k.target_id == p.row.entry_id
                for k in preparation.final_links(db, d)
            )
            for d in deciding
        )
    ):
        raise DocumentError(
            INVALID_REQUEST,
            f"{p.row.handle} is {p.status_before}: its statement, fields, links and "
            "evidence change only with its status, or in a change that puts in effect a "
            "decision that affects it",
        )


def check_session(sessions: frozenset[str], p: Planned, author: Author) -> None:
    """A decision scoped to a session names one of the project's; an agent scopes a
    decision to its own session, or takes its own session's scope off, and no other."""
    session, before = p.fields.get("session"), p.fields_before.get("session")
    if session == before:
        return
    if author.kind != "user" and {session, before} - {None, author.session_id}:
        raise records.refused("an agent scopes a decision to its own session, and no other")
    if session is not None and session not in sessions:
        raise DocumentError(NOT_FOUND, f"no session {session} in this project")
