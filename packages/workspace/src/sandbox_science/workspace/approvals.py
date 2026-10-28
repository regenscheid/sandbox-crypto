# SPDX-License-Identifier: MIT
"""The user's answers to permission requests (D76, D97): the chat shows each request that
waits as a card, and the user allows it once, for the session or for the project, within
the request's ceiling, or denies it.

The requests reach the chat from the worker, which holds the policy middleware's event
store (``source: worker``), for the workspace's operator client only:

- ``mcp``: a connector tool call the policy middleware holds (D98). A delegated run's call
  names the child that asked, with the brief its parent gave it and, where the child's
  run recorded them, its own words (D99); one its parent withdrew is not listed, cannot be
  approved, and is denied. The MCP proxy, which held such calls until it retired
  (2026-10-26), was a second source.
- ``network``: an HTTP or git request of research code the policy middleware escalated,
  and ``packages``: a provisioning operation's file set, or a package file research code
  fetched through the package cache.
- ``hold``: a detection rule's hold of a session's outward calls (D103), which the
  middleware raised: the agent's MCP calls by session, or research code's by sandbox,
  shown in every session of the project. ``clear`` resumes the calls; ``stop`` stops the
  session's run (the one the card names, else the one it is answered in) and leaves its
  counts standing, so that calls that go on hold it again. Sandbox compute goes on.

A card carries a structured summary, never the payload: the tool and its significant
arguments, or the method, host, path, content type, size and a JSON body's top-level
fields. ``scopes`` lists the answers the request's ceiling allows, and the server refuses
any other, whatever a client sends. A grant (``orchestrator.project_grants``) covers an
effect class on one server, a host for research code's reads, or a package source; a
class grant never covers research code's call, nor a host grant the agent's.

Routes:

- ``GET …/sessions/{session}/permission-requests``: the requests that wait in the session,
  with network and package requests that name no session in every session of the project.
- ``POST …/permission-requests/{seq}/answer`` with ``{"decision": "once" | "session" |
  "project" | "deny" | "clear" | "stop", "kind": "mcp" | "network" | "packages" | "hold",
  "source": "worker" | null, "hours": null | number, "session": null | id}``; ``clear``
  and ``stop`` answer a hold only, ``session`` names the one to stop.
  ``hours`` gives a session or project grant an expiry; without it, it lasts until revoked.
- ``GET …/grants``: the project's grants, with when each was last used; ``POST
  …/grants/{grant}/revoke`` ends one at once.

A project grant and clearing a hold are admin-only actions (identity.py, D104): with an
admin group configured, only its members may take them, while every user may allow once,
for the session, deny or stop. A grant's author is the user who gave or revoked it.

The relay answers each new request a grant covers, and notes the grant's use. It answers
the request itself (an MCP call with its exact arguments, so a grant never stretches to a
call of another class) or, for a host grant, the host's reads for that sandbox and policy
revision; each such answer lasts minutes, so a revoked grant stops reaching the
middleware soon after, and covers nothing new at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from aiohttp import web
from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract, ErrorKind, Identifier
from sandbox_science.contracts.permission_requests import (
    HoldAsked,
    HttpSummary,
    PackagesAsked,
    WorkerPermissionAnswer,
    WorkerPermissionRequest,
)
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.delegation.requests import (
    ChildRequest,
    ChildRequests,
    brief_text,
)
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.project_grants import (
    MAX_GRANT_HOURS,
    Asked,
    GrantListing,
    ProjectGrant,
    ProjectGrants,
)
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy import normalize_host
from sandbox_science.policy.approvals import offered

from .context import CONTEXT, WorkspaceContext, identifier, json_answer, json_body, session_record
from .identity import author, require_admin
from .requests import document_answer, dumps

_log = logging.getLogger(__name__)

GRANT_HOURS: Final = 0.25
"""How long an answer the relay gives the middleware for one call lasts, in hours."""
HOST_HOURS: Final = 1.0
"""How long the relay's answer for a host grant lets the middleware pass the host's reads
for one sandbox and policy revision, in hours: research code is refused once an hour at
most, while a revoked grant stops reaching the middleware within the hour."""
ONCE_HOURS: Final = 1.0
"""How long an approval of one request (its exact arguments or body) lasts."""
RELAY_SECONDS: Final = 3.0
MAX_REQUESTS: Final = 256
Kind = Literal["mcp", "network", "packages", "hold"]
Source = Literal["worker"]
"""Where a request is read and answered: the worker, which holds the policy middleware's
event store. The retired MCP proxy was another."""


class ChildAsked(Contract):
    """The delegated run whose call is held (D99): agent-attributed text, never the user's.
    The workspace sets it from its own records; the worker's listing never carries it."""

    delegation_id: str
    run: str
    kind: str
    brief: str
    """What the parent asked the child to do, bounded: the parent agent's words."""
    words: str
    """The child's own words at the call, bounded."""


