# SPDX-License-Identifier: MIT
"""Where a delegated run works: the session's environment, or one of its own
(docs/specs/child-environments.md, CE-2 to CE-7).

A project's ``child_environments`` setting places its delegations: ``off``, the default,
shares the session's environment; ``tasks`` gives every task one of its own, while checks
share; ``all`` gives every delegation one. A task brief may ask to share
(``environment: shared``), which is honoured; asking for its own under ``off`` is
answered with a notice, and the task shares. The worker admits a child environment as
the delegation starts, refusing it at once past the project's or the pool's limits (a
``limit_reached`` that names the limit; nothing is queued, and nothing silently shares).

``ChildPlacement`` is the worker's side for one session: admission, waiting until the
environment is ready, and, at the run's end, the hand-back into the session's
``work/<delegation_id>/`` and the environment's end. The account of that end is what the
delegation's outcome keeps (CE-11).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Capabilities,
    Clock,
    ErrorKind,
    ExecutionContext,
)
from sandbox_science.contracts.child_environments import (
    ChildEndReason,
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentList,
    ChildEnvironmentRequest,
    ChildHandBack,
    HandBackRequest,
    RetainedFile,
    hand_back_target,
)
from sandbox_science.contracts.worker import MAX_MANIFEST_FILES, validate_relative_path
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .records import ChildEnvironments, DelegationRecords, DelegationSettings, held_plan

_log = logging.getLogger(__name__)

Placement = Literal["shared", "own"]
OWN_UNDER_OFF: Final = (
    "This project runs every delegated run in the session's environment "
    "(child_environments is off), so the task shares it as briefed otherwise."
)
NOT_OFFERED: Final = (
    "The worker offers this project no environments of their own (its policy's "
    "child_environments_max is 0, or the pool has no child profiles), so the delegated "
    "run shares the session's environment."
)
WAITING: Final = (
    "The child works in an environment of its own, made from the session's; it starts "
    "once that environment is ready. Its files come back under {target}/ when it ends."
)
UNAVAILABLE: Final = "environment_unavailable"
"""The ``stopped`` of a delegation whose environment never became ready (CE-4)."""
WALL_TIME: Final = "environment_time"
"""The ``stopped`` of a delegated run its environment's wall time ended (CE-9)."""
READY_MARGIN: Final = 120.0
"""Seconds past a child environment's ``ready_by`` that the orchestrator still waits for
the worker's own verdict on it."""
TRIES: Final = 3
"""Tries at a hand-back or an end the worker did not answer, as across a worker restart."""


def placement(
    setting: ChildEnvironments, kind: Literal["check", "task"], asked: Placement | None
) -> tuple[Placement, str | None]:
    """Where a delegation works, and a notice for a request the setting does not grant
    (CE-2). A check never asks: it works on its own only under ``all``."""
    if setting == "off":
        return "shared", OWN_UNDER_OFF if asked == "own" else None
    if asked == "shared":
        return "shared", None
    if kind == "check" and setting != "all":
        return "shared", None
    return "own", None


def file_inputs(inputs: Sequence[str]) -> tuple[str, ...]:
    """The brief's inputs that may name session files (DA-17), to copy into a child
    environment's ``inputs/`` (CE-7): those shaped as a workspace path; the worker lists
    the ones that name nothing. Text with spaces or newlines is not a path."""
    found: list[str] = []
    for text in inputs:
        candidate = text.strip().removeprefix("./")
        if not candidate or any(c.isspace() for c in candidate):
            continue
        try:
            validate_relative_path(candidate)
        except ValueError:
            continue
        if candidate != "." and candidate not in found:
            found.append(candidate)
    return tuple(found[:20])


