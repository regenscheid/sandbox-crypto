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

import re
from collections.abc import Sequence
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

FORMAT: Final = "sandbox-check-result-2"
TASK_FORMAT: Final = "sandbox-task-result-2"
MAX_RESULT_FILES: Final = 50
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
    "verdict": "confirmed (only with complete coverage) | refuted | inconclusive",
    "summary": "what you checked, how, and what you found (at most 8000 characters)",
    "coverage": {
        "examined": ["each part of what the entry rests on that you examined (1 to 20)"],
        "not_examined": ["each part you did not examine; empty only when you examined all"],
    },
    "refutation": "only and always when refuted: the step, value or counterexample that "
    "contradicts the entry",
    "obstacle": "only and always when blocked: what prevents the check",
    "deviations": ["where you departed from the brief, and why (at most 10)"],
    "open_questions": ["what you could not settle, such as a question for the user"],
}
"""The result's fields, as the correction notice describes them."""


class Coverage(StrictContract):
    """What a check examined of what the entry rests on, and what it did not: its
    statement, fields and assumptions, each proof and the steps and values it checked."""

    examined: tuple[Item, ...] = Field(min_length=1, max_length=20)
    not_examined: tuple[Item, ...] = Field(default=(), max_length=20)


class CheckResult(StrictContract):
    """What a check returns (``sandbox-check-result-2``). Its fields are the delegated
    run's claims; the outcome's ``ended`` is the platform's observation. A refutation
    cites what contradicts the entry, and a confirmation examined everything the entry
    rests on (PD-18, as the coordinator amended it on 2026-10-15)."""

    entry: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=999_999_999)
    status: Literal["complete", "partial", "blocked"]
    verdict: Literal["confirmed", "refuted", "inconclusive"]
    summary: Annotated[str, _texts(8000)]
    coverage: Coverage
    refutation: Annotated[str, _texts(2000)] | None = None
    obstacle: Annotated[str, _texts(1000)] | None = None
    deviations: tuple[Item, ...] = _ITEMS
    open_questions: tuple[Item, ...] = _ITEMS

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "blocked") != (self.obstacle is not None):
            raise ValueError("a blocked check names its obstacle, and only a blocked one does")
        if self.verdict == "confirmed" and self.status != "complete":
            raise ValueError("only a complete check confirms")
        if self.verdict == "confirmed" and self.coverage.not_examined:
            raise ValueError(
                "a check that did not examine everything the entry rests on does not confirm it"
            )
        if (self.verdict == "refuted") != (self.refutation is not None):
            raise ValueError(
                "a refuted check cites, in refutation, the step, value or counterexample "
                "that contradicts the entry, and only a refuted one does"
            )
        return self


TASK_SHAPE: Final[dict[str, JsonValue]] = {
    "status": "complete | partial | blocked",
    "summary": "what you did, how, and what you found or made (at most 8000 characters)",
    "obstacle": "only and always when blocked: what prevents the task",
    "deviations": ["where you departed from the brief, and why (at most 10)"],
    "open_questions": ["what you could not settle, such as a question for the user"],
    "dismissals": [
        {"what": "a line of work you dropped", "limit": "time | tools | context | ideas"}
    ],
    "obstructions": [
        {"claim": "what you claim cannot be done", "evidence": "why, as you found it"}
    ],
    "result_files": ["the path of each file that is a result, which your calls wrote or published"],
}
"""A task's result's fields, as the correction notice describes them."""


class Dismissal(StrictContract):
    """A line of work a task dropped, and what ran out (LG-6's ``limit``)."""

    what: Item
    limit: Literal["time", "tools", "context", "ideas"]


class Obstruction(StrictContract):
    """An obstruction a task claims, with its evidence as it found it (LG-6, LG-7)."""

    claim: Annotated[str, _texts(2000)]
    evidence: Annotated[str, _texts(4000)]


class TaskResult(StrictContract):
    """What a task returns (``sandbox-task-result-2``): the delegated run's claims about
    what it did, the lines of work it dropped, the obstructions it claims, and the files
    that are its results (RA-38); the outcome's ``ended`` is the platform's observation
    (PD-17)."""

    status: Literal["complete", "partial", "blocked"]
    summary: Annotated[str, _texts(8000)]
    obstacle: Annotated[str, _texts(1000)] | None = None
    deviations: tuple[Item, ...] = _ITEMS
    open_questions: tuple[Item, ...] = _ITEMS
    dismissals: tuple[Dismissal, ...] = Field(default=(), max_length=10)
    obstructions: tuple[Obstruction, ...] = Field(default=(), max_length=5)
    result_files: tuple[Annotated[str, _texts(4096)], ...] = Field(
        default=(), max_length=MAX_RESULT_FILES
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "blocked") != (self.obstacle is not None):
            raise ValueError("a blocked task names its obstacle, and only a blocked one does")
        if len(set(self.result_files)) != len(self.result_files):
            raise ValueError("a result file is listed once")
        return self


Read = tuple[str, ...]
"""What a run read whole, as its tools report it: an entry's statement at a revision
(``("entry", id, revision)``), a document's block at a revision (``("block", id,
revision, block)``) or a file by its SHA-256 (``("file", sha256)``)."""


