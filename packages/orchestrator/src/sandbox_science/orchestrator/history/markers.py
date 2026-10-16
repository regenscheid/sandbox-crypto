# SPDX-License-Identifier: MIT
"""Current and retired markers (docs/specs/context-management.md, CM-21a).

At each rebuild code decides, from the records, whether each stored marker is current or
retired; nothing a model wrote enters the decision, and the stored records never change.
The rules compare IDs, handles, statuses and revisions, which the skeleton stored beside
each marker's text (``skeleton``):

- a decision line is retired when a later change of the same decision exists, a later
  fold's line or a later revision in the ledger, or the decision was ``reversed``;
- a caveat from a ledger entry, a caveat line or an evidence scope, when the entry was
  set aside (``superseded``, ``withdrawn``, ``refuted``) or its current revision no
  longer holds it;
- a work record that ended at a limit or with a claimed obstruction, when its issue is
  no longer ``open``;
- an inconclusive check, when the entry it checked has a later revision than the one
  checked;
- an incomplete read, when the read register shows the source read whole later, naming
  the message that completed it;
- a failure aggregate, once its fold is merged into an overview.

A marker stored before these fields were, or whose entry cannot be read, stays current:
a marker is never retired on a guess. Two of CM-21a's causes wait for their records: a
later identical call that succeeded, and an assessment that decided a claimed
obstruction; until then those markers retire only by the other causes.
"""

from __future__ import annotations

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
    """What the ledger holds for an entry now: its status, current revision, caveat
    lines, and evidence as (type, scope)."""

    status: str
    revision: int
    caveats: frozenset[str] = frozenset()
    evidence: frozenset[tuple[str, str]] = frozenset()


Entries = Callable[[str], "Held | None"]
"""An entry's current state by its handle; ``None`` when it cannot be read."""
Completed = Callable[[str, str], "str | None"]
"""The message whose read made a source whole, by its kind and source; ``None``."""


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
        held: dict[str, Held | None] = {}
        reads: dict[tuple[str, str], str | None] = {}

        def entry(handle: str) -> Held | None:
            if handle not in held:
                try:
                    held[handle] = self.entries(handle) if self.entries is not None else None
                except Exception:  # unknown, then: the marker stays current
                    held[handle] = None
            return held[handle]

        def read(kind: str, source: str) -> str | None:
            if (kind, source) not in reads:
                try:
                    found = self.completed(kind, source) if self.completed is not None else None
                except Exception:
                    found = None
                reads[kind, source] = found
            return reads[kind, source]

        found: dict[str, dict[int, str]] = {}
        for fold, record in records:
            gone: dict[int, str] = {}
            for index, marker in enumerate(record.markers):
                why = self._why(marker, record, newest, entry, read)
                if why is not None:
                    gone[index] = why
            if gone:
                found[fold] = gone
        return found

    def _why(
        self,
        marker: Mapping[str, Any],
        record: FoldRecord,
        newest: Mapping[str, str],
        entry: Callable[[str], Held | None],
        read: Callable[[str, str], str | None],
    ) -> str | None:
        kind = marker.get("kind")
        if kind == "failures":
            return "its fold was merged into an overview" if record.level > 1 else None
        if kind == "read":
            source = marker.get("source")
            if not isinstance(source, list) or len(cast(list[object], source)) != 2:
                return None
            pair = cast(list[object], source)
            done = read(str(pair[0]), str(pair[1]))
            return f"read whole later ({done})" if done is not None else None
        subject = str(marker.get("entry") or "")
        if not subject:
            return None
        if kind == "decision":
            at = marker.get("at")
            later = newest.get(subject)
            if at and later and later != at:
                return f"a later change of {subject} ({later})"
            now = entry(subject)
            if now is None:
                return None
            if now.status == "reversed" and marker.get("status") != "reversed":
                return f"{subject} was reversed (ledger_read {subject})"
            revision = marker.get("revision")
            if isinstance(revision, int) and now.revision > revision:
                return f"{subject} changed later, now revision {now.revision} ({now.status})"
            return None
        now = entry(subject)
        if now is None:
            return None
        if "work" in marker:  # a work record on an issue
            if now.status != "open":
                return f"its issue {subject} is {now.status}"
            return None
        if "check" in marker:
            revision = marker.get("revision")
            if isinstance(revision, int) and now.revision != revision:
                return f"{subject} changed after the check, now revision {now.revision}"
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


def ledger_entries(ledger: Ledger, project: str) -> Entries:
    """An ``Entries`` reading ``project``'s ledger."""

    def read(handle: str) -> Held | None:
        try:
            view = ledger.entry(project, handle)
        except Exception:  # a handle that does not exist, or a store that cannot be read
            return None
        caveats = cast(list[object], view.fields.get("caveats") or [])
        return Held(
            status=view.status,
            revision=view.revision,
            caveats=frozenset(flat(str(caveat)) for caveat in caveats),
            evidence=frozenset(
                (flat(str(link.type)), flat(link.scope or "")) for link in view.evidence
            ),
        )

    return read


def register_reads(register: Register, project: str) -> Completed:
    """A ``Completed`` reading ``project``'s read register."""

    def read(kind: str, source: str) -> str | None:
        return register.completed(project, kind, source)

    return read
