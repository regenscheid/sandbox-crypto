# SPDX-License-Identifier: MIT
"""The research-state digest (docs/specs/context-management.md, CM-16, CM-26, CM-37a):
what the ledger, the session plan and the read register hold, rendered by code into a
run's state block, so that where the research stands never rests on a summary.

``Digest.sections`` gathers its nine sections, in CM-16's order and with its shares;
``render`` fits them in a budget: a section's unused share passes to the sections after
it, and what is left at the end goes to the decisions; a section over its share ends with
a line saying how many it left out and how to list them. Within a section, the entries
this session touched come first, then the most recently changed, then the most linked.
Every line ends with its basis (CM-9) and the call that reads it whole.

The labels are the ledger's own: an entry's kind, session, standing (CM-11, which
``ledger.standing`` decides) and basis, and its open flags with their causes; the digest
recomputes none of them. The user's standing constraints are the decisions in effect
with the basis ``user`` or ``user_quoted``, the project's and this session's; beside a
``user_quoted`` one, code copies the quotes its anchors hold, which the ledger verified
against the user's message when it was written (CM-9). Decisions meant for another
session are counted, not shown (CM-10). ``constraints`` is what a delegated task gets
(CM-37a); a check gets nothing.

Not built yet: fold proposals (section 8, step f3), and among the open items the review
findings, jobs and approvals a session waits for, and recorded artifacts in the index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.history.reads import reference_reads
from sandbox_science.orchestrator.ledger.models import EntrySummary, EntryView
from sandbox_science.orchestrator.ledger.store import Ledger
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.session_plans import PlanAuthority, PlanScope, SessionPlans

Json = dict[str, Any]

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
    ("proposals", "8. Fold proposals waiting", 0.05),
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
STANDINGS: Final = ("attempt", "claimed", "disputed", "flagged", "impossible")
"""CM-11's labels, in the order findings show them, after the plain findings."""
SCANNED: Final = 2000
"""The most entries of one type the digest reads, newest first."""
CLIP: Final = 300


@dataclass(frozen=True)
class Line:
    """One item of a section, with what orders it."""

    text: str
    touched: bool = False
    updated: str = ""
    linked: int = 0


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


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tag(basis: str | None) -> str:
    return TAGS.get(basis or "", UNRECORDED)


def _ordered(lines: list[Line]) -> list[Line]:
    """The session's own first, then the most recently changed, then the most linked."""
    by_link = sorted(lines, key=lambda line: -line.linked)
    by_time = sorted(by_link, key=lambda line: line.updated, reverse=True)
    return sorted(by_time, key=lambda line: not line.touched)


