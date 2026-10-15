# SPDX-License-Identifier: MIT
"""A delegated run's result: its final answer, checked against its output schema
(plans-and-delegation spec, PD-17 to PD-19, PD-41).

A check's final answer is a JSON object (``CheckResult``). Its texts are checked as any
agent text is: no character the user may not see. It names the entry and revision it
checked, which must be those of its brief, and ``confirmed`` comes only with a complete
check. The ``Outcome`` hook reads each closing answer: a valid one is the result; an
invalid one gets one correction, a turn with the errors (PD-18). If the correction does
not validate either, or the run has no turn left, the answer stands and the result is
missing, with the errors; nothing is filled in. The answer itself is kept unchanged as
the raw payload. The hook also notes whether the run read the entry at that revision
with ``ledger_read``, and the target of each of its proofs at the revision its link pins
(``Read``), which the ledger's gate asks for (LG-17).

A result stands only for a run that ended with that answer (``result_of``): one stopped
by a limit, a cancel or a failure has none, whatever it answered before.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import BeforeValidator, Field, JsonValue, ValidationError, model_validator

from sandbox_science.contracts.model import StrictContract
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.ledger.gate import supporting
from sandbox_science.orchestrator.ledger.models import EvidenceLink
from sandbox_science.orchestrator.notices import OUTCOME_CORRECTION

FORMAT: Final = "sandbox-check-result-1"
MAX_ERRORS: Final = 5
MAX_BYTES: Final = 16384
"""The longest result, as UTF-8 JSON: whole, it fits a tool result."""
ERROR_CHARS: Final = 500
KEPT_ERRORS: Final = 4
_FENCED: Final = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def _text(value: object, *, limit: int) -> object:
    """An agent's text as a result keeps it: trimmed, its line ends made line feeds, 1 to
    ``limit`` characters, and holding no character the user may not see."""
    if not isinstance(value, str):
        return value
    kept = value.strip()
    if not 1 <= len(kept) <= limit:
        raise ValueError(f"a text of 1 to {limit} characters, not {len(kept)}")
    try:
        return agent_text(kept, "the text")
    except DocumentError as error:
        raise ValueError(error.message) from None


def _texts(limit: int) -> object:
    return BeforeValidator(partial(_text, limit=limit))


Item = Annotated[str, _texts(1000)]
_ITEMS = Field(default=(), max_length=10)

SHAPE: Final[dict[str, JsonValue]] = {
    "entry": "the entry you checked, as the brief names it",
    "revision": "the revision you checked, as the brief names it",
    "status": "complete | partial | blocked",
    "verdict": "confirmed (only with complete) | refuted | inconclusive",
    "summary": "what you checked, how, and what you found (at most 8000 characters)",
    "obstacle": "only and always when blocked: what prevents the check",
    "deviations": ["where you departed from the brief, and why (at most 10)"],
    "open_questions": ["what you could not settle, such as a question for the user"],
}
"""The result's fields, as the correction notice describes them."""


class CheckResult(StrictContract):
    """What a check returns (``sandbox-check-result-1``). Its fields are the delegated
    run's claims; the outcome's ``ended`` is the platform's observation."""

    entry: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=999_999_999)
    status: Literal["complete", "partial", "blocked"]
    verdict: Literal["confirmed", "refuted", "inconclusive"]
    summary: Annotated[str, _texts(8000)]
    obstacle: Annotated[str, _texts(1000)] | None = None
    deviations: tuple[Item, ...] = _ITEMS
    open_questions: tuple[Item, ...] = _ITEMS

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "blocked") != (self.obstacle is not None):
            raise ValueError("a blocked check names its obstacle, and only a blocked one does")
        if self.verdict == "confirmed" and self.status != "complete":
            raise ValueError("only a complete check confirms")
        return self


Read = tuple[str, str, int] | tuple[str, str]
"""What a run read, as its tools report it: an entry or a document at a revision
(``("entry", id, revision)``, ``("document", id, revision)``) or a file by its SHA-256
(``("file", sha256)``)."""


@dataclass(frozen=True)
class Target:
    """The entry and revision a check's brief names, and what its proofs rest on: the
    reads that cover each proof's target at the revision its link pins."""

    entry_id: str
    handle: str
    revision: int
    proofs: frozenset[Read] = field(default_factory=frozenset[Read])

    def named(self, entry: str) -> bool:
        return entry in (self.entry_id, self.handle)


