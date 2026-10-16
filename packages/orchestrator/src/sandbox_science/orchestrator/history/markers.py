# SPDX-License-Identifier: MIT
"""Current and retired markers (docs/specs/context-management.md, CM-21a).

At each rebuild code decides, from the records, whether each stored marker is current or
retired; nothing a model wrote enters the decision, and the stored records never change.
The rules compare IDs, handles, statuses, revisions and the fields that decide each
marker, which the skeleton stored beside its text (``skeleton``):

- a decision line is retired when a later fold holds a later change of the same
  decision, or the ledger's decision now has another status (``reversed`` among them;
  the line that records the reversal is current) or another reopen condition. A change
  that touched neither, such as a new title or link, leaves it current;
- a caveat from a ledger entry, a caveat line or an evidence scope, when the entry was
  set aside (``superseded``, ``withdrawn``, ``refuted``) or its current revision no
  longer holds it;
- a work record that ended at a limit or with a claimed obstruction: on an issue, when
  the issue is no longer ``open``; on a claim or result, when the entry was set aside,
  or a later assessment of it decided it, ``accepted`` or ``rejected``; a claimed
  obstruction, when the assessment of that record decided it;
- an inconclusive check, when the statement, fields or evidence of the entry it checked
  differ now from those at the revision it checked;
- an incomplete read, when the read register shows the source read whole by a read after
  the marker's, of the same content: a PDF's or a file's key is its digest; another
  source's reads before the marker's are not counted, since its content may have
  changed (``Register.completed``);
- a failure aggregate, when a later call of the same tool with the same arguments
  succeeded for every call it counts, or once its fold is merged into an overview, unless
  a work record cites one of its calls (anchors on its result).

A marker stored before these fields were, a work marker without its record's ID, or one
whose entry, work records or calls cannot be read, stays current: a marker is never
retired on a guess. One of CM-21a's causes waits for its record: a later check that
ended; until then an inconclusive check retires only by the other cause.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from .archive import PAGE
from .record import FoldRecord, flat
from .skeleton import failed, outcome_of

if TYPE_CHECKING:
    from sandbox_science.orchestrator.ledger.store import Ledger

    from .archive import Archive, Archived
    from .reads import Register

Json = dict[str, Any]

SET_ASIDE: Final = frozenset({"superseded", "withdrawn", "refuted"})
"""The statuses that set an entry aside: its caveats retire with it."""


@dataclass(frozen=True)
class Held:
    """What the ledger holds for an entry at a revision: its status, the revision,
    caveat lines, evidence as (type, scope), a decision's reopen condition, and a digest
    of its statement, fields and evidence (``material``), which a check examined."""

    status: str
    revision: int
    caveats: frozenset[str] = frozenset()
    evidence: frozenset[tuple[str, str]] = frozenset()
    reopen: str | None = None
    material: str | None = None


Entries = Callable[[str, "int | None"], "Held | None"]
"""An entry by its handle, at a revision or its current one; ``None`` when it cannot be
read."""
Completed = Callable[[str, str, "str | None"], "str | None"]
"""The message whose read made a source whole, after the read of the message named, by
its kind and source; ``None``."""
DECIDING: Final = frozenset({"accepted", "rejected"})
"""The verdicts of an assessment that decide what it assesses."""


@dataclass(frozen=True)
class Work:
    """A work record on an entry, as far as its markers need it."""

    record: str
    position: int
    ending: str
    verdict: str | None = None
    assesses: str | None = None


Works = Callable[[str], "Sequence[Work] | None"]
"""An entry's work records, by its handle; ``None`` when they cannot be read."""
Cites = Callable[[Sequence[str]], "str | None"]
"""A work record whose anchors name one of the messages given, if any."""
UNKNOWN: Final = "unknown"
"""What a reader that failed answers: the marker stays current."""


