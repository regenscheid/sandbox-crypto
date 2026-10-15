# SPDX-License-Identifier: MIT
"""The project's frontier for the GUI (docs/specs/ledger.md, LG-18 to LG-24, LG-34).

``GET /api/v0/projects/{project}/frontier`` answers a page of the project's frontier
categories, in the order they were defined (``after``, ``limit``); ``GET
…/frontier/{category}`` its frontier: the best known results under each set of
conditions, with those dominated, not compared and the candidates (LG-20 to LG-23), as
many as ``limit`` groups from ``groups_after`` on, and the first ``SHOWN_RESULTS`` of
each kind of result in a group, with how many more;
``GET …/frontier/{category}/definition`` one category, at its current revision or
``revision``; ``GET …/frontier/{category}/history`` its changes to the best known, newest
first (``before``, ``limit``), and ``GET …/frontier/{category}/past?at=`` its best known
results as they were at a time (LG-24). ``POST …/frontier`` is the user's definition
of a category, or its revision at the revision read (same-origin, the user's only).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from aiohttp import web
from pydantic import AwareDatetime

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.models import (
    Category,
    CategoryPage,
    FrontierGroup,
    FrontierHistory,
    FrontierPast,
    FrontierView,
)
from sandbox_science.orchestrator.projects import ProjectError

from . import documents
from .context import identifier, json_body
from .ledger import matched, query_of, read_answer

PATH: Final = "/api/v0/projects/{project}/frontier"
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_NUMBER: Final = re.compile(r"[1-9][0-9]{0,8}")
_LIMIT: Final = re.compile(r"[1-9][0-9]?|100")
_COUNT: Final = re.compile(r"0|[1-9][0-9]{0,8}")

SHOWN_RESULTS: Final = 100
"""The most results of one kind in a group, and unplaced, an answer lists."""


class FrontierCategories(Contract):
    """A page of a project's frontier categories."""

    project_id: Identifier
    observed_at: AwareDatetime
    page: CategoryPage


class FrontierAnswer(Contract):
    """A category's frontier as its results read now, a page of its groups."""

    project_id: Identifier
    observed_at: AwareDatetime
    frontier: FrontierView
    next_groups_after: int | None
    """Where the next page of groups begins; none on the last."""


class FrontierHistoryAnswer(Contract):
    """A category's frontier changes, newest first."""

    project_id: Identifier
    observed_at: AwareDatetime
    history: FrontierHistory


class FrontierPastAnswer(Contract):
    """A category's best known results as they were at a past time."""

    project_id: Identifier
    observed_at: AwareDatetime
    past: FrontierPast


class FrontierCategory(Contract):
    """One frontier category at one revision."""

    project_id: Identifier
    observed_at: AwareDatetime
    category: Category


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, categories)
    app.router.add_post(PATH, define)
    app.router.add_get(PATH + "/{category}", frontier)
    app.router.add_get(PATH + "/{category}/definition", category)
    app.router.add_get(PATH + "/{category}/history", changes)
    app.router.add_get(PATH + "/{category}/past", then)


async def categories(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    query = query_of(request, {"after", "limit"})
    after = matched(_NAME, query.get("after"), "cursor")
    limit = int(matched(_LIMIT, query.get("limit", "50"), "limit") or 50)

    def read(ledger: Ledger) -> str:
        page = ledger.categories(project, after=after, limit=limit)
        return FrontierCategories(
            project_id=project, observed_at=datetime.now(UTC), page=page
        ).model_dump_json()

    return await read_answer(request, project, read)


async def frontier(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    name = matched(_NAME, request.match_info["category"], "frontier category")
    assert name is not None  # noqa: S101 - a path part is always there
    query = query_of(request, {"groups_after", "limit", "candidates_before"})
    start = int(matched(_COUNT, query.get("groups_after"), "groups_after") or 0)
    older = matched(_NUMBER, query.get("candidates_before"), "candidates_before")
    limit = int(matched(_LIMIT, query.get("limit", "20"), "limit") or 20)

    def read(ledger: Ledger) -> str:
        view = ledger.frontier(
            project, name, candidates_before=None if older is None else int(older)
        )
        end = start + limit
        return FrontierAnswer(
            project_id=project,
            observed_at=datetime.now(UTC),
            frontier=view.model_copy(
                update={
                    "groups": [_shown(group) for group in view.groups[start:end]],
                    "unplaced": view.unplaced[:SHOWN_RESULTS],
                    "unplaced_more": max(0, len(view.unplaced) - SHOWN_RESULTS),
                }
            ),
            next_groups_after=end if len(view.groups) > end else None,
        ).model_dump_json()

    return await read_answer(request, project, read)


def _shown(group: FrontierGroup) -> FrontierGroup:
    """``group`` with the first ``SHOWN_RESULTS`` of each kind of result, and how many
    more of each."""
    parts = ("best", "dominated", "not_compared", "candidates")
    return group.model_copy(
        update={
            **{part: getattr(group, part)[:SHOWN_RESULTS] for part in parts},
            "more": {
                part: len(getattr(group, part)) - SHOWN_RESULTS
                for part in parts
                if len(getattr(group, part)) > SHOWN_RESULTS
            },
        }
    )


async def changes(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    name = matched(_NAME, request.match_info["category"], "frontier category")
    assert name is not None  # noqa: S101 - a path part is always there
    query = query_of(request, {"before", "limit"})
    before = matched(_NUMBER, query.get("before"), "cursor")
    limit = int(matched(_LIMIT, query.get("limit", "50"), "limit") or 50)

    def read(ledger: Ledger) -> str:
        found = ledger.frontier_history(
            project, name, before=None if before is None else int(before), limit=limit
        )
        return FrontierHistoryAnswer(
            project_id=project, observed_at=datetime.now(UTC), history=found
        ).model_dump_json()

    return await read_answer(request, project, read)


async def then(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    name = matched(_NAME, request.match_info["category"], "frontier category")
    assert name is not None  # noqa: S101 - a path part is always there
    raw = query_of(request, {"at"}).get("at")
    try:
        at = datetime.fromisoformat(raw or "")
    except ValueError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "at is a time with its zone") from None
    if at.tzinfo is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "at is a time with its zone")

    def read(ledger: Ledger) -> str:
        return FrontierPastAnswer(
            project_id=project, observed_at=datetime.now(UTC), past=ledger.frontier_past(
                project, name, at)
        ).model_dump_json()  # fmt: skip

    return await read_answer(request, project, read)


async def category(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    name = matched(_NAME, request.match_info["category"], "frontier category")
    assert name is not None  # noqa: S101 - a path part is always there
    raw = matched(_NUMBER, query_of(request, {"revision"}).get("revision"), "revision")

    def read(ledger: Ledger) -> str:
        found = ledger.category(project, name, None if raw is None else int(raw))
        return FrontierCategory(
            project_id=project, observed_at=datetime.now(UTC), category=found
        ).model_dump_json()

    return await read_answer(request, project, read)


async def define(request: web.Request) -> web.Response:
    """POST …/frontier: the user defines a category, or revises the one at
    ``expected``. Same-origin, the user's only."""
    project = identifier(request, "project")
    raw: object = await json_body(request, "frontier category JSON")

    def defined(store: DocumentStore) -> dict[str, object]:
        return Ledger(store).define_category(project, raw, USER).model_dump(mode="json")

    return await documents.answer(request, defined, writer=True)
