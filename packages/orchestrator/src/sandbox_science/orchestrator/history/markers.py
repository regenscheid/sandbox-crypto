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
  the issue is no longer ``open``; on a claim or result, when the entry was set aside;
- an inconclusive check, when the statement, fields or evidence of the entry it checked
  differ now from those at the revision it checked;
- an incomplete read, when the read register shows the source read whole by a read after
  the marker's, of the same content: a PDF's or a file's key is its digest; another
  source's reads before the marker's are not counted, since its content may have
  changed (``Register.completed``);
- a failure aggregate, once its fold is merged into an overview.

A marker stored before these fields were, or whose entry cannot be read, stays current:
a marker is never retired on a guess. Three of CM-21a's causes wait for their records: a
later identical call that succeeded, a later check that ended, and an assessment that
decided a claimed obstruction; until then those markers retire only by the other causes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from .record import FoldRecord, flat

if TYPE_CHECKING:
    from sandbox_science.orchestrator.ledger.store import Ledger

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


class Judge:
    """Decides which stored markers are retired, at one rebuild."""

    def __init__(self, entries: Entries | None = None, completed: Completed | None = None):
        self.entries, self.completed = entries, completed

    def retired(self, records: Sequence[tuple[str, FoldRecord]]) -> dict[str, dict[int, str]]:
        """For each record, by its archive ID, its retired markers by their index, each
        with why: ``records`` are all the session's, oldest first, those an overview
        covers among them."""
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

        found: dict[str, dict[int, str]] = {}
        for fold, record in records:
            gone: dict[int, str] = {}
            for index, marker in enumerate(record.markers):
                why = _why(marker, record, newest, entry, read)
                if why is not None:
                    gone[index] = why
            if gone:
                found[fold] = gone
        return found


def _why(
    marker: Mapping[str, Any],
    record: FoldRecord,
    newest: Mapping[str, str],
    entry: Callable[[str, int | None], Held | None],
    read: Callable[[str, str, str | None], str | None],
) -> str | None:
    """Why ``marker`` is retired, or ``None`` while it is current."""
    kind = marker.get("kind")
    if kind == "failures":
        return "its fold was merged into an overview" if record.level > 1 else None
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
        if subject.startswith("I-"):
            return f"its issue {subject} is {now.status}" if now.status != "open" else None
        return f"{subject} is {now.status}" if now.status in SET_ASIDE else None
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


def register_reads(register: Register, project: str) -> Completed:
    """A ``Completed`` reading ``project``'s read register."""

    def read(kind: str, source: str, after: str | None) -> str | None:
        return register.completed(project, kind, source, after=after)

    return read
