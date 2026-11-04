# SPDX-License-Identifier: MIT
"""Explicit ledger rows, prepared mutations and transaction-scoped inputs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, get_args

from pydantic import BaseModel, ValidationError

from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author

from .models import (
    PREFIXES,
    EntryChange,
    EntryLink,
    EvidenceLink,
    Mutation,
)

Json = dict[str, Any]


TEXTS: Final = TextRevisions("statement")


@dataclass
class Row:
    entry_id: str
    project: str
    type: str
    number: int
    origin: str | None
    head: int

    @property
    def handle(self) -> str:
        return f"{PREFIXES[self.type]}-{self.number}"


@dataclass
class Planned:
    """One change of a mutation, resolved: the entry it makes or revises, and what the
    new revision holds."""

    change: EntryChange
    row: Row
    created: bool
    title: str = ""
    statement: str | None = None
    status: str = ""
    status_before: str | None = None
    fields: Json = field(default_factory=dict[str, Any])
    fields_before: Json = field(default_factory=dict[str, Any])
    """The fields of the revision before; none for an entry the change creates."""
    links: list[EntryLink] | None = None
    """The new revision's links; None keeps the revision before's."""
    previous: list[EntryLink] = field(default_factory=list[EntryLink])
    """The links of the revision before."""
    evidence: list[EvidenceLink] | None = None
    """The new revision's evidence links; None keeps the revision before's."""
    changed: list[str] = field(default_factory=list[str])
    basis: str | None = None
    """Where the new revision comes from (CM-9)."""
    anchors: list[Json] = field(default_factory=list[Json])
    pins: list[Json] | None = None
    """The statement's pins, when a ``repin`` moved them; otherwise they are made when
    the revision is written (figures spec, FG-7, FG-8)."""


def shown(p: Planned) -> str:
    """How a refusal names ``p``'s entry: its handle, and for one the change creates under
    a key, the key the change names it by, which the model knows it by."""
    return f"{p.row.handle} (new:{p.change.key})" if p.created and p.change.key else p.row.handle


def refused(message: str) -> DocumentError:
    return DocumentError(POLICY_DENIED, f"not_permitted_for_role: {message}")


def encoded(fields: Json) -> str:
    """Fields as the table keeps them: UTF-8 as is, no NaN or infinity."""
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def links_added(p: Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the change's new revision holds that the one before did not."""
    before = {(link.target, link.target_id) for link in p.previous if link.kind == kind}
    return [
        link
        for link in p.links or ()
        if link.kind == kind and (link.target, link.target_id) not in before
    ]


