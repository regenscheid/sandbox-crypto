# SPDX-License-Identifier: MIT
"""Child environments (docs/specs/child-environments.md): a delegated run's own sandbox.

A delegation whose project places it in an environment of its own asks the worker for a
child environment as it is admitted. The worker checks the request against its child
profiles, the project's policy and its counts, and refuses it before anything is created
(CE-9). Admitted, it records the child environment's row, then, in the background:

1. creates an environment of kind ``child`` through OpenShell, from the session
   environment's image, policy revision, providers and admitted hosts, placed by the
   child profile (CE-1, CE-3, CE-10);
2. probes isolation: a tier below the request's minimum or the session environment's
   fails it (as JS-11);
3. replays the session's recorded package changes at their recorded versions and checks
   the result against the session's lockfile, as a job sandbox does;
4. copies the request's inputs to ``inputs/`` (CE-7), within the profile's staging bound;

all within the profile's ``schedule_timeout``, past which it fails
``environment_unavailable`` and is released (CE-4). Ready, the delegated run works in it
as in a session environment: the executor, kernels, file tools and recorder are the
worker's ordinary ones. At the delegation's end the orchestrator asks for the hand-back
(CE-6): the worker copies the files it names into the session environment, under
``work/<delegation_id>/``, the delegation's write area (DA-19), where a file the child
wrote in its write area keeps its path, within the job collection's bounds (1,000 files, 100 MiB a
write, a 30-minute deadline, the profile's byte cap), and stores a manifest once. Then
the orchestrator ends it: released after a complete hand-back or a session close, kept
stopped for ``child_retention`` otherwise (CE-4, JS-25's rule).

Locks, outermost first: ``_admitting`` while a request is counted and its row inserted;
a child environment's own lock (``_lock``) while it is prepared, handed back, ended or
swept; and inside that, the environments' per-environment lock that ``Environments``
takes for the child environment's create, stop and delete. The hand-back's copies go
through the execution manager, which takes the session's or the child's environment
lock by itself, one at a time, never while this module holds another environment's.
Ending a child environment cancels its preparation first, so it never waits behind a
creation that may take minutes.

A worker restart: a child environment still being created or prepared fails
(``worker_restarted``) and is released, since its preparation is not resumed; a ready
one is an environment like any other, reattached by its record; a hand-back cut short
goes on from its recorded steps when the orchestrator asks again or ends it, and copies
nothing twice; retained ones are released at their deadline.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import posixpath
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    ListFilesRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.child_environments import (
    HAND_BACK_MANIFEST,
    HAND_BACK_RESERVED,
    ChildEndReason,
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentRequest,
    ChildFailure,
    ChildHandBack,
    HandBackRequest,
    child_environment_id,
    hand_back_path,
    hand_back_reserved,
)
from sandbox_science.contracts.worker import (
    DEFAULT_COLLECT_MAX_BYTES,
    MAX_FILE_WRITE_BYTES,
    MAX_MANIFEST_FILES,
    ManifestFile,
    ReplayedPackages,
    UncollectedFile,
    validate_relative_path,
)
from sandbox_science.policy import effective_resources
from sandbox_science.policy.model import Resources

from .child_records import ACTIVE, ChildRecord
from .environments import Environments, ScheduleTimeoutError
from .errors import WorkerError
from .job_sandboxes import EnvironmentPort, JobFailureError, TooLargeError, read_whole, replay
from .settings import ChildProfile, EnvironmentSettings

if TYPE_CHECKING:
    from .store import Store

_log = logging.getLogger(__name__)

DEFAULT_LIMIT: Final = 2
"""``child_environments_max`` when neither the policy nor the ceiling sets it (CE-9)."""
OPERATION_PREFIX: Final = "chd-"
"""The worker's own operation IDs for a child environment's steps: ``chd-<12 hex>.<step>``."""
RESERVED_OPERATION: Final = re.compile(r"chd-[0-9a-f]{12}\..*")
"""Operation IDs no client may use, so none can take a child environment step's ID first."""
INPUTS: Final = "inputs"
"""Where inputs are copied in the child environment (CE-7)."""
WALL_TIME_GRACE: Final = 3600.0
"""Seconds past its wall time after which the worker ends a ready child environment that
the orchestrator did not end: kept stopped, as after an incomplete hand-back."""
LARGEST_SHOWN: Final = 5
"""The files too large to hand back that a retained child environment's message names,
largest first."""
_FAILURE_KINDS: Final[dict[str, ChildFailure]] = {
    "isolation_insufficient": "isolation_insufficient",
    "package_replay_failed": "package_replay_failed",
    "environment_mismatch": "environment_mismatch",
}


class _Prepared(BaseModel):
    """What preparation found (step ``prepared``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    isolation_tier: int | None = None
    packages: ReplayedPackages = ReplayedPackages()
    inputs: tuple[ManifestFile, ...] = ()
    missing: tuple[str, ...] = ()


class _Planned(BaseModel):
    """The inputs admission found (step ``inputs``): files to copy and paths missing."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    files: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    min_isolation_tier: int | None = None


