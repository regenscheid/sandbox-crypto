# SPDX-License-Identifier: MIT
"""A delegated run's result: what it submits with ``submit_result``, checked against its
output schema (plans-and-delegation spec, PD-17 to PD-19, PD-41, PD-45).

A delegated run, a check or a task, returns its result through ``submit_result``, a tool
only delegated runs are offered, whose input schema is made for the run: a check's
(``check_schema``) is ``CheckResult``'s, naming the entry and revision of its brief; a
task's (``task_schema``) is ``TaskResult``'s, its ``deliverables``, when the brief lists
any, a list of one item for each, each item of its deliverable's kind with that kind's
fields alone and its ``id`` among the brief's numbers of that kind. The ``Outcome`` hook
checks a submission inside the call, with the checks below, and answers it: every
problem, by its place in the result and in words, with the form the brief asked for; or
what it recorded, for the run to check. A run submits as often as it needs: a submission
that checks replaces the one before, and one that does not replaces nothing.

A check's result is a JSON object (``CheckResult``). Its texts are checked as any agent
text is: no character the user may not see. It names the entry and revision it checked,
which must be those of its brief, and ``confirmed`` comes only with a complete check. The
hook also notes whether the run read the entry at that revision with ``ledger_read``, and
the target of each of its proofs at the revision its link pins (``Read``), which the
ledger's gate asks for (LG-17).

A task's result (``sandbox-task-result-3``) accounts for the deliverables its brief
lists, exactly when it lists any (PD-45): one item for each, by its number, whose
reference the hook checks against what the run's calls recorded, as it checks a result
file (RA-38). Its ``lessons`` are the child's notes on operating the tools (DA-27):
agent text, data for the parent, read by no code. Two forms models keep giving are read
as what they unambiguously say, and the kept result's ``platform_notes`` says so (PD-18
as amended 2026-11-01): ``deliverables`` given as an object keyed by number becomes the
list, in the brief's order, each key the item's ``id`` (a value that is not an object, or
names another ``id``, stays an error); and an artifact produced with its path alone gets
the one revision the run's calls recorded of that path (none or several stay an error).

A closing answer that comes with no result submitted gets the platform's notice that the
result goes through ``submit_result``, with its form, at most ``NUDGES`` times in a run.
An answer that holds a valid result as JSON, read leniently (``candidates``: the last
fenced ``json`` block, or the last top-level JSON object, prose around it allowed), is
the result instead, its ``platform_notes`` saying it came in the answer; so is a report a
limit's last call gives, where no tool runs, and before which the hook's notice asks the run
to write the JSON it would have submitted (``report_notices``). A submission or an answer
that checks but leaves deliverables out stands, unless a later one checks, as the child's
result with the platform's status ``blocked`` and its obstacle, marked ``set_by: platform``
(PD-45).
Without any, the parent still gets a result, the platform's, never stronger than the
records: ``blocked``, its obstacle that the run submitted none, the run's answer as its
summary, each deliverable ``not_produced``, no result files, and a note naming the files
the run's calls wrote or published.

A result stands for a run that ended with an answer or at a limit (``result_of``): at a
limit, one that said ``complete`` is ``partial``, with an obstacle naming the limit, both
the platform's. A run cancelled, interrupted or failed has none.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import BeforeValidator, Field, JsonValue, ValidationError, model_validator

from sandbox_science.contracts.model import StrictContract
from sandbox_science.orchestrator.context.history.archive import body
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.ledger.gate import supporting
from sandbox_science.orchestrator.ledger.models import EvidenceLink
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.notices import OUTCOME_LAST_CALL, OUTCOME_REPORT, OUTCOME_SUBMIT
from sandbox_science.orchestrator.purpose import without
from sandbox_science.orchestrator.worker_client import dump

from .prompt import PRODUCED, produced_form

FORMAT: Final = "sandbox-check-result-2"
TASK_FORMAT: Final = "sandbox-task-result-3"
MAX_RESULT_FILES: Final = 50
MAX_ERRORS: Final = 5
MAX_BYTES: Final = 16384
"""The longest check result, as UTF-8 JSON: whole, it fits a tool result."""
TASK_BYTES: Final = 28 * 1024
"""The longest task result, as UTF-8 JSON (PD-45): with the platform's mark it stays
within the 32 KiB the delegations table holds for a result."""
MAX_LESSONS: Final = 5
LESSON_CHARS: Final = 200
MAX_ACCOUNTED: Final = 12
OBSTACLE_BYTES: Final = 2048
"""The longest obstacle the platform writes for deliverables left out, as UTF-8: with
it, a task's longest result stays within the table's 32 KiB."""
ERROR_CHARS: Final = 500
KEPT_ERRORS: Final = 4
_FENCED: Final = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)
SUBMIT: Final = "submit_result"
"""The tool a delegated run submits its result with."""
NUDGES: Final = 3
"""The closing answers with no result submitted that get the platform's notice, in a
run; the next one stands."""
MAX_PROBLEMS: Final = 20
"""The most problems the answer to a refused submission names."""
SUMMARY_BYTES: Final = 16384
"""The most of a run's answer, as UTF-8, that the platform's result keeps as its summary:
with the rest, it stays within the table's 32 KiB."""
NOTE_CHARS: Final = 3000
"""The longest note the platform writes of the files a run's calls wrote."""
LIMITS: Final = frozenset(
    {
        "turn_limit",
        "context_limit",
        "run_budget",
        "budget",
        "runaway",
        "resource_pause",
        "resource_report",
        "resource_time",
        "resource_storage",
        "resource_retention",
        "environment_time",
    }
)
"""The stops of a run at a limit, or by the runaway guard (PD-19): its delegation ends
``limit_reached``, and the answer of its report round, if it took one, is its report
(RS-6f)."""
NO_SUBMISSION: Final = "the run ended without submitting a result"
"""The obstacle of the platform's result for a run that submitted none."""
ANSWER: Final = "answer"
REPORT: Final = "report"
CAME: Final = {
    ANSWER: "the result came in the run's closing answer, not through submit_result",
    REPORT: "the result came in the run's report at its limit, not through submit_result",
}
"""The platform's note on a result read from a run's text, by where it came from."""
LIMIT_OBSTACLE: Final = "the run reached a limit ({limit}) before it ended"
"""The obstacle of a run's result at a limit, which the platform marks as its own."""


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


def _optional(value: object, *, limit: int) -> object:
    """A text only some results hold, such as a blocked one's obstacle: left blank, it is
    absent. A model that fills every field of the shape writes "" for one that does not
    apply, and the correction then asked for a text that only a blocked result may hold
    (lab 42, B6)."""
    if isinstance(value, str) and not value.strip():
        return None
    return _text(value, limit=limit)


def _optional_texts(limit: int) -> object:
    return BeforeValidator(partial(_optional, limit=limit))


def _line(value: object, *, limit: int) -> object:
    """An agent's text on one line, such as a lesson."""
    found = _text(value, limit=limit)
    if isinstance(found, str) and "\n" in found:
        raise ValueError("a text on one line")
    return found


Item = Annotated[str, _texts(1000)]
_ITEMS = Field(default=(), max_length=10)


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
    refutation: Annotated[str | None, _optional_texts(2000)] = None
    obstacle: Annotated[str | None, _optional_texts(1000)] = None
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