@dataclass(frozen=True)
class Proof:
    """One of the entry's proofs: its evidence link, which a read of the entry must show,
    and the read that covers its target."""

    evidence_id: str
    read: Read


@dataclass(frozen=True)
class Target:
    """The entry and revision a check's brief names, and its proofs."""

    entry_id: str
    handle: str
    revision: int
    proofs: frozenset[Proof] = field(default_factory=frozenset[Proof])

    def named(self, entry: str) -> bool:
        return entry in (self.entry_id, self.handle)


def proof_reads(links: Sequence[EvidenceLink]) -> frozenset[Proof]:
    """The entry's proofs that can count, each with the read that covers its target: an
    entry's whole statement, or a document's block, at the revision the link pins, or a
    whole file with the artifact's SHA-256. A proof of another target, as none is yet,
    is never covered."""
    found: set[Proof] = set()
    for link in links:
        if not (supporting(link) and link.type == "proof"):
            continue
        read: Read = ("unreadable", link.evidence_id)
        if link.target == "entry" and link.revision is not None:
            read = ("entry", link.target_id, str(link.revision))
        elif link.target == "block" and link.revision is not None and link.block is not None:
            read = ("block", link.target_id, str(link.revision), link.block)
        elif link.target == "artifact" and link.sha256 is not None:
            read = ("file", link.sha256)
        found.add(Proof(link.evidence_id, read))
    return frozenset(found)


def _parsed[R: CheckResult | TaskResult](answer: str | None, schema: type[R]) -> R:
    """The ``schema`` result an answer gives: a JSON object, alone or in one fenced block.
    Raises ``ValueError`` with what is wrong."""
    text = (answer or "").strip()
    if (fenced := _FENCED.match(text)) is not None:
        text = fenced[1]
    try:
        result = schema.model_validate_json(text)
    except ValidationError as error:
        found = [
            f"{'.'.join(str(part) for part in e['loc']) or 'result'}: {e['msg']}"
            for e in error.errors()[:MAX_ERRORS]
        ]
        raise ValueError("; ".join(found)) from None
    if len(result.model_dump_json().encode()) > MAX_BYTES:
        raise ValueError(f"the result is longer than {MAX_BYTES} bytes")
    return result


def parse_task(answer: str | None) -> TaskResult:
    """The result a task's answer gives; raises ``ValueError`` with what is wrong."""
    return _parsed(answer, TaskResult)