class PendingApproval(Contract):
    """A request that waits for the user's answer, as its card shows it."""

    seq: int
    kind: Kind = "mcp"
    source: Source = "worker"
    """Set by the workspace."""
    server: str = ""
    tool: str = ""
    method: str = ""
    policy: str
    """The policy revision the request was held under."""
    session: str | None = None
    run: str | None = None
    """The run whose call it is, as the run service named it (DA-4): attribution only."""
    rule: str | None = None
    reason: str | None = None
    recorded_at: datetime
    expires_at: datetime
    timed_out: bool
    on_timeout: str
    child: ChildAsked | None = None
    """The delegated run that asked, if one did."""
    caller_kind: str = "code"
    """``agent``, ``code`` or ``user``: one not said is taken as code's, which no class
    grant covers."""
    effect: str | None = None
    """A tool call's effect class (D96)."""
    max_grant: str | None = None
    """The widest grant the policy allows for it (D97)."""
    summary: dict[str, str] = Field(default_factory=dict[str, str])
    """A tool call's significant arguments, by kind (url, host, recipient, title, size,
    query)."""
    http: HttpSummary | None = None
    packages: PackagesAsked | None = None
    hold: HoldAsked | None = None
    """What a detection rule saw, for a hold of the session's calls (D103)."""
    scopes: list[str] = Field(default_factory=list[str])
    """The answers its ceiling allows, narrowest first; set by the workspace."""


class PendingListing(Contract):
    requests: list[PendingApproval]


class Decision(Contract):
    decision: Literal["once", "session", "project", "deny", "clear", "stop"]
    kind: Kind = "mcp"
    source: Source | None = None
    hours: float | None = Field(default=None, gt=0, le=MAX_GRANT_HOURS)
    session: Identifier | None = None
    """For ``stop``: the session whose run stops, when the hold names none."""


class AnswerResult(Contract):
    seq: int
    decision: str
    kind: Kind = "mcp"
    source: Source = "worker"
    granted: list[ProjectGrant] = Field(default_factory=list[ProjectGrant])


@dataclass
class Configuration:
    worker: WorkerClient | None = None
    clock: Clock = SYSTEM_CLOCK
    relayed: set[tuple[str, int]] = field(default_factory=set[tuple[str, int]])
    """Requests the relay answered, by project, so it answers each once."""


APPROVALS: Final = web.AppKey("approvals", Configuration)
_SEQ: Final = TypeAdapter(int)


def add_routes(app: web.Application) -> None:
    app.router.add_get(
        "/api/v0/projects/{project}/sessions/{session}/permission-requests", session_requests
    )
    app.router.add_post("/api/v0/projects/{project}/permission-requests/{seq}/answer", answer)
    app.router.add_get("/api/v0/projects/{project}/grants", grants)
    app.router.add_post("/api/v0/projects/{project}/grants/{grant}/revoke", revoke)


def configure(
    app: web.Application, worker: WorkerClient | None, clock: Clock = SYSTEM_CLOCK
) -> None:
    """Answer requests from the workspace through ``worker``, and relay grants."""
    app[APPROVALS] = Configuration(worker, clock)
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


def _worker(request: web.Request) -> WorkerClient:
    config = request.app.get(APPROVALS)
    if config is None or config.worker is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "permission requests are not answered here")
    return config.worker


