# SPDX-License-Identifier: MIT
"""The user's answers to connector tool calls the policy escalates (D76; owner decision,
2026-10-21): the chat shows a call that waits, and the user allows it once, for the
project, or for any escalated tool of its server, or denies it.

The MCP proxy holds each such call and is the only authority on it: the workspace reads
and answers its project's permission requests as an operator caller, a token of its own
that makes no MCP call. Whatever the scope, an answer covers only calls the policy
escalates, so it never reaches a denied tool. Routes:

- ``GET …/sessions/{session}/permission-requests``: the session's calls that wait, each
  with its permission request's number, server, tool, the policy's reason and when it
  times out. Never the call's arguments: the proxy keeps only their digest, and the page
  shows them from the session's own transcript. A delegated run's call names the child
  that asked, with the brief its parent gave it and the child's own words (D99); one its
  parent withdrew is not listed, cannot be approved, and is denied at the proxy.
- ``POST …/permission-requests/{seq}/answer`` with ``{"decision": "once" | "project" |
  "server" | "deny"}``: once approves this call; project remembers the tool for the
  project, server any escalated tool of the server (``approval_settings.py``); deny
  refuses this call.
- ``GET`` and ``POST …/approvals/settings``: the remembered approvals, which the user may
  remove, at the revision read.

A remembered approval is given to the proxy for ``GRANT_HOURS`` at a time: the relay
gives it again when a call it covers is held, so removing it here ends it soon after,
and it outlasts the proxy's own records, which a relaunch does not keep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

import aiohttp
from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.orchestrator.approval_settings import Grant, ProjectApprovals
from sandbox_science.orchestrator.connectors import check_proxy
from sandbox_science.orchestrator.delegation.requests import ChildRequest, ChildRequests
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.projects import ProjectError

from .context import CONTEXT, identifier, json_answer, json_body, session_record
from .requests import document_answer, dumps

_log = logging.getLogger(__name__)

GRANT_HOURS: Final = 0.25
"""How long the proxy holds a remembered approval it was given, in hours."""
ONCE_HOURS: Final = 1.0
"""How long an approval of one call (its exact arguments) lasts at the proxy."""
RELAY_SECONDS: Final = 3.0
MAX_REQUESTS: Final = 256


class ChildAsked(Contract):
    """The delegated run whose call is held (D99): agent-attributed text, never the user's.
    The workspace sets it from its own records; the proxy's listing never carries it."""

    delegation_id: str
    run: str
    kind: str
    brief: str
    """What the parent asked the child to do, bounded: the parent agent's words."""
    words: str
    """The child's own words at the call, bounded."""


class PendingApproval(Contract):
    """A call the proxy holds for the user's answer."""

    seq: int
    server: str
    tool: str
    method: str
    policy: str
    """The policy revision the call was held under."""
    session: str | None = None
    rule: str | None = None
    reason: str | None = None
    recorded_at: datetime
    expires_at: datetime
    timed_out: bool
    on_timeout: str
    child: ChildAsked | None = None
    """The delegated run that asked, if one did."""


class PendingListing(Contract):
    requests: list[PendingApproval]


class Decision(Contract):
    decision: Literal["once", "project", "server", "deny"]


class AnswerResult(Contract):
    seq: int
    decision: str
    remembered: Grant | None = None


class ApprovalClient:
    """The proxy's operator routes, with the workspace's own operator token, which never
    enters a browser response or an exception."""

    def __init__(self, url: str, token: str) -> None:
        check_proxy(url, token)  # loopback or https, and a token that fits a header
        self.url, self.token = url.rstrip("/"), token

    async def pending(self) -> list[PendingApproval]:
        async with (
            self._session() as http,
            http.get(
                self.url + "/permission-requests", headers=self._headers(), allow_redirects=False
            ) as response,
        ):
            if response.status != 200:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "the MCP proxy's requests are unavailable"
                )
            raw = await response.content.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ProjectError(ErrorKind.UNAVAILABLE, "the MCP proxy's answer is too large")
            try:
                found = PendingListing.model_validate_json(raw)
            except ValidationError:
                raise ProjectError(ErrorKind.UNAVAILABLE, "the MCP proxy answered badly") from None
            return found.requests[:MAX_REQUESTS]

    async def answer(self, seq: int, *, approve: bool, scope: str, hours: float | None) -> None:
        body = {"approve": approve, "scope": scope, "hours": hours}
        async with (
            self._session() as http,
            http.post(
                f"{self.url}/permission-requests/{seq}/answer",
                json=body,
                headers=self._headers(),
                allow_redirects=False,
            ) as response,
        ):
            if response.status == 404:
                raise ProjectError(ErrorKind.NOT_FOUND, f"no permission request {seq}")
            if response.status != 201:
                raise ProjectError(ErrorKind.UNAVAILABLE, "the MCP proxy did not take the answer")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token}

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False)


@dataclass
class Configuration:
    project: str
    client: ApprovalClient
    relayed: set[int] = field(default_factory=set[int])
    """Requests the relay answered, so it answers each once."""


APPROVALS: Final = web.AppKey("mcp_approvals", Configuration)
_SEQ: Final = TypeAdapter(int)


def add_routes(app: web.Application) -> None:
    app.router.add_get(
        "/api/v0/projects/{project}/sessions/{session}/permission-requests", session_requests
    )
    app.router.add_post("/api/v0/projects/{project}/permission-requests/{seq}/answer", answer)
    app.router.add_get("/api/v0/projects/{project}/approvals/settings", settings)
    app.router.add_post("/api/v0/projects/{project}/approvals/settings", change_settings)


