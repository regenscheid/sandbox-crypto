# SPDX-License-Identifier: MIT
"""Read-only provisioning observations for environments in a trusted project registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, TypeVar

from aiohttp import hdrs, web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.contracts.provision_observations import (
    Observation,
    ProvisionDetail,
    ProvisionFilePage,
    ProvisionObservation,
    RevisionProbePage,
    RevisionSummaryPage,
)
from sandbox_science.orchestrator.projects import (
    MAX_SESSION_RECORD_BYTES,
    ProjectError,
    ProjectRecord,
    ProjectStore,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .provisioning_client import MAX_BYTES, ProvisionObserver

MAX_ENVIRONMENTS: Final = 64
MAX_CALLS: Final = 8
MAX_LOGIN_CALLS: Final = 2
CALL_TIMEOUT: Final = 6.0
DIFF_TIMEOUT: Final = 10.0
PATH: Final = "/api/v0/projects/{project}/environments/{environment}/provisioning/observations"
ENVIRONMENTS_PATH: Final = "/api/v0/projects/{project}/provisioning/environments"
ROUTE_PREFIX: Final = "provisioning-observation-"
_IDENTIFIER: Final = TypeAdapter[str](Identifier)
T = TypeVar("T")
Kind = Literal["environments", "summary", "detail", "files", "revisions", "probes", "lockfile-diff"]


class LoginEndedError(Exception):
    """The original authenticated workspace login ended during a read."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class ProjectSnapshot:
    """A bounded project read whose original file and directory remain owned.

    Supported registry mutations replace the record atomically. Retaining its inode
    prevents a complete replacement/restore from hiding behind identical JSON bytes.
    No filesystem or lifecycle lock is held across a worker request.
    """

    def __init__(
        self,
        descriptor: int,
        root: int,
        identity: tuple[int, int, int, int, int],
        digest: bytes,
        record: ProjectRecord,
        environments: tuple[str, ...],
        root_path: Path,
    ) -> None:
        self.record = record
        self.environments = environments
        self._descriptor = descriptor
        self._root = root
        self._identity = identity
        self._digest = digest
        self._root_path = root_path

    def matches(self, other: ProjectSnapshot) -> bool:
        """Whether the same unchanged canonical record still supplies this authority."""
        before, after = os.fstat(self._root), os.fstat(other._root)
        held_matches = (
            (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            and self._identity == other._identity
            and _identity(os.fstat(self._descriptor)) == self._identity
            and _identity(os.fstat(other._descriptor)) == other._identity
            and self._digest == other._digest
            and self.record == other.record
            and self.environments == other.environments
            and self._root_path == other._root_path
        )
        if not held_matches:
            return False
        # Two retained descriptors can agree after their directory has ceased to
        # be the canonical pathname. Prove the pathname's root and leaf as well;
        # keep these temporary descriptors inside the same owned file operation.
        current_root = current_file = -1
        try:
            current_root = os.open(self._root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            name = f"{self.record.project_id}.json"
            current_file = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current_root
            )
            current = os.fstat(current_root)
            current_leaf = os.fstat(current_file)
            leaf = os.stat(name, dir_fd=current_root, follow_symlinks=False)
            canonical = os.stat(self._root_path, follow_symlinks=False)
            return (
                (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino)
                and (canonical.st_dev, canonical.st_ino) == (before.st_dev, before.st_ino)
                and stat.S_ISDIR(canonical.st_mode)
                and stat.S_ISREG(leaf.st_mode)
                and _identity(current_leaf) == self._identity
                and _identity(leaf) == self._identity
            )
        finally:
            if current_file >= 0:
                os.close(current_file)
            if current_root >= 0:
                os.close(current_root)

    def close(self) -> None:
        """Release the retained descriptors after their actual owner has drained."""
        os.close(self._descriptor)
        os.close(self._root)


def project_snapshot(store: ProjectStore, project: str) -> ProjectSnapshot:
    """Read one canonical registry file without following links or opening devices.

    The one-MiB preflight and cap+1 read precede JSON parsing. This does not make a
    blocked regular-file read interruptible; the async owner must retain its thread,
    descriptors and admission until it really finishes.
    """
    try:
        _IDENTIFIER.validate_python(project, strict=True)
    except ValidationError as error:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid project ID") from error
    root = descriptor = -1
    try:
        root_path = store.root
        root = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            f"{project}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= MAX_SESSION_RECORD_BYTES:
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry is not a bounded record")
        data = bytearray()
        while len(data) <= MAX_SESSION_RECORD_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_SESSION_RECORD_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != before.st_size or _identity(os.fstat(descriptor)) != _identity(before):
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry changed during read")
        record = ProjectRecord.model_validate_json(data, strict=True)
        if record.project_id != project:
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry has inconsistent scope")
        environments = {record.default_environment_id}
        for key, session in record.sessions.items():
            if key != session.session_id or (
                not session.dedicated and session.environment_id != record.default_environment_id
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "project registry has inconsistent scope")
            times = (
                (session.opened_at,)
                if session.closed_at is None
                else (
                    session.opened_at,
                    session.closed_at,
                )
            )
            if any(value.tzinfo is None or value.utcoffset() is None for value in times) or (
                session.closed_at is not None and session.closed_at < session.opened_at
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "project session times are not coherent")
            environments.add(session.environment_id)
            if len(environments) > MAX_ENVIRONMENTS:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "project environment list exceeds its bound"
                )
        if (
            any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (record.created_at, record.updated_at)
            )
            or record.updated_at < record.created_at
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry times are not coherent")
        result = ProjectSnapshot(
            descriptor,
            root,
            _identity(before),
            hashlib.sha256(data).digest(),
            record,
            tuple(sorted(environments)),
            root_path,
        )
        descriptor = root = -1
        return result
    except FileNotFoundError:
        raise ProjectError(ErrorKind.NOT_FOUND, "no such recorded project") from None
    except (OSError, ValidationError, ValueError, RecursionError) as error:
        raise ProjectError(ErrorKind.UNAVAILABLE, "project registry is unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if root >= 0:
            os.close(root)


@dataclass(frozen=True)
class Selection:
    """A finite route selection; browser input cannot supply a worker path/context."""

    kind: Kind
    operation: str | None = None
    seq: int | None = None
    revision: int | None = None
    pin: str | None = None
    after: int = -1
    target: int | None = None
    offset: int = 0
    limit: int = 8
    before: int | None = None
    before_pin: str | None = None
    after_pin: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "environments",
            "summary",
            "detail",
            "files",
            "revisions",
            "probes",
            "lockfile-diff",
        }:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation selection")
        if self.kind in {"detail", "files"}:
            try:
                _IDENTIFIER.validate_python(self.operation, strict=True)
            except ValidationError as error:
                raise ProjectError(
                    ErrorKind.INVALID_REQUEST, "invalid operation selection"
                ) from error
            if (
                self.seq is not None and (type(self.seq) is not int or not 0 <= self.seq <= 2**62)
            ) or (self.kind == "files" and self.seq is None):
                raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid operation sequence")
        if self.kind == "probes" and (
            type(self.revision) is not int
            or not 0 <= self.revision <= 2**62
            or type(self.pin) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.pin) is None
        ):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid revision pin selection")
        if (
            type(self.after) is not int
            or not -1 <= self.after <= 2**62
            or (
                self.target is not None
                and (type(self.target) is not int or not 0 <= self.target <= 2**62)
            )
            or type(self.offset) is not int
            or not 0 <= self.offset <= 2**62
            or type(self.limit) is not int
            or not 1 <= self.limit <= 8
        ):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation page selection")
        if self.kind == "lockfile-diff" and (
            type(self.before) is not int
            or not 0 <= self.before <= 2**62
            or self.after < 0
            or any(
                type(pin) is not str or re.fullmatch(r"[0-9a-f]{64}", pin) is None
                for pin in (self.before_pin, self.after_pin)
            )
            or self.offset > 80_000
        ):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid exact lockfile pair selection")