class Judge:
    """Decides which stored markers are retired, at one rebuild."""

    def __init__(
        self,
        entries: Entries | None = None,
        completed: Completed | None = None,
        works: Works | None = None,
        cites: Cites | None = None,
    ):
        self.entries, self.completed, self.works, self.cites = entries, completed, works, cites

    def retired(
        self, records: Sequence[tuple[str, FoldRecord]], rows: Sequence[Archived] = ()
    ) -> dict[str, dict[int, str]]:
        """For each record, by its archive ID, its retired markers by their index, each
        with why: ``records`` are all the session's, oldest first, those an overview
        covers among them; ``rows`` the session's archived messages, where later calls
        are looked for."""
        newest: dict[str, str] = {}
        for _, record in records:  # first-level records hold each change once, in order
            if record.level == 1:
                for marker in record.markers:
                    if marker.get("kind") == "decision" and marker.get("at"):
                        newest[str(marker.get("subject"))] = str(marker["at"])
        held: dict[tuple[str, int | None], Held | None] = {}
        reads: dict[tuple[str, str, str | None], str | None] = {}

        def entry(handle: str, revision: int | None = None) -> Held | None:
            if (handle, revision) not in held:
                try:
                    found = self.entries(handle, revision) if self.entries is not None else None
                except Exception:  # unknown, then: the marker stays current
                    found = None
                held[handle, revision] = found
            return held[handle, revision]

        def read(kind: str, source: str, after: str | None) -> str | None:
            if (kind, source, after) not in reads:
                try:
                    found = self.completed(kind, source, after) if self.completed else None
                except Exception:
                    found = None
                reads[kind, source, after] = found
            return reads[kind, source, after]

        listed: dict[str, Sequence[Work] | None] = {}

        def works(handle: str) -> Sequence[Work] | None:
            if handle not in listed:
                try:
                    listed[handle] = self.works(handle) if self.works is not None else None
                except Exception:
                    listed[handle] = None
            return listed[handle]

        def cited(ids: Sequence[str]) -> str | None:
            try:
                return self.cites(ids) if self.cites is not None else None
            except Exception:  # unknown: the marker stays current
                return UNKNOWN

        sources = _Sources(entry, read, works, cited, _succeeded(rows) if rows else lambda _: None)
        found: dict[str, dict[int, str]] = {}
        for fold, record in records:
            gone: dict[int, str] = {}
            for index, marker in enumerate(record.markers):
                why = _why(marker, record, newest, sources)
                if why is not None:
                    gone[index] = why
            if gone:
                found[fold] = gone
        return found


@dataclass(frozen=True)
class _Sources:
    """What a rebuild reads to judge markers, each read once."""

    entry: Callable[[str, int | None], Held | None]
    read: Callable[[str, str, str | None], str | None]
    works: Callable[[str], Sequence[Work] | None]
    cited: Callable[[Sequence[str]], str | None]
    succeeded: Callable[[Sequence[str]], str | None]


def _why(
    marker: Mapping[str, Any],
    record: FoldRecord,
    newest: Mapping[str, str],
    sources: _Sources,
) -> str | None:
    """Why ``marker`` is retired, or ``None`` while it is current."""
    kind, entry, read = marker.get("kind"), sources.entry, sources.read
    if kind == "failures":
        ids = [str(at) for at in cast(list[object], marker.get("ids") or [])]
        if ids and (done := sources.succeeded(ids)) is not None:
            return f"a later identical call succeeded ({done})"
        if record.level == 1 or (ids and sources.cited(ids) is not None):
            return None  # a work record citing its calls keeps it (or no reader could say)
        return "its fold was merged into an overview"
    if kind == "read":
        source = marker.get("source")
        if not isinstance(source, list) or len(cast(list[object], source)) != 2:
            return None
        pair = cast(list[object], source)
        at = marker.get("at")
        done = read(str(pair[0]), str(pair[1]), str(at) if at else None)
        return f"read whole later ({done})" if done is not None else None
    subject = str(marker.get("entry") or "")
    if not subject:
        return None
    if kind == "decision":
        return _decision(marker, subject, newest, entry(subject, None))
    now = entry(subject, None)
    if now is None:
        return None
    if "work" in marker:  # a work record's ending
        if subject.startswith("I-") and now.status != "open":
            return f"its issue {subject} is {now.status}"
        if not subject.startswith("I-") and now.status in SET_ASIDE:
            return f"{subject} is {now.status}"
        return _assessed(marker, subject, sources.works(subject))
    if "check" in marker:
        revision = marker.get("revision")
        then = entry(subject, revision) if isinstance(revision, int) else None
        if then is None or then.material is None or now.material is None:
            return None
        if then.material != now.material:
            return f"{subject} changed after the check, from revision {revision} to {now.revision}"
        return None
    if now.status in SET_ASIDE:
        return f"{subject} is {now.status}"
    if "caveat" in marker and flat(str(marker["caveat"])) not in now.caveats:
        return f"revision {now.revision} of {subject} no longer holds it"
    if "evidence" in marker:
        pair = cast(list[object], marker["evidence"])
        held = (flat(str(pair[0])), flat(str(pair[1] or ""))) if len(pair) == 2 else None
        if held is not None and held not in now.evidence:
            return f"revision {now.revision} of {subject} no longer holds that evidence"
    return None


