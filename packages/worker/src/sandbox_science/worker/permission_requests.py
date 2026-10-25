# SPDX-License-Identifier: MIT
"""Permission requests in the middleware's store, listed and answered for the workspace
(D97, D98).

The policy middleware records research code's escalated HTTP and git requests and the
agent's escalated MCP calls straight to a server, and the worker its provisioning
operations' file sets, in one event store. An operator client (the workspace) lists the
unanswered ones of a project, with what their cards show, and relays the user's answers
to them. Requests of other projects' sandboxes are not found, and a request no
environment of the worker owns is no one's; an MCP request is its key's project's, which
the middleware takes from the sandbox's compiled config. A download from the package
cache is a package request, answered for that file only: a ``host`` answer would cover
the whole cache.

Any client may also confirm one MCP request for its own projects (``confirm``): whether
it is a given call's and stands as a refusal said. The harness asks before it trusts a
refusal that names a request, since an MCP server can write one; the answer is a yes or
a no, never the request's record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo
from sandbox_science.contracts.permission_requests import (
    MAX_LISTED,
    HoldAsked,
    HttpSummary,
    McpAsked,
    PackagesAsked,
    PackageSource,
    PermissionConfirmation,
    WorkerPermissionAnswer,
    WorkerPermissionAnswered,
    WorkerPermissionRequest,
)
from sandbox_science.policy.approvals import (
    PACKAGES_MAX_GRANT,
    PermissionRequest,
    network_max_grant,
)

from .permissions import Permissions

_SCOPES = {
    "network": ("request", "host"),
    "packages": ("request", "files"),
    "mcp": ("call",),
    "hold": ("session",),
}
_GRANTS = frozenset({"none", "once", "session", "project"})


class PermissionRequestError(Exception):
    """A request that is not this client's to see, or an answer it cannot take."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


