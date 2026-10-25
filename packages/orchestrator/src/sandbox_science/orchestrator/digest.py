# SPDX-License-Identifier: MIT
"""The research-state digest (docs/specs/context-management.md, CM-16, CM-26, CM-37a):
what the ledger, the session plan and the read register hold, rendered by code into a
run's state block, so that where the research stands never rests on a summary.

``Digest.sections`` gathers its nine sections, in CM-16's order and with its shares, each
on its own, so that one that cannot be read leaves the others; ``render`` fits them in a
budget: a section's unused share passes to the sections after it, and what is left at
the end goes to the decisions; a section over its share ends with a line saying how many
it left out and how to list them, counted in its share. Within a section, the entries
this session wrote come first, then the most recently changed, then the most linked.
Every line ends with its basis (CM-9) and the call that reads it whole. ``Rendered``
names the handles shown and those left out, which folds keep apart (CM-21b).

The labels are the ledger's own: an entry's kind, session, standing (CM-11, which
``ledger.standing`` decides) and basis, and its open flags with their causes; the digest
recomputes none of them. The user's standing constraints are the decisions in effect
with the basis ``user`` or ``user_quoted``, the project's and this session's. Beside a
``user_quoted`` one go the quotes its anchors hold that are the user's: each anchored on
a message the user wrote, and found in the user's words there, word for word, when the
digest is rendered. Any other quote an anchor holds is not shown: an agent wrote it.
Decisions meant for another session are counted, not shown (CM-10). ``constraints`` is
what a delegated task gets (CM-37a): a constraint an agent recorded shows the user's
checked words and its statement marked as the agent's, without its reason. A check gets
nothing.

The folds' proposals waiting for the session (CM-15) are listed in section 8, each with
its basis and the fold that proposed it, to accept or dismiss with ``ledger_proposals``;
they are not the ledger's and settle nothing. The open items list the session's open
and stale review findings (CR-41). Not built yet: among the open items the jobs and
approvals a session waits for, and recorded artifacts in the index.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.context.history.reads import reference_keys, reference_reads
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.ledger.models import MIN_QUOTE, EntrySummary, EntryView
from sandbox_science.orchestrator.ledger.proposals import FoldProposals
from sandbox_science.orchestrator.ledger.provenance import users_words
from sandbox_science.orchestrator.ledger.store import Ledger
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.session_plans import PlanAuthority, PlanScope, SessionPlans

Json = dict[str, Any]
_log = logging.getLogger(__name__)

SHARE: Final = 0.1
"""The share of a run's input, in characters, that the digest takes at most."""
SECTIONS: Final = (
    ("plan", "1. The session plan", 0.10),
    ("decisions", "2. Decisions", 0.30),
    ("questions", "3. Open questions, hypotheses and contradictions", 0.10),
    ("assumptions", "4. Active assumptions", 0.10),
    ("findings", "5. Findings", 0.20),
    ("sources", "6. Sources and how far they were read", 0.05),
    ("open", "7. This session's open items", 0.05),
    (
        "proposals",
        "8. Fold proposals waiting (written by a model from folded messages, "
        "untrusted until their anchors are read)",
        0.05,
    ),
    ("index", "9. Delegation outcomes", 0.05),
)
"""CM-16's sections, in order, with their shares of the digest's budget."""
TAGS: Final = {
    "user": "[user]",
    "user_quoted": "[user, quoted]",
    "observed": "[observed]",
    "interpreted": "[interpreted]",
}
UNRECORDED: Final = "[basis not recorded]"
ACCEPTED_LABELS: Final = {
    "user_quoted": "user quoted",
    "observed": "observed",
    "interpreted": "interpreted",
}
STANDINGS: Final = ("impossible", "flagged", "disputed", "claimed", "attempt")
"""CM-11's labels, in the order findings show them, before the plain findings: what
stops repeated work stays in view first."""
USERS: Final = ("user", "task", "comment")
"""The archive's kinds of message that can hold the user's own words (CM-9)."""
SCANNED: Final = 2000
"""The most entries of one type a section reads, newest first, of those it can show."""
CLIP: Final = 300
HANDLE: Final = re.compile(r"(?<![\w-])(?:REF|[ICRADT])-\d+(?![\w-])")
"""A ledger handle in a line's text."""