def _assessed(marker: Mapping[str, Any], subject: str, works: Sequence[Work] | None) -> str | None:
    """Why a work marker is retired by an assessment: a claimed obstruction, by the one of
    its record that decided it; a claim's or result's, by a later one that decided it."""
    record = marker.get("record")
    own = next((w for w in works or () if w.record == record), None)
    if own is None:  # a marker from before records were named, or unread: current
        return None
    for work in works or ():
        if work.ending != "assessment" or work.verdict not in DECIDING:
            continue
        if marker.get("work") == "obstruction_claimed" and work.assesses == own.record:
            return f"the assessment {work.record} decided the claimed obstruction ({work.verdict})"
        if not subject.startswith("I-") and work.position > own.position:
            return f"the assessment {work.record} decided {subject} ({work.verdict})"
    return None


def _succeeded(rows: Sequence[Archived]) -> Callable[[Sequence[str]], str | None]:
    """Whether each failed call, by its result's message, was followed by a call of the
    same tool with the same arguments that succeeded; the last such result's message."""
    calls: dict[tuple[str, str], tuple[str, str]] = {}
    results: list[Archived] = []
    for row in rows:
        if row.entry.role == "assistant":
            for call in row.entry.tool_calls:
                function = cast(Json, call.get("function") or {})
                args = _same(str(function.get("arguments") or ""))
                calls[row.run, str(call.get("id"))] = (str(function.get("name")), args)
        elif row.entry.kind == "tool_result":
            results.append(row)
    by_id = {row.message_id: row for row in results}

    def key(row: Archived) -> tuple[str, str] | None:
        return calls.get((row.run, str(row.entry.call_id)))

    def later(at: str) -> str | None:
        row = by_id.get(at)
        wanted = key(row) if row is not None else None
        if row is None or wanted is None:
            return None
        return next(
            (
                found.message_id
                for found in results
                if found.position > row.position and key(found) == wanted and _done(found)
            ),
            None,
        )

    def check(ids: Sequence[str]) -> str | None:
        found = [later(at) for at in ids]
        done = [at for at in found if at is not None]
        if not done or len(done) < len(found):  # every call it counts, or it stays current
            return None
        return max(done, key=lambda at: by_id[at].position)

    return check


def _done(row: Archived) -> bool:
    try:
        found: object = json.loads(row.entry.content or "")
    except ValueError:
        return False  # a result that is not the tool's JSON: not known to have succeeded
    return isinstance(found, dict) and not failed(outcome_of(cast(Json, found)))