class PermissionDesk:
    """The user's side of the middleware's store: list and answer, for allowed projects."""

    def __init__(
        self,
        permissions: Permissions,
        environments: Callable[[], list[EnvironmentInfo]],
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.permissions = permissions
        self.environments = environments
        self.clock = clock

    def listing(self, project_id: str, now: datetime) -> list[WorkerPermissionRequest]:
        """The project's unanswered requests, oldest first. Blocking."""
        self.permissions.refresh()
        owners = self._owners()
        mine = [
            request
            for request in self.permissions.unanswered()
            if self._project(request, owners) == project_id
        ][:MAX_LISTED]
        payloads = self.permissions.escalations(request.seq for request in mine)
        listed: list[WorkerPermissionRequest] = []
        for request in mine:
            payload = payloads.get(request.seq, {})
            if request.key.kind == "mcp":
                found = _described_call(request, payload, project_id, now)
            elif request.key.kind == "hold":
                found = _described_hold(request, payload, project_id)
            else:
                owner = owners[str(request.key.fields()["sandbox_id"])]
                found = _described(request, payload, owner, now)
            if found is not None:
                listed.append(found)
        return listed

    def confirm(
        self,
        seq: int,
        allows: Callable[[str], bool],
        asked: PermissionConfirmation,
        now: datetime,
    ) -> bool:
        """Whether MCP request ``seq`` is ``asked``'s call and stands as its refusal said,
        for a client allowed the call's project. Blocking."""
        if not allows(asked.project_id):
            return False
        self.permissions.refresh()
        request = self.permissions.pending(seq)
        if request is None or request.key.kind != "mcp":
            return False
        call = {
            "project_id": asked.project_id,
            "server": asked.server,
            "method": asked.method,
            "tool": asked.tool,
            "arguments": asked.arguments,
        }
        return self.permissions.confirms(seq, asked.reason, call, now)

    @staticmethod
    def _project(request: PermissionRequest, owners: Mapping[str, EnvironmentInfo]) -> str | None:
        """Whose request it is: an MCP call's key names its project; a sandbox's request
        is its environment's project's, if the worker owns the environment."""
        fields = request.key.fields()
        if request.key.kind == "mcp" or (request.key.kind == "hold" and fields["project_id"]):
            return str(fields["project_id"])
        if request.key.kind not in ("http", "packages", "hold"):
            return None
        owner = owners.get(str(fields["sandbox_id"]))
        return owner.project_id if owner is not None else None

    def answer(
        self,
        seq: int,
        allows: Callable[[str], bool],
        answer: WorkerPermissionAnswer,
        now: datetime,
    ) -> WorkerPermissionAnswered:
        """Record the user's answer to request ``seq``. Blocking."""
        self.permissions.refresh()
        request = self.permissions.pending(seq)
        project = self._project(request, self._owners()) if request is not None else None
        if request is None or project is None or not allows(project):
            raise PermissionRequestError(f"no permission request {seq}", missing=True)
        payload = self.permissions.escalations([seq]).get(seq, {})
        kind = _kind(request, payload)
        cache_file = request.key.kind == "http" and kind == "packages"
        wanted = ("request",) if cache_file else _SCOPES[kind]
        if answer.scope not in wanted:
            raise PermissionRequestError(
                f"permission request {seq} takes the scope {' or '.join(wanted)}"
            )
        try:
            record, decided = self.permissions.answer_request(
                seq, approve=answer.approve, scope=answer.scope, hours=answer.hours, now=now
            )
        except ValueError as exc:
            raise PermissionRequestError(str(exc)) from None
        return WorkerPermissionAnswered(
            seq=seq, answer=record, kind="approval" if decided == "approval" else "denial"
        )

    def _owners(self) -> dict[str, EnvironmentInfo]:
        return {info.sandbox_id: info for info in self.environments() if info.sandbox_id}


def _kind(request: PermissionRequest, payload: Mapping[str, object]) -> str:
    if request.key.kind in ("mcp", "hold"):
        return request.key.kind
    return "packages" if request.key.kind == "packages" or "package" in payload else "network"


def _text(value: object) -> str | None:
    return value[:600] if isinstance(value, str) else None


def _described(
    request: PermissionRequest,
    payload: Mapping[str, object],
    owner: EnvironmentInfo,
    now: datetime,
) -> WorkerPermissionRequest | None:
    fields = request.key.fields()
    kind = _kind(request, payload)
    http: HttpSummary | None = None
    packages: PackagesAsked | None = None
    if request.key.kind == "http":
        method = str(fields["method"])
        try:
            http = HttpSummary.model_validate(payload.get("summary"))
        except ValidationError:  # recorded before summaries: what the key says
            http = HttpSummary(
                method=method, host=str(fields["host"]), port=int(fields["port"]),
                path=str(fields["path"])[:600], size=0,
            )  # fmt: skip
        ceiling = network_max_grant(method).value
        if payload.get("max_grant") == "once":
            ceiling = "once"  # a detection rule's hold of one call (D103)
    else:
        ceiling = PACKAGES_MAX_GRANT.value
    if kind == "packages":
        packages = _packages(request, payload)
        ceiling = PACKAGES_MAX_GRANT.value
    caller = payload.get("caller_kind")
    try:
        return WorkerPermissionRequest.model_validate(
            {
                "seq": request.seq,
                "kind": kind,
                "project_id": owner.project_id,
                "environment_id": owner.environment_id,
                "session_id": payload.get("session_id"),
                # A request from before caller kinds were recorded: research code's,
                # unless it is a provisioning operation's, which the agent asked for.
                "caller_kind": caller
                if caller in ("agent", "code", "user")
                else ("agent" if request.key.kind == "packages" else "code"),
                "policy": fields["policy_digest"],
                "rule": _text(payload.get("rule")),
                "reason": _text(payload.get("reason")),
                "max_grant": ceiling,
                "http": http,
                "packages": packages,
                "recorded_at": request.recorded,
                "expires_at": request.recorded + request.timeout,
                "timed_out": request.expired(now),
                "on_timeout": request.on_timeout,
            }
        )
    except ValidationError:
        return None  # a record this listing cannot show is not offered for an answer


def _described_call(
    request: PermissionRequest, payload: Mapping[str, object], project_id: str, now: datetime
) -> WorkerPermissionRequest | None:
    """An MCP call the middleware holds (D98), as the proxy's listing describes one."""
    fields = request.key.fields()
    summary = payload.get("summary")
    effect, ceiling = _text(payload.get("effect")), payload.get("max_grant")
    session = payload.get("session")
    try:
        return WorkerPermissionRequest.model_validate(
            {
                "seq": request.seq,
                "kind": "mcp",
                "project_id": project_id,
                "session_id": session if isinstance(session, str) else None,
                "run": _text(payload.get("run")) or None,
                # Only the agent's calls reach a server directly; anything else is code's,
                # which no class grant covers.
                "caller_kind": "agent" if payload.get("caller_kind") == "agent" else "code",
                "policy": fields["policy_digest"],
                "rule": _text(payload.get("rule")),
                "reason": _text(payload.get("reason")),
                # An unknown ceiling allows an answer for the one call only.
                "max_grant": ceiling if ceiling in _GRANTS else "once",
                "mcp": McpAsked(
                    server=str(fields["server"])[:600],
                    method=str(fields["method"])[:600],
                    tool=str(fields["tool"])[:600],
                    effect=effect,
                    summary=_summary(summary),
                ),
                "recorded_at": request.recorded,
                "expires_at": request.recorded + request.timeout,
                "timed_out": request.expired(now),
                "on_timeout": request.on_timeout,
            }
        )
    except ValidationError:
        return None  # a record this listing cannot show is not offered for an answer


def _described_hold(
    request: PermissionRequest, payload: Mapping[str, object], project_id: str
) -> WorkerPermissionRequest | None:
    """A detection rule's hold of a session's outward calls (D103): the agent's session, or
    research code's sandbox, whose environment's sessions all show it."""
    session = request.key.fields()["session"]
    recorded = payload.get("policy")
    policy = cast(dict[str, object], recorded) if isinstance(recorded, dict) else {}
    try:
        return WorkerPermissionRequest.model_validate(
            {
                "seq": request.seq,
                "kind": "hold",
                "project_id": project_id,
                "session_id": session or None,
                "caller_kind": "agent" if payload.get("caller_kind") == "agent" else "code",
                "policy": _text(policy.get("digest")) or "",
                "rule": _text(payload.get("rule")),
                "reason": _text(payload.get("reason")),
                "max_grant": "once",
                "hold": HoldAsked.model_validate(payload.get("hold")),
                "recorded_at": request.recorded,
                "expires_at": request.recorded + request.timeout,
                "timed_out": False,  # a hold lasts until it is answered
                "on_timeout": request.on_timeout,
            }
        )
    except ValidationError:
        return None  # a record this listing cannot show is not offered for an answer


def _summary(value: object) -> dict[str, str]:
    """A recorded call summary's text fields, bounded as the card's contract bounds them."""
    if not isinstance(value, dict):
        return {}
    items = [
        (name[:600], text[:600])
        for name, text in cast(dict[object, object], value).items()
        if isinstance(name, str) and isinstance(text, str)
    ]
    return dict(items[:20])


def _packages(request: PermissionRequest, payload: Mapping[str, object]) -> PackagesAsked:
    sources: dict[tuple[str, str | None], PackageSource] = {}
    one = payload.get("package")
    files = payload.get("files")
    items: list[object] = cast(list[object], files) if isinstance(files, list) else []
    if isinstance(one, dict):
        items = [cast(object, one)]
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        ecosystem, channel = entry.get("ecosystem"), entry.get("channel")
        if isinstance(ecosystem, str) and ecosystem:
            source = (ecosystem[:32], channel[:600] if isinstance(channel, str) else None)
            sources.setdefault(source, PackageSource(ecosystem=source[0], channel=source[1]))
    name = cast(dict[str, object], one).get("name") if isinstance(one, dict) else None
    return PackagesAsked(
        sources=list(sources.values())[:64],
        files=len(request.files) if request.key.kind == "packages" else 1,
        asked=request.asked,
        purpose=request.purpose,
        package=_text(name),
    )