@dataclass(frozen=True)
class Line:
    """One item of a section, with what orders it and the handle that reads it whole."""

    text: str
    touched: bool = False
    updated: str = ""
    linked: int = 0
    handle: str | None = None
    pinned: bool = False
    """A line no later line of its section may displace: one of the user's constraints."""
    fields: Mapping[str, Any] | None = None
    """What the line shows of its entry, field by field, each whole or ``None`` where the
    line clips it: what a fold record's marker is compared with (CM-21b)."""


@dataclass
class Section:
    key: str
    title: str
    share: float
    lines: list[Line] = field(default_factory=list[Line])
    hint: str = ""
    """How to list what the section leaves out."""
    note: str | None = None
    """A line the section always ends with, such as the count of other sessions'."""


@dataclass(frozen=True)
class Rendered:
    """The digest's text, the handles it shows, and those it leaves out (CM-21b)."""

    text: str
    shown: tuple[str, ...] = ()
    left_out: tuple[str, ...] = ()
    lines: Mapping[str, str] = field(default_factory=dict[str, str])
    """The line each handle shown has, as the digest shows it."""
    fields: Mapping[str, Mapping[str, Any]] = field(default_factory=dict[str, Mapping[str, Any]])
    """For each handle shown, the fields its line shows (``Line.fields``): a fold record's
    marker takes its short form only when they hold its own exactly (CM-21b)."""
    cited: frozenset[tuple[str, str]] = frozenset()
    """The sources, by kind and key, that an active reference cites: incomplete reads of
    them rank above the rest when the fold region spills (CM-21b)."""
    named: frozenset[str] = frozenset()
    """The handles the plan and the open questions name: the markers on them come first
    among those the fold region keeps (CM-21b)."""
    pinned: frozenset[str] = frozenset()
    """The user's standing constraints, which are never shortened."""


def _whole(text: str, limit: int) -> str | None:
    """``text`` on one line as ``_clip`` shows it, or ``None`` when that clips it."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else None


def _wholes(items: Sequence[str], limit: int) -> set[str]:
    """The ``items`` that ``_clip`` of their join with "; " shows whole."""
    found: set[str] = set()
    joined = " ".join("; ".join(items).split())
    position = 0
    for item in items:
        flat = " ".join(item.split())
        end = position + len(flat)
        if len(joined) <= limit or end <= limit - 1:
            found.add(flat)
        position = end + 2
    return found


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tag(basis: str | None, accepted: bool = False) -> str:
    """A basis as the digest shows it; a fold proposal's the user accepted keeps its own,
    marked so: its words are the model's (the coordinator, 2026-10-16)."""
    if accepted:
        return f"[{ACCEPTED_LABELS.get(basis or '', 'basis not recorded')}, accepted by the user]"
    return TAGS.get(basis or "", UNRECORDED)


def accepted(view: EntryView) -> bool:
    """Whether ``view`` is a fold's proposal the user accepted: the user's revision, whose
    basis is not the user's own."""
    return view.author.get("kind") == "user" and view.basis not in (None, "user")


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _ordered(lines: list[Line]) -> list[Line]:
    """The session's own first, then the most recently changed, then the most linked."""
    by_link = sorted(lines, key=lambda line: -line.linked)
    by_time = sorted(by_link, key=lambda line: line.updated, reverse=True)
    return sorted(by_time, key=lambda line: not line.touched)


Pick = Callable[[EntrySummary], bool]