def asked_of(item: PendingApproval) -> Asked:
    """What a request asks for, as grants compare it."""
    if item.kind == "mcp":
        return Asked(
            "mcp", item.caller_kind, item.session, item.max_grant,
            server=item.server, effect=item.effect,
        )  # fmt: skip
    if item.kind == "network" and item.http is not None:
        try:
            host = normalize_host(item.http.host)
        except ValueError:
            host = None
        return Asked(
            "network", item.caller_kind, item.session, item.max_grant,
            host=host, method=item.http.method.upper(),
        )  # fmt: skip
    sources = tuple(
        (source.ecosystem, source.channel)
        for source in (item.packages.sources if item.packages else [])
    )
    return Asked("packages", item.caller_kind, item.session, item.max_grant, sources=sources)


def _offered(item: PendingApproval) -> PendingApproval:
    if item.kind == "hold":
        return item.model_copy(update={"scopes": []})  # cleared or stopped, never granted
    asked = asked_of(item)
    scopes = offered(item.max_grant, item.effect, session=item.session is not None)
    usable = [scope for scope in scopes if asked.grantable(scope) is None]
    return item.model_copy(update={"scopes": usable})


def _from_worker(found: WorkerPermissionRequest) -> PendingApproval:
    call = found.mcp if found.kind == "mcp" else None
    return PendingApproval(
        seq=found.seq,
        kind=found.kind,
        source="worker",
        server=call.server if call else "",
        tool=call.tool if call else "",
        effect=call.effect if call else None,
        summary=dict(call.summary) if call else {},
        method=call.method if call else found.http.method if found.http else "",
        policy=found.policy,
        session=found.session_id,
        run=found.run,
        rule=found.rule,
        reason=found.reason,
        recorded_at=found.recorded_at,
        expires_at=found.expires_at,
        timed_out=found.timed_out,
        on_timeout=found.on_timeout,
        caller_kind=found.caller_kind,
        max_grant=found.max_grant,
        http=found.http,
        packages=found.packages,
        hold=found.hold,
    )


async def _worker_pending(worker: WorkerClient, project: str) -> list[PendingApproval]:
    listing = await worker.permission_requests(project)
    return [_from_worker(item) for item in listing.requests[:MAX_REQUESTS]]