class _Copied(BaseModel):
    """One file of a hand-back, copied or left out (step ``copy-<n>``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    collected: ManifestFile | None = None
    left: UncollectedFile | None = None
    size: int | None = None
    """The size of a file left out because it was too large, as its first read said."""


def size_text(size: int) -> str:
    """A size in binary units, as a person reads it: ``1.5 GiB``, ``100 MiB``."""
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f}".removesuffix(".0") + f" {unit}"
    return f"{size} bytes"


def too_large(error: TooLargeError) -> str:
    """Why a hand-back left a file out for its size: its size and the limit it passed."""
    size = f"{size_text(error.size)} ({error.size} bytes)"
    if error.per_file:
        return (
            f"{size} is over the {size_text(MAX_FILE_WRITE_BYTES)} ({MAX_FILE_WRITE_BYTES}"
            " bytes) a hand-back copies per file; it stays in the child environment while"
            " that is retained"
        )
    return (
        f"{size} is over the {error.limit} bytes left of the child profile's hand-back"
        " bytes; it stays in the child environment while that is retained"
    )


def largest_left(steps: dict[str, str]) -> str:
    """The files a hand-back left out for their size, largest first, as a clause of the
    retained child environment's message; empty when it left none for that."""
    found: list[tuple[int, str]] = []
    for step, record in steps.items():
        if not step.startswith("copy-"):
            continue
        copied = _Copied.model_validate_json(record)
        if copied.left is not None and copied.size is not None:
            found.append((copied.size, copied.left.path))
    if not found:
        return ""
    found.sort(key=lambda item: (-item[0], item[1]))
    shown = ", ".join(f"{path} ({size_text(size)})" for size, path in found[:LARGEST_SHOWN])
    more = f" and {len(found) - LARGEST_SHOWN} more" if len(found) > LARGEST_SHOWN else ""
    return (
        f"; {len(found)} too large to copy (over {size_text(MAX_FILE_WRITE_BYTES)} a file,"
        f" or past the hand-back's bytes), the largest {shown}{more}"
    )


class _PrepareError(Exception):
    def __init__(self, failure: ChildFailure, message: str) -> None:
        super().__init__(message)
        self.failure: ChildFailure = failure
        self.message = message


_EVENTS: Final[dict[str, str]] = {
    "preparing": "child_environment_created",
    "ready": "child_environment_ready",
    "collecting": "child_environment_collecting",
    "released": "child_environment_released",
    "retained": "child_environment_retained",
}
"""The life-cycle events a phase change logs (CE-11)."""


def limit(resources: Resources) -> int:
    """The child environments a project may have at once (CE-9)."""
    found = resources.child_environments_max
    return DEFAULT_LIMIT if found is None else found


def launch_settings(profile: ChildProfile, resources: Resources) -> str:
    """The child profile's driver configuration, narrowed by the policy's caps (CE-3)."""
    try:
        return profile.driver_json(resources)
    except ValueError as exc:
        raise WorkerError(ErrorKind.POLICY_DENIED, str(exc)) from exc