def _same(args: str) -> str:
    """Arguments in one form, so that the same ones written apart compare equal."""
    try:
        return json.dumps(json.loads(args), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return args


def _decision(
    marker: Mapping[str, Any], subject: str, newest: Mapping[str, str], now: Held | None
) -> str | None:
    """Why a decision line is retired: a later change of its decision, in a later fold or
    in the ledger, of what decides it, its status or reopen condition."""
    at, later = marker.get("at"), newest.get(subject)
    if at and later and later != at:
        return f"a later change of {subject} ({later})"
    if now is None:
        return None
    status = marker.get("status")
    if isinstance(status, str) and now.status != status:
        if now.status == "reversed":
            return f"{subject} was reversed (ledger_read {subject})"
        return f"{subject} is now {now.status} (ledger_read {subject})"
    reopen = marker.get("reopen")
    if isinstance(reopen, str) and now.reopen is not None and flat(reopen) != now.reopen:
        return f"the reopen condition of {subject} changed (ledger_read {subject})"
    return None


Decided = tuple[str, int, str]
"""A decision as the ledger holds it: its handle, revision and status."""
CURRENT: Final = frozenset({"proposed", "in_effect", "rejected"})
"""A decision's statuses while its line is current (CM-21a)."""


def current_decisions(
    archive: Archive, ledger: Ledger, project: str, session: str
) -> tuple[Decided, ...]:
    """The session's current decisions: those its fold records' decision lines name, as
    the ledger holds them now; what a ``research_state`` stop waits on to change (CM-45)."""
    from .record import FoldRecord  # the archive's records

    handles: set[str] = set()
    start = 1
    while True:
        page = archive.messages(project, session, start=start)
        for row in page:
            if row.entry.kind == "fold" and (found := FoldRecord.of(row)) is not None:
                handles |= {
                    str(m.get("entry") or m.get("subject"))
                    for m in found.markers
                    if m.get("kind") == "decision"
                }
        if len(page) < PAGE:
            break
        start = page[-1].position + 1
    entries = ledger_entries(ledger, project)  # an entry that cannot be read counts as none
    held = {handle: entries(handle, None) for handle in sorted(handles)}
    return tuple(
        (handle, found.revision, found.status)
        for handle, found in held.items()
        if found is not None and found.status in CURRENT
    )


def ledger_entries(ledger: Ledger, project: str) -> Entries:
    """An ``Entries`` reading ``project``'s ledger."""

    def read(handle: str, revision: int | None = None) -> Held | None:
        try:
            view = ledger.entry(project, handle, revision)
        except Exception:  # a handle that does not exist, or a store that cannot be read
            return None
        caveats = cast(list[object], view.fields.get("caveats") or [])
        reopen = view.fields.get("reopen_condition")
        links = [
            link.model_dump(mode="json", exclude={"evidence_id", "added", "checked"})
            for link in view.evidence
        ]
        material = json.dumps([view.statement, view.fields, links], sort_keys=True, default=str)
        return Held(
            status=view.status,
            revision=view.revision,
            caveats=frozenset(flat(str(caveat)) for caveat in caveats),
            evidence=frozenset(
                (flat(str(link.type)), flat(link.scope or "")) for link in view.evidence
            ),
            reopen=flat(str(reopen)) if reopen else "",
            material=hashlib.sha256(material.encode()).hexdigest(),
        )

    return read


def ledger_works(ledger: Ledger, project: str) -> Works:
    """A ``Works`` reading ``project``'s ledger: an entry's work records, at most 2,000."""

    def read(handle: str) -> Sequence[Work] | None:
        found: list[Work] = []
        before: int | None = None
        try:
            while len(found) < 2000:
                page = ledger.work(project, handle, before=before, limit=100)
                found += [
                    Work(r.record_id, r.position, r.ending, r.verdict, r.assesses)
                    for r in page.records
                ]
                if page.next_before is None:
                    return found
                before = page.next_before
        except Exception:  # an entry that does not exist, or a store that cannot be read
            return None
        return None  # more than it reads: not known, so its markers stay current

    return read


def ledger_cites(ledger: Ledger, project: str) -> Cites:
    """A ``Cites`` reading ``project``'s work records' anchors (CM-9)."""

    def read(messages: Iterable[str]) -> str | None:
        with ledger.control.read() as tx:
            row = tx.sql.execute(
                "SELECT w.record_id FROM ledger_work w, json_each(coalesce(w.anchors,'[]')) a "
                "WHERE w.project=? AND json_extract(a.value,'$.message') IN "
                "(SELECT value FROM json_each(?)) LIMIT 1",
                (project, json.dumps(list(messages))),
            ).fetchone()
        return None if row is None else str(row[0])

    return read


def register_reads(register: Register, project: str) -> Completed:
    """A ``Completed`` reading ``project``'s read register."""

    def read(kind: str, source: str, after: str | None) -> str | None:
        return register.completed(project, kind, source, after=after)

    return read