def proof_reads(links: Sequence[EvidenceLink]) -> frozenset[Read]:
    """The reads that cover the targets of ``links`` that are proofs which can count: an
    entry or a document at the revision the link pins, an artifact by its SHA-256. A
    proof of another target, as none is yet, is never covered."""
    found: set[Read] = set()
    for link in links:
        if not (supporting(link) and link.type == "proof"):
            continue
        if link.target == "entry" and link.revision is not None:
            found.add(("entry", link.target_id, link.revision))
        elif link.target == "block" and link.revision is not None:
            found.add(("document", link.target_id, link.revision))
        elif link.target == "artifact" and link.sha256 is not None:
            found.add(("file", link.sha256))
        else:
            found.add(("unreadable", link.evidence_id))
    return frozenset(found)


def parse(answer: str | None, target: Target | None = None) -> CheckResult:
    """The result an answer gives: a JSON object, alone or in one fenced block, naming
    ``target`` when one is given. Raises ``ValueError`` with what is wrong."""
    text = (answer or "").strip()
    if (fenced := _FENCED.match(text)) is not None:
        text = fenced[1]
    try:
        result = CheckResult.model_validate_json(text)
    except ValidationError as error:
        found = [
            f"{'.'.join(str(part) for part in e['loc']) or 'result'}: {e['msg']}"
            for e in error.errors()[:MAX_ERRORS]
        ]
        raise ValueError("; ".join(found)) from None
    if len(result.model_dump_json().encode()) > MAX_BYTES:
        raise ValueError(f"the result is longer than {MAX_BYTES} bytes")
    if target is not None and not (
        target.named(result.entry) and result.revision == target.revision
    ):
        raise ValueError(
            f"the result names {result.entry} at revision {result.revision}; the brief "
            f"names {target.handle} at revision {target.revision}"
        )
    return result


class Outcome(TurnHook):
    """A delegated run's result, read from its closing answers, and whether it read the
    entry it checks."""

    name = "outcome"

    def __init__(self, target: Target | None = None) -> None:
        self.target = target
        self.result: CheckResult | None = None
        self.errors: list[str] = []
        self.corrected = False
        self.read = False
        """Whether a ``ledger_read`` of the run returned the target at its revision."""
        self.seen: set[Read] = set()
        """What the run's tools read, of what the target's proofs rest on."""

    @property
    def read_proofs(self) -> bool | None:
        """Whether the run read the target of each of the entry's proofs at the revision
        its link pins; ``None`` for a run that checks no entry."""
        return None if self.target is None else self.target.proofs <= self.seen

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        if self.target is None or "error" in record:
            return
        if record.get("ledger") == "ledger_read":
            found = _entry(run.messages[-1].get("content"))
            if found is None:
                return
            entry, revision = str(found.get("entry_id")), found.get("revision")
            if isinstance(revision, int):
                self._saw(("entry", entry, revision))
            if self.target.named(entry) and revision == self.target.revision:
                self.read = True
            return
        read = record.get("read")
        if not isinstance(read, dict):
            return
        shown = cast(dict[str, object], read)
        if isinstance(shown.get("document"), str) and isinstance(shown.get("revision"), int):
            self._saw(("document", cast(str, shown["document"]), cast(int, shown["revision"])))
        elif isinstance(shown.get("sha256"), str):
            self._saw(("file", cast(str, shown["sha256"])))

    def _saw(self, read: Read) -> None:
        if self.target is not None and read in self.target.proofs:
            self.seen.add(read)

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        try:
            self.result = parse(answer, self.target)
        except ValueError as error:
            self.result = None
            self.errors = [*self.errors, str(error)[:ERROR_CHARS]][-KEPT_ERRORS:]
            if not self.corrected:
                # One correction (PD-18); with no turn or room left, the answer stands.
                self.corrected = True
                found = {"errors": self.errors[-1:], "format": FORMAT, "fields": SHAPE}
                return Closing("continue", (Notice(self.name, (), OUTCOME_CORRECTION.text, found),))
        return Closing()

    def summary(self) -> object | None:
        return {
            "format": FORMAT,
            "result": self.result.model_dump(mode="json") if self.result is not None else None,
            "errors": list(self.errors),
            "read": self.read,
            "read_proofs": self.read_proofs,
        }


def result_of(outcome: Outcome, stopped: str, failed: bool) -> CheckResult | None:
    """The result of a run that ended with the answer it gave: ``None`` for one stopped by
    a limit, a cancel or a failure, whatever an earlier closing parsed."""
    return outcome.result if stopped == "answered" and not failed else None


def _entry(content: object) -> Mapping[str, object] | None:
    """The entry a ``ledger_read`` result shows, under any notices that ride on it."""
    try:
        value = json.loads(str(content))
        if isinstance(value, dict) and "tool_result" in value:
            value = json.loads(str(cast(dict[str, object], value)["tool_result"]))
    except ValueError:
        return None
    return cast(Mapping[str, object], value) if isinstance(value, dict) else None
