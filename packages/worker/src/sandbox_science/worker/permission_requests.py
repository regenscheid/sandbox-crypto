# SPDX-License-Identifier: MIT
"""Network and package permission requests, listed and answered for the workspace (D97).

The policy middleware records research code's escalated HTTP and git requests, and the
worker its provisioning operations' file sets, in one event store. An operator client
(the workspace) lists the unanswered ones of a project's sandboxes, with what their cards
show, and relays the user's answers to them. Requests of other projects' sandboxes are
not found, and a request no environment of the worker owns is no one's. A download from
the package cache is a package request, answered for that file only: a ``host`` answer
would cover the whole cache.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo
from sandbox_science.contracts.permission_requests import (
    MAX_LISTED,
    HttpSummary,
    PackagesAsked,
    PackageSource,
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

_SCOPES = {"network": ("request", "host"), "packages": ("request", "files")}


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
            if request.key.kind in ("http", "packages")
            and (owner := owners.get(str(request.key.fields()["sandbox_id"]))) is not None
            and owner.project_id == project_id
        ][:MAX_LISTED]
        payloads = self.permissions.escalations(request.seq for request in mine)
        listed: list[WorkerPermissionRequest] = []
        for request in mine:
            owner = owners[str(request.key.fields()["sandbox_id"])]
            found = _described(request, payloads.get(request.seq, {}), owner, now)
            if found is not None:
                listed.append(found)
        return listed

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
        owner = (
            self._owners().get(str(request.key.fields().get("sandbox_id", "")))
            if request is not None
            else None
        )
        if request is None or owner is None or not allows(owner.project_id):
            raise PermissionRequestError(f"no permission request {seq}", missing=True)
        if request.key.kind not in ("http", "packages"):
            raise PermissionRequestError(f"permission request {seq} is not answered here")
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
