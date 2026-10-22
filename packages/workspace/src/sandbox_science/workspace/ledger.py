# SPDX-License-Identifier: MIT
"""The project ledger, read-only, for the GUI's ledger and board views (LG-34).

``GET /api/v0/projects/{project}/ledger`` answers a page of entries, oldest first, each
with its handle, type, status and title; ``type`` and ``status`` filter, ``after`` and
``limit`` page. ``GET …/ledger/{entry}`` answers an entry at its current revision, or at
``revision``, with its links and the links other entries hold to it, 200 at a time from
``incoming_after``, with their count, and for a reference how far the project's
sessions read its sources (``read``, from the read register: CM-12, LG-15); ``GET
…/ledger/{entry}/history`` its revisions, newest first, before ``before``. An entry is
named by its ID or its handle. Reads go through the read-only control store.

A session's fold proposals (CM-15): ``GET /api/v0/projects/{project}/sessions/{session}/
proposals`` lists those waiting; ``POST …/proposals/{proposal}/accepted`` accepts one
with the user's authority under an ``operation_id``, and ``POST …/dismissed`` dismisses
one with a ``reason``. Same-origin, the user's only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from aiohttp import web
from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore
from sandbox_science.orchestrator.documents.errors import NOT_FOUND
from sandbox_science.orchestrator.ledger import EntryHistory, EntryPage, EntryView, Ledger
from sandbox_science.orchestrator.ledger.models import (
    MAX_CLEARED,
    EntryCitations,
    FlagCause,
    FlagID,
    GateView,
    WorkPage,
)
from sandbox_science.orchestrator.ledger.proposals import FoldProposals, ProposalView
from sandbox_science.orchestrator.projects import ProjectError

from .context import CONTEXT, identifier, json_body
from .requests import document_answer, encoded_response

PATH: Final = "/api/v0/projects/{project}/ledger"
PROPOSALS: Final = "/api/v0/projects/{project}/sessions/{session}/proposals"
_PROPOSAL: Final = re.compile(r"prop-[0-9a-f]{24}")
_ENTRY: Final = re.compile(r"entry-[0-9a-f]{24}|(?:I|C|R|A|D|T|REF)-[1-9][0-9]{0,8}")
_NUMBER: Final = re.compile(r"[1-9][0-9]{0,8}")
_POSITION: Final = re.compile(r"[1-9][0-9]{0,17}")
_COUNT: Final = re.compile(r"0|[1-9][0-9]{0,8}")
_WORD: Final = re.compile(r"[a-z_]{1,32}")


class LedgerListing(Contract):
    """A page of a project's ledger entries."""

    project_id: Identifier
    observed_at: AwareDatetime
    page: EntryPage


class SourceRead(Contract):
    """How far the project's sessions read one source of a reference (CM-12, LG-15), as
    the read register put its reads together: ``whole``, ``partial`` (with the pages,
    byte ranges or character ranges ``covered``), ``abstract_only``, ``preview_only``, or
    ``unknown`` when the reading tool says nothing of it."""

    kind: str
    source: str
    extent: Literal["whole", "partial", "abstract_only", "preview_only", "unknown"]
    covered: dict[str, JsonValue] | None = None
    versions: str | None = None


class LedgerEntry(Contract):
    """One ledger entry at one revision; a reference's with how far its sources were read."""

    project_id: Identifier
    observed_at: AwareDatetime
    entry: EntryView
    read: list[SourceRead] = Field(default_factory=list[SourceRead])


class LedgerGate(Contract):
    """The gate for one ledger entry as it reads now (LG-17)."""

    project_id: Identifier
    observed_at: AwareDatetime
    gate: GateView


class LedgerCitations(Contract):
    """The consistency reviewer's checks of the quotes a ledger entry's revision holds."""

    project_id: Identifier
    observed_at: AwareDatetime
    citations: EntryCitations


class LedgerWork(Contract):
    """A page of the work records on a ledger entry, newest first (LG-6)."""

    project_id: Identifier
    observed_at: AwareDatetime
    work: WorkPage


class FoldProposalList(Contract):
    """A session's fold proposals waiting (CM-15), oldest first: each written by a model
    from folded material, which the user accepts or dismisses."""

    proposals: list[ProposalView]