def hand_back_plan(
    result: Mapping[str, JsonValue] | None,
    recorded: Mapping[str, Mapping[str, object]],
    published: Collection[str],
    files: Mapping[str, JsonValue] | None,
) -> tuple[tuple[str, ...], tuple[RetainedFile, ...]]:
    """What a hand-back copies, and what it names by revision instead (CE-6): the
    result's ``result_files`` and the paths its deliverables give, copied unless the
    child published them (control retains them already); and every file its calls
    recorded whose content the recorder could not retain."""
    copy: list[str] = []
    retained: dict[str, RetainedFile] = {}

    def add(path: str) -> None:
        path = path.removeprefix("./")
        try:
            validate_relative_path(path)
        except ValueError:
            return
        if path in copy or path in retained:
            return
        found = recorded.get(path)
        artifact, revision = (found or {}).get("artifact_id"), (found or {}).get("revision")
        if (
            found is not None
            and path in published
            and found.get("content_retained") is True
            and isinstance(artifact, str)
            and isinstance(revision, int)
            and revision >= 1
        ):
            sha = found.get("sha256")
            retained[path] = RetainedFile(
                path=path,
                artifact_id=artifact,
                revision=revision,
                sha256=sha if isinstance(sha, str) else None,
            )
        else:
            copy.append(path)

    if result is not None:
        declared = result.get("result_files")
        for path in declared if isinstance(declared, list) else []:
            if isinstance(path, str):
                add(path)
        accounted = result.get("deliverables")
        for item in accounted if isinstance(accounted, list) else []:
            if isinstance(item, dict) and isinstance(path := item.get("path"), str):
                add(path)
    listed = files.get("files") if files is not None else None
    for entry in listed if isinstance(listed, list) else []:
        if (
            isinstance(entry, dict)
            and isinstance(path := entry.get("path"), str)
            and entry.get("deleted") is not True
            and entry.get("content_retained") is not True
        ):
            add(path)
    return tuple(copy[: 2 * MAX_MANIFEST_FILES]), tuple(retained.values())[:MAX_MANIFEST_FILES]


class ChildEnvironmentClient(Protocol):
    """The worker client's calls a placement makes."""

    async def capabilities(self, environment_id: str) -> Capabilities: ...

    async def create_child_environment(
        self, request: ChildEnvironmentRequest
    ) -> ChildEnvironment: ...

    async def child_environment(
        self, environment_id: str, context: ExecutionContext
    ) -> ChildEnvironment: ...

    async def child_environments(self, context: ExecutionContext) -> ChildEnvironmentList: ...

    async def hand_back(self, environment_id: str, request: HandBackRequest) -> ChildHandBack: ...

    async def end_child_environment(
        self, environment_id: str, request: ChildEnvironmentEnd
    ) -> ChildEnvironment: ...