class Digest:
    """The research state of one project, as a run of one of its sessions is given it."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        plans: SessionPlans | None = None,
        questions: Questions | None = None,
        delegations: DelegationRecords | None = None,
        proposals: FoldProposals | None = None,
        findings: Findings | None = None,
    ) -> None:
        self.ledger, self.plans, self.proposals = ledger, plans, proposals
        self.findings = findings
        self.questions, self.delegations = questions, delegations
        self._listed: dict[tuple[str, str], list[EntrySummary]] = {}
        self.unread: dict[str, int] = {}
        """Of each type, how many entries a section could show but did not read, past
        ``SCANNED``."""

    def _views(self, project: str, kind: str, pick: Pick) -> list[tuple[EntrySummary, EntryView]]:
        """The entries of one type that ``pick`` keeps, by their summaries, the newest
        ``SCANNED`` of them with their views; how many it passed over is counted."""
        if (project, kind) not in self._listed:
            found: list[EntrySummary] = []
            after: str | None = None
            while True:
                page = self.ledger.entries(project, kind=kind, after=after, limit=200)
                found += page.entries
                if page.next_after is None:
                    break
                after = page.next_after
            self._listed[project, kind] = found
        kept = [s for s in self._listed[project, kind] if pick(s)]
        newest = sorted(kept, key=lambda s: s.updated, reverse=True)[:SCANNED]
        if len(kept) > SCANNED:
            self.unread[kind] = self.unread.get(kind, 0) + len(kept) - SCANNED
        return [(s, self.ledger.entry(project, s.entry_id)) for s in newest]

    def _line(self, view: EntryView, session: str, text: str) -> Line:
        touched = view.author.get("session_id") == session
        hint = f" {_tag(view.basis, accepted(view))} (ledger_read {view.handle})"
        return Line(text + hint, touched, view.updated, view.incoming_total, view.handle)

    def users_quotes(self, project: str, view: EntryView) -> list[tuple[str, str]]:
        """The quotes ``view``'s anchors hold that are the user's: each on a message the
        user wrote, found there word for word (as CM-9 checks a quote); with its message."""
        found: list[tuple[str, str]] = []
        with self.ledger.control.read() as tx:
            for anchor in view.anchors:
                quote, message = anchor.get("quote"), anchor.get("message")
                if anchor.get("kind") not in USERS or not isinstance(quote, str):
                    continue
                said = [_lines(s) for s in users_words(tx.sql, project, str(message))]
                wanted = _lines(quote)
                if any(
                    len(wanted.strip()) >= min(MIN_QUOTE, len(s.strip())) and wanted in s
                    for s in said
                ):
                    found.append((str(message), quote))
        return found

    def _quotes(self, project: str, view: EntryView) -> str:
        return "".join(
            f"\n  the user's words, as the agent quoted them and the platform found them in "
            f'the user\'s message {message}: "{_clip(quote, 600)}"'
            for message, quote in self.users_quotes(project, view)
        )

    @staticmethod
    def _standing(session: str) -> Pick:
        """The decisions in effect for ``session`` that may be the user's constraints:
        ``standing`` says which are."""

        def pick(s: EntrySummary) -> bool:
            return s.status == "in_effect" and s.session in (None, session)

        return pick

    @staticmethod
    def standing(summary: EntrySummary, view: EntryView) -> bool:
        """A standing constraint: the user's, quoted by an agent, or a fold's proposal the
        user accepted."""
        return summary.basis in ("user", "user_quoted") or accepted(view)

    def constraints(self, project: str, session: str) -> list[Line]:
        """The user's standing constraints, the project's and this session's, for a
        delegated task (CM-37a): one the user set as the user wrote it; one an agent
        recorded with the user's checked words and its statement marked as the agent's,
        without its reason, which is the agent's too."""
        self._listed, self.unread = {}, {}
        lines: list[Line] = []
        for summary, view in self._views(project, "decision", self._standing(session)):
            if not self.standing(summary, view):
                continue
            scoped = "; for this session only" if view.session is not None else ""
            if view.basis == "user":
                text = f"{view.handle} {_clip(view.statement)}{scoped} [user]"
            elif accepted(view):
                text = (
                    f"{view.handle} (written by a fold's model, accepted by the user: "
                    f"{_clip(view.statement)}){scoped}{self._quotes(project, view)}"
                )
            else:
                text = (
                    f"{view.handle} (recorded by an agent: {_clip(view.statement)}){scoped}"
                    f"{self._quotes(project, view)}"
                )
            lines.append(Line(text, handle=view.handle))
        return lines

    def _decision(self, project: str, view: EntryView, session: str) -> Line:
        fields = view.fields
        text = f"{view.handle} ({view.status}) {_clip(view.statement)}"
        text += f"; reason: {_clip(view.reason, 200)}"
        if fields.get("excluded_scope"):
            text += f"; excludes: {_clip(str(fields['excluded_scope']), 200)}"
        if fields.get("reopen_condition"):
            text += f"; reopen if: {_clip(str(fields['reopen_condition']), 200)}"
        if view.session is not None:
            text += "; for this session only"
        if view.flags:
            causes = ", ".join(sorted({flag.cause for flag in view.flags}))
            text += f"; open flags: {causes}"
        reopen = fields.get("reopen_condition")
        shown = {
            "status": view.status,
            "reason": _whole(view.reason, 200),
            "reopen": _whole(str(reopen), 200) if reopen else "",
        }
        line = replace(self._line(view, session, text), fields=shown)
        if view.basis != "user_quoted":
            return line
        return replace(line, text=line.text + self._quotes(project, view))

    def sections(self, project: str, session: str) -> list[Section]:
        """CM-16's sections for a main run of ``session``, as the stores hold them now; a
        section that cannot be read says so, and the others stand."""
        self._listed, self.unread = {}, {}
        found = {key: Section(key, title, share) for key, title, share in SECTIONS}
        gatherers: dict[str, Callable[[Section], None]] = {
            "plan": lambda s: self._plan(s, project, session),
            "decisions": lambda s: self._decisions(s, project, session),
            "questions": lambda s: self._questions(s, project, session),
            "assumptions": lambda s: self._assumptions(s, project, session),
            "findings": lambda s: self._findings(s, project, session),
            "sources": lambda s: self._sources(s, project, session),
            "open": lambda s: self._items(s, found["index"], project, session),
            "proposals": lambda s: self._proposals(s, project, session),
        }
        for key, gather in gatherers.items():
            try:
                gather(found[key])
            except (DocumentError, ControlError, sqlite3.Error) as error:
                _log.warning("the digest's %s could not be read: %s", key, error)
                found[key].lines, found[key].note = [], f"(This section could not be read: {error})"
        for key, kind in (("decisions", "decision"), ("findings", "claim"), ("questions", "issue")):
            if self.unread.get(kind):
                extra = f"{self.unread[kind]} older entries of this kind were not read for it."
                found[key].note = f"{found[key].note} {extra}" if found[key].note else extra
        return list(found.values())

    def _decisions(self, section: Section, project: str, session: str) -> None:
        standing = self._standing(session)
        elsewhere: dict[str, int] = {}
        groups: list[list[Line]] = [[], [], [], []]
        for summary, view in self._views(project, "decision", lambda s: s.status != "withdrawn"):
            if summary.session not in (None, session):
                elsewhere[summary.session] = elsewhere.get(summary.session, 0) + 1
                continue
            line = self._decision(project, view, session)
            if standing(summary) and self.standing(summary, view):
                groups[0].append(replace(line, pinned=True))
            elif summary.status in ("rejected", "reversed") or (
                summary.status == "in_effect" and view.fields.get("excluded_scope")
            ):
                groups[1].append(line)
            elif summary.status == "in_effect":
                groups[2].append(line)
            elif summary.status == "proposed":
                groups[3].append(line)
        section.lines = [line for group in groups for line in _ordered(group)]
        section.hint = "ledger_list with type decision"
        if elsewhere:
            counted = ", ".join(f"{name}: {n}" for name, n in sorted(elsewhere.items()))
            section.note = f"Decisions meant for other sessions, not shown: {counted}."

    def _questions(self, section: Section, project: str, session: str) -> None:
        def pick(s: EntrySummary) -> bool:
            kind = s.kind or "question"
            return s.status == "open" and kind in ("question", "hypothesis", "contradiction")

        lines: list[Line] = []
        for _, view in self._views(project, "issue", pick):
            text = f"{view.handle} ({view.kind or 'question'}) {_clip(view.statement)}"
            concerns = [k.handle or k.target_id for k in view.links if k.kind == "concerns"]
            if concerns:
                text += f"; concerns {', '.join(concerns)}"
            lines.append(self._line(view, session, text))
        section.lines, section.hint = _ordered(lines), "ledger_list with type issue"

    def _assumptions(self, section: Section, project: str, session: str) -> None:
        lines: list[Line] = []
        for _, view in self._views(project, "assumption", lambda s: s.status == "active"):
            text = f"{view.handle} {_clip(view.statement)}"
            relying = [link.handle or "" for link in view.incoming if link.kind == "assumes"]
            if relying:
                more = " and others" if view.incoming_total > len(view.incoming) else ""
                text += f"; relied on by {', '.join(relying)}{more}"
            if view.flags:
                text += f"; open flags: {', '.join(sorted({f.cause for f in view.flags}))}"
            lines.append(self._line(view, session, text))
        section.lines, section.hint = _ordered(lines), "ledger_list with type assumption"

    def _findings(self, section: Section, project: str, session: str) -> None:
        plain: list[Line] = []
        labelled: dict[str, list[Line]] = {label: [] for label in STANDINGS}

        def shown(s: EntrySummary) -> bool:
            return s.status != "withdrawn"

        found = self._views(project, "claim", shown) + self._views(project, "result", shown)
        found += self._views(project, "issue", lambda s: s.standing is not None)
        for summary, view in found:
            kind = f", {view.kind}" if view.kind else ""
            text = f"{view.handle} ({view.status}{kind}) {_clip(view.statement)}"
            types = sorted({e.type for e in view.evidence})
            if types:
                text += f"; evidence: {', '.join(types)}"
            scopes = [e.scope for e in view.evidence if e.scope]
            if scopes:
                text += f"; scope: {_clip('; '.join(scopes), 200)}"
            caveats = cast(list[str], view.fields.get("caveats") or [])
            if caveats:
                text += f"; caveats: {_clip('; '.join(caveats), 300)}"
            whole = _wholes(scopes, 200)
            held: dict[str, Any] = {
                "status": view.status,
                "caveats": frozenset(_wholes(caveats, 300)),
                "evidence": frozenset(
                    (str(e.type), " ".join((e.scope or "").split()))
                    for e in view.evidence
                    if not e.scope or " ".join(e.scope.split()) in whole
                ),
            }
            standing = summary.standing
            if standing is None:
                plain.append(replace(self._line(view, session, text), fields=held))
                continue
            text = f"[{standing.text}] {text}"
            if standing.scope:
                text += f"; within: {_clip(standing.scope, 200)}"
            if standing.assumes:
                text += f"; assumes {', '.join(standing.assumes)}"
            if standing.by:
                text += f"; by {standing.by}"
            labelled[standing.label].append(replace(self._line(view, session, text), fields=held))
        section.lines = [
            *(line for label in STANDINGS for line in _ordered(labelled[label])),
            *_ordered(plain),
        ]
        section.hint = "ledger_list with type claim or result; the labelled ones come first"

    def _sources(self, section: Section, project: str, session: str) -> None:
        lines: list[Line] = []
        for _, view in self._views(project, "reference", lambda s: s.status == "active"):
            reads = reference_reads(self.ledger.control, project, view.fields)
            text = f"{view.handle} {_clip(view.title, 200)}"
            shown: list[str] = []
            for read in reads:
                covered = cast(Json, read.get("covered") or {})
                pages = f" pages {covered['pages']}" if "pages" in covered else ""
                of = f" of {covered['of']}" if covered.get("of") else ""
                shown.append(f"{read['kind']} {read['extent']}{pages}{of}")
            text += f"; read: {'; '.join(shown)}" if shown else "; read: not in this project"
            cites = frozenset(reference_keys(self.ledger.control, project, view.fields))
            lines.append(replace(self._line(view, session, text), fields={"cites": cites}))
        section.lines, section.hint = _ordered(lines), "ledger_list with type reference"

    def _plan(self, section: Section, project: str, session: str) -> None:
        section.hint = "plan_read(current)"
        if self.plans is None:
            return
        scope = PlanScope(project_id=project, session_id=session)
        snapshot = self.plans.current(PlanAuthority(scope=scope, role="main", run_id="digest"))
        if snapshot is None:
            return
        body, head = snapshot.version.body, snapshot.head
        lines = [
            Line(
                f"Goal (plan {head.ref.plan_id} version {head.ref.version}, {head.status}): "
                f"{_clip(body.goal)} [plan] (plan_read current)",
                touched=True,
            )
        ]
        latest = {s.step_id: s.latest for s in snapshot.steps}
        steps = [step for phase in body.phases for step in phase.steps]
        current = next(
            (
                step.step_id
                for step in steps
                if (p := latest.get(step.step_id)) is None or p.status != "completed"
            ),
            None,
        )
        for step in steps:
            progress = latest.get(step.step_id)
            status = progress.status if progress is not None else "pending"
            note = f": {_clip(progress.note, 200)}" if progress is not None else ""
            mark = " (current)" if step.step_id == current else ""
            lines.append(Line(f"{step.step_id}{mark} {status} {_clip(step.title)}{note}", True))
        section.lines = lines

    def _proposals(self, section: Section, project: str, session: str) -> None:
        """The folds' proposals waiting for this session, oldest first: what each proposes,
        its basis, and the fold that proposed it (CM-15)."""
        section.hint = "ledger_proposals"
        for found in self.proposals.waiting(project, session) if self.proposals else ():
            text = _clip(found.proposal.summary())
            tag = _tag(found.proposal.basis)
            section.lines.append(Line(f"{found.proposal_id} {text} {tag} (fold {found.fold})"))

    def _items(self, waiting: Section, index: Section, project: str, session: str) -> None:
        waiting.hint = index.hint = "delegation_status, review_findings and the chat"
        if self.findings is not None:
            listed = self.findings.listing(project, session, statuses=("open", "stale"))
            for finding in listed["findings"]:
                if finding.get("origin") == "math":
                    continue
                status = str(finding["status"])
                waiting.lines.append(
                    Line(
                        f"{finding['finding_id']} {finding['severity']} review finding, "
                        f"{status}: {_clip(str(finding['statement']), 200)}"
                    )
                )
        if self.questions is not None:
            waiting.lines += [
                Line(f"{q.question_id} question to the user, not answered: {_clip(q.text, 200)}")
                for q in self.questions.open(project, session)
            ]
        if self.delegations is not None:
            listed, _ = self.delegations.listed(project, session)
            for d in reversed(listed):
                if d.ended is None:
                    waiting.lines.append(Line(f"{d.delegation_id} {d.kind} running: {_title(d)}"))
                else:
                    index.lines.append(Line(f"{d.delegation_id} {d.kind} {d.ended}: {_title(d)}"))