def parse(answer: str | None, target: Target | None = None) -> CheckResult:
    """The result an answer gives: a JSON object, alone or in one fenced block, naming
    ``target`` when one is given. Raises ``ValueError`` with what is wrong."""
    result = _parsed(answer, CheckResult)
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

    def __init__(self, target: Target | None = None, *, task: bool = False) -> None:
        """``target`` is a check's entry and revision; ``task`` reads a task's result."""
        self.target = target
        self.task = task
        self.result: CheckResult | TaskResult | None = None
        self.errors: list[str] = []
        self.corrected = False
        self.read = False
        """Whether a ``ledger_read`` of the run showed the target's whole statement at its
        revision."""
        self.seen: set[Read] = set()
        """What the run's tools showed whole."""
        self.ranges: dict[str, list[tuple[int, int]]] = {}
        """The byte ranges of each file, by its SHA-256, that the run's reads showed whole:
        a file is read whole once they cover it, however many reads it took."""
        self.shown: set[str] = set()
        """The target's evidence links its reads of the target's revision showed."""
        self.created: list[str] = []
        """The entries the run's ledger changes created, by ID (LG-6's ``produced``)."""
        self.recorded: dict[str, dict[str, JsonValue]] = {}
        """The latest revision each path got from the run's calls, recorded or published,
        by the path as the run named it (RA-36, RA-38)."""
        self.pinned: list[dict[str, JsonValue]] = []
        """The result files a task declared, pinned to the revisions its calls made."""

    @property
    def read_proofs(self) -> bool | None:
        """Whether the run's reads of the entry showed each of its proofs, and read the
        target of each whole, at the revision its link pins; ``None`` for a run that
        checks no entry."""
        if self.target is None:
            return None
        return all(p.evidence_id in self.shown and p.read in self.seen for p in self.target.proofs)

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        self._created(record)
        self._files(record)
        read = record.get("read")
        if self.target is None or "error" in record or not isinstance(read, dict):
            return
        shown = cast(dict[str, object], read)
        revision = shown.get("revision")
        if record.get("ledger") == "ledger_read" and isinstance(revision, int):
            entry = str(shown.get("entry"))
            if shown.get("complete") is True:
                self.seen.add(("entry", entry, str(revision)))
            if self.target.named(entry) and revision == self.target.revision:
                self.read = self.read or shown.get("complete") is True
                listed = shown.get("evidence")
                if isinstance(listed, list):
                    self.shown.update(str(e) for e in cast(list[object], listed))
        elif isinstance(shown.get("document"), str) and isinstance(revision, int):
            blocks = shown.get("blocks")
            if isinstance(blocks, list):
                document = cast(str, shown["document"])
                self.seen.update(
                    ("block", document, str(revision), str(block))
                    for block in cast(list[object], blocks)
                )
        elif isinstance(sha256 := shown.get("sha256"), str):
            self._file(sha256, shown)

    def _file(self, sha256: str, shown: dict[str, object]) -> None:
        """A read of a file: one that showed it whole, or the last of the reads that
        together showed every byte of it, from its start to its end, with that SHA-256."""
        if shown.get("complete") is True:
            self.seen.add(("file", sha256))
            return
        offset, length, size = shown.get("offset"), shown.get("shown"), shown.get("size")
        if not (isinstance(offset, int) and isinstance(length, int) and isinstance(size, int)):
            return
        if length <= 0:
            return
        ranges = self.ranges.setdefault(sha256, [])
        ranges.append((offset, offset + length))
        reached = 0
        for start, end in sorted(ranges):
            if start > reached:
                return
            reached = max(reached, end)
        if size > 0 and reached >= size:
            self.seen.add(("file", sha256))

    def _created(self, record: dict[str, Any]) -> None:
        if record.get("ledger") not in ("ledger_change", "ledger_import") or "error" in record:
            return
        result = record.get("result")
        entries = (
            cast(dict[str, object], result).get("entries") if isinstance(result, dict) else None
        )
        for entry in cast(list[object], entries) if isinstance(entries, list) else []:
            if isinstance(entry, dict) and cast(dict[str, object], entry).get("created") is True:
                found = cast(dict[str, object], entry).get("entry_id")
                if isinstance(found, str) and found not in self.created:
                    self.created.append(found)

    def _files(self, record: dict[str, Any]) -> None:
        """The revisions a call made: those it recorded, and the one it published."""
        if "error" in record:
            return
        report = record.get("recorded_files")
        listed = cast(dict[str, object], report).get("files") if isinstance(report, dict) else None
        for entry in cast(list[object], listed) if isinstance(listed, list) else []:
            if isinstance(entry, dict):
                found = cast(dict[str, JsonValue], entry)
                if isinstance(path := found.get("path"), str):
                    self.recorded[path] = found
        snapshot, receipt = record.get("artifact"), record.get("artifact_inventory")
        if isinstance(snapshot, dict) and isinstance(receipt, dict):
            published = cast(dict[str, Any], receipt)
            ref = published.get("revision")
            path = cast(dict[str, Any], snapshot).get("source_path")
            recorded = published.get("state") == "recorded"
            if recorded and isinstance(ref, dict) and isinstance(path, str):
                pinned = cast(dict[str, Any], ref)
                self.recorded[path] = {
                    "path": path,
                    "artifact_id": pinned.get("artifact_id"),
                    "revision": pinned.get("revision"),
                    "sha256": pinned.get("sha256"),
                    "size": pinned.get("size"),
                    "content_retained": published.get("worker_snapshot") is not None,
                    "deleted": False,
                }

    def _pin(self, result: TaskResult) -> list[dict[str, JsonValue]]:
        """The task's result files, each pinned to the latest revision its calls made, or
        ``ValueError`` naming those its calls did not make (RA-38)."""
        pinned: list[dict[str, JsonValue]] = []
        missing: list[str] = []
        for path in result.result_files:
            found = self.recorded.get(path.removeprefix("./"))
            if found is None or found.get("deleted") is True:
                missing.append(path)
                continue
            pinned.append({k: v for k, v in found.items() if k not in ("deleted", "reproduced")})
        if missing:
            raise ValueError(
                f"result_files: your calls wrote or published no file {', '.join(missing[:5])}; "
                "a result file is one your calls wrote, as they named it, or published"
            )
        return pinned

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        try:
            if self.task:
                found = parse_task(answer)
                self.pinned = self._pin(found)
                self.result = found
            else:
                self.result = parse(answer, self.target)
        except ValueError as error:
            self.result = None
            self.pinned = []
            self.errors = [*self.errors, str(error)[:ERROR_CHARS]][-KEPT_ERRORS:]
            if not self.corrected:
                # One correction (PD-18); with no turn or room left, the answer stands.
                self.corrected = True
                found = {"errors": self.errors[-1:], "format": self.format, "fields": self.shape}
                return Closing("continue", (Notice(self.name, (), OUTCOME_CORRECTION.text, found),))
        return Closing()

    @property
    def format(self) -> str:
        return TASK_FORMAT if self.task else FORMAT

    @property
    def shape(self) -> dict[str, JsonValue]:
        return TASK_SHAPE if self.task else SHAPE

    def summary(self) -> object | None:
        return {
            "format": self.format,
            "result": self.result.model_dump(mode="json") if self.result is not None else None,
            "errors": list(self.errors),
            "read": self.read,
            "read_proofs": self.read_proofs,
            "result_files": list(self.pinned),
        }


def result_of(outcome: Outcome, stopped: str, failed: bool) -> CheckResult | TaskResult | None:
    """The result of a run that ended with the answer it gave: ``None`` for one stopped by
    a limit, a cancel or a failure, whatever an earlier closing parsed."""
    return outcome.result if stopped == "answered" and not failed else None
