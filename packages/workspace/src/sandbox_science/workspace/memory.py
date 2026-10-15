# SPDX-License-Identifier: MIT
"""The project memory, for the GUI's Memory page (docs/specs/project-memory.md, PM-4).

``GET /api/v0/projects/{project}/memory`` answers the accepted entries in the page's
order, the memory's version, how many characters of entries an agent receives at most
(``bound``) and how many entries fit, and how many proposals wait. ``GET
…/memory/proposals`` answers the waiting proposals, oldest first, each with the entry it
would change as that entry is now. ``GET …/memory/{entry}/history`` answers an entry's
versions, newest first, 50 at a time before ``before``. ``POST …/memory`` is the user's
change: add, edit, remove or move an entry, or accept or decline a proposal, naming the
revision seen, with an operation ID whose retry returns the first result.
"""

from __future__ import annotations

import re
from typing import Final

from aiohttp import web

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.memory import (
    GIVEN_CHARS,
    Memory,
    MemoryChange,
    MemoryEntryHistory,
    MemoryProposal,
    ProjectMemory,
)
from sandbox_science.orchestrator.projects import ProjectError

from . import documents
from .context import json_body

PATH: Final = "/api/v0/projects/{project}/memory"
_ENTRY: Final = re.compile(r"mem-[1-9][0-9]{0,8}")
_NUMBER: Final = re.compile(r"[1-9][0-9]{0,8}")


class MemoryAnswer(Contract):
    """A project's memory, and the characters of entries an agent receives at most."""

    memory: Memory
    bound: int


class MemoryProposals(Contract):
    """The proposals that wait for the user."""

    proposals: tuple[MemoryProposal, ...]


class MemoryHistory(Contract):
    """A page of an entry's versions."""

    history: MemoryEntryHistory


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, memory)
    app.router.add_post(PATH, change)
    app.router.add_get(PATH + "/proposals", proposals)
    app.router.add_get(PATH + "/{entry}/history", history)


def _query(request: web.Request, allowed: frozenset[str] = frozenset()) -> dict[str, str]:
    query = dict(request.query)
    if set(query) - allowed or len(query) != len(request.query):
        what = ", ".join(sorted(allowed)) or "no parameters"
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"the memory read takes {what}")
    return query


async def memory(request: web.Request) -> web.Response:
    _query(request)
    return await documents.answer(
        request,
        lambda store: MemoryAnswer(
            memory=ProjectMemory(store).memory(request.match_info["project"]), bound=GIVEN_CHARS
        ).model_dump(mode="json"),
    )


async def proposals(request: web.Request) -> web.Response:
    _query(request)
    return await documents.answer(
        request,
        lambda store: MemoryProposals(
            proposals=ProjectMemory(store).proposals(request.match_info["project"])
        ).model_dump(mode="json"),
    )


async def history(request: web.Request) -> web.Response:
    entry = request.match_info["entry"]
    before = _query(request, frozenset({"before"})).get("before")
    if _ENTRY.fullmatch(entry) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "a memory entry is named mem-N")
    if before is not None and _NUMBER.fullmatch(before) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "before is a revision")

    def read(store: DocumentStore) -> dict[str, object]:
        found = ProjectMemory(store).history(
            request.match_info["project"], entry, before=None if before is None else int(before)
        )
        return MemoryHistory(history=found).model_dump(mode="json")

    return await documents.answer(request, read)


async def change(request: web.Request) -> web.Response:
    _query(request)
    value = await json_body(request, "memory change JSON")

    def changed(store: DocumentStore) -> dict[str, object]:
        result: MemoryChange = ProjectMemory(store).change(
            request.match_info["project"], value, USER
        )
        return result.model_dump(mode="json")

    return await documents.answer(request, changed, writer=True)