def _title(delegation: Delegation) -> str:
    """A delegation's title: a check's entry and revision, a task's first words."""
    brief = delegation.brief
    check = brief.get("check")
    if isinstance(check, dict):
        named = f"of {check.get('handle')} at revision {check.get('revision')}"
    else:
        named = _clip(str(brief.get("task") or ""), 100)
    return f"{named} (delegation_status {delegation.delegation_id})"


def render(sections: Sequence[Section], budget: int) -> Rendered:
    """The sections within ``budget`` characters, by their shares (CM-16). An empty
    section is left out, its share passing on."""
    rooms = [int(budget * section.share) for section in sections]
    fitted: list[tuple[str, list[Line], list[Line]]] = []
    carry = 0
    for index, section in enumerate(sections):
        fitted.append(_fit(section, rooms[index] + carry))
        carry = rooms[index] + carry - len(fitted[-1][0])
    decisions = next((i for i, s in enumerate(sections) if s.key == "decisions"), None)
    if decisions is not None and carry > 0:
        fitted[decisions] = _fit(sections[decisions], len(fitted[decisions][0]) + carry)
    # What the current plan step and the open questions name (CM-21b's priority).
    named = {
        found
        for section in sections
        for line in section.lines
        if section.key == "questions" or (section.key == "plan" and " (current) " in line.text)
        for found in HANDLE.findall(line.text)
    }
    return Rendered(
        "".join(text for text, _, _ in fitted),
        tuple(line.handle for _, shown, _ in fitted for line in shown if line.handle),
        tuple(line.handle for _, _, left in fitted for line in left if line.handle),
        {line.handle: line.text for _, shown, _ in fitted for line in shown if line.handle},
        {
            line.handle: line.fields
            for _, shown, _ in fitted
            for line in shown
            if line.handle and line.fields is not None
        },
        frozenset(
            cited
            for section in sections
            if section.key == "sources"
            for line in section.lines
            for cited in cast(frozenset[tuple[str, str]], (line.fields or {}).get("cites", ()))
        ),
        frozenset(named),
        frozenset(
            line.handle
            for section in sections
            for line in section.lines
            if line.pinned and line.handle
        ),
    )