def configure(app: web.Application, project: str, client: ApprovalClient) -> None:
    """Answer the project's requests from the workspace, and relay remembered approvals."""
    app[APPROVALS] = Configuration(project, client)
    relay: list[asyncio.Task[None]] = []

    async def start(running: web.Application) -> None:
        relay.append(asyncio.create_task(_relay(running)))

    async def stop(_: web.Application) -> None:
        for task in relay:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)


def _configured(request: web.Request, project: str) -> Configuration:
    config = request.app.get(APPROVALS)
    if config is None or config.project != project:
        raise ProjectError(ErrorKind.NOT_FOUND, "this project's tool calls are not answered here")
    return config


async def session_requests(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    await asyncio.to_thread(session_record, context.projects, project, session)
    config = request.app.get(APPROVALS)
    if config is None or config.project != project:
        return json_answer(dumps({"requests": []}))
    found = [item for item in await config.client.pending() if item.session == session]
    children = await asyncio.to_thread(
        ChildRequests(context.documents).in_session, project, session
    )
    listed = [
        item.model_copy(update={"child": _asked(children.get(item.seq))})
        for item in found
        if (child := children.get(item.seq)) is None or child.withdrawn_by is None
    ]
    return json_answer(dumps({"requests": [item.model_dump(mode="json") for item in listed]}))


def _asked(child: ChildRequest | None) -> ChildAsked | None:
    if child is None:
        return None
    return ChildAsked(
        delegation_id=child.delegation_id,
        run=child.run,
        kind=child.kind,
        brief=child.brief,
        words=child.words,
    )


async def answer(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    try:
        seq = _SEQ.validate_python(int(request.match_info["seq"]))
    except ValueError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid permission request") from None
    try:
        decision = Decision.model_validate(await json_body(request, "answer"), strict=True)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "an answer is once, project, server or deny"
        ) from None
    config = _configured(request, project)
    withdrawn = await asyncio.to_thread(
        ChildRequests(request.app[CONTEXT].documents).withdrawn, project, [seq]
    )
    if seq in withdrawn:  # a withdrawal only refuses: the user cannot approve it after
        raise ProjectError(
            ErrorKind.CONFLICT,
            f"permission request {seq} was withdrawn by the agent that delegated its caller",
        )
    asked = next((item for item in await config.client.pending() if item.seq == seq), None)
    if asked is None:
        raise ProjectError(ErrorKind.CONFLICT, f"permission request {seq} no longer waits")
    remembered: Grant | None = None
    if decision.decision in ("project", "server"):
        remembered = Grant(
            server=asked.server, tool=asked.tool if decision.decision == "project" else None
        )
        grant = remembered

        def remember(store: DocumentStore) -> None:
            approvals = ProjectApprovals(store)
            current = approvals.get(project)
            approvals.change(
                project,
                {
                    "expected": current.revision,
                    "grants": [g.model_dump(mode="json") for g in (*current.grants, grant)],
                    "operation_id": "approval-" + secrets.token_hex(12),
                },
                USER,
            )

        await asyncio.to_thread(remember, request.app[CONTEXT].writer)
    scope, hours = {
        "once": ("call", ONCE_HOURS),
        "project": ("tool", GRANT_HOURS),
        "server": ("server", GRANT_HOURS),
        "deny": ("call", None),
    }[decision.decision]
    await config.client.answer(seq, approve=decision.decision != "deny", scope=scope, hours=hours)
    result = AnswerResult(seq=seq, decision=decision.decision, remembered=remembered)
    return json_answer(dumps(result.model_dump(mode="json")))


async def settings(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    return await document_answer(
        request, lambda store: ProjectApprovals(store).get(project).model_dump(mode="json")
    )


async def change_settings(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    value = await json_body(request, "approval settings")

    def write(store: DocumentStore) -> dict[str, object]:
        found, replayed = ProjectApprovals(store).change(project, value, USER)
        return {**found.model_dump(mode="json"), "replayed": replayed}

    return await document_answer(request, write, access="write")


async def _relay(app: web.Application) -> None:
    """Give the proxy each remembered approval again when a call it covers is held, and
    deny each held call a parent withdrew (D99): a withdrawal only ever refuses."""
    config = app[APPROVALS]
    store = app[CONTEXT].documents
    while True:
        try:
            pending = await config.client.pending()
            withdrawn = await asyncio.to_thread(
                ChildRequests(store).withdrawn,
                config.project,
                [held.seq for held in pending if held.seq not in config.relayed],
            )
            for held in pending:
                if held.seq in config.relayed or held.timed_out:
                    continue
                if held.seq in withdrawn:
                    await config.client.answer(held.seq, approve=False, scope="call", hours=None)
                    config.relayed.add(held.seq)
                    continue
                grant = await asyncio.to_thread(
                    ProjectApprovals(store).covering, config.project, held.server, held.tool
                )
                if grant is None:
                    continue
                await config.client.answer(
                    held.seq,
                    approve=True,
                    scope="tool" if grant.tool is not None else "server",
                    hours=GRANT_HOURS,
                )
                config.relayed.add(held.seq)
        except asyncio.CancelledError:
            raise
        except Exception:  # the proxy may be down for a while: try again later
            _log.debug("remembered approvals were not relayed this time", exc_info=True)
        await asyncio.sleep(RELAY_SECONDS)