class Dismissal(StrictContract):
    """A line of work a task dropped, and what ran out (LG-6's ``limit``)."""

    what: Item
    limit: Literal["time", "tools", "context", "ideas"]


class Obstruction(StrictContract):
    """An obstruction a task claims, with its evidence as it found it (LG-6, LG-7)."""

    claim: Annotated[str, _texts(2000)]
    evidence: Annotated[str, _texts(4000)]


class Accounted(StrictContract):
    """How a task accounts for one deliverable of its brief (PD-45): ``produced`` with a
    reference of the deliverable's kind, ``partial`` with what exists, if anything, and
    what is missing, or ``not_produced`` with why. The reference is checked against the
    brief's kind and the run's calls by the ``Outcome`` hook."""

    id: str = Field(pattern=r"^d([1-9]|1[0-2])$")
    status: Literal["produced", "partial", "not_produced"]
    path: Annotated[str | None, _optional_texts(4096)] = None
    revision: int | None = Field(default=None, ge=1, le=999_999_999)
    entry: Annotated[str | None, _optional_texts(128)] = None
    answer: Annotated[str | None, _optional_texts(1000)] = None
    missing: Annotated[str | None, _optional_texts(500)] = None
    reason: Annotated[str | None, _optional_texts(500)] = None

    @property
    def references(self) -> tuple[str, ...]:
        """The reference fields it gives."""
        found = {"path": self.path, "revision": self.revision, "entry": self.entry}
        found["answer"] = self.answer
        return tuple(name for name, value in found.items() if value is not None)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "partial") != (self.missing is not None):
            raise ValueError(f"{self.id}: a partial deliverable says what is missing, and only it")
        if (self.status == "not_produced") != (self.reason is not None):
            raise ValueError(f"{self.id}: one not produced says why, and only it")
        if self.status == "not_produced" and self.references:
            raise ValueError(f"{self.id}: one not produced names nothing it produced")
        return self


Lesson = Annotated[str, BeforeValidator(partial(_line, limit=LESSON_CHARS))]


class TaskResult(StrictContract):
    """What a task returns (``sandbox-task-result-3``): the delegated run's claims about
    what it did, the lines of work it dropped, the obstructions it claims, the files that
    are its results (RA-38), its accounting for the brief's deliverables (PD-45) and its
    lessons (DA-27); the outcome's ``ended`` is the platform's observation (PD-17)."""

    status: Literal["complete", "partial", "blocked"]
    summary: Annotated[str, _texts(8000)]
    obstacle: Annotated[str | None, _optional_texts(1000)] = None
    deviations: tuple[Item, ...] = _ITEMS
    open_questions: tuple[Item, ...] = _ITEMS
    dismissals: tuple[Dismissal, ...] = Field(default=(), max_length=10)
    obstructions: tuple[Obstruction, ...] = Field(default=(), max_length=5)
    result_files: tuple[Annotated[str, _texts(4096)], ...] = Field(
        default=(), max_length=MAX_RESULT_FILES
    )
    deliverables: Annotated[tuple[Accounted, ...], Field(max_length=MAX_ACCOUNTED)] | None = None
    lessons: tuple[Lesson, ...] = Field(default=(), max_length=MAX_LESSONS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "blocked") != (self.obstacle is not None):
            raise ValueError("a blocked task names its obstacle, and only a blocked one does")
        if len(set(self.result_files)) != len(self.result_files):
            raise ValueError("a result file is listed once")
        listed = [item.id for item in self.deliverables or ()]
        if len(set(listed)) != len(listed):
            raise ValueError("a deliverable is accounted for once")
        return self

    def kept(self) -> dict[str, JsonValue]:
        """The result as the outcome keeps it: without ``deliverables`` when the brief
        listed none, and without ``lessons`` when it gave none, as a result before them."""
        found = cast(dict[str, JsonValue], self.model_dump(mode="json"))
        if self.deliverables is None:
            del found["deliverables"]
        if not self.lessons:
            del found["lessons"]
        return found


@dataclass(frozen=True)
class Asked:
    """A deliverable of a task's brief, as its result must account for it (PD-45)."""

    id: str
    title: str
    kind: str

    @property
    def named(self) -> str:
        return f"{self.id} ({self.title})"


def asked_of(brief: Mapping[str, JsonValue]) -> tuple[Asked, ...]:
    """The deliverables a stored brief lists, in order; none for a check's."""
    found = brief.get("deliverables")
    items = cast(list[object], found) if isinstance(found, list) else []
    return tuple(
        Asked(str(item.get("id")), str(item.get("title")), str(item.get("kind")))
        for item in (cast(dict[str, object], i) for i in items if isinstance(i, dict))
    )


_KINDS: Final = {
    "artifact": "an artifact",
    "file": "a file",
    "entry": "an entry",
    "answer": "an answer",
}

EntryMade = Callable[[str, frozenset[str]], Awaitable[bool]]
"""Whether an entry, by its handle or ID, exists in the project and has a work record
among the IDs given: those the run's ledger changes appended."""


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


KEYED_NOTE: Final = (
    "deliverables: given as an object keyed by deliverable number; the platform made it "
    "the list the result's form has, in the brief's order, each key the item's id, and "
    "changed nothing else"
)
"""The platform's note on a result whose ``deliverables`` it listed (PD-18)."""
PATH_NOTE: Final = (
    "{ids}: each artifact gave its path without its revision; the platform gave each the "
    "one revision the run's calls recorded of that path"
)
"""The platform's note on a result whose artifacts it gave their revisions (PD-18)."""


def _unfenced(answer: str | None) -> str:
    """An answer's JSON text: alone, or the content of its one fenced block."""
    text = (answer or "").strip()
    if (fenced := _FENCED.match(text)) is not None:
        text = fenced[1]
    return text