@dataclass
class ChildPlacement:
    """A session's child environments, through the worker (CE-1, CE-4, CE-6)."""

    client: ChildEnvironmentClient
    context: ExecutionContext
    """The session's: its environment is the one children's are made from."""
    min_isolation_tier: int | None = None
    clock: Clock = SYSTEM_CLOCK
    poll_seconds: float = 5.0

    async def offered(self) -> bool:
        """Whether the worker offers this project child environments at all: a pool with
        child profiles, under a policy whose ``child_environments_max`` is not 0 (CE-9).
        Without them every delegation shares the session's environment."""
        found = await self.client.capabilities(self.context.environment_id)
        return "child_environments" in found.features

    async def admit(
        self,
        delegation_id: str,
        inputs: Sequence[str],
        profile: str | None = None,
        project_profile: str | None = None,
    ) -> ChildEnvironment:
        """Admit the delegation's own environment, of ``profile`` or the project's
        (``project_profile``, or the pool's default); the worker's refusal as it gave it."""
        return await self.client.create_child_environment(
            ChildEnvironmentRequest(
                context=self.context,
                delegation_id=delegation_id,
                profile=profile,
                project_profile=project_profile,
                inputs=file_inputs(inputs),
                min_isolation_tier=self.min_isolation_tier,
            )
        )

    async def get(self, environment_id: str) -> ChildEnvironment:
        return await self.client.child_environment(environment_id, self.context)

    async def listed(self) -> tuple[ChildEnvironment, ...]:
        return (await self.client.child_environments(self.context)).environments

    async def ready(self, environment_id: str) -> ChildEnvironment:
        """The child environment once the worker says it is ready or has failed; past its
        ``ready_by`` and a margin without a verdict, it is ended as unavailable."""
        while True:
            found = await self.get(environment_id)
            if found.phase not in ("creating", "preparing"):
                return found
            if self.clock.now() > found.ready_by + timedelta(seconds=READY_MARGIN):
                ended = await self.end(environment_id, "abandoned")
                return ended.model_copy(
                    update={
                        "failure": "environment_unavailable",
                        "message": "the worker did not make it ready in time",
                    }
                )
            await self.clock.sleep(self.poll_seconds)

    async def end(self, environment_id: str, reason: ChildEndReason) -> ChildEnvironment:
        return await self._retried(
            lambda: self.client.end_child_environment(
                environment_id, ChildEnvironmentEnd(context=self.context, reason=reason)
            )
        )

    async def close(
        self,
        environment_id: str,
        delegation_id: str,
        plan: tuple[tuple[str, ...], tuple[RetainedFile, ...]] | None,
    ) -> dict[str, JsonValue]:
        """Hand back what ``plan`` names, when the run reached its end, then end the child
        environment; the account the delegation's outcome keeps. Without ``plan``, or when
        the hand-back fails, it is still ended: the worker keeps one whose hand-back was
        not completed, with the reason, and releases one that never became ready (CE-4)."""
        back: ChildHandBack | None = None
        errors: list[str] = []
        if plan is not None:
            files, retained = plan
            request = HandBackRequest(
                context=self.context, delegation_id=delegation_id, files=files, retained=retained
            )
            try:
                back = await self._retried(lambda: self.client.hand_back(environment_id, request))
            except WorkerAPIError as failure:
                _log.warning(
                    "child environment %s handed nothing back: %s", environment_id, failure
                )
                errors.append(f"the hand-back failed: {failure.message}")
        try:
            ended = await self.end(environment_id, "delegation_ended")
        except WorkerAPIError as failure:
            _log.warning("child environment %s was not ended: %s", environment_id, failure)
            errors.append(f"it was not ended: {failure.message}")
            try:
                ended = await self.get(environment_id)
            except WorkerAPIError:
                return {
                    "environment_id": environment_id,
                    "end": "unknown",
                    "message": "; ".join(errors),
                }
        return account(ended, back or ended.hand_back, "; ".join(errors) or None)

    async def _retried[T](self, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(TRIES):
            try:
                return await call()
            except WorkerAPIError as error:
                if error.kind is not ErrorKind.UNAVAILABLE or attempt + 1 == TRIES:
                    raise
                await self.clock.sleep(2.0**attempt)
        raise AssertionError("unreachable")


async def continue_in(
    placement: ChildPlacement,
    previous: str,
    delegation_id: str,
    brief: Mapping[str, JsonValue],
) -> tuple[str, str | None]:
    """Where a resumed delegated run works (CE-12): in ``previous``, its last child
    environment, while it is ready; otherwise in a new one for ``delegation_id``, made as
    the original ``brief``'s was, with its inputs copied again. One whose hand-back a
    crash cut short (``collecting``) first finishes it, and is ended. Returns the
    environment and, for a new one, the one it replaced; raises ``WorkerAPIError`` when
    the new one is refused or never becomes ready."""
    try:
        found = await placement.get(previous)
    except WorkerAPIError as error:
        if error.kind is not ErrorKind.NOT_FOUND:
            raise
        found = None
    if found is not None and found.phase == "ready":
        return previous, None
    if found is not None and found.phase == "collecting":
        # Its run ended and handed back part of its files: the worker finishes the
        # recorded hand-back as it ends it, so they reach the session.
        await placement.end(previous, "delegation_ended")
    inputs, profile = brief.get("inputs"), brief.get("profile")
    named = profile if isinstance(profile, str) else None
    made = await placement.admit(
        delegation_id,
        [str(i) for i in inputs] if isinstance(inputs, list) else [],
        named,
        named,
    )
    ready = await placement.ready(made.environment_id)
    if ready.phase != "ready":
        raise WorkerAPIError(
            ErrorKind.UNAVAILABLE,
            f"environment_unavailable: {ready.message or 'it was not made ready'}",
        )
    return made.environment_id, previous


async def reconcile(
    placement: ChildPlacement,
    records: DelegationRecords,
    settings: DelegationSettings,
) -> list[str]:
    """End the session's child environments nothing will use (CE-4): one no delegation
    names, past ``child_orphan_grace`` from its admission (admitted, then never
    written: the orchestrator stopped in between), released; and one whose delegations
    have all ended, idle past ``child_idle_max`` (or ``child_orphan_idle``) from the last
    end, such as an interrupted run never resumed: released when it was interrupted,
    ended as its delegation otherwise. Their IDs. A worker that cannot say is left
    alone; the session's close and the worker's sweep end them later."""
    context = placement.context
    try:
        found = await placement.listed()
    except WorkerAPIError as error:
        _log.warning("child environments not reconciled: %s", error)
        return []
    now = placement.clock.now()
    idle = timedelta(seconds=settings.child_idle_max or settings.child_orphan_idle)
    ended: list[str] = []
    for environment in found:
        if environment.phase not in ("creating", "preparing", "ready", "collecting"):
            continue
        holders = await threaded(
            records.by_environment,
            context.project_id,
            context.session_id,
            environment.environment_id,
            environment.delegation_id,
        )
        reason: ChildEndReason | None = None
        if any(d.review == "awaiting" or d.ended is None for d in holders):
            # A running task's, or a returned one's that awaits its parent's review, which
            # holds it until it is closed (PD-46).
            continue
        if not holders:
            if now - environment.created_at > timedelta(seconds=settings.child_orphan_grace):
                reason = "abandoned"
        elif all(d.ended_at is not None for d in holders):
            last = max(d.ended_at for d in holders if d.ended_at is not None)
            if now - last > idle:
                interrupted = any(d.ended == "interrupted" for d in holders)
                reason = "abandoned" if interrupted else "delegation_ended"
        if reason is None:
            continue
        # A delegation whose account still plans a hand-back (a reopened task's earlier
        # results, PD-46) gets it before the environment goes (CE-6).
        planned = next(
            (
                (d, plan)
                for d in holders
                if d.environment_id == environment.environment_id
                and (plan := held_plan(d.environment)) is not None
            ),
            None,
        )
        try:
            if planned is not None:
                holder, (files, named) = planned
                account = await placement.close(
                    environment.environment_id,
                    holder.delegation_id,
                    (files, tuple(RetainedFile.model_validate(r) for r in named)),
                )
                await threaded(
                    records.closed_environment,
                    context.project_id,
                    context.session_id,
                    holder.delegation_id,
                    account,
                )
            else:
                await placement.end(environment.environment_id, reason)
        except WorkerAPIError as error:
            _log.warning(
                "child environment %s was not ended: %s", environment.environment_id, error
            )
            continue
        ended.append(environment.environment_id)
    return ended


def account(
    found: ChildEnvironment, back: ChildHandBack | None, error: str | None = None
) -> dict[str, JsonValue]:
    """A child environment's end as a delegation's outcome keeps it (CE-11)."""
    end = (
        "failed"
        if found.failure is not None and found.phase == "released" and found.ready_at is None
        else found.phase
    )
    kept: dict[str, JsonValue] = {
        "environment_id": found.environment_id,
        "profile": found.profile,
        "end": end,
    }
    if found.failure is not None:
        kept["failure"] = found.failure
    if found.message is not None:
        kept["message"] = found.message
    if found.retained_until is not None:
        kept["retained_until"] = found.retained_until.isoformat()
    if found.inputs or found.inputs_missing:
        kept["inputs"] = [f.path for f in found.inputs]
        if found.inputs_missing:
            kept["inputs_missing"] = list(found.inputs_missing)
    if back is not None:
        kept["hand_back"] = {
            "target": back.target,
            "complete": back.complete,
            "copied": [f.path for f in back.collected],
            "retained": [
                {"path": r.path, "artifact_id": r.artifact_id, "revision": r.revision}
                for r in back.retained
            ],
            "left": [{"path": f.path, "reason": f.reason} for f in back.not_collected],
        }
    if error is not None:
        kept["error"] = error
    return kept


def waiting_notice(delegation_id: str) -> str:
    return WAITING.format(target=hand_back_target(delegation_id))


__all__ = [
    "NOT_OFFERED",
    "OWN_UNDER_OFF",
    "UNAVAILABLE",
    "WALL_TIME",
    "ChildEnvironmentClient",
    "ChildPlacement",
    "Placement",
    "account",
    "continue_in",
    "file_inputs",
    "hand_back_plan",
    "placement",
    "reconcile",
    "waiting_notice",
]