def links_dropped(p: Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the revision before held that the change's new one does not."""
    if p.links is None:
        return []
    after = {(link.target, link.target_id) for link in p.links if link.kind == kind}
    return [
        link
        for link in p.previous
        if link.kind == kind and (link.target, link.target_id) not in after
    ]


def anchors_json(anchors: list[Json]) -> str:
    return json.dumps(anchors, sort_keys=True, ensure_ascii=False)


SHOWN: Final = 4
"""The most problems one refusal lists; it counts the rest."""

_FORMS: Final[dict[str, str]] = {
    "operation_id": "letters, digits and . _ : -, at most 128, from a letter or digit",
    "assessment": "a work record's ID, work- and 24 hexadecimal digits",
    "message": "an archive message's ID, msg- and 16 hexadecimal digits, from its header",
    "memory": "a memory entry at a revision, such as mem-3@2",
    "flags_cleared": "a flag's ID, flag- and 24 hexadecimal digits, from ledger_read",
    "sha256": "64 lowercase hexadecimal digits",
}
"""The form of a field whose pattern refused a value, by the field's name."""

_FIELD_NAMES: Final = frozenset(
    {
        "kind",
        "caveats",
        "reopen_condition",
        "session",
        "category",
        "conditions",
        "measures",
        "identifiers",
        "authors",
        "published",
        "version",
        "retrieved",
        "pdf",
        "excluded_scope",
        "assessment",
    }
)
"""The names of the types' own fields, which a change gives under ``fields``."""

_MISPLACED: Final[dict[tuple[str, str], str]] = {
    ("EntryChange", "work"): (
        "move it to the call's own work list, beside changes, each record naming its "
        'entry: "work": [{"entry": "I-1", "tried": "...", "ending": "limit", "limit": "tools"}]'
    ),
    ("EntryChange", "read"): (
        "leave it out: the ledger records how far a session read a reference's sources "
        "itself, from what the PDF and e-print tools showed"
    ),
    ("EntryChange", "reason"): "give it once, as the call's reason",
    ("EntryChange", "operation_id"): "give it once, as the call's operation_id",
    ("EntryChange", "handle"): "name the entry a revision is of with entry",
    ("EntryChange", "revision"): "give the revision you read as expected",
    ("Mutation", "entry"): "give each entry's change in changes",
}
"""What to do instead with a key given where it does not belong, by the model there."""


def _model(annotation: object) -> type[BaseModel] | None:
    """The model a field's annotation holds, alone, in a list or a dict, or as an option."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _model(arg)
        if found is not None:
            return found
    return None


def _at(model: type[BaseModel] | None, loc: tuple[int | str, ...]) -> type[BaseModel] | None:
    """The model at ``loc`` within ``model``; list positions and dict keys pass through."""
    current = model
    for part in loc:
        if current is None:
            return None
        if isinstance(part, int):
            continue
        found = current.model_fields.get(part)
        if found is None:
            return None
        current = _model(found.annotation)
    return current


def _problem(item: Any, model: type[BaseModel] | None, at: str) -> str:
    """One validation problem: where it is (after ``at``), what is wrong, and what to give
    instead."""
    loc = tuple(item["loc"])
    where = at + ".".join(str(part) for part in loc) or "the arguments"
    if len(where) > 120:
        where = f"{'.'.join(str(part) for part in loc[:4])}.….{loc[-1]}"[:120]
    kind, message = item["type"], str(item["msg"])
    name = next((part for part in reversed(loc) if isinstance(part, str)), "")
    given = item.get("input")
    if kind == "value_error":
        return f"{where}: {message.removeprefix('Value error, ')}"
    if kind == "extra_forbidden":
        holder = _at(model, loc[:-1])
        hint = _MISPLACED.get((holder.__name__ if holder is not None else "", name))
        if hint is None and holder is not None and holder.__name__ == "EntryChange":
            hint = "give it in fields" if name in _FIELD_NAMES else None
        found = f"{where}: {message}: {hint or f'leave {name} out'}"
        if holder is not None:
            found += f"; its place takes {', '.join(holder.model_fields)}"
        return found
    if kind == "missing":
        return f"{where}: {message}: give {name}"
    if kind == "string_too_short":
        holder = _at(model, loc[:-1])
        found = holder.model_fields.get(name) if holder is not None else None
        optional = found is not None and not found.is_required()
        return f"{where}: {message}: give the text" + (f", or leave {name} out" if optional else "")
    if kind == "string_too_long":
        size = f" (it has {len(given)})" if isinstance(given, str) else ""
        split = "; split a longer caveat into several" if "caveats" in loc else ""
        return f"{where}: {message}{size}: shorten it{split}"
    if kind == "literal_error":
        return f"{where}: {message}" + (f" (got {given!r})" if isinstance(given, str) else "")
    if kind == "string_pattern_mismatch":
        form = _FORMS.get(name) or _FORMS.get(next((str(p) for p in loc if str(p) in _FORMS), ""))
        return f"{where}: {f'give {form}' if form else message}" + (
            f" (got {given[:40]!r})" if isinstance(given, str) else ""
        )
    if isinstance(given, str) and kind.endswith(("_type", "_parsing")):
        return f"{where}: {message} (got the text {given[:40]!r})"
    return f"{where}: {message}"


def validation_error(
    error: ValidationError, model: type[BaseModel] | None = None, at: str = ""
) -> str:
    """The problems pydantic found, as many as ``SHOWN``, each at its path, after ``at``,
    with what to give instead: a key that does not belong names the keys its place takes
    (``model`` is the one validated, which says them), and where it belongs if it is
    misplaced."""
    found = error.errors()
    shown = "; ".join(clip(_problem(item, model, at), 400) for item in found[:SHOWN])
    return shown + (f"; and {len(found) - SHOWN} more" if len(found) > SHOWN else "")


class Problems:
    """The invalid requests a change's independent checks find, reported in one refusal,
    so that one retry can fix them all. Any other refusal stops the change at once."""

    def __init__(self) -> None:
        self.found: list[DocumentError] = []

    @contextmanager
    def check(self) -> Generator[None]:
        try:
            yield
        except DocumentError as error:
            if error.kind != INVALID_REQUEST:
                raise
            self.found.append(error)

    def stop(self) -> None:
        """Refuse with what was found, if anything: one problem as it is, several
        numbered, as many as ``SHOWN``, each once, the first one's details kept."""
        if not self.found:
            return
        # One problem found twice, as by two links of a kind, is said once.
        found = list({error.message: error for error in self.found}.values())
        if len(found) == 1:
            raise found[0]
        shown = " ".join(f"({n}) {e.message}" for n, e in enumerate(found[:SHOWN], 1))
        more = len(found) - SHOWN
        raise DocumentError(
            INVALID_REQUEST,
            f"{len(found)} problems, each to fix: {shown}"
            + (f" ({more} more)" if more > 0 else ""),
            found[0].details,
        )


@dataclass(frozen=True)
class MutationInput:
    """Inputs bound to the facade's active transaction; never a transaction owner."""

    db: sqlite3.Connection
    project: str
    mutation: Mutation
    author: Author
    role: str
    pinned: dict[tuple[str, int, str, str | None], str]
    provenance: Json | None


@dataclass(frozen=True)
class PreparedMutation:
    """Resolved revisions and new-entry keys, ready for cross-entry rules."""

    inputs: MutationInput
    entries: list[Planned]
    keys: dict[str, Row]


@dataclass(frozen=True)
class EvaluatedMutation:
    """A prepared mutation with the entries that passed the decision gate."""

    prepared: PreparedMutation
    gated: frozenset[str]