def _number(raw: str, minimum: int, maximum: int = 2**62) -> int:
    if len(raw) > 19 or re.fullmatch(r"(?:0|[1-9][0-9]*|-1)", raw) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation selection counter")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "observation selection counter exceeds its bound"
        )
    return value


def _selection(request: web.Request, kind: Kind) -> Selection:
    allowed: set[str] = {
        "environments": set[str](),
        "summary": set[str](),
        "detail": {"seq"},
        "files": {"seq", "offset", "limit"},
        "revisions": {"after", "limit", "target"},
        "probes": {"pin", "offset", "limit"},
        "lockfile-diff": {"before", "before_pin", "after", "after_pin", "offset", "limit"},
    }[kind]
    query = request.query
    if request.can_read_body or len(query) != len(set(query)) or set(query) - allowed:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "observation accepts only its selected scope")
    operation = request.match_info.get("operation")
    try:
        if operation is not None:
            _IDENTIFIER.validate_python(operation, strict=True)
    except ValidationError as error:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "invalid provisioning operation ID"
        ) from error
    if kind == "files" and "seq" not in query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "operation selection requires its exact seq")
    if kind == "lockfile-diff" and not {"before", "before_pin", "after", "after_pin"} <= set(query):
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "comparison requires both exact revision pins"
        )
    pin = query.get("pin")
    if kind == "probes" and (pin is None or re.fullmatch(r"[0-9a-f]{64}", pin) is None):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "revision selection requires its exact pin")
    return Selection(
        kind,
        operation=operation,
        seq=_number(query["seq"], 0) if "seq" in query else None,
        revision=_number(request.match_info["revision"], 0)
        if "revision" in request.match_info
        else None,
        pin=pin,
        after=_number(query.get("after", "-1"), 0 if kind == "lockfile-diff" else -1),
        target=_number(query["target"], 0) if "target" in query else None,
        offset=_number(query.get("offset", "0"), 0, 80_000 if kind == "lockfile-diff" else 2**62),
        limit=_number(query.get("limit", "8"), 1, 8),
        before=_number(query["before"], 0) if "before" in query else None,
        before_pin=query.get("before_pin"),
        after_pin=query.get("after_pin"),
    )