def _listed(answer: str | None, order: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """An answer whose ``deliverables`` is an object keyed by number, as the list the
    result's form has, with the platform's note; any other answer as it is, with none.

    Each key becomes its item's ``id``, in the order of the brief's deliverables
    (``order``), then any other key in the order given, which the accounting then
    refuses as one the brief does not have. Only that form, with nothing to choose, is
    listed: a value that is not an object, one whose own ``id`` differs from its key, or a
    key given twice raises ``ValueError`` saying so, and nothing is filled in."""
    repeated: list[dict[str, object]] = []
    """The objects that gave a key twice, kept so that none is freed and its ID reused."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        found = dict(items)
        if len(found) != len(items):
            repeated.append(found)
        return found

    try:
        loaded: object = json.loads(_unfenced(answer), object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        # Not JSON, or nested past what Python reads: the schema's own reading says what
        # is wrong with it.
        return answer, ()
    if not isinstance(loaded, dict):
        return answer, ()
    whole = cast(dict[str, object], loaded)
    keyed = whole.get("deliverables")
    if not isinstance(keyed, dict):
        return answer, ()
    given = cast(dict[str, object], keyed)
    form = "deliverables: a list with one object for each deliverable, its number as id"
    if any(found is given for found in repeated):
        raise ValueError(f"{form}; given as an object keyed by number, with a key given twice")
    ranked = {key: rank for rank, key in enumerate(order)}
    items: list[object] = []
    for key in sorted(given, key=lambda k: ranked.get(k, len(ranked))):
        value = given[key]
        if not isinstance(value, dict):
            raise ValueError(f"{form}; given as an object keyed by number, {key} is no object")
        item = cast(dict[str, object], value)
        if "id" in item and item["id"] != key:
            raise ValueError(
                f"{form}; given as an object keyed by number, key {key} holds another id"
            )
        items.append({**item, "id": key})
    return json.dumps({**whole, "deliverables": items}), (KEYED_NOTE,)


class SchemaError(ValueError):
    """An answer its schema refuses, with each of its errors, at most ``MAX_ERRORS``, and
    each of them, at most ``MAX_PROBLEMS``, by its place in the result (``located``)."""

    def __init__(self, errors: Sequence[str], located: Sequence[tuple[str, str]] = ()) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)
        self.located = tuple(located)


def _path(loc: Sequence[int | str]) -> str:
    """A place in a result as a JSON path reads it, such as ``deliverables[0].revision``;
    ``result`` for the whole."""
    found = ""
    for part in loc:
        found += f"[{part}]" if isinstance(part, int) else (f".{part}" if found else str(part))
    return found or "result"


def _plain(message: str) -> str:
    """A validation message in plain words, without pydantic's prefix."""
    return message.removeprefix("Value error, ")


def _parsed[R: CheckResult | TaskResult](
    answer: str | None, schema: type[R], limit: int = MAX_BYTES
) -> R:
    """The ``schema`` result an answer gives: a JSON object, alone or in one fenced block.
    Raises ``ValueError`` with what is wrong, ``SchemaError`` when the schema refuses it."""
    text = _unfenced(answer)
    try:
        result = schema.model_validate_json(text)
    except ValidationError as error:
        listed = error.errors()
        found = [
            f"{'.'.join(str(part) for part in e['loc']) or 'result'}: {e['msg']}"
            for e in listed[:MAX_ERRORS]
        ]
        located = [(_path(e["loc"]), _plain(e["msg"])) for e in listed[:MAX_PROBLEMS]]
        raise SchemaError(found, located) from None
    if len(result.model_dump_json().encode()) > limit:
        raise ValueError(f"the result is longer than {limit} bytes")
    return result


def read_task(answer: str | None, order: Sequence[str] = ()) -> tuple[TaskResult, tuple[str, ...]]:
    """The result a task's answer gives, its ``deliverables`` listed in the order of the
    brief's (``order``) when it gave them keyed by number, and the platform's notes on
    what it listed; raises ``ValueError`` with what is wrong."""
    text, notes = _listed(answer, order)
    return _parsed(text, TaskResult, TASK_BYTES), notes


def parse_task(answer: str | None, order: Sequence[str] = ()) -> TaskResult:
    """The result a task's answer gives; raises ``ValueError`` with what is wrong."""
    return read_task(answer, order)[0]


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


def _fenced(text: str) -> list[str]:
    """The contents of an answer's fenced blocks marked ``json``, or not marked, in order:
    a block opens with a line that begins with three backticks and closes at the next."""
    found: list[str] = []
    lines: list[str] | None = None
    marked = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            if lines is not None:
                lines.append(line)
            continue
        if lines is None:
            lines, marked = [], stripped[3:].strip().lower() in ("", "json")
            continue
        if marked:
            found.append("\n".join(lines))
        lines = None
    return found


def _last_object(text: str) -> str | None:
    """The text of the last top-level JSON object in ``text``: each ``{`` that begins an
    object, outside an object read before it, read whole."""
    decoder = json.JSONDecoder()
    last: str | None = None
    start = text.find("{")
    while start >= 0:
        try:
            found, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)
            continue
        if isinstance(found, dict):
            last = text[start:end]
        start = text.find("{", end)
    return last


def _object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, RecursionError):
        return False


def candidates(answer: str | None) -> tuple[str, ...]:
    """The JSON objects an answer may give its result in, read leniently, most likely
    first: its last fenced ``json`` block, then its last top-level JSON object, prose
    before or after either allowed (lab round 2: every child wrote a sentence before its
    JSON, and one a stub block before it)."""
    text = answer or ""
    found: list[str] = []
    blocks = [block.strip() for block in _fenced(text) if _object(block.strip())]
    if blocks:
        found.append(blocks[-1])
    last = _last_object(text)
    if last is not None and last not in found:
        found.append(last)
    return tuple(found)


def _string(description: str, limit: int, *, least: int = 0) -> dict[str, Any]:
    found: dict[str, Any] = {"type": "string", "maxLength": limit}
    if least:
        found["minLength"] = least
    return {**found, "description": description}


def _strings(description: str, most: int, limit: int = 1000) -> dict[str, Any]:
    return {
        "type": "array",
        "maxItems": most,
        "description": description,
        "items": {"type": "string", "minLength": 1, "maxLength": limit},
    }


_STATUSES: Final[list[Any]] = ["complete", "partial", "blocked"]
_LISTS: Final[dict[str, Any]] = {
    "deviations": _strings("where you departed from the brief, and why", 10),
    "open_questions": _strings(
        "what you could not settle, such as a question your parent did not answer", 10
    ),
}


def check_schema(target: Target | None = None) -> dict[str, Any]:
    """``submit_result``'s schema for a check: ``CheckResult``'s, its entry and revision
    those of its brief (``target``)."""
    entry = _string("the entry you checked, as the brief names it", 128, least=1)
    revision: dict[str, Any] = {
        "type": "integer",
        "minimum": 1,
        "description": "the revision you checked, as the brief names it",
    }
    if target is not None:
        entry["enum"] = [target.handle]
        revision["enum"] = [target.revision]
    return {
        "type": "object",
        "properties": {
            "entry": entry,
            "revision": revision,
            "status": {
                "type": "string",
                "enum": _STATUSES,
                "description": "complete, partial, or blocked when something prevents the check",
            },
            "verdict": {
                "type": "string",
                "enum": ["confirmed", "refuted", "inconclusive"],
                "description": "confirmed only from a complete check that examined everything "
                "the entry rests on; refuted only with its refutation",
            },
            "summary": _string("what you checked, how, and what you found", 8000, least=1),
            "coverage": {
                "type": "object",
                "description": "what you examined of what the entry rests on, and what not",
                "properties": {
                    "examined": {
                        **_strings(
                            "each part of what the entry rests on that you examined, such as "
                            "its statement, an assumption, a proof's step or a computed value",
                            20,
                        ),
                        "minItems": 1,
                    },
                    "not_examined": _strings(
                        "each part you did not examine; empty only when you examined all", 20
                    ),
                },
                "required": ["examined"],
                "additionalProperties": False,
            },
            "refutation": _string(
                "only and always when refuted: the step, value or counterexample that "
                "contradicts the entry, as you found it",
                2000,
            ),
            "obstacle": _string("only and always when blocked: what prevents the check", 1000),
            **_LISTS,
        },
        "required": ["entry", "revision", "status", "verdict", "summary", "coverage"],
        "additionalProperties": False,
    }


_REFERENCES: Final[dict[str, dict[str, Any]]] = {
    "artifact": {
        "path": _string("its path, as your calls wrote or published it", 4096, least=1),
        "revision": {
            "type": "integer",
            "minimum": 1,
            "description": "the revision of it your calls recorded or published",
        },
    },
    "file": {"path": _string("its path, as your calls wrote it", 4096, least=1)},
    "entry": {
        "entry": _string(
            "the handle of the entry you created or revised, such as T-3", 128, least=1
        )
    },
    "answer": {"answer": _string("the answer itself", 1000, least=1)},
}
"""The fields that name what a deliverable of each kind produced (PD-45)."""