class ChildEnvironments:
    """Admits, prepares, hands back, retains and releases child environments (CE-1 to CE-12)."""

    def __init__(
        self,
        environments: Environments,
        store: Store,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sweep_seconds: float = 3600.0,
        collect_timeout: float = 1800.0,
    ) -> None:
        self.environments = environments
        self.settings: EnvironmentSettings = environments.settings
        self.store = store
        self.records = store.child_environments
        self.clock = clock
        self.sweep_seconds = sweep_seconds
        """Between sweeps: retained ones past their deadline, ready ones past their wall time."""
        self.collect_timeout = collect_timeout
        """A hand-back's own deadline (JS-15's 30 minutes), from when its copying began."""
        self.port: EnvironmentPort | None = None
        """The worker's executors; set once the manager and provisioner exist."""
        self._admitting = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._preparing: dict[str, asyncio.Task[None]] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # Admission (CE-3, CE-7, CE-9), before anything is created.

    def granted(self, resources: Resources, own: str | None) -> list[str]:
        """The child profiles a project may use (CE-3): the policy's list, as the pool
        offers them; without one, the project's own (or the pool's default) only."""
        names = resources.child_profiles if resources.child_profiles is not None else (own,)
        return [name for name in names if name is not None and name in self.settings.child_profiles]

    def offered(self, resources: Resources) -> bool:
        """Whether a project under ``resources`` may have child environments here."""
        return bool(self.settings.child_profiles) and limit(resources) > 0

    def resources(self, environment_id: str) -> Resources:
        """The effective policy limits of a session environment."""
        governing = self.environments.governing(environment_id)
        if governing is None:
            raise WorkerError(
                ErrorKind.CONFLICT,
                f"environment {environment_id!r} has no ready policy to make a child"
                " environment under",
            )
        return effective_resources(governing.policy.policy, governing.ceiling)[0]

    async def admit(self, request: ChildEnvironmentRequest) -> tuple[ChildEnvironment, bool]:
        """Admit a child environment and start making it; the existing one for the same
        delegation. Refuses with the reason, creating nothing (CE-9)."""
        context = request.context
        existing = self.records.for_delegation(context.project_id, request.delegation_id)
        if existing is not None:
            return self._same(existing, context), False
        session = self.environments.records.get(context.environment_id)
        if (
            session is None
            or session.kind != "session"
            or session.project_id != (context.project_id)
        ):
            raise WorkerError(
                ErrorKind.UNSUPPORTED,
                "child environments start from a session environment the worker created",
            )
        resources = self.resources(context.environment_id)
        allowed = limit(resources)
        if not self.settings.child_profiles:
            raise WorkerError(ErrorKind.UNSUPPORTED, "this pool offers no child profiles")
        if allowed == 0:
            raise WorkerError(
                ErrorKind.POLICY_DENIED,
                "child environments are off for this project (child_environments_max is 0)",
            )
        own = request.project_profile or self.settings.child_default
        name = request.profile or own
        granted = self.granted(resources, own)
        profile = self.settings.child_profiles.get(name) if name is not None else None
        if name is None or profile is None or name not in granted:
            listed = ", ".join(granted) or "none"
            raise WorkerError(
                ErrorKind.POLICY_DENIED,
                f"child profile {name!r} is not granted to this project (granted: {listed})"
                if name is not None
                else f"name a child profile; this pool has no default (granted: {listed})",
            )
        declared = min(profile.isolation_tier, self.environments.isolation_tier)
        if request.min_isolation_tier is not None and declared < request.min_isolation_tier:
            raise WorkerError(
                ErrorKind.POLICY_DENIED,
                f"isolation_insufficient: child profile {name!r} gives isolation tier"
                f" {declared}; tier {request.min_isolation_tier} is required",
            )
        arch = profile.node_selector.get("kubernetes.io/arch")
        recorded = self.environments.architecture(context.environment_id)
        if recorded is not None and recorded != arch:
            raise WorkerError(
                ErrorKind.UNSUPPORTED,
                f"child profile {name!r} runs on {arch}; the session environment's image"
                f" was placed on {recorded}",
            )
        launch_settings(profile, resources)
        planned = await self._plan_inputs(context, request.inputs, profile)
        async with self._admitting:
            # Counted and inserted with no await between, so two admissions cannot
            # both pass the same count.
            existing = self.records.for_delegation(context.project_id, request.delegation_id)
            if existing is not None:
                return self._same(existing, context), False
            active = self.records.listed(phases=ACTIVE)
            project = [row for row in active if row.project_id == context.project_id]
            same = [row for row in active if row.profile == name]
            for count, most, what in (
                (len(project), allowed, "this project's child_environments_max"),
                (len(same), profile.max_concurrent, f"child profile {name!r}'s max_concurrent"),
                (len(active), self.settings.max_child_environments, "the pool's"
                 " max_child_environments"),
            ):  # fmt: skip
                if most is not None and count >= most:
                    # A definite refusal, as a job sandbox's limit is (JS-10): nothing ran.
                    raise WorkerError(
                        ErrorKind.POLICY_DENIED,
                        f"limit_reached: {what} of {most} is reached; child environments"
                        f" alive: {', '.join(row.environment_id for row in project) or 'none'}",
                    )
            now = self.clock.now()
            record = ChildRecord(
                environment_id=child_environment_id(context.project_id),
                delegation_id=request.delegation_id,
                project_id=context.project_id,
                session_id=context.session_id,
                session_environment=context.environment_id,
                profile=name,
                phase="creating",
                failure=None,
                message=None,
                ready_by=now + timedelta(seconds=profile.schedule_timeout),
                expires_at=now + timedelta(seconds=profile.wall_time_max),
                ready_at=None,
                retained_until=None,
                created_at=now,
                updated_at=now,
            )
            self.records.insert(record)
            stored = planned.model_copy(update={"min_isolation_tier": request.min_isolation_tier})
            self.records.record_step(record.environment_id, "inputs", stored.model_dump_json(), now)
        self._spawn(record.environment_id)
        return self.view(record), True

    def _same(self, record: ChildRecord, context: ExecutionContext) -> ChildEnvironment:
        if (record.session_id, record.session_environment) != (
            context.session_id,
            context.environment_id,
        ):
            raise WorkerError(
                ErrorKind.CONFLICT, "the delegation has a child environment of another session"
            )
        return self.view(record)

    async def _plan_inputs(
        self, context: ExecutionContext, paths: tuple[str, ...], profile: ChildProfile
    ) -> _Planned:
        """The input files, and the paths that name nothing; refuses inputs over the
        profile's staging bound, naming the file that crosses it (CE-7)."""
        files: list[tuple[str, int]] = []
        missing: list[str] = []
        for path in dict.fromkeys(paths):
            found = await self._files_of(context, path)
            if found is None:
                missing.append(path)
            else:
                files += found
            if len(files) > MAX_MANIFEST_FILES:
                raise WorkerError(
                    ErrorKind.INVALID_REQUEST,
                    f"inputs_too_large: the inputs hold over {MAX_MANIFEST_FILES} files",
                )
        files = list(dict.fromkeys(files))  # an input inside another named one
        total = 0
        for path, size in files:
            total += size
            if total > profile.stage_max_bytes:
                raise WorkerError(
                    ErrorKind.INVALID_REQUEST,
                    f"inputs_too_large: input {path} brings the inputs to {total} bytes, over"
                    f" the child profile's {profile.stage_max_bytes}",
                )
        return _Planned(files=tuple(path for path, _ in files), missing=tuple(missing))

    async def _files_of(self, context: ExecutionContext, path: str) -> list[tuple[str, int]] | None:
        """The regular files at ``path`` with their sizes, following no symlink; None when
        it names nothing (a brief's input may be text)."""
        port = self._port()
        parent, name = posixpath.split(path)
        try:
            listing = await port.list_files(ListFilesRequest(context=context, path=parent or "."))
        except WorkerError as error:
            if error.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        entry = next((entry for entry in listing.entries if entry.name == name), None)
        if entry is None:
            return None
        if entry.type == "file":
            return [(path, entry.size)]
        if entry.type != "directory":
            raise WorkerError(
                ErrorKind.INVALID_REQUEST,
                f"input {path} is a {entry.type}; inputs are files or directories, and no"
                " symlink is followed",
            )
        found: list[tuple[str, int]] = []
        pending = [path]
        while pending:
            directory = pending.pop()
            listing = await port.list_files(ListFilesRequest(context=context, path=directory))
            if listing.truncated:
                raise WorkerError(
                    ErrorKind.INVALID_REQUEST, f"inputs_too_large: {directory} has too many entries"
                )
            for item in listing.entries:
                child = f"{directory}/{item.name}"
                if item.type == "file":
                    found.append((child, item.size))
                elif item.type == "directory":
                    pending.append(child)
                if len(found) > MAX_MANIFEST_FILES:
                    raise WorkerError(
                        ErrorKind.INVALID_REQUEST,
                        f"inputs_too_large: the inputs hold over {MAX_MANIFEST_FILES} files",
                    )
        return sorted(found)

    # Preparation (CE-1, CE-4, CE-7, CE-10).

    def _spawn(self, environment_id: str) -> None:
        task = asyncio.ensure_future(self._prepare(environment_id))
        self._preparing[environment_id] = task
        task.add_done_callback(lambda _: self._preparing.pop(environment_id, None))

    async def _prepare(self, environment_id: str) -> None:
        async with self._lock(environment_id):
            record = self._record(environment_id)
            if record.phase != "creating":
                return
            try:
                await self._make_ready(record)
            except asyncio.CancelledError:
                raise  # ended while it was made: ``end`` releases it
            except _PrepareError as error:
                await self._fail(environment_id, error.failure, error.message)
            except Exception as exc:
                _log.exception("child environment %s could not be prepared", environment_id)
                await self._fail(
                    environment_id,
                    "environment_unavailable",
                    f"the child environment could not be prepared ({type(exc).__name__})",
                )

    async def _make_ready(self, record: ChildRecord) -> None:
        port = self._port()
        environment_id = record.environment_id
        profile = self.settings.child_profiles.get(record.profile)
        if profile is None:
            raise _PrepareError(
                "environment_unavailable",
                f"the pool no longer offers child profile {record.profile!r}",
            )
        resources = self.resources(record.session_environment)
        remaining = (record.ready_by - self.clock.now()).total_seconds()
        if remaining <= 0:
            raise _PrepareError("environment_unavailable", "its schedule timeout ran out")
        try:
            await self.environments.create_child(
                record.session_environment,
                environment_id,
                record.delegation_id,
                record.session_id,
                launch_settings(profile, resources),
                require_workspace_prefix=profile.require_workspace_prefix,
                schedule_timeout=remaining,
            )
        except ScheduleTimeoutError as error:
            raise _PrepareError("environment_unavailable", error.message) from error
        except WorkerError as error:
            raise _PrepareError(
                "environment_unavailable",
                f"the child environment could not be created: {error.message}",
            ) from error
        self._update(environment_id, phase="preparing")
        session = ExecutionContext(
            project_id=record.project_id,
            session_id=record.session_id,
            environment_id=record.session_environment,
        )
        child = session.model_copy(update={"environment_id": environment_id})
        planned = _Planned.model_validate_json(self.records.steps(environment_id)["inputs"])
        remaining = (record.ready_by - self.clock.now()).total_seconds()
        try:
            async with asyncio.timeout(max(0.0, remaining)):
                location = await port.location(environment_id)
                theirs = await port.location(record.session_environment)
                minimum = max(planned.min_isolation_tier or 0, theirs.isolation_tier or 0)
                if (
                    location.isolation_tier is None
                    or location.isolation_tier < minimum
                    or (location.isolation_tier >= 2 and location.isolation_evidence is None)
                ):
                    # A tier above 1 stands only on the probe's evidence, as a session
                    # environment's does (D43).
                    raise _PrepareError(
                        "isolation_insufficient",
                        f"the child environment runs at isolation tier {location.isolation_tier};"
                        f" the request or the session environment needs {minimum}",
                    )
                packages = await replay(port, _operation(environment_id), session, child)
                inputs = await self._stage(session, child, planned.files, profile)
        except TimeoutError as error:
            raise _PrepareError(
                "environment_unavailable",
                f"it was not ready within the profile's schedule timeout of"
                f" {profile.schedule_timeout:g} s",
            ) from error
        except JobFailureError as error:
            raise _PrepareError(
                _FAILURE_KINDS.get(error.reason, "environment_unavailable"), error.message
            ) from error
        prepared = _Prepared(
            isolation_tier=location.isolation_tier,
            packages=packages,
            inputs=tuple(inputs),
            missing=planned.missing,
        )
        now = self.clock.now()
        self.records.record_step(environment_id, "prepared", prepared.model_dump_json(), now)
        self._update(environment_id, phase="ready", ready_at=now)
        _log.info("child environment %s is ready", environment_id)

    async def _stage(
        self,
        session: ExecutionContext,
        child: ExecutionContext,
        paths: tuple[str, ...],
        profile: ChildProfile,
    ) -> list[ManifestFile]:
        """Copy the inputs to ``inputs/`` in the child environment (CE-7)."""
        port = self._port()
        budget = profile.stage_max_bytes
        staged: list[ManifestFile] = []
        for path in paths:
            target = f"{INPUTS}/{path}"
            try:
                validate_relative_path(target)
                data, digest = await read_whole(port, session, path, budget)
                await port.write_file(
                    WriteFileRequest(
                        context=child, path=target, content_b64=base64.b64encode(data).decode()
                    )
                )
            except TooLargeError as error:
                raise _PrepareError("inputs_too_large", f"input {path}: {error}") from error
            except (ValueError, WorkerError) as error:
                message = error.message if isinstance(error, WorkerError) else str(error)
                raise _PrepareError("staging_failed", f"input {path}: {message}") from error
            budget -= len(data)
            staged.append(ManifestFile(path=target, size=len(data), sha256=digest))
        return staged

    # The hand-back (CE-6).

    async def hand_back(self, environment_id: str, request: HandBackRequest) -> ChildHandBack:
        """Copy the named files into the session's ``work/<delegation_id>/``; the stored
        manifest when it was made already. A hand-back cut short goes on."""
        async with self._lock(environment_id):
            record = self._owned(environment_id, request.context)
            text = request.model_dump_json()
            recorded = self.records.steps(environment_id).get("request")
            if recorded is not None and recorded != text:
                raise WorkerError(
                    ErrorKind.CONFLICT, "another hand-back of this child environment is recorded"
                )
            stored = self.records.hand_back(environment_id)
            if stored is not None:
                # Its copy in the session may be what a restart cut short.
                await self._write_copy(request.context, stored)
                return stored
            if record.phase not in ("ready", "collecting"):
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"the child environment is {record.phase}; only a ready one hands back",
                )
            self.records.record_step(environment_id, "request", text, self.clock.now())
            return await self._collect(record, request)

    async def _collect(self, record: ChildRecord, request: HandBackRequest) -> ChildHandBack:
        environment_id = record.environment_id
        self._update(environment_id, phase="collecting")
        session = request.context
        child = session.model_copy(update={"environment_id": environment_id})
        target = request.target
        paths = list(dict.fromkeys(request.files))
        collected: list[ManifestFile] = []
        left: list[UncollectedFile] = []
        complete = True
        if len(paths) > MAX_MANIFEST_FILES:
            complete = False
            left += [
                UncollectedFile(path=path, reason="over 1,000 files")
                for path in paths[MAX_MANIFEST_FILES : 2 * MAX_MANIFEST_FILES]
            ][:MAX_MANIFEST_FILES]
            paths = paths[:MAX_MANIFEST_FILES]
        now = self.clock.now()
        began = datetime.fromisoformat(
            json.loads(
                self.records.record_step(
                    environment_id, "collecting", json.dumps(now.isoformat()), now
                )
            )
        )
        steps = self.records.steps(environment_id)
        profile = self.settings.child_profiles.get(record.profile)
        budget = profile.collect_max_bytes if profile is not None else DEFAULT_COLLECT_MAX_BYTES

        def apply(copied: _Copied) -> int:
            nonlocal complete
            if copied.collected is not None:
                collected.append(copied.collected)
                return copied.collected.size
            complete = False
            if copied.left is not None and len(left) < MAX_MANIFEST_FILES:
                left.append(copied.left)
            return 0

        pending: list[tuple[int, str]] = []
        for index, path in enumerate(paths):
            recorded = steps.get(f"copy-{index}")
            if recorded is None:
                pending.append((index, path))
            else:
                budget -= apply(_Copied.model_validate_json(recorded))
        remaining = max(0.0, self.collect_timeout - (now - began).total_seconds())
        # A file of the write area keeps its path; another goes below it (DA-19, CE-6). Of
        # two that would land on one path, such as ``x`` and ``work/<id>/x``, the first
        # listed is copied.
        first: dict[str, str] = {}
        for path in paths:
            first.setdefault(hand_back_path(target, path), path)
        try:
            async with asyncio.timeout(remaining):
                for index, path in pending:
                    destination = hand_back_path(target, path)
                    if first[destination] != path:
                        copied = _Copied(
                            left=UncollectedFile(
                                path=path[:4096],
                                reason=f"{first[destination]!r} is handed back to the same "
                                f"path, {destination!r}"[:300],
                            )
                        )
                    elif hand_back_reserved(target, path):
                        copied = _Copied(
                            left=UncollectedFile(
                                path=path[:4096],
                                reason=f"{HAND_BACK_RESERVED}/ is the hand-back's own; no file "
                                "is copied there",
                            )
                        )
                    else:
                        copied = await self._copy(path, destination, budget, session, child)
                    recorded = self.records.record_step(
                        environment_id, f"copy-{index}", copied.model_dump_json(), self.clock.now()
                    )
                    budget -= apply(_Copied.model_validate_json(recorded))
        except TimeoutError:
            complete = False
            left.append(
                UncollectedFile(path=".", reason="the hand-back ran past its 30-minute deadline")
            )
        manifest = ChildHandBack(
            delegation_id=request.delegation_id,
            environment_id=environment_id,
            target=target,
            collected=tuple(collected),
            retained=request.retained,
            not_collected=tuple(left[:MAX_MANIFEST_FILES]),
            complete=complete,
            ended_at=self.clock.now(),
        )
        # The session's copy first, then the record that says the hand-back is made: a
        # restart between them writes the copy again from the recorded steps.
        await self._write_copy(session, manifest)
        self.records.save_hand_back(environment_id, manifest, self.clock.now())
        return self.records.hand_back(environment_id) or manifest

    async def _copy(
        self,
        source: str,
        target: str,
        budget: int,
        session: ExecutionContext,
        child: ExecutionContext,
    ) -> _Copied:
        """Copy one file into the session, or say why it was left out."""

        def leave(reason: str) -> _Copied:
            return _Copied(left=UncollectedFile(path=source[:4096], reason=reason[:300]))

        try:
            validate_relative_path(target)
        except ValueError:
            return leave("its path in the session would be too long")
        try:
            data, digest = await read_whole(self._port(), child, source, budget)
        except TooLargeError as error:
            # Writes into a sandbox take 100 MiB at most, and the worker has no ranged
            # write yet (JS's delivery step 4): the file stays in the child environment,
            # which is retained, and the reason names its size and the limit.
            left = leave(too_large(error))
            return left.model_copy(update={"size": error.size})
        except WorkerError as error:
            return leave(error.message)
        try:
            await self._port().write_file(
                WriteFileRequest(
                    context=session, path=target, content_b64=base64.b64encode(data).decode()
                )
            )
        except WorkerError as error:
            return leave(f"writing it to the session failed: {error.message}")
        return _Copied(collected=ManifestFile(path=target, size=len(data), sha256=digest))

    async def _write_copy(self, session: ExecutionContext, manifest: ChildHandBack) -> None:
        """``work/<delegation_id>/.handback/manifest.json`` in the session: an untrusted copy,
        written whole by one file write."""
        text = manifest.model_dump_json(indent=2).encode()
        try:
            await self._port().write_file(
                WriteFileRequest(
                    context=session,
                    path=f"{manifest.target}/{HAND_BACK_MANIFEST}",
                    content_b64=base64.b64encode(text).decode(),
                )
            )
        except WorkerError as error:
            _log.warning("the hand-back manifest's copy was not written: %s", error.message)

    # The end (CE-4, JS-25).

    async def end(self, environment_id: str, request: ChildEnvironmentEnd) -> ChildEnvironment:
        """Release the child environment, or keep it stopped after an incomplete hand-back.
        Ending one that has ended already answers its record."""
        self._owned(environment_id, request.context)
        await self._stop_preparing(environment_id)
        async with self._lock(environment_id):
            record = self._owned(environment_id, request.context)
            return self.view(await self._end(record, request.reason))

    async def _end(self, record: ChildRecord, reason: ChildEndReason) -> ChildRecord:
        environment_id = record.environment_id
        if record.phase in ("released", "retained"):
            return record
        steps = self.records.steps(environment_id)
        if reason in ("session_closed", "abandoned"):
            self.records.record_step(
                environment_id, "release", json.dumps(reason), self.clock.now()
            )
        closing = "release" in self.records.steps(environment_id)
        manifest = self.records.hand_back(environment_id)
        if record.phase == "collecting" and manifest is None and "request" in steps:
            manifest = await self._collect(
                record, HandBackRequest.model_validate_json(steps["request"])
            )
        if record.phase in ("creating", "preparing"):
            if record.failure is None:
                self._update(
                    environment_id,
                    message="its delegation ended before it was ready",
                )
            return await self._release(environment_id)
        if closing or (manifest is not None and manifest.complete):
            return await self._release(environment_id)
        why = (
            "no hand-back was made before its delegation ended"
            if manifest is None
            else f"its hand-back left {len(manifest.not_collected)} files behind"
            + largest_left(self.records.steps(environment_id))
        )
        return await self._retain(environment_id, why[:1000])

    async def _fail(self, environment_id: str, failure: ChildFailure, message: str) -> None:
        _log.warning("child environment %s failed (%s): %s", environment_id, failure, message)
        self._update(environment_id, failure=failure, message=message[:1000])
        await self._release(environment_id)

    async def _release(self, environment_id: str) -> ChildRecord:
        """Delete the child environment by its recorded identity; kept retained, due at
        once, when the deletion is not confirmed, so the sweep tries again."""
        info = self.environments.records.get(environment_id)
        now = self.clock.now()
        if info is None or info.state is EnvironmentState.DELETED:
            return self._update(environment_id, phase="released")
        try:
            await self.environments.delete(environment_id)
        except WorkerError as error:
            _log.warning("child environment %s was not released: %s", environment_id, error)
            return self._update(environment_id, phase="retained", retained_until=now)
        return self._update(environment_id, phase="released")

    async def _retain(self, environment_id: str, why: str) -> ChildRecord:
        """Stop the child environment and keep it until the retention deadline, releasing
        its project's oldest retained ones beyond its limit."""
        record = self._record(environment_id)
        info = self.environments.records.get(environment_id)
        if info is None or info.state is EnvironmentState.DELETED:
            return self._update(environment_id, phase="released")
        try:
            await self.environments.stop_job(environment_id)
        except WorkerError as error:
            _log.warning("child environment %s did not stop: %s", environment_id, error)
        now = self.clock.now()
        kept = self._update(
            environment_id,
            phase="retained",
            message=why,
            retained_until=now + timedelta(seconds=self.settings.child_retention),
        )
        retained = self.records.listed(phases=frozenset({"retained"}), project_id=record.project_id)
        retained.sort(key=lambda row: (row.updated_at, row.environment_id))
        surplus = max(0, len(retained) - self.settings.child_retention_per_project)
        for row in retained[:surplus]:
            if row.environment_id == environment_id:
                kept = await self._release(environment_id)
                continue
            # Never waits for another child environment's lock while holding this one's:
            # one in use now is released by a later retention or the sweep.
            other = self._locks.setdefault(row.environment_id, asyncio.Lock())
            if other.locked():
                continue
            async with other:
                if self._record(row.environment_id).phase == "retained":
                    await self._release(row.environment_id)
        return kept

    async def close_session(self, context: ExecutionContext) -> list[str]:
        """Release a closing session's child environments without retention (JS-25's
        rule); return their IDs. Retained ones keep their deadline."""
        ended: list[str] = []
        for row in self.records.listed(
            phases=ACTIVE, project_id=context.project_id, session_id=context.session_id
        ):
            if row.session_environment != context.environment_id:
                continue
            await self._stop_preparing(row.environment_id)
            async with self._lock(row.environment_id):
                await self._end(self._record(row.environment_id), "session_closed")
            ended.append(row.environment_id)
        return ended

    async def sweep(self) -> list[str]:
        """Release retained child environments past their deadline, and end those past
        their wall time and its grace that the orchestrator did not end: a hand-back cut
        short is finished, and one never made leaves the environment kept."""
        now = self.clock.now()
        released: list[str] = []
        for row in self.records.listed(phases=frozenset({"retained", "ready", "collecting"})):
            async with self._lock(row.environment_id):
                current = self._record(row.environment_id)
                if current.phase == "retained":
                    due = current.retained_until is not None and current.retained_until <= now
                    if due and (await self._release(row.environment_id)).phase == "released":
                        released.append(row.environment_id)
                elif now >= current.expires_at + timedelta(seconds=WALL_TIME_GRACE):
                    current = self._update(
                        row.environment_id,
                        failure="wall_time",
                        message="its wall time ran out and its delegation did not end it",
                    )
                    await self._end(current, "delegation_ended")
        # Released rows past the retention are pruned: their delegations keep the account.
        self.records.prune(now - timedelta(seconds=self.settings.child_retention))
        return released

    async def start(self) -> None:
        """After a restart: fail and release the child environments a previous worker was
        still making, since their preparation is not resumed; then sweep."""
        for row in self.records.listed(phases=frozenset({"creating", "preparing"})):
            async with self._lock(row.environment_id):
                try:
                    await self._fail(
                        row.environment_id,
                        "worker_restarted",
                        "the worker restarted while it was being made; nothing ran in it",
                    )
                except WorkerError as error:
                    _log.warning(
                        "child environment %s is not reconciled: %s", row.environment_id, error
                    )
        await self.sweep()
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep_forever())

    async def _sweep_forever(self) -> None:
        while True:
            await self.clock.sleep(self.sweep_seconds)
            try:
                await self.sweep()
            except Exception:
                _log.exception("sweeping child environments failed")

    async def close(self) -> None:
        tasks = [*self._preparing.values()]
        if self._sweeper is not None:
            tasks.append(self._sweeper)
            self._sweeper = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # Reading.

    def get(self, environment_id: str, context: ExecutionContext) -> ChildEnvironment:
        return self.view(self._owned(environment_id, context))

    def listed(self, project_id: str, session_id: str) -> list[ChildEnvironment]:
        return [
            self.view(row)
            for row in self.records.listed(project_id=project_id, session_id=session_id)
        ]

    def view(self, record: ChildRecord) -> ChildEnvironment:
        steps = self.records.steps(record.environment_id)
        prepared = (
            _Prepared.model_validate_json(steps["prepared"]) if "prepared" in steps else _Prepared()
        )
        missing = prepared.missing
        if "inputs" in steps and "prepared" not in steps:
            missing = _Planned.model_validate_json(steps["inputs"]).missing
        return ChildEnvironment(
            environment_id=record.environment_id,
            delegation_id=record.delegation_id,
            project_id=record.project_id,
            session_id=record.session_id,
            session_environment=record.session_environment,
            profile=record.profile,
            phase=record.phase,
            failure=record.failure,
            message=record.message,
            isolation_tier=prepared.isolation_tier,
            inputs=prepared.inputs,
            inputs_missing=missing,
            packages=prepared.packages,
            created_at=record.created_at,
            ready_by=record.ready_by,
            ready_at=record.ready_at,
            expires_at=record.expires_at,
            retained_until=record.retained_until,
            hand_back=self.records.hand_back(record.environment_id),
        )

    # Helpers.

    def _update(self, environment_id: str, **changes: Any) -> ChildRecord:
        """Change a row, and log its life-cycle event when its phase changes (CE-11): once
        a transition, with its environment, delegation and session."""
        before = self._record(environment_id)
        after = self.records.update(environment_id, now=self.clock.now(), **changes)
        if after.phase != before.phase:
            event = _EVENTS.get(after.phase)
            if event is not None:
                _log.info(
                    "child environment %s is %s",
                    environment_id,
                    after.phase,
                    extra={
                        "event": event,
                        "environment": environment_id,
                        "delegation": after.delegation_id,
                        "session": after.session_id,
                        "project": after.project_id,
                        "failure": after.failure,
                    },
                )
        return after

    def _port(self) -> EnvironmentPort:
        if self.port is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "child environments are not ready yet")
        return self.port

    def _record(self, environment_id: str) -> ChildRecord:
        record = self.records.get(environment_id)
        if record is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no child environment {environment_id!r}")
        return record

    def _owned(self, environment_id: str, context: ExecutionContext) -> ChildRecord:
        """The row, when the context is its session's; another's is ``not_found``."""
        record = self.records.get(environment_id)
        if record is None or (
            record.project_id,
            record.session_id,
            record.session_environment,
        ) != (context.project_id, context.session_id, context.environment_id):
            raise WorkerError(ErrorKind.NOT_FOUND, f"no child environment {environment_id!r}")
        return record

    async def _stop_preparing(self, environment_id: str) -> None:
        task = self._preparing.get(environment_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @contextlib.asynccontextmanager
    async def _lock(self, environment_id: str) -> AsyncGenerator[None]:
        async with self._locks.setdefault(environment_id, asyncio.Lock()):
            yield


def _operation(environment_id: str) -> str:
    """The prefix of the worker's own operation IDs for a child environment's steps."""
    return OPERATION_PREFIX + environment_id[-12:]


__all__ = [
    "DEFAULT_LIMIT",
    "OPERATION_PREFIX",
    "RESERVED_OPERATION",
    "ChildEnvironments",
    "launch_settings",
    "limit",
]
