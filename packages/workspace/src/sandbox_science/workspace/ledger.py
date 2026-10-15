# SPDX-License-Identifier: MIT
"""The project ledger, read-only, for the GUI's ledger and board views (LG-34).

``GET /api/v0/projects/{project}/ledger`` answers a page of entries, oldest first, each
with its handle, type, status and title; ``type`` and ``status`` filter, ``after`` and
``limit`` page. ``GET …/ledger/{entry}`` answers an entry at its current revision, or at
``revision``, with its links and the links other entries hold to it, 200 at a time from
``incoming_after``, with their count; ``GET
…/ledger/{entry}/history`` its revisions, newest first, before ``before``. An entry is
named by its ID or its handle. Reads go through the read-only control store.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Final

from aiohttp import web
from pydantic import AwareDatetime, ConfigDict, Field, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.ledger import EntryHistory, EntryPage, EntryView, Ledger
from sandbox_science.orchestrator.projects import ProjectError

from . import documents
from .context import CONTEXT, identifier, json_answer, json_body

PATH: Final = "/api/v0/projects/{project}/ledger"
_ENTRY: Final = re.compile(r"entry-[0-9a-f]{24}|(?:I|C|R|A|D|T|REF)-[1-9][0-9]{0,8}")
_NUMBER: Final = re.compile(r"[1-9][0-9]{0,8}")
_COUNT: Final = re.compile(r"0|[1-9][0-9]{0,8}")
_WORD: Final = re.compile(r"[a-z_]{1,32}")


class LedgerListing(Contract):
    """A page of a project's ledger entries."""

    project_id: Identifier
    observed_at: AwareDatetime
    page: EntryPage


class LedgerEntry(Contract):
    """One ledger entry at one revision."""

    project_id: Identifier
    observed_at: AwareDatetime
    entry: EntryView


class LedgerHistory(Contract):
    """A page of a ledger entry's revisions."""

    project_id: Identifier
    observed_at: AwareDatetime
    history: EntryHistory


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listing)
    app.router.add_get(PATH + "/{entry}", entry)
    app.router.add_get(PATH + "/{entry}/history", history)
    app.router.add_post(PATH + "/{entry}/checks", check)
    app.router.add_post(PATH + "/{entry}/flags", clear)


def _query(request: web.Request, allowed: set[str]) -> dict[str, str]:
    query = dict(request.query)
    if set(query) - allowed or len(query) != len(request.query) or request.can_read_body:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, f"the ledger read takes only {', '.join(sorted(allowed))}"
        )
    return query


def _match(pattern: re.Pattern[str], value: str | None, what: str) -> str | None:
    if value is not None and pattern.fullmatch(value) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {what}")
    return value


def _entry(request: web.Request) -> str:
    value = _match(_ENTRY, request.match_info["entry"], "ledger entry")
    assert value is not None  # noqa: S101 - a path part is always there
    return value


async def _answer(
    request: web.Request, project: str, read: Callable[[Ledger], str]
) -> web.Response:
    context = request.app[CONTEXT]

    def view() -> str:
        context.projects.get(project)  # an unknown project is not found
        return read(Ledger(context.documents))

    async with asyncio.timeout(context.timeouts.call):
        return json_answer(await asyncio.to_thread(view))


async def listing(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    query = _query(request, {"type", "status", "after", "limit"})
    kind = _match(_WORD, query.get("type"), "entry type")
    status = _match(_WORD, query.get("status"), "status")
    after = _match(_ENTRY, query.get("after"), "cursor")
    limit = int(_match(_NUMBER, query.get("limit", "50"), "limit") or 50)

    def read(ledger: Ledger) -> str:
        page = ledger.entries(project, kind=kind, status=status, after=after, limit=limit)
        return LedgerListing(
            project_id=project, observed_at=datetime.now(UTC), page=page
        ).model_dump_json()

    return await _answer(request, project, read)


async def entry(request: web.Request) -> web.Response:
    project, reference = identifier(request, "project"), _entry(request)
    query = _query(request, {"revision", "incoming_after"})
    raw = _match(_NUMBER, query.get("revision"), "revision")
    after = int(_match(_COUNT, query.get("incoming_after", "0"), "incoming_after") or 0)

    def read(ledger: Ledger) -> str:
        found = ledger.entry(
            project, reference, None if raw is None else int(raw), incoming_after=after
        )
        return LedgerEntry(
            project_id=project, observed_at=datetime.now(UTC), entry=found
        ).model_dump_json()

    return await _answer(request, project, read)


async def history(request: web.Request) -> web.Response:
    project, reference = identifier(request, "project"), _entry(request)
    raw = _match(_NUMBER, _query(request, {"before"}).get("before"), "revision")

    def read(ledger: Ledger) -> str:
        found = ledger.history(project, reference, before=None if raw is None else int(raw))
        return LedgerHistory(
            project_id=project, observed_at=datetime.now(UTC), history=found
        ).model_dump_json()

    return await _answer(request, project, read)


class CheckRequest(Contract):
    """The user's record that they checked an entry at the revision they read (LG-17)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: Annotated[int, Field(ge=1, le=999999999)]
    note: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


async def check(request: web.Request) -> web.Response:
    """POST …/ledger/{entry}/checks: the user checked the entry at ``revision``, the one
    the page read; a later revision is a conflict. Same-origin, the user's only."""
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

    return await documents.answer(request, checked, writer=True)


class ClearRequest(Contract):
    """The user's clearing of open flags on an entry, with the reason (LG-29)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    flags: list[Annotated[str, Field(pattern=r"^flag-[0-9a-f]{24}$")]] = Field(
        min_length=1, max_length=64
    )
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

    def cleared(store: DocumentStore) -> dict[str, object]:
        result = Ledger(store).clear_flags(
            project,
            reference,
            value.flags,
            USER,
            reason=value.reason,
            operation_id=value.operation_id,
        )
        return result.model_dump(mode="json")

    return await documents.answer(request, cleared, writer=True)