async def _fetch(
    observer: ProvisionObserver, project: str, environment: str, wanted: Selection
) -> Observation:
    path = f"/v1/environments/{environment}/provisioning/observations"
    params = {"project_id": project}
    result: Observation
    match wanted.kind:
        case "summary":
            result = await observer.get(path, params, ProvisionObservation)
        case "detail":
            result = await observer.get(
                f"{path}/operations/{wanted.operation}",
                {**params, **({"seq": str(wanted.seq)} if wanted.seq is not None else {})},
                ProvisionDetail,
            )
            if result.summary.provision_id != wanted.operation or (
                wanted.seq is not None and result.summary.seq != wanted.seq
            ):
                raise WorkerAPIError(
                    ErrorKind.UNAVAILABLE, "worker returned another operation selection"
                )
        case "files":
            result = await observer.get(
                f"{path}/operations/{wanted.operation}/files",
                {
                    **params,
                    "seq": str(wanted.seq),
                    "offset": str(wanted.offset),
                    "limit": str(wanted.limit),
                },
                ProvisionFilePage,
            )
            if (result.provision_id, result.seq, result.offset, result.limit) != (
                wanted.operation,
                wanted.seq,
                wanted.offset,
                wanted.limit,
            ):
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another file page")
        case "revisions":
            result = await observer.get(
                f"{path}/revisions",
                {
                    **params,
                    "after": str(wanted.after),
                    "limit": str(wanted.limit),
                    **({"target": str(wanted.target)} if wanted.target is not None else {}),
                },
                RevisionSummaryPage,
            )
            if (result.after, result.limit) != (wanted.after, wanted.limit) or (
                wanted.target is not None and result.target != wanted.target
            ):
                raise WorkerAPIError(
                    ErrorKind.UNAVAILABLE, "worker returned another revision window"
                )
        case "probes":
            result = await observer.get(
                f"{path}/revisions/{wanted.revision}/probes",
                {
                    **params,
                    "pin": str(wanted.pin),
                    "offset": str(wanted.offset),
                    "limit": str(wanted.limit),
                },
                RevisionProbePage,
            )
            if (result.revision, result.record_sha256, result.offset, result.limit) != (
                wanted.revision,
                wanted.pin,
                wanted.offset,
                wanted.limit,
            ):
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another probe page")
        case "lockfile-diff":
            result = await observer.get_diff(
                f"{path}/lockfile-diff",
                {
                    **params,
                    "before": str(wanted.before),
                    "before_pin": str(wanted.before_pin),
                    "after": str(wanted.after),
                    "after_pin": str(wanted.after_pin),
                    "offset": str(wanted.offset),
                    "limit": str(wanted.limit),
                },
            )
            if (
                result.before.revision,
                result.before.record_sha256,
                result.after.revision,
                result.after.record_sha256,
                result.offset,
                result.limit,
            ) != (
                wanted.before,
                wanted.before_pin,
                wanted.after,
                wanted.after_pin,
                wanted.offset,
                wanted.limit,
            ):
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another lockfile pair")
        case _:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation selection")
    if (result.project_id, result.environment_id) != (project, environment):
        raise WorkerAPIError(
            ErrorKind.UNAVAILABLE, "worker returned another project or environment"
        )
    return result