async def session_requests(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    await asyncio.to_thread(session_record, context.projects, project, session)
    config = request.app.get(APPROVALS)
    listed: list[PendingApproval] = []
    if config is not None and config.worker is not None:
        try:
            found = await _worker_pending(config.worker, project)
        except WorkerAPIError:  # the CLI path remains
            _log.warning("the worker's permission requests are unavailable", exc_info=True)
            found = []
        mine = [item for item in found if item.session in (None, session)]
        children = await asyncio.to_thread(
            ChildRequests(context.documents).in_session, project, session
        )
        kept = [item for item in mine if not _withdrawn(item, children)]
        listed += [
            item.model_copy(update={"child": _asked(child)})
            if (child := _child_of(item, children)) is not None
            else item
            for item in await _with_children(context, project, session, kept)
        ]
    shown = [_offered(item).model_dump(mode="json") for item in listed]
    return json_answer(dumps({"requests": shown}))


async def _with_children(
    context: WorkspaceContext, project: str, session: str, items: list[PendingApproval]
) -> list[PendingApproval]:
    """The middleware's held calls with the delegated run and its brief, where the call's
    run is a delegated one of this session (DA-9). The middleware keeps no words of the
    child's: the child's run records them as it waits (``child_requests``, which
    ``_child_of`` reads), and a call whose wait it did not record shows the brief alone. A
    main run, or one the session's records do not list, asks as itself."""
    runs = [item.run for item in items if item.run and item.session == session]
    if not runs:
        return items
    found = await asyncio.to_thread(
        DelegationRecords(context.documents).of_runs, project, session, runs
    )
    return [
        item.model_copy(update={"child": _delegated(delegation)})
        if (delegation := found.get(item.run or "")) is not None and item.session == session
        else item
        for item in items
    ]


def _delegated(delegation: Delegation) -> ChildAsked:
    return ChildAsked(
        delegation_id=delegation.delegation_id,
        run=delegation.run,
        kind=delegation.kind,
        brief=brief_text(delegation.brief),
        words="",
    )


def _child_of(item: PendingApproval, children: dict[int, ChildRequest]) -> ChildRequest | None:
    """The child's record of a held call: one its run recorded for this very request, by
    number, run, server and tool, so that an older record under the retired MCP proxy's
    numbering never stands for a request of the middleware's."""
    child = children.get(item.seq)
    if child is None or item.kind != "mcp":
        return None
    if (child.run, child.server, child.tool) != (item.run, item.server, item.tool):
        return None
    return child


def _withdrawn(item: PendingApproval, children: dict[int, ChildRequest]) -> bool:
    """Whether the delegating run withdrew this held call (D99): it is refused, never
    approved."""
    child = _child_of(item, children)
    return child is not None and child.withdrawn_by is not None


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


def ledger_answer(item: PendingApproval, decision: str) -> tuple[str, float | None]:
    """The scope and hours of the answer the middleware records: the request itself (an
    MCP call with its exact arguments), or for a host grant the host's reads in that
    sandbox."""
    approve = decision != "deny"
    if item.kind == "mcp":
        return "call", (None if not approve else ONCE_HOURS if decision == "once" else GRANT_HOURS)
    if item.kind == "packages" and item.http is None:
        return "files", None  # the operation's own deadline bounds it
    if item.kind == "network" and decision in ("session", "project"):
        return "host", HOST_HOURS
    return "request", (None if not approve else ONCE_HOURS if decision == "once" else GRANT_HOURS)


async def _send(
    config: Configuration, item: PendingApproval, *, approve: bool, scope: str, hours: float | None
) -> None:
    if config.worker is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "permission requests are not answered here")
    await config.worker.answer_permission_request(
        item.seq,
        WorkerPermissionAnswer.model_validate({"approve": approve, "scope": scope, "hours": hours}),
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
            ErrorKind.INVALID_REQUEST, "an answer is once, session, project or deny"
        ) from None
    context = request.app[CONTEXT]
    config = request.app.get(APPROVALS)
    if config is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "this project's requests are not answered here")
    pending = await _worker_pending(_worker(request), project)
    held = next((item for item in pending if item.seq == seq and item.kind == decision.kind), None)
    if held is None:
        raise ProjectError(ErrorKind.CONFLICT, f"permission request {seq} no longer waits")
    if held.kind == "mcp" and held.session is not None:
        children = await asyncio.to_thread(
            ChildRequests(context.documents).in_session, project, held.session
        )
        if _withdrawn(held, children):  # a withdrawal only refuses: never approved after
            raise ProjectError(
                ErrorKind.CONFLICT,
                f"permission request {seq} was withdrawn by the agent that delegated its caller",
            )
    if (held.kind == "hold") != (decision.decision in ("clear", "stop")):
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "a hold is cleared or stopped, and only a hold is"
        )
    if held.kind == "hold":
        if decision.decision == "clear":
            require_admin(request, "clearing a hold")
        return await _answer_hold(request, project, config, held, decision)
    if decision.decision == "project":
        require_admin(request, "a project grant")
    asked = asked_of(held)
    if decision.decision != "deny" and decision.decision not in _offered(held).scopes:
        # The ceiling is the server's to enforce, whatever a client sends (D97).
        raise ProjectError(
            ErrorKind.POLICY_DENIED,
            asked.grantable(decision.decision) or "this request takes no such answer",
        )
    granted: list[ProjectGrant] = []
    if decision.decision in ("session", "project"):
        now = config.clock.now()
        granted = await asyncio.to_thread(
            ProjectGrants(context.writer).give,
            project, asked, decision.decision, author(request), now, decision.hours,
        )  # fmt: skip
    scope, hours = ledger_answer(held, decision.decision)
    await _send(config, held, approve=decision.decision != "deny", scope=scope, hours=hours)
    if granted:
        await asyncio.to_thread(
            ProjectGrants(context.writer).used,
            project, [grant.grant_id for grant in granted], config.clock.now(),
        )  # fmt: skip
    config.relayed.add((project, seq))
    result = AnswerResult(
        seq=seq, decision=decision.decision, kind=held.kind, source=held.source, granted=granted
    )
    return json_answer(dumps(result.model_dump(mode="json")))