def _accounted(kind: str, ids: Sequence[str]) -> dict[str, Any]:
    """The form of an item accounting for the brief's deliverables of one kind, ``ids``:
    that kind's fields, and no other kind's."""
    references = _REFERENCES[kind]
    gives = " and ".join(references)
    return {
        "type": "object",
        "description": f"{', '.join(ids)}: {_KINDS[kind]}{' each' if len(ids) > 1 else ''}; "
        f"produced gives its {gives}, partial what exists and what is missing, not_produced "
        "why",
        "properties": {
            "id": {"type": "string", "enum": list(ids)},
            "status": {"type": "string", "enum": ["produced", "partial", "not_produced"]},
            **references,
            "missing": _string("only and always when partial: what is missing", 500),
            "reason": _string("only and always when not_produced: why", 500),
        },
        "required": ["id", "status"],
        "additionalProperties": False,
    }


def task_schema(asked: Sequence[Asked] = ()) -> dict[str, Any]:
    """``submit_result``'s schema for a task: ``TaskResult``'s, with ``deliverables`` when
    its brief lists any (``asked``), one item for each, a ``oneOf`` by their kinds."""
    properties: dict[str, Any] = {
        "status": {
            "type": "string",
            "enum": _STATUSES,
            "description": "complete only when every deliverable of your brief was produced; "
            "blocked when something prevents the task",
        },
        "summary": _string(
            "what you did, how, and what you found or made, naming the files and entries",
            8000,
            least=1,
        ),
        "obstacle": _string("only and always when blocked: what prevents the task", 1000),
        **_LISTS,
        "dismissals": {
            "type": "array",
            "maxItems": 10,
            "description": "each line of work you dropped, and what ran out",
            "items": {
                "type": "object",
                "properties": {
                    "what": _string("the line of work", 1000, least=1),
                    "limit": {"type": "string", "enum": ["time", "tools", "context", "ideas"]},
                },
                "required": ["what", "limit"],
                "additionalProperties": False,
            },
        },
        "obstructions": {
            "type": "array",
            "maxItems": 5,
            "description": "each claim that something cannot be done, with its evidence",
            "items": {
                "type": "object",
                "properties": {
                    "claim": _string("what you claim cannot be done", 2000, least=1),
                    "evidence": _string("why, as you found it", 4000, least=1),
                },
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        },
        "result_files": {
            "type": "array",
            "maxItems": MAX_RESULT_FILES,
            "description": "the paths of the files that are your results, as your calls wrote "
            "or published them; the platform pins each to the revision your calls made",
            "items": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        "lessons": {
            "type": "array",
            "maxItems": MAX_LESSONS,
            "description": "what you learned operating the tools that a later run could use, "
            "each one line, stated as an observation",
            "items": {"type": "string", "minLength": 1, "maxLength": LESSON_CHARS},
        },
    }
    required: list[Any] = ["status", "summary"]
    if asked:
        kinds: dict[str, list[str]] = {}
        for item in asked:
            if item.kind in _REFERENCES:
                kinds.setdefault(item.kind, []).append(item.id)
        branches = [_accounted(kind, ids) for kind, ids in kinds.items()]
        items: dict[str, Any] = branches[0] if len(branches) == 1 else {"oneOf": branches}
        properties["deliverables"] = {
            "type": "array",
            "minItems": len(asked),
            "maxItems": len(asked),
            "description": "one item for each deliverable of your brief, its number as id, "
            "honestly: produced with what you produced, partial with what exists and what is "
            "missing, or not_produced with why, rather than left out",
            "items": items,
        }
        required.append("deliverables")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


DESCRIPTION: Final = (
    "Submit your result to your parent: your final answer does not carry it. The platform "
    "checks it at once against your brief and your calls, and answers with each problem, "
    "by its place in the result, or with what it recorded. Submit again as often as you "
    "need: a submission that checks replaces the one before. Once it is recorded, check "
    "the values it echoes, then end your run with a short final answer."
)
SPEC: Final = ToolSpec(SUBMIT, DESCRIPTION, {"type": "object"})
"""``submit_result`` as the capabilities reserve its name; each run is offered its own
schema (``Outcome.spec``)."""


@dataclass(frozen=True)
class Reading:
    """What the checks make of one JSON text offered as a run's result."""

    result: CheckResult | TaskResult | None = None
    pinned: tuple[dict[str, JsonValue], ...] = ()
    """A task's result files, pinned to the revisions its calls made (RA-38)."""
    notes: tuple[str, ...] = ()
    """The platform's notes on the forms it read (PD-18)."""
    missing: tuple[Asked, ...] = ()
    """The brief's deliverables it leaves out (PD-45)."""
    problems: tuple[tuple[str, str], ...] = ()
    """What else is wrong with it, each by its place in the result."""

    @property
    def valid(self) -> bool:
        return self.result is not None and not self.missing and not self.problems

    @property
    def omits(self) -> bool:
        """Whether it checks but for the deliverables it leaves out."""
        return self.result is not None and bool(self.missing) and not self.problems

    def listed(self) -> list[tuple[str, str]]:
        """Every problem, the deliverables left out first."""
        found = list(self.problems)
        if self.missing:
            named = ", ".join(asked.named for asked in self.missing)
            found.insert(0, ("deliverables", f"your result does not account for {named}"))
        return found


_FIELDS: Final = ("result_files", "deliverables", "status", "entry", "revision")


def _placed(problems: Sequence[str], ids: Sequence[str]) -> list[tuple[str, str]]:
    """The accounting's problems, each by its place: the item of the deliverable it names,
    the field it begins with, or the result."""
    found: list[tuple[str, str]] = []
    for problem in problems:
        named = next((i for i in re.findall(r"\bd\d+\b", problem) if i in ids), None)
        field_name, _, rest = problem.partition(": ")
        if named is not None:
            found.append((f"deliverables[{ids.index(named)}]", problem))
        elif field_name in _FIELDS and rest:
            found.append((field_name, rest))
        else:
            found.append(("result", problem))
    return found


def _given(text: str) -> list[str]:
    """The ``id`` of each item of a result's ``deliverables``, in order, as far as the text
    gives them, for placing problems; an empty string for an item that names none."""
    try:
        loaded: object = json.loads(text)
    except (ValueError, RecursionError):
        return []
    given = (
        cast(dict[str, object], loaded).get("deliverables") if isinstance(loaded, dict) else None
    )
    if not isinstance(given, list):
        return []
    return [
        str(cast(dict[str, object], item).get("id") or "") if isinstance(item, dict) else ""
        for item in cast(list[object], given)
    ]


class Outcome(TurnHook):
    """A delegated run's result, as it submits it with ``submit_result`` or, failing that,
    as its closing answers give it, and whether it read the entry it checks."""

    name = "outcome"

    def __init__(
        self,
        target: Target | None = None,
        *,
        task: bool = False,
        deliverables: Sequence[Asked] = (),
        entry_made: EntryMade | None = None,
    ) -> None:
        """``target`` is a check's entry and revision; ``task`` reads a task's result,
        which accounts for ``deliverables``, its brief's (PD-45); ``entry_made`` checks
        an entry a deliverable names that the run's changes did not revise, by its work
        records; without it, only the entries the run's changes revised count."""
        self.target = target
        self.task = task
        self.deliverables = tuple(deliverables)
        self.entry_made = entry_made
        self.result: CheckResult | TaskResult | None = None
        """The result that stands: the latest that checked, or, without one, the latest
        that checked but for the deliverables it left out."""
        self.valid = False
        """Whether ``result`` checked whole: it is not one that left deliverables out."""
        self.source: str | None = None
        """Where ``result`` came from: ``submit_result``, the run's closing answer
        (``answer``) or its report at a limit (``report``)."""
        self.marked: dict[str, JsonValue] | None = None
        """The platform's status and obstacle for a result that left deliverables
        unaccounted for (PD-45), which the kept result shows over the child's."""
        self.errors: list[str] = []
        """The problems of the latest submissions and answers that did not check."""
        self.notes: tuple[str, ...] = ()
        """The platform's notes on the result that stands: the forms it read (PD-18), and
        where it came from when not through ``submit_result``."""
        self.submissions = 0
        """The run's calls of ``submit_result``."""
        self.nudges = 0
        """The platform's notices to the run that it submitted no result."""
        self.answer: str | None = None
        """The run's latest closing answer, which the platform's result keeps."""
        self.stopped: str | None = None
        """How the run ended, once its loop ended normally (``finishing``)."""
        self.reminded = False
        """Whether the run was told that its last call with tools had come."""
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
        self.published: set[str] = set()
        """The paths the run published, whose content control retains: a child
        environment's hand-back names them by revision instead of copying them (CE-6)."""
        self.pinned: list[dict[str, JsonValue]] = []
        """The result files a task declared, pinned to the revisions its calls made."""
        self.revisions: dict[str, set[int]] = {}
        """Every revision each path got from the run's calls, recorded or published."""
        self.revised: set[str] = set()
        """The entries the run's ledger changes created or revised, by ID and handle."""
        self.work: set[str] = set()
        """The work records the run's ledger changes appended, by ID."""

    def carry(
        self, files: Sequence[Mapping[str, JsonValue] | None], made: Sequence[str] = ()
    ) -> None:
        """What a reopened task's earlier runs recorded (``files``, as their outcomes keep
        them, with their pinned result files) and the entries their changes made, by ID
        and handle (``made``), which its next result may name as its own calls' (PD-46):
        the same task's work, carried over before its next run's calls."""
        for kept in files:
            if kept is None:
                continue
            for key in ("files", "results"):
                listed = kept.get(key)
                for entry in cast(list[object], listed) if isinstance(listed, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    found = cast(dict[str, JsonValue], entry)
                    if isinstance(path := found.get("path"), str):
                        self.recorded.setdefault(path, found)
                        self._revision(path, found)
        self.revised.update(made)

    @property
    def read_proofs(self) -> bool | None:
        """Whether the run's reads of the entry showed each of its proofs, and read the
        target of each whole, at the revision its link pins; ``None`` for a run that
        checks no entry."""
        if self.target is None:
            return None
        return all(p.evidence_id in self.shown and p.read in self.seen for p in self.target.proofs)

    @property
    def spec(self) -> ToolSpec:
        """``submit_result`` as this run is offered it, its schema the brief's."""
        schema = task_schema(self.deliverables) if self.task else check_schema(self.target)
        return ToolSpec(SUBMIT, DESCRIPTION, schema)

    @property
    def form(self) -> dict[str, JsonValue]:
        """The form the brief asks for, as a refused submission and the platform's notices
        show it: the fields a result must give, and each deliverable's produced form."""
        if not self.task:
            found: dict[str, JsonValue] = {
                "required": ["entry", "revision", "status", "verdict", "summary", "coverage"]
            }
            if self.target is not None:
                found |= {"entry": self.target.handle, "revision": self.target.revision}
            return found
        required: list[JsonValue] = ["status", "summary"]
        if not self.deliverables:
            return {"required": required, "deliverables": "none: leave the field out"}
        return {
            "required": [*required, "deliverables"],
            "deliverables": [
                produced_form(asked.id, asked.kind) if asked.kind in PRODUCED else asked.id
                for asked in self.deliverables
            ],
            "otherwise": 'each one partial, with "missing", or not_produced, with "reason"',
        }

    # --- What the run's calls showed and made -------------------------------------------

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
            if not isinstance(entry, dict):
                continue
            changed = cast(dict[str, object], entry)
            self.revised.update(
                str(changed[key]) for key in ("entry_id", "handle") if key in changed
            )
            if changed.get("created") is True:
                found = changed.get("entry_id")
                if isinstance(found, str) and found not in self.created:
                    self.created.append(found)
        work = cast(dict[str, object], result).get("work") if isinstance(result, dict) else None
        self.work.update(
            str(w) for w in (cast(list[object], work) if isinstance(work, list) else [])
        )

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
                    self._revision(path, found)
        snapshot, receipt = record.get("artifact"), record.get("artifact_inventory")
        if isinstance(snapshot, dict) and isinstance(receipt, dict):
            published = cast(dict[str, Any], receipt)
            ref = published.get("revision")
            path = cast(dict[str, Any], snapshot).get("source_path")
            recorded = published.get("state") == "recorded"
            if recorded and isinstance(ref, dict) and isinstance(path, str):
                self.published.add(path)
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
                self._revision(path, self.recorded[path])

    def _revision(self, path: str, found: Mapping[str, object]) -> None:
        revision = found.get("revision")
        if isinstance(revision, int) and found.get("deleted") is not True:
            self.revisions.setdefault(path, set()).add(revision)

    # --- The checks ---------------------------------------------------------------------

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

    def _unaccounted(self, result: TaskResult) -> tuple[list[Asked], list[str]]:
        """The brief's deliverables the result leaves out, and what else is wrong with
        its accounting: one the brief does not have, a reference of another kind or one
        the run's calls did not make, or a status that does not agree (PD-45)."""
        listed = result.deliverables
        if not self.deliverables:
            if listed is not None:
                return [], ["deliverables: the brief lists none; leave the field out"]
            return [], []
        given = {item.id: item for item in listed or ()}
        missing = [asked for asked in self.deliverables if asked.id not in given]
        known = {asked.id for asked in self.deliverables}
        problems = [
            f"deliverables: the brief has no deliverable {item.id}"
            for item in listed or ()
            if item.id not in known
        ]
        for asked in self.deliverables:
            if (item := given.get(asked.id)) is not None:
                problems.extend(self._reference(asked, item))
        produced = all(item.status == "produced" for item in listed or ())
        if not missing and result.status == "complete" and not produced:
            problems.append("status: complete only when every deliverable was produced")
        return missing, problems

    def _reference(self, asked: Asked, item: Accounted) -> list[str]:
        """What is wrong with how ``item`` names what it produced, for ``asked``'s kind;
        an entry is checked later, against the ledger (``_entries``)."""
        fields = {"artifact": ("path", "revision"), "file": ("path",)}
        fields |= {"entry": ("entry",), "answer": ("answer",)}
        allowed = fields.get(asked.kind, ())
        gives = " and ".join(allowed)
        if other := [name for name in item.references if name not in allowed]:
            return [f"{asked.id} is {_KINDS.get(asked.kind)}: it gives {gives}, not {other[0]}"]
        if item.status == "produced" and len(item.references) != len(allowed):
            error = f"{asked.id} is {_KINDS.get(asked.kind)}: produced, it gives {gives}"
            if asked.kind == "artifact" and item.path is not None:
                made = sorted(self.revisions.get(item.path.removeprefix("./"), ()))
                error += (
                    f"; your calls recorded revisions {', '.join(map(str, made[:10]))} of it"
                    if made
                    else "; your calls recorded no revision of it"
                )
            return [error]
        path = item.path.removeprefix("./") if item.path is not None else None
        if path is not None and item.revision is not None:
            if item.revision not in self.revisions.get(path, set()):
                return [
                    f"{asked.id}: your calls recorded or published no revision {item.revision} "
                    f"of {item.path}"
                ]
        elif path is not None:
            found = self.recorded.get(path)
            if found is None or found.get("deleted") is True:
                return [f"{asked.id}: your calls wrote or published no file {item.path}"]
        return []

    def _resolved(self, result: TaskResult) -> tuple[TaskResult, tuple[str, ...]]:
        """The result with each artifact it says it produced by its path alone given the
        one revision the run's calls recorded of that path, which the path then names
        with nothing to choose, and the platform's note; a path they recorded no revision
        of, or several, is left as given, an error (PD-18, PD-45)."""
        kinds = {asked.id: asked.kind for asked in self.deliverables}
        items: list[Accounted] = []
        named: list[str] = []
        for item in result.deliverables or ():
            if (
                kinds.get(item.id) == "artifact"
                and item.status == "produced"
                and item.references == ("path",)
                and item.path is not None
                and len(made := self.revisions.get(item.path.removeprefix("./"), set[int]())) == 1
            ):
                item = item.model_copy(update={"revision": next(iter(made))})
                named.append(item.id)
            items.append(item)
        if not named:
            return result, ()
        note = PATH_NOTE.format(ids=", ".join(named))
        return result.model_copy(update={"deliverables": tuple(items)}), (note,)

    async def _entries(self, result: TaskResult) -> list[str]:
        """The entries the result names that do not check: each must exist and have a
        revision or a work record the run's ledger changes made (PD-45)."""
        problems: list[str] = []
        for item in result.deliverables or ():
            entry = item.entry
            if entry is None or entry in self.revised:
                continue
            made = self.entry_made is not None and await self.entry_made(
                entry, frozenset(self.work)
            )
            if not made:
                problems.append(
                    f"{item.id}: your ledger changes made no revision or work record of {entry}"
                )
        return problems

    async def _leniently(self, answer: str | None) -> tuple[list[Asked], list[str]]:
        """What the accounting finds wrong with an answer the schema refuses, as far as
        its deliverables can be read: each item that validates on its own is checked as
        a valid result's is, and the brief's deliverables that no item names are left
        out; an item that does not validate counts as given and is not checked, and
        ``deliverables`` that is no list is not read (PD-18, PD-45). The answer stays
        invalid: this only lets its problems be named at once."""
        order = [asked.id for asked in self.deliverables]
        try:
            text, _ = _listed(answer, order)
            loaded: object = json.loads(_unfenced(text))
        except (ValueError, RecursionError):
            return [], []
        if not isinstance(loaded, dict):
            return [], []
        whole = cast(dict[str, object], loaded)
        given = whole.get("deliverables")
        if given is not None and not isinstance(given, list):
            return [], []
        items: list[Accounted] = []
        unread: set[str] = set()
        for value in cast(list[object], given or []):
            try:
                items.append(Accounted.model_validate(value))
            except ValidationError:
                named = (
                    cast(dict[str, object], value).get("id") if isinstance(value, dict) else None
                )
                if isinstance(named, str):
                    unread.add(named)
        # A result as far as it could be read, unvalidated, for the checks that read only
        # its status and its deliverables.
        read = TaskResult.model_construct(
            status=whole.get("status"), deliverables=None if given is None else tuple(items)
        )
        read, _ = self._resolved(read)
        missing, problems = self._unaccounted(read)
        problems.extend(await self._entries(read))
        return [asked for asked in missing if asked.id not in unread], problems

    async def _reading(self, text: str) -> Reading:
        """What the checks make of ``text``, a JSON object offered as the result: for a
        task, its schema's, its accounting's, its result files' and its entries' (PD-45,
        RA-38); for a check, its schema's and its brief's entry and revision."""
        if not self.task:
            try:
                return Reading(parse(text, self.target))
            except SchemaError as error:
                return Reading(problems=error.located)
            except ValueError as error:
                return Reading(problems=(("result", str(error)),))
        order = [asked.id for asked in self.deliverables]
        try:
            found, listed = read_task(text, order)
        except ValueError as error:
            if isinstance(error, SchemaError):
                located = list(error.located)
            else:
                located = _placed([str(error)], [])
            missing, problems = await self._leniently(text)
            try:
                ids = _given(_listed(text, order)[0] or "")
            except ValueError:
                ids = []
            return Reading(missing=tuple(missing), problems=(*located, *_placed(problems, ids)))
        found, resolved = self._resolved(found)
        missing, problems = self._unaccounted(found)
        pinned: list[dict[str, JsonValue]] = []
        try:
            pinned = self._pin(found)
        except ValueError as error:
            problems.insert(0, str(error))
        problems.extend(await self._entries(found))
        ids = [item.id for item in found.deliverables or ()]
        return Reading(
            found,
            tuple(pinned),
            (*listed, *resolved),
            tuple(missing),
            tuple(_placed(problems, ids)),
        )

    async def _answered(self, answer: str | None) -> Reading | None:
        """The best reading of the JSON an answer holds (``candidates``): the first that
        checks, else the first that checks but for deliverables it leaves out, else the
        first; ``None`` for an answer that holds none."""
        readings = [await self._reading(text) for text in candidates(answer)]
        found = next((r for r in readings if r.valid), None)
        found = found or next((r for r in readings if r.omits), None)
        return found or (readings[0] if readings else None)

    # --- The result that stands --------------------------------------------------------

    def _stand(self, reading: Reading, source: str) -> None:
        """``reading`` as the result that stands, from ``source``: whole, or blocked by the
        platform for the deliverables it leaves out, the block kept already, so that it
        stands if no turn is left to amend it (PD-45)."""
        result = reading.result
        if result is None:
            return
        self.result, self.valid, self.source = result, reading.valid, source
        self.pinned = list(reading.pinned)
        self.notes = (*reading.notes, *((CAME[source],) if source in CAME else ()))
        self.marked = None
        if isinstance(result, TaskResult) and reading.missing:
            self.marked = _blocked(result, reading.missing)

    def _refused(self, reading: Reading) -> list[tuple[str, str]]:
        """A reading's problems, at most ``MAX_PROBLEMS``, each cut to ``ERROR_CHARS``,
        and kept as the run's latest errors."""
        found = [(at, text[:ERROR_CHARS]) for at, text in reading.listed()[:MAX_PROBLEMS]]
        said = "; ".join(f"{at}: {text}" for at, text in found) or "no result"
        self.errors = [*self.errors, said[:ERROR_CHARS]][-KEPT_ERRORS:]
        return found

    async def submit(self, arguments: str) -> tuple[str, dict[str, Any]]:
        """``submit_result``: the run's result, checked now. One that checks is recorded,
        replacing the one before, and echoed for the run to check; one that does not is
        refused with each of its problems and the brief's form, and replaces nothing, but
        one that checks but for deliverables it leaves out stands while none checks."""
        self.submissions += 1
        reading = await self._reading(arguments)
        if reading.valid:
            self._stand(reading, SUBMIT)
            return dump(self._echo()), {
                "submitted": {"submission": self.submissions, "recorded": True}
            }
        if reading.omits and not self.valid:
            self._stand(reading, SUBMIT)
        found = self._refused(reading)
        said = f"{len(found)} problem{'' if len(found) == 1 else 's'}"
        refused: dict[str, JsonValue] = {
            "kind": "invalid_request",
            "message": f"not recorded, for the {said} below: fix each and submit the whole "
            "result again",
            "problems": [{"at": at, "problem": text} for at, text in found],
            "form": self.form,
        }
        return dump({"error": refused}), {
            "error": "invalid_request",
            "submitted": {
                "submission": self.submissions,
                "recorded": False,
                "problems": len(found),
            },
        }

    def _echo(self) -> dict[str, JsonValue]:
        """What a recorded submission says back: its values, for the run to check."""
        result = self.result
        values: dict[str, JsonValue]
        if isinstance(result, CheckResult):
            coverage = result.coverage
            said = (
                f"verdict {result.verdict}, status {result.status}, "
                f"{len(coverage.examined)} parts examined and {len(coverage.not_examined)} not"
            )
            values = {
                "entry": result.entry,
                "revision": result.revision,
                "status": result.status,
                "verdict": result.verdict,
                "examined": list[JsonValue](coverage.examined),
                "not_examined": list[JsonValue](coverage.not_examined),
            }
        else:
            result = cast(TaskResult, result)
            accounted = result.deliverables or ()
            statuses = [item.status for item in accounted]
            counted = ", ".join(
                f"{statuses.count(s)} {s.replace('_', ' ')}"
                for s in ("produced", "partial", "not_produced")
                if s in statuses
            )
            said = f"status {result.status}"
            if accounted:
                said += f", {len(accounted)} deliverables accounted for ({counted})"
            said += f", {len(self.pinned)} result file{'' if len(self.pinned) == 1 else 's'} pinned"
            values = {"status": result.status}
            if accounted:
                values["deliverables"] = [
                    {
                        k: v
                        for k, v in item.model_dump(mode="json").items()
                        if v is not None and k not in ("missing", "reason")
                    }
                    for item in accounted
                ]
            values["result_files"] = [
                {"path": pinned.get("path"), "revision": pinned.get("revision")}
                for pinned in self.pinned
            ]
        echoed: dict[str, JsonValue] = {
            "recorded": f"{said}; your run can end now",
            "submission": self.submissions,
            "values": values,
        }
        if self.notes:
            echoed["platform_notes"] = list[JsonValue](self.notes)
        echoed["next"] = (
            "Check these values against your work. Submit again to change any; otherwise end "
            "your run with a short final answer."
        )
        return echoed

    # --- The run's closings --------------------------------------------------------------

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        """Before a run's last call with tools at its turn limit, when it submitted no
        result: that the call after it, its report, runs no tool."""
        if point != "turn" or self.reminded or run.turns_left != 1 or self._submitted:
            return ()
        self.reminded = True
        return (Notice(self.name, (), OUTCOME_LAST_CALL.text, {"tool": SUBMIT, "form": self.form}),)

    @property
    def _submitted(self) -> bool:
        return self.valid and self.source == SUBMIT

    async def report_notices(self, run: RunView, limit: str) -> Sequence[Notice]:
        """The report round at a limit, where no tool runs: the run writes, as its answer,
        the JSON object it would have submitted, which is read leniently as its result
        (PD-19); with a result recorded, the notice says so."""
        payload: dict[str, JsonValue] = {
            "limit": limit,
            "recorded": self._submitted,
            "form": self.form,
        }
        return (Notice(self.name, (), OUTCOME_REPORT.text, payload),)

    async def model_returned(self, run: RunView) -> None:
        """A report round's answer at a limit, where no tool runs: its text is the run's
        latest answer, whose JSON, read leniently, is the report's result, as is a call
        of ``submit_result`` in it, which does not run. A hook that stops the run at its
        closing, as a parent's stop does, asks this hook nothing more."""
        if run.halted is None:
            return
        messages = run.context.messages
        last = messages[-1] if messages else None
        if last is None or last.get("role") != "assistant":
            return
        text = last.get("content")
        calls = cast(list[dict[str, Any]], last.get("tool_calls") or [])
        submitted = [
            cast(dict[str, Any], call.get("function") or {})
            for call in calls
            if cast(dict[str, Any], call.get("function") or {}).get("name") == SUBMIT
        ]
        readings: list[Reading] = []
        if submitted:
            arguments = without(str(submitted[-1].get("arguments") or ""))
            readings.append(await self._reading(arguments))
        if isinstance(text, str) and text.strip():
            self.answer = body(text)
            if (found := await self._answered(self.answer)) is not None:
                readings.append(found)
        for reading in sorted(readings, key=lambda r: not r.valid):
            if reading.valid or (reading.omits and not self.valid):
                self._stand(reading, REPORT)
                return

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        """A closing answer: it stands once a result was submitted. Without one, a result
        the answer holds as JSON is taken (a report's at a limit too); otherwise the run
        is told, at most ``NUDGES`` times, that its result goes through ``submit_result``,
        and then the answer stands (PD-18)."""
        self.answer = answer
        limited = run.halted is not None
        if self._submitted and not limited:
            return Closing()
        reading = await self._answered(answer)
        source = REPORT if limited else ANSWER
        if reading is not None and reading.valid:
            self._stand(reading, source)
            return Closing()
        if self.valid:
            return Closing()
        if reading is not None and reading.omits:
            self._stand(reading, source)
        if limited or self.nudges >= NUDGES:
            return Closing()
        self.nudges += 1
        payload: dict[str, JsonValue] = {
            "tool": SUBMIT,
            "notice": self.nudges,
            "of": NUDGES,
            "form": self.form,
        }
        if reading is not None:
            payload["answer_problems"] = [
                {"at": at, "problem": text} for at, text in self._refused(reading)
            ]
        return Closing("continue", (Notice(self.name, (), OUTCOME_SUBMIT.text, payload),))

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        self.stopped = stopped
        return None

    # --- The result the delegation keeps ------------------------------------------------

    def kept(self, stopped: str = "answered") -> dict[str, JsonValue] | None:
        """The result as the delegation keeps it, for a run that stopped as ``stopped``:
        the one that stands, a task's with the platform's mark over the child's status
        when deliverables were left out (PD-45), and its notes, when there are any
        (PD-18); at a limit, ``complete`` made ``partial`` with an obstacle naming the
        limit, both the platform's; without one, the platform's (``NO_SUBMISSION``). A run
        that stopped otherwise, cancelled or interrupted, keeps none."""
        if stopped != "answered" and stopped not in LIMITS:
            return None
        limit = stopped if stopped in LIMITS else None
        if self.result is None:
            return self._platform(limit)
        found: dict[str, JsonValue] = (
            cast(dict[str, JsonValue], self.result.model_dump(mode="json"))
            if isinstance(self.result, CheckResult)
            else self.result.kept()
        )
        marks = dict(self.marked or {})
        notes = list[JsonValue](self.notes)
        if limit is not None:
            marks, noted = _limited(found, marks, limit)
            notes.extend(noted)
        kept = {**found, **marks}
        if notes:
            kept["platform_notes"] = notes
        return kept

    def _platform(self, limit: str | None) -> dict[str, JsonValue]:
        """The platform's result for a run that submitted none, never stronger than the
        records: ``blocked``, with the obstacle that it submitted none, the run's answer as
        its summary, each deliverable not produced for that reason, no result files, and
        the files its calls wrote or published named in a note (PD-45)."""
        obstacle = (
            NO_SUBMISSION
            if limit is None
            else f"the run reached a limit ({limit}) without submitting a result"
        )
        summary, said = self._summary()
        found: dict[str, JsonValue]
        if self.task:
            found = {
                "status": "blocked",
                "summary": summary,
                "obstacle": obstacle,
                "deviations": [],
                "open_questions": [],
                "dismissals": [],
                "obstructions": [],
                "result_files": [],
            }
            marked = ["status", "obstacle", "summary"]
            if self.deliverables:
                found["deliverables"] = [
                    {
                        "id": asked.id,
                        "status": "not_produced",
                        **dict.fromkeys(("path", "revision", "entry", "answer", "missing")),
                        "reason": obstacle,
                    }
                    for asked in self.deliverables
                ]
                marked.append("deliverables")
        else:
            target = self.target
            found = {
                **(
                    {"entry": target.handle, "revision": target.revision}
                    if target is not None
                    else {}
                ),
                "status": "blocked",
                "verdict": "inconclusive",
                "summary": summary,
                "coverage": {"examined": [], "not_examined": []},
                "refutation": None,
                "obstacle": obstacle,
                "deviations": [],
                "open_questions": [],
            }
            marked = ["status", "verdict", "obstacle", "summary", "coverage"]
        return {
            **found,
            "set_by": dict.fromkeys(marked, "platform"),
            "platform_notes": [said, self._written()],
        }

    def _summary(self) -> tuple[str, str]:
        """The platform's result's summary, the run's latest closing answer within a
        summary's bounds, and its note saying so."""
        text = (self.answer or "").strip()
        if not text:
            return "The run gave no closing answer.", (
                "no result was submitted: the platform wrote this result, and the run gave no "
                "closing answer"
            )
        cut = text[:8000]
        if len(cut.encode()) > SUMMARY_BYTES:
            cut = cut.encode()[: SUMMARY_BYTES - 3].decode(errors="ignore")
        if cut != text:
            cut = cut[:-1] + "…"
        try:
            kept = agent_text(cut, "the summary")
        except DocumentError:
            return (
                "The run's closing answer holds characters a summary may not; the delegation "
                "keeps it as its report.",
                "no result was submitted: the platform wrote this result",
            )
        return kept, (
            "no result was submitted: the platform wrote this result, its summary the run's "
            "closing answer as the run wrote it"
        )

    def _written(self) -> str:
        """The platform's note naming the files the run's calls wrote or published, with
        their revisions, so that the parent can look."""
        files = [
            (path, found.get("revision"))
            for path, found in self.recorded.items()
            if found.get("deleted") is not True
        ]
        if not files:
            return "the run's calls wrote or published no file"
        shown: list[str] = []
        used = 0
        for path, revision in files:
            item = f"{path} (revision {revision})" if isinstance(revision, int) else path
            if shown and used + len(item) > NOTE_CHARS:
                break
            shown.append(item[:NOTE_CHARS])
            used += len(item) + 2
        rest = len(files) - len(shown)
        more = f", and {rest} more" if rest else ""
        return f"the run's calls wrote or published {', '.join(shown)}{more}"

    @property
    def format(self) -> str:
        return TASK_FORMAT if self.task else FORMAT

    def summary(self) -> object | None:
        kept = self.kept(self.stopped) if self.stopped is not None else None
        source = self.source if self.result is not None else "platform" if kept else None
        return {
            "format": self.format,
            "result": kept,
            "errors": list(self.errors),
            "read": self.read,
            "read_proofs": self.read_proofs,
            "result_files": list(self.pinned),
            "submissions": self.submissions,
            "nudges": self.nudges,
            "source": source,
        }


def _blocked(result: TaskResult, missing: Sequence[Asked]) -> dict[str, JsonValue]:
    """The platform's mark over a result that leaves deliverables out (PD-45): status
    ``blocked`` and its obstacle naming them, the child's status kept beside them."""
    named = ", ".join(a.named for a in missing)
    obstacle = f"deliverables not accounted for: {named}"
    if len(obstacle.encode()) > OBSTACLE_BYTES:
        # Twelve long titles of wide characters: cut, so that the mark keeps the result
        # within the table's bound.
        obstacle = obstacle.encode()[: OBSTACLE_BYTES - 3].decode(errors="ignore") + "…"
    marked: dict[str, JsonValue] = {
        "status": "blocked",
        "obstacle": obstacle,
        "set_by": {"status": "platform", "obstacle": "platform"},
        "child_status": result.status,
    }
    if result.obstacle is not None:
        marked["child_obstacle"] = result.obstacle
    return marked


def _limited(
    found: Mapping[str, JsonValue], marks: dict[str, JsonValue], limit: str
) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    """The platform's mark over a result of a run stopped at ``limit``: ``complete`` made
    ``partial`` (and a check's ``confirmed``, which only a complete check gives, made
    ``inconclusive``), with an obstacle naming the limit, the child's own beside them; a
    blocked result keeps its obstacle, and a note names the limit."""
    status = marks.get("status", found.get("status"))
    if status == "blocked":
        return marks, [f"the run reached a limit ({limit}) before it ended"]
    mark: dict[str, JsonValue] = {"obstacle": LIMIT_OBSTACLE.format(limit=limit)}
    set_by: dict[str, JsonValue] = {"obstacle": "platform"}
    if status == "complete":
        mark |= {"status": "partial", "child_status": "complete"}
        set_by["status"] = "platform"
    if found.get("verdict") == "confirmed":
        mark |= {"verdict": "inconclusive", "child_verdict": "confirmed"}
        set_by["verdict"] = "platform"
    earlier = marks.get("set_by")
    given = cast(dict[str, JsonValue], earlier) if isinstance(earlier, dict) else {}
    return {**marks, **mark, "set_by": {**given, **set_by}}, []


def by_platform(result: Mapping[str, JsonValue] | None) -> bool:
    """Whether a kept result is the platform's, written for a run that submitted none."""
    marked = result.get("set_by") if result is not None else None
    return isinstance(marked, dict) and marked.get("summary") == "platform"


def result_of(outcome: Outcome, stopped: str, failed: bool) -> dict[str, JsonValue] | None:
    """The result a delegated run keeps (``Outcome.kept``): for a run that ended with its
    answer, or at a limit, where a limit no longer discards a result; ``None`` for one
    cancelled, interrupted or failed."""
    return None if failed else outcome.kept(stopped)