class Digest:
    """The research state of one project, as a run of one of its sessions is given it."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        plans: SessionPlans | None = None,
        questions: Questions | None = None,
        delegations: DelegationRecords | None = None,
    ) -> None:
        self.ledger, self.plans = ledger, plans
        self.questions, self.delegations = questions, delegations
        self._read: dict[tuple[str, str], list[tuple[EntrySummary, EntryView]]] = {}

    def _views(self, project: str, kind: str) -> list[tuple[EntrySummary, EntryView]]:
        """The project's entries of one type, newest ``SCANNED``, each with its view; read
        once for each rendering."""
        if (project, kind) not in self._read:
            self._read[project, kind] = self._scan(project, kind)
        return self._read[project, kind]

    def _scan(self, project: str, kind: str) -> list[tuple[EntrySummary, EntryView]]:
        found: list[EntrySummary] = []
        after: str | None = None
        while True:
            page = self.ledger.entries(project, kind=kind, after=after, limit=200)
            found += page.entries
            if page.next_after is None or len(found) >= SCANNED:
                break
            after = page.next_after
        return [(s, self.ledger.entry(project, s.entry_id)) for s in found[-SCANNED:]]

    def _line(self, view: EntryView, session: str, text: str) -> Line:
        touched = view.author.get("session_id") == session
        hint = f" {_tag(view.basis)} (ledger_read {view.handle})"
        return Line(text + hint, touched, view.updated, view.incoming_total)

    def constraints(self, project: str, session: str) -> list[Line]:
        """The user's standing constraints, the project's and this session's, each with
        the user's quoted words beside it (CM-16, CM-37a)."""
        self._read = {}
        return [
            line
            for summary, view in self._views(project, "decision")
            if (line := self._constraint(summary, view, session)) is not None
        ]

    def _constraint(self, summary: EntrySummary, view: EntryView, session: str) -> Line | None:
        if summary.status != "in_effect" or summary.basis not in ("user", "user_quoted"):
            return None
        if summary.session not in (None, session):
            return None
        return self._decision(view, session)

    def _decision(self, view: EntryView, session: str) -> Line:
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
        line = self._line(view, session, text)
        quotes = [a for a in view.anchors if isinstance(a.get("quote"), str)]
        if view.basis == "user_quoted" and quotes:
            said = "".join(
                f"\n  the user's own words, copied by the platform ({a.get('message')}): "
                f'"{_clip(str(a["quote"]), 600)}"'
                for a in quotes
            )
            line = Line(line.text + said, line.touched, line.updated, line.linked)
        return line

    def sections(self, project: str, session: str) -> list[Section]:
        """CM-16's sections for a main run of ``session``, as the stores hold them now."""
        self._read = {}
        found = {key: Section(key, title, share) for key, title, share in SECTIONS}
        found["plan"].lines = self._plan(project, session)
        found["plan"].hint = "plan_read(current)"
        decisions = self._views(project, "decision")
        elsewhere: dict[str, int] = {}
        groups: list[list[Line]] = [[], [], [], []]
        for summary, view in decisions:
            if summary.session not in (None, session):
                elsewhere[summary.session] = elsewhere.get(summary.session, 0) + 1
            elif (line := self._constraint(summary, view, session)) is not None:
                groups[0].append(line)
            elif summary.status in ("rejected", "reversed") or (
                summary.status == "in_effect" and view.fields.get("excluded_scope")
            ):
                groups[1].append(self._decision(view, session))
            elif summary.status == "in_effect":
                groups[2].append(self._decision(view, session))
            elif summary.status == "proposed":
                groups[3].append(self._decision(view, session))
        found["decisions"].lines = [line for group in groups for line in _ordered(group)]
        found["decisions"].hint = "ledger_list with type decision"
        if elsewhere:
            counted = ", ".join(f"{name}: {n}" for name, n in sorted(elsewhere.items()))
            found["decisions"].note = f"Decisions meant for other sessions, not shown: {counted}."
        found["questions"].lines = _ordered(
            [
                self._line(view, session, self._issue(view))
                for summary, view in self._views(project, "issue")
                if summary.status == "open"
                and (summary.kind or "question") in ("question", "hypothesis", "contradiction")
            ]
        )
        found["questions"].hint = "ledger_list with type issue"
        found["assumptions"].lines = _ordered(
            [
                self._line(view, session, self._assumption(view))
                for summary, view in self._views(project, "assumption")
                if summary.status == "active"
            ]
        )
        found["assumptions"].hint = "ledger_list with type assumption"
        found["findings"].lines = self._findings(project, session)
        found["findings"].hint = "ledger_list with type claim or result"
        found["sources"].lines = _ordered(
            [
                self._line(view, session, self._source(project, view))
                for summary, view in self._views(project, "reference")
                if summary.status == "active"
            ]
        )
        found["sources"].hint = "ledger_list with type reference"
        found["open"].lines, found["index"].lines = self._items(project, session)
        found["open"].hint = found["index"].hint = "delegation_status and the chat"
        return list(found.values())

    def _plan(self, project: str, session: str) -> list[Line]:
        if self.plans is None:
            return []
        scope = PlanScope(project_id=project, session_id=session)
        snapshot = self.plans.current(PlanAuthority(scope=scope, role="main", run_id="digest"))
        if snapshot is None:
            return []
        body, head = snapshot.version.body, snapshot.head
        lines = [
            Line(
                f"Goal (plan {head.ref.plan_id} version {head.ref.version}, {head.status}): "
                f"{_clip(body.goal)} [plan] (plan_read current)",
                touched=True,
            )
        ]
        latest = {s.step_id: s.latest for s in snapshot.steps}
        current = next(
            (
                step.step_id
                for phase in body.phases
                for step in phase.steps
                if (p := latest.get(step.step_id)) is None or p.status != "completed"
            ),
            None,
        )
        for phase in body.phases:
            for step in phase.steps:
                progress = latest.get(step.step_id)
                status = progress.status if progress is not None else "pending"
                note = f": {_clip(progress.note, 200)}" if progress is not None else ""
                mark = " (current)" if step.step_id == current else ""
                lines.append(Line(f"{step.step_id}{mark} {status} {_clip(step.title)}{note}", True))
        return lines

    def _issue(self, view: EntryView) -> str:
        kind = view.kind or "question"
        text = f"{view.handle} ({kind}) {_clip(view.statement)}"
        concerns = [link.handle or link.target_id for link in view.links if link.kind == "concerns"]
        if concerns:
            text += f"; concerns {', '.join(concerns)}"
        return text

    def _assumption(self, view: EntryView) -> str:
        text = f"{view.handle} {_clip(view.statement)}"
        relying = [link.handle or "" for link in view.incoming if link.kind == "assumes"]
        if relying:
            more = " and others" if view.incoming_total > len(view.incoming) else ""
            text += f"; relied on by {', '.join(relying)}{more}"
        if view.flags:
            text += f"; open flags: {', '.join(sorted({f.cause for f in view.flags}))}"
        return text

    def _findings(self, project: str, session: str) -> list[Line]:
        plain: list[Line] = []
        labelled: dict[str, list[Line]] = {label: [] for label in STANDINGS}
        found = self._views(project, "claim") + self._views(project, "result")
        found += [(s, v) for s, v in self._views(project, "issue") if s.standing is not None]
        for summary, view in found:
            if summary.status == "withdrawn":
                continue
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
            standing = summary.standing
            if standing is None:
                plain.append(self._line(view, session, text))
                continue
            text = f"[{standing.text}] {text}"
            if standing.scope:
                text += f"; within: {_clip(standing.scope, 200)}"
            if standing.assumes:
                text += f"; assumes {', '.join(standing.assumes)}"
            if standing.by:
                text += f"; by {standing.by}"
            labelled[standing.label].append(self._line(view, session, text))
        return [
            *_ordered(plain),
            *(line for label in STANDINGS for line in _ordered(labelled[label])),
        ]

    def _source(self, project: str, view: EntryView) -> str:
        reads = reference_reads(self.ledger.control, project, view.fields)
        text = f"{view.handle} {_clip(view.title, 200)}"
        if not reads:
            return text + "; read: not in this project's sessions"
        shown: list[str] = []
        for read in reads:
            covered = cast(Json, read.get("covered") or {})
            pages = f" pages {covered['pages']}" if "pages" in covered else ""
            of = f" of {covered['of']}" if covered.get("of") else ""
            shown.append(f"{read['kind']} {read['extent']}{pages}{of}")
        return text + f"; read: {'; '.join(shown)}"

    def _items(self, project: str, session: str) -> tuple[list[Line], list[Line]]:
        waiting: list[Line] = []
        ended: list[Line] = []
        if self.questions is not None:
            waiting += [
                Line(f"{q.question_id} question to the user, not answered: {_clip(q.text, 200)}")
                for q in self.questions.open(project, session)
            ]
        if self.delegations is not None:
            listed, _ = self.delegations.listed(project, session)
            for d in reversed(listed):
                if d.ended is None:
                    waiting.append(Line(f"{d.delegation_id} {d.kind} running: {_title(d)}"))
                else:
                    ended.append(Line(f"{d.delegation_id} {d.kind} {d.ended}: {_title(d)}"))
        return waiting, ended