async def _answer_hold(
    request: web.Request,
    project: str,
    config: Configuration,
    held: PendingApproval,
    decision: Decision,
) -> web.Response:
    """Clear a hold, which resumes the session's outward calls, or stop the session's run
    and leave the hold answered: calls that go on raise a new one."""
    context = request.app[CONTEXT]
    session = held.session or decision.session
    if decision.decision == "stop":
        if session is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "stop names the session to stop")

        def stop() -> None:
            session_record(context.projects, project, session)
            lease = RunState(context.documents).lease(project, session)
            if lease is not None:
                RunState(context.writer).cancel(project, session, lease.run)

        await asyncio.to_thread(stop)
    await _send(config, held, approve=decision.decision == "clear", scope="session", hours=None)
    result = AnswerResult(seq=held.seq, decision=decision.decision, kind="hold", source="worker")
    return json_answer(dumps(result.model_dump(mode="json")))


async def grants(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    return await document_answer(
        request, lambda store: ProjectGrants(store).listing(project).model_dump(mode="json")
    )


async def revoke(request: web.Request) -> web.Response:
    project, grant = identifier(request, "project"), request.match_info["grant"]
    clock = request.app[APPROVALS].clock if APPROVALS in request.app else SYSTEM_CLOCK
    by = author(request)

    def write(store: DocumentStore) -> GrantListing:
        return ProjectGrants(store).revoke(project, grant, by, clock.now())

    return await document_answer(request, write, access="write")


async def _relay_one(
    config: Configuration, store: DocumentStore, writer: DocumentStore,
    project: str, held: PendingApproval,
) -> None:  # fmt: skip
    grants = ProjectGrants(store)
    found = await asyncio.to_thread(grants.covering, project, asked_of(held), config.clock.now())
    if not found:
        return
    scope = found[0].scope
    ledger_scope, hours = ledger_answer(held, scope)
    await _send(config, held, approve=True, scope=ledger_scope, hours=hours)
    config.relayed.add((project, held.seq))
    await asyncio.to_thread(
        ProjectGrants(writer).used, project, [grant.grant_id for grant in found],
        config.clock.now(),
    )  # fmt: skip


async def _relay_worker(
    config: Configuration, store: DocumentStore, writer: DocumentStore, projects: Sequence[str]
) -> None:
    if config.worker is None:
        return
    for project in projects:
        children: dict[str, dict[int, ChildRequest]] = {}
        for held in await _worker_pending(config.worker, project):
            if held.kind == "hold":
                continue  # only the user clears a hold
            if (project, held.seq) in config.relayed or held.timed_out:
                continue
            if held.kind == "mcp" and held.session is not None:
                if held.session not in children:
                    children[held.session] = await asyncio.to_thread(
                        ChildRequests(store).in_session, project, held.session
                    )
                if _withdrawn(held, children[held.session]):
                    await _send(config, held, approve=False, scope="call", hours=None)
                    config.relayed.add((project, held.seq))
                    continue
            await _relay_one(config, store, writer, project, held)


async def _relay(app: web.Application) -> None:
    """Answer each held request a grant covers, and deny each held call a parent withdrew
    (D99): a withdrawal only ever refuses."""
    config = app[APPROVALS]
    store, writer = app[CONTEXT].documents, app[CONTEXT].writer
    while True:
        try:
            projects = await asyncio.to_thread(ProjectGrants(store).projects_with_worker_grants)
            withdrawing = await asyncio.to_thread(ChildRequests(store).projects_withdrawing)
            await _relay_worker(config, store, writer, sorted({*projects, *withdrawing}))
        except asyncio.CancelledError:
            raise
        except Exception:  # the worker may be down for a while
            _log.debug("grants were not relayed this time", exc_info=True)
        await asyncio.sleep(RELAY_SECONDS)