class LedgerHistory(Contract):
    """A page of a ledger entry's revisions."""

    project_id: Identifier
    observed_at: AwareDatetime
    history: EntryHistory


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listing)
    app.router.add_get(PATH + "/{entry}", entry)
    app.router.add_get(PATH + "/{entry}/history", history)
    app.router.add_get(PATH + "/{entry}/gate", gate_read)
    app.router.add_get(PATH + "/{entry}/work", work_read)
    app.router.add_get(PATH + "/{entry}/citations", citations)
    app.router.add_post(PATH + "/{entry}/checks", check)
    app.router.add_post(PATH + "/{entry}/assessments", assess)
    app.router.add_post(PATH + "/{entry}/flags", clear)
    app.router.add_post(PATH + "/{entry}/flags/reopened", reopen)
    app.router.add_post(PATH + "/{entry}/dominates/dropped", drop)
    app.router.add_get(PROPOSALS, proposals_read)
    app.router.add_post(PROPOSALS + "/{proposal}/accepted", proposal_accepted)
    app.router.add_post(PROPOSALS + "/{proposal}/dismissed", proposal_dismissed)


def query_of(request: web.Request, allowed: set[str]) -> dict[str, str]:
    query = dict(request.query)
    if set(query) - allowed or len(query) != len(request.query) or request.can_read_body:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, f"the ledger read takes only {', '.join(sorted(allowed))}"
        )
    return query


def matched(pattern: re.Pattern[str], value: str | None, what: str) -> str | None:
    if value is not None and pattern.fullmatch(value) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {what}")
    return value


def _entry(request: web.Request) -> str:
    value = matched(_ENTRY, request.match_info["entry"], "ledger entry")
    assert value is not None  # noqa: S101 - a path part is always there
    return value


async def read_answer(
    request: web.Request, project: str, read: Callable[[Ledger], str]
) -> web.Response:
    context = request.app[CONTEXT]
    return await encoded_response(
        request,
        lambda: read(Ledger(context.documents)),
        project=project,
        timeout=context.timeouts.call,
    )