def _fit(section: Section, room: int) -> tuple[str, list[Line], list[Line]]:
    """A section within ``room``: as many of its lines as fit, in order, a shorter later
    one where a longer one did not, but never one after a pinned line that did not (the
    user's constraints are never displaced); and when some do not, the line that says so,
    which counts against the room too."""
    if not section.lines and section.note is None:
        return "", [], []
    head = f"## {section.title}\n"
    note = f"{section.note}\n" if section.note else ""
    total = len(head) + len(note) + sum(len(line.text) + 1 for line in section.lines)
    reserve = 0 if total <= room else len(_omitted(len(section.lines), section.hint))
    used = len(head) + len(note) + reserve
    shown: list[Line] = []
    left: list[Line] = []
    blocked = False
    for line in section.lines:
        if not blocked and used + len(line.text) + 1 <= room:
            shown.append(line)
            used += len(line.text) + 1
        else:
            left.append(line)
            blocked = blocked or line.pinned
    text = head + "".join(line.text + "\n" for line in shown)
    if left:
        text += _omitted(len(left), section.hint)
    return text + note, shown, left


def _omitted(count: int, hint: str) -> str:
    return f"({count} more left out of this digest; {hint} lists them.)\n"


def constraints_block(lines: Sequence[Line]) -> str:
    """A delegated task's state block body (CM-37a): the user's standing constraints."""
    if not lines:
        return ""
    return "## The user's standing constraints\n" + "".join(line.text + "\n" for line in lines)