def _title(delegation: Delegation) -> str:
    """A delegation's title: a check's entry and revision, a task's first words."""
    brief = delegation.brief
    check = brief.get("check")
    if isinstance(check, dict):
        named = f"of {check.get('handle')} at revision {check.get('revision')}"
    else:
        named = _clip(str(brief.get("task") or ""), 100)
    return f"{named} (delegation_status {delegation.delegation_id})"


def render(sections: Sequence[Section], budget: int) -> str:
    """The sections within ``budget`` characters, by their shares (CM-16). An empty
    section is left out, its share passing on."""
    rooms = [int(budget * section.share) for section in sections]
    texts: list[str] = []
    carry = 0
    for index, section in enumerate(sections):
        text = _fit(section, rooms[index] + carry)
        carry = rooms[index] + carry - len(text)
        texts.append(text)
    decisions = next((i for i, s in enumerate(sections) if s.key == "decisions"), None)
    if decisions is not None and carry > 0:
        given = len(texts[decisions]) + carry
        texts[decisions] = _fit(sections[decisions], given)
    return "".join(texts)


def _fit(section: Section, room: int) -> str:
    if not section.lines and section.note is None:
        return ""
    text = f"## {section.title}\n"
    shown = 0
    note = f"{section.note}\n" if section.note else ""
    for line in section.lines:
        if len(text) + len(line.text) + 1 + len(note) > room:
            break
        text += line.text + "\n"
        shown += 1
    left = len(section.lines) - shown
    if left:
        text += f"({left} more left out of this digest; {section.hint} lists them.)\n"
    return text + note


def constraints_block(lines: Sequence[Line]) -> str:
    """A delegated task's state block body (CM-37a): the user's standing constraints."""
    if not lines:
        return ""
    return "## The user's standing constraints\n" + "".join(line.text + "\n" for line in lines)