async def listing(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    query = query_of(request, {"type", "status", "after", "limit"})
    kind = matched(_WORD, query.get("type"), "entry type")
    status = matched(_WORD, query.get("status"), "status")
    after = matched(_ENTRY, query.get("after"), "cursor")
    limit = int(matched(_NUMBER, query.get("limit", "50"), "limit") or 50)

    def read(ledger: Ledger) -> str:
        page = ledger.entries(project, kind=kind, status=status, after=after, limit=limit)
        return LedgerListing(
            project_id=project, observed_at=datetime.now(UTC), page=page
        ).model_dump_json()

    return await read_answer(request, project, read)


async def entry(request: web.Request) -> web.Response:
    project, reference = identifier(request, "project"), _entry(request)
    query = query_of(request, {"revision", "incoming_after"})
    raw = matched(_NUMBER, query.get("revision"), "revision")
    after = int(matched(_COUNT, query.get("incoming_after", "0"), "incoming_after") or 0)

    def read(ledger: Ledger) -> str:
        found = ledger.entry(
            project, reference, None if raw is None else int(raw), incoming_after=after
        )
        return LedgerEntry(
            project_id=project,
            observed_at=datetime.now(UTC),
            entry=found,
            read=[SourceRead.model_validate(read) for read in ledger.reads(project, found)],
        ).model_dump_json()

    return await read_answer(request, project, read)


async def history(request: web.Request) -> web.Response:
    project, reference = identifier(request, "project"), _entry(request)
    raw = matched(_NUMBER, query_of(request, {"before"}).get("before"), "revision")

    def read(ledger: Ledger) -> str:
        found = ledger.history(project, reference, before=None if raw is None else int(raw))
        return LedgerHistory(
            project_id=project, observed_at=datetime.now(UTC), history=found
        ).model_dump_json()

    return await read_answer(request, project, read)


async def gate_read(request: web.Request) -> web.Response:
    """GET …/ledger/{entry}/gate: each clause of the gate for the entry as it reads now,
    and the decisions that affect it; a read, which changes nothing."""
    project, reference = identifier(request, "project"), _entry(request)
    query_of(request, set())

    def read(ledger: Ledger) -> str:
        return LedgerGate(
            project_id=project, observed_at=datetime.now(UTC), gate=ledger.gate(project, reference)
        ).model_dump_json()

    return await read_answer(request, project, read)


async def citations(request: web.Request) -> web.Response:
    """GET …/ledger/{entry}/citations: each quote of the entry's revision (``?revision=``,
    the current one by default) with the consistency reviewer's checks of it, newest
    first, as many as 20 with their count, each saying whether it checked that revision's
    material (CR-4); a read, which changes nothing."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = matched(_NUMBER, query_of(request, {"revision"}).get("revision"), "revision")

    def read(ledger: Ledger) -> str:
        found = ledger.citations(project, reference, None if raw is None else int(raw))
        return LedgerCitations(
            project_id=project, observed_at=datetime.now(UTC), citations=found
        ).model_dump_json()

    return await read_answer(request, project, read)


async def work_read(request: web.Request) -> web.Response:
    """GET …/ledger/{entry}/work: the work records on an issue, claim or result, newest
    first, as many as ``limit`` (1 to 100, 50 by default) before ``before`` (a record's
    position), with ``next_before``; a read, which changes nothing."""
    project, reference = identifier(request, "project"), _entry(request)
    query = query_of(request, {"before", "limit"})
    before = matched(_POSITION, query.get("before"), "before")
    limit = int(matched(_NUMBER, query.get("limit", "50"), "limit") or 50)
    if limit > 100:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "limit is 1 to 100")

    def read(ledger: Ledger) -> str:
        found = ledger.work(
            project, reference, before=None if before is None else int(before), limit=limit
        )
        return LedgerWork(
            project_id=project, observed_at=datetime.now(UTC), work=found
        ).model_dump_json()

    return await read_answer(request, project, read)


class CheckRequest(Contract):
    """The user's record that they checked an entry at the revision they read (LG-17)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: Annotated[int, Field(ge=1, le=999999999)]
    note: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


async def check(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/checks: the user checked the entry at ``revision``, the one
    the page read; a later revision is a conflict. A reference's check vouches for the
    PDF the revision names (LG-16). Same-origin, the user's only."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = await json_body(request, "ledger check JSON")
    try:
        value = CheckRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid ledger check") from None

    def checked(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).check(
            project,
            reference,
            value.revision,
            USER,
            operation_id=value.operation_id,
            note=value.note,
        )
        return result.model_dump(mode="json")

    return await document_answer(request, checked, access="write")


class AssessRequest(Contract):
    """The user's assessment of an obstruction claimed on an issue, at the revision read
    (LG-7): the claim's work record, the verdict, and what the user read, as a note."""

    model_config = ConfigDict(extra="forbid", strict=True)

    claim: Annotated[str, Field(pattern=r"^work-[0-9a-f]{24}$")]
    verdict: Literal["accepted", "rejected", "inconclusive"]
    revision: Annotated[int, Field(ge=1, le=999999999)]
    note: Annotated[str, Field(min_length=1, max_length=8192)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


async def assess(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/assessments: the user accepts, rejects or cannot decide an
    obstruction claimed on the issue, as it reads at ``revision``, the one the page read; a
    later revision is a conflict (LG-7). Same-origin, the user's only."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = await json_body(request, "ledger assessment JSON")
    try:
        value = AssessRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid ledger assessment") from None

    def assessed(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).assess(
            project,
            reference,
            value.claim,
            value.verdict,
            value.revision,
            USER,
            note=value.note,
            operation_id=value.operation_id,
        )
        return result.model_dump(mode="json")

    return await document_answer(request, assessed, access="write")


class DropRequest(Contract):
    """The user's dropping of a ``dominates`` link the entry holds, at the revision read,
    with the reason (LG-22)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target: Annotated[str, Field(min_length=1, max_length=128)]
    revision: Annotated[int, Field(ge=1, le=999999999)]
    reason: Annotated[str, Field(min_length=1, max_length=4096)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


async def drop(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/dominates/dropped: the user drops the entry's ``dominates``
    link to ``target``, with the reason, at ``revision``, the one the page read; a later
    revision is a conflict. Same-origin, the user's only."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = await json_body(request, "dominates link JSON")
    try:
        value = DropRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid dropping of a link") from None

    def dropped(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).drop_dominates(
            project,
            reference,
            value.target,
            value.revision,
            USER,
            reason=value.reason,
            operation_id=value.operation_id,
        )
        return result.model_dump(mode="json")

    return await document_answer(request, dropped, access="write")


class ClearRequest(Contract):
    """The user's clearing of open flags on an entry, with the reason (LG-29): those
    ``flags`` names, or else all its open flags, or those of ``cause``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    flags: list[FlagID] | None = Field(default=None, min_length=1, max_length=MAX_CLEARED)
    cause: FlagCause | None = None
    reason: Annotated[str, Field(min_length=1, max_length=4096)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class ReopenRequest(Contract):
    """The user's reopening of cleared flags on an entry, with the reason (LG-29)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    flags: list[FlagID] = Field(min_length=1, max_length=64)
    reason: Annotated[str, Field(min_length=1, max_length=4096)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


async def clear(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/flags: the user clears open flags on the entry, with the
    reason: it still holds, or the change does not affect it. Same-origin, the user's
    only."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = await json_body(request, "ledger flags JSON")
    try:
        value = ClearRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid flag clearing") from None
    if value.flags is not None and value.cause is not None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "name the flags or a cause, not both")

    def cleared(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).clear_flags(
            project,
            reference,
            value.flags,
            USER,
            reason=value.reason,
            operation_id=value.operation_id,
            cause=value.cause,
        )
        return result.model_dump(mode="json")

    return await document_answer(request, cleared, access="write")


async def reopen(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/flags/reopened: the user opens cleared flags on the entry
    again, with the reason, each as a new flag. Same-origin, the user's only."""
    project, reference = identifier(request, "project"), _entry(request)
    raw = await json_body(request, "ledger flags JSON")
    try:
        value = ReopenRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid flag reopening") from None

    def reopened(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).reopen_flags(
            project,
            reference,
            value.flags,
            USER,
            reason=value.reason,
            operation_id=value.operation_id,
        )
        return result.model_dump(mode="json")

    return await document_answer(request, reopened, access="write")


async def proposals_read(request: web.Request) -> web.Response:
    """GET …/sessions/{session}/proposals: the session's fold proposals waiting."""
    project, session = identifier(request, "project"), identifier(request, "session")
    query_of(request, set())

    def read(ledger: Ledger) -> str:
        waiting = FoldProposals(ledger).waiting(project, session)
        return FoldProposalList(proposals=waiting).model_dump_json()

    return await read_answer(request, project, read)


class AcceptRequest(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class DismissRequest(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]


async def proposal_accepted(request: web.Request) -> web.Response:
    """POST …/proposals/{proposal}/accepted: the user accepts a fold's proposal, which the
    ledger records with the user's authority; again, its first result."""
    project, proposal = identifier(request, "project"), _proposal(request)
    session = identifier(request, "session")
    raw = await json_body(request, "proposal JSON")
    try:
        value = AcceptRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid acceptance") from None

    def accepted(store: DocumentStore) -> dict[str, object]:
        proposals = _of_session(store, project, session, proposal)
        return proposals.accept(project, proposal, value.operation_id, USER)

    return await document_answer(request, accepted, access="write")


async def proposal_dismissed(request: web.Request) -> web.Response:
    """POST …/proposals/{proposal}/dismissed: the user dismisses a fold's proposal."""
    project, proposal = identifier(request, "project"), _proposal(request)
    session = identifier(request, "session")
    raw = await json_body(request, "proposal JSON")
    try:
        value = DismissRequest.model_validate(raw)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid dismissal") from None

    def dismissed(store: DocumentStore) -> dict[str, object]:
        proposals = _of_session(store, project, session, proposal)
        return proposals.dismiss(project, proposal, value.reason, USER).model_dump(mode="json")

    return await document_answer(request, dismissed, access="write")


def _of_session(store: DocumentStore, project: str, session: str, proposal: str) -> FoldProposals:
    """The store's proposals, once ``proposal`` is found to be ``session``'s."""
    proposals = FoldProposals(Ledger(store))
    if proposals.get(project, proposal).session != session:
        raise DocumentError(NOT_FOUND, f"no fold proposal {proposal} in this session")
    return proposals


def _proposal(request: web.Request) -> str:
    value = matched(_PROPOSAL, request.match_info["proposal"], "fold proposal")
    assert value is not None  # noqa: S101 - a path part is always there
    return value