class ProvisionProxy:
    """Immediate bounded admission; actual file/request work remains owned until drain."""

    def __init__(self, store: ProjectStore, worker: WorkerClient | None) -> None:
        self.store, self.worker = store, worker
        self._files = ThreadPoolExecutor(max_workers=2, thread_name_prefix="provisioning-registry")
        self._tasks: set[asyncio.Task[str]] = set()
        self._scopes: set[tuple[str, str | None]] = set()
        self._logins: dict[str, int] = {}
        self._closing = self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def _file(self, call: Callable[[], T]) -> T:
        future = asyncio.get_running_loop().run_in_executor(self._files, call)
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
                if future.done():
                    try:
                        value = future.result()
                    except BaseException:
                        raise asyncio.CancelledError from None
                    break
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            if isinstance(value, ProjectSnapshot):
                await self._file(value.close)
            raise asyncio.CancelledError
        return value

    async def _read(
        self,
        project: str,
        environment: str | None,
        wanted: Selection,
        authorized: Callable[[], bool],
    ) -> str:
        if not authorized():
            raise LoginEndedError
        snapshot = await self._file(lambda: project_snapshot(self.store, project))
        try:
            if not authorized():
                raise LoginEndedError
            worker = self.worker
            observer: ProvisionObserver | None = None
            if wanted.kind == "environments":
                value = {
                    "project_id": project,
                    "default_environment_id": snapshot.record.default_environment_id,
                    "environment_ids": snapshot.environments,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
                answer = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            else:
                if environment not in snapshot.environments:
                    raise ProjectError(
                        ErrorKind.NOT_FOUND, "no recorded environment in this project"
                    )
                if worker is None:
                    raise ProjectError(
                        ErrorKind.UNSUPPORTED, "provisioning views need a configured worker"
                    )
                observer = ProvisionObserver(worker)
                assert environment is not None  # noqa: S101 - recorded membership above
                answer = (await _fetch(observer, project, environment, wanted)).model_dump_json()
            if not authorized():
                raise LoginEndedError
            fresh = await self._file(lambda: project_snapshot(self.store, project))
            try:
                matches = await self._file(lambda: snapshot.matches(fresh))
            except OSError as error:
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "project registry proof is unavailable"
                ) from error
            finally:
                await self._file(fresh.close)
            if not authorized():
                raise LoginEndedError
            if (
                not matches
                or self._closing
                or self.worker is not worker
                or (observer is not None and not observer.current())
            ):
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "observation binding changed during the read"
                )
            if len(answer.encode("utf-8")) > MAX_BYTES:
                raise ProjectError(ErrorKind.UNAVAILABLE, "workspace observation exceeds its bound")
            return answer
        except WorkerAPIError as error:
            raise ProjectError(error.kind, error.message) from error
        finally:
            await self._file(snapshot.close)

    async def read(
        self,
        project: str,
        environment: str | None,
        wanted: Selection,
        principal: str,
        authorized: Callable[[], bool],
    ) -> str:
        """Respond within six seconds without releasing still-running owned work."""
        if type(wanted) is not Selection:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation selection")
        wanted.__post_init__()
        try:
            _IDENTIFIER.validate_python(project, strict=True)
            if environment is not None:
                _IDENTIFIER.validate_python(environment, strict=True)
        except ValidationError as error:
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation scope") from error
        scope = (project, environment)
        if (
            self._closing
            or len(self._tasks) >= MAX_CALLS
            or scope in self._scopes
            or self._logins.get(principal, 0) >= MAX_LOGIN_CALLS
        ):
            raise ProjectError(
                ErrorKind.UNAVAILABLE, "provisioning observation capacity is unavailable"
            )
        self._scopes.add(scope)
        self._logins[principal] = self._logins.get(principal, 0) + 1
        task = asyncio.create_task(self._read(project, environment, wanted, authorized))
        self._tasks.add(task)

        def finished(done: asyncio.Task[str]) -> None:
            self._tasks.discard(done)
            self._scopes.discard(scope)
            remaining = self._logins[principal] - 1
            if remaining:
                self._logins[principal] = remaining
            else:
                del self._logins[principal]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            async with asyncio.timeout(
                DIFF_TIMEOUT if wanted.kind == "lockfile-diff" else CALL_TIMEOUT
            ):
                return await asyncio.shield(task)
        except TimeoutError as error:
            task.cancel()
            raise ProjectError(
                ErrorKind.UNAVAILABLE, "provisioning observation timed out"
            ) from error
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _drain(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._files.shutdown(wait=True)
        self._closed = True

    async def close(self) -> None:
        """Drain actual owners through repeated cancellation before shared client close."""
        self._closing = True
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


PROXY: Final = web.AppKey("provisioning_observations", ProvisionProxy)


def add_routes(
    app: web.Application,
    store: ProjectStore,
    worker: WorkerClient | None,
    principal: Callable[[web.Request], str],
    authorized: Callable[[web.Request], bool],
) -> None:
    """Register only authenticated read projections; no action routes are added."""
    proxy = ProvisionProxy(store, worker)
    app[PROXY] = proxy

    def handler(kind: Kind) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def observed(request: web.Request) -> web.Response:
            try:
                answer = await proxy.read(
                    request.match_info["project"],
                    request.match_info.get("environment"),
                    _selection(request, kind),
                    principal(request),
                    lambda: authorized(request),
                )
                if not authorized(request):
                    raise LoginEndedError
                return web.Response(
                    text=answer,
                    content_type="application/json",
                    headers={hdrs.CACHE_CONTROL: "no-store"},
                )
            except LoginEndedError:
                status, error_kind, message = (
                    401,
                    ErrorKind.POLICY_DENIED,
                    "the workspace login has ended",
                )
            except ProjectError as error:
                error_kind, message = error.kind, error.message
                status = {
                    ErrorKind.INVALID_REQUEST: 400,
                    ErrorKind.NOT_FOUND: 404,
                    ErrorKind.CONFLICT: 409,
                    ErrorKind.UNSUPPORTED: 422,
                }.get(error.kind, 503)
                if not authorized(request):
                    status, error_kind, message = (
                        401,
                        ErrorKind.POLICY_DENIED,
                        "the workspace login has ended",
                    )
            return web.json_response(
                {
                    "error": {"kind": error_kind.value, "message": message},
                    "observed_at": datetime.now(UTC).isoformat(),
                },
                status=status,
                headers={hdrs.CACHE_CONTROL: "no-store"},
            )

        return observed

    routes: tuple[tuple[Kind, str], ...] = (
        ("environments", ENVIRONMENTS_PATH),
        ("summary", PATH),
        ("detail", PATH + "/operations/{operation}"),
        ("files", PATH + "/operations/{operation}/files"),
        ("revisions", PATH + "/revisions"),
        ("probes", PATH + "/revisions/{revision}/probes"),
        ("lockfile-diff", PATH + "/lockfile-diff"),
    )
    for kind, path in routes:
        app.router.add_get(path, handler(kind), name=ROUTE_PREFIX + kind)
