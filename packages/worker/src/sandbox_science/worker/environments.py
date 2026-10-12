# SPDX-License-Identifier: MIT
"""Research environments: OpenShell sandboxes the worker creates, revises and deletes (D73).

The worker is trusted (Z1) and holds the gateway's credentials; the orchestrator is
not. So the orchestrator names an environment, a base profile and a policy revision,
and the worker does the rest:

1. It loads the revision from the pool's policy store, where an operator put it
   (``sandbox-science-worker policy add``). The orchestrator cannot add one.
2. It compiles the policy for OpenShell under the pool ceiling, with the sandbox's
   filesystem section.
3. It checks that every provider the policy lists is registered in the gateway with a
   profile that names no endpoints, so that OpenShell binds each credential only
   where the compiled policy says (D57).
4. It creates the sandbox with exactly those providers, and binds the sandbox to the
   revision in the policy middleware's store.

A revision replaces the sandbox's network sections with ``openshell policy set``,
carrying the filesystem section the sandbox reports, then rebinds. Between the two
steps the middleware denies requests as ``policy_stale``, so the change fails closed.

An environment nobody uses can be suspended (W3): ``suspend.Suspender`` stops the
sandbox, which keeps its writable disk, and the next use starts it again through the
backend's ``prepare`` hook. Kernels do not survive; the worker reports the loss.

A sandbox resolves only the host names its compiled policy names. When a policy
reaches hosts it does not name, through a wildcard rule or a non-deny default, the
worker admits them on demand (D69): ``admission.DraftWatcher`` reads the proposals
OpenShell drafts for refused connections, and ``admit`` names each host the policy
could allow a request to in a new compilation of the same revision. The worker
records every decision, and decides the recorded hosts again under each new revision.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
)
from sandbox_science.policy import (
    HardFloor,
    LoadedPolicy,
    PolicyLoadError,
    load_ceiling,
    normalize_host,
)
from sandbox_science.policy.openshell import (
    Compiled,
    CompileError,
    FilesystemPolicy,
    admission_refusal,
    compile_sandbox,
    load_targets,
)
from sandbox_science.policy.store import PolicyStore, StoreError

from .errors import WorkerError
from .provisioner import Governing
from .settings import EnvironmentSettings, OpenShellBackendSettings

_log = logging.getLogger(__name__)


async def _owned[T](work: Awaitable[T]) -> T:
    """Retain lifecycle ownership until actual cleanup ends, despite repeat cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


SANDBOX_PREFIX: Final = "ss-"
MAX_SANDBOX_NAME: Final = 19
"""OpenShell's limit on a sandbox's name (``MAX_ROUTABLE_NAME_LEN``)."""
_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SANDBOX_COMMAND: Final = ("sleep", "infinity")
_POLL_SECONDS: Final = 1.0
_POLICY_TIMEOUT: Final = 120
_RESUME_TIMEOUT: Final = 180.0
_START_BACKOFF: Final = 60.0
_DEAD_PHASES: Final = frozenset({"Error", "Deleting", "Completed"})
"""OpenShell sandbox phases from which no sandbox becomes ready."""
MAX_POLICY_YAML: Final = 320 * 1024
"""The largest compiled policy, as YAML, that admission grows to. OpenShell limits a
sandbox's policy to 256 KiB as protobuf, which is smaller than its YAML."""
MAX_NOTICES: Final = 10
"""Admission notices one result carries at most; the newest are kept."""
MAX_RECORDED: Final = 512
"""Decisions kept per environment: every admitted one, then the newest refusals."""

Release = Callable[[str], Awaitable[None]]
ReleaseIdle = Callable[[str, float], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class Admission:
    """The worker's decision on a host a sandbox could not reach (D69).

    ``admitted`` means the policy could allow some request to the host on this port;
    ``reason`` says why not otherwise.
    """

    host: str
    port: int
    admitted: bool
    reason: str
    decided_at: datetime


def printable(text: str, limit: int = 255) -> str:
    """Text a sandbox chose, safe for records, logs and the reasons OpenShell stores."""
    return "".join(char if char.isprintable() else "?" for char in text[:limit])


def new_sandbox_name() -> str:
    """A fresh OpenShell sandbox name: the prefix and 64 random bits, in 19 characters.

    OpenShell v0.1.3 takes at most 19 characters, a DNS label without the ``--`` it uses
    between the segments of routable names. A name derived from the environment ID left
    room for only 32 bits of a hash, so a client could choose an ID whose name was
    another project's sandbox (review of D78). The record keeps the name.
    """
    return f"{SANDBOX_PREFIX}{secrets.token_hex((MAX_SANDBOX_NAME - len(SANDBOX_PREFIX)) // 2)}"


class OpenShellCli:
    """Runs ``openshell`` commands for the worker, with the backend's CLI environment."""

    def __init__(self, settings: OpenShellBackendSettings, env: dict[str, str]) -> None:
        self.cli = settings.cli
        self.env = env

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        """Run a command and return its output; raise ``unavailable`` if it fails."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.cli,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"cannot run {self.cli}: {exc}") from exc
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} took over {timeout:.0f} s"
            ) from exc
        except asyncio.CancelledError:
            process.kill()
            raise
        if process.returncode != 0:
            text = _ANSI.sub("", (errors or output).decode("utf-8", "replace")).strip()
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} failed: {text[-500:]}"
            )
        return output.decode("utf-8", "replace")

    async def json(self, *args: str) -> object:
        text = await self.run(*args, "-o", "json")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} did not return JSON"
            ) from exc

    async def sandbox(self, name: str) -> dict[str, object] | None:
        """The sandbox's description, or None when the gateway has no sandbox by that name."""
        try:
            document = await self.json("sandbox", "get", name)
        except WorkerError as error:
            if "not found" in error.message:
                return None
            raise
        if not isinstance(document, dict):
            raise WorkerError(ErrorKind.UNAVAILABLE, "openshell sandbox get returned no object")
        return cast(dict[str, object], document)


class EnvironmentRecords:
    """The worker's record of the environments it manages, in its own SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS environments ("
            " environment_id TEXT PRIMARY KEY, sandbox TEXT NOT NULL, document TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS admissions ("
            " environment_id TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,"
            " admitted INTEGER NOT NULL, reason TEXT NOT NULL, decided_at TEXT NOT NULL,"
            " PRIMARY KEY (environment_id, host, port))"
        )
        # No two environments' records name one sandbox.
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS environments_sandbox ON environments (sandbox)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS policy_grants ("
            " project_id TEXT NOT NULL, revision TEXT NOT NULL,"
            " PRIMARY KEY (project_id, revision))"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS confirmed_deletions ("
            " environment_id TEXT PRIMARY KEY, sandbox TEXT NOT NULL, sandbox_id TEXT)"
        )
        self._lock = threading.Lock()

    def confirm_deletion(self, info: EnvironmentInfo) -> None:
        """Remember gateway-confirmed deletion before fallible local cleanup."""
        with self._lock:
            self._db.execute(
                "INSERT INTO confirmed_deletions VALUES (?,?,?) ON CONFLICT(environment_id)"
                " DO UPDATE SET sandbox=excluded.sandbox,sandbox_id=excluded.sandbox_id",
                (info.environment_id, info.sandbox, info.sandbox_id),
            )

    def deletion_confirmed(self, info: EnvironmentInfo) -> bool:
        """A proof belongs only to this exact retained sandbox binding."""
        with self._lock:
            return (
                self._db.execute(
                    "SELECT 1 FROM confirmed_deletions"
                    " WHERE environment_id=? AND sandbox=? AND sandbox_id IS ?",
                    (info.environment_id, info.sandbox, info.sandbox_id),
                ).fetchone()
                is not None
            )

    def get(self, environment_id: str) -> EnvironmentInfo | None:
        with self._lock:
            row = self._db.execute(
                "SELECT document FROM environments WHERE environment_id = ?", (environment_id,)
            ).fetchone()
        return None if row is None else EnvironmentInfo.model_validate_json(row[0])

    def save(self, info: EnvironmentInfo) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO environments (environment_id, sandbox, document) VALUES (?, ?, ?)"
                " ON CONFLICT (environment_id) DO UPDATE SET"
                " sandbox = excluded.sandbox, document = excluded.document",
                (info.environment_id, info.sandbox, info.model_dump_json()),
            )

    def live(self, projects: Iterable[str]) -> list[str]:
        """The IDs of the projects' environments that are not deleted."""
        wanted = set(projects)
        with self._lock:
            rows = self._db.execute("SELECT document FROM environments").fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [
            info.environment_id
            for info in infos
            if info.project_id in wanted and info.state is not EnvironmentState.DELETED
        ]

    def sandbox_taken(self, sandbox: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM environments WHERE sandbox = ?", (sandbox,)
            ).fetchone()
        return row is not None

    def ready(self) -> list[EnvironmentInfo]:
        """The environments whose sandboxes are ready."""
        with self._lock:
            rows = self._db.execute(
                "SELECT document FROM environments ORDER BY environment_id"
            ).fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [info for info in infos if info.state is EnvironmentState.READY]

    def decide(self, environment_id: str, admissions: Iterable[Admission]) -> None:
        """Record decisions on hosts, replacing earlier ones for the same host and port.

        Each is stamped with the time it is recorded, so that a reader of decisions
        made after a time never misses one recorded later.
        """
        with self._lock:
            now = datetime.now(UTC)
            admissions = [replace(admission, decided_at=now) for admission in admissions]
            self._db.executemany(
                "INSERT INTO admissions"
                " (environment_id, host, port, admitted, reason, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (environment_id, host, port) DO UPDATE SET"
                " admitted = excluded.admitted, reason = excluded.reason,"
                " decided_at = excluded.decided_at",
                [
                    (
                        environment_id,
                        admission.host,
                        admission.port,
                        int(admission.admitted),
                        admission.reason,
                        admission.decided_at.isoformat(),
                    )
                    for admission in admissions
                ],
            )
            self._db.execute(
                "DELETE FROM admissions WHERE environment_id = ? AND rowid NOT IN ("
                " SELECT rowid FROM admissions WHERE environment_id = ?"
                " ORDER BY admitted DESC, decided_at DESC LIMIT ?)",
                (environment_id, environment_id, MAX_RECORDED),
            )

    def grant(self, project_id: str, revision: str) -> None:
        """Let clients limited to the project use a policy revision (D78)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO policy_grants (project_id, revision) VALUES (?, ?)"
                " ON CONFLICT DO NOTHING",
                (project_id, revision),
            )

    def granted(self, project_id: str, revision: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM policy_grants WHERE project_id = ? AND revision = ?",
                (project_id, revision),
            ).fetchone()
        return row is not None

    def forget(self, environment_id: str) -> None:
        """Drop an environment's decisions, when its sandbox is deleted."""
        with self._lock:
            self._db.execute("DELETE FROM admissions WHERE environment_id = ?", (environment_id,))

    def decided_since(self, environment_id: str, since: datetime) -> list[Admission]:
        """An environment's decisions made after ``since``, oldest first."""
        return [a for a in self.admissions(environment_id) if a.decided_at > since]

    def admissions(self, environment_id: str) -> list[Admission]:
        """An environment's decisions, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT host, port, admitted, reason, decided_at FROM admissions"
                " WHERE environment_id = ? ORDER BY decided_at, host, port",
                (environment_id,),
            ).fetchall()
        return [
            Admission(
                host=str(host),
                port=int(port),
                admitted=bool(admitted),
                reason=str(reason),
                decided_at=datetime.fromisoformat(str(decided_at)),
            )
            for host, port, admitted, reason, decided_at in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class Environments:
    """Creates, revises and deletes the sandboxes behind logical environments."""

    def __init__(
        self,
        backend: OpenShellBackendSettings,
        settings: EnvironmentSettings,
        state_dir: Path,
        cli_env: dict[str, str],
    ) -> None:
        self.settings = settings
        self.handmade_sandboxes = frozenset(backend.sandboxes)
        self.cli = OpenShellCli(backend, cli_env)
        self.store = PolicyStore(settings.policy_store)
        self.floor = HardFloor(frozenset(settings.admin_hosts))
        try:
            self.targets = load_targets(settings.targets)
            self.ceiling = load_ceiling(settings.ceiling)
        except PolicyLoadError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"environment settings: {exc}") from exc
        self.root = state_dir / "environments"
        self.records = EnvironmentRecords(state_dir / "environments.sqlite3")
        self.release: Release | None = None
        """Stops the environment's executor; the execution manager sets it."""
        self.release_idle: ReleaseIdle | None = None
        """Stops the executor unless the environment was used since a time; the manager sets it."""
        self.deleted: list[Release] = []
        """Told of each environment deleted, by a client or given up by the worker, in
        turn: the shared files' sync forgets it (D79), and the execution manager its
        revisions."""
        self.required_deleted: list[Release] = []
        """Required drains: failure retains the fence and prevents subsequent cleanup."""
        self.confirmed_deleted: list[Release] = []
        """Required cleanup only for gateway-confirmed deletion of the exact binding."""
        self.reusing: list[Release] = []
        """Required original-binding checks before allocating a replacement sandbox."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        """Callers holding or waiting for each environment's lock."""
        self._start_failures: dict[str, tuple[float, str]] = {}

    def close(self) -> None:
        self.records.close()

    def sandbox(self, environment_id: str) -> str | None:
        """The sandbox a ready environment runs in, for the backend."""
        info = self.records.get(environment_id)
        return info.sandbox if info is not None and info.state is EnvironmentState.READY else None

    def manages(self, environment_id: str) -> bool:
        """Whether the worker has ever created the environment, whatever its state now."""
        return self.records.get(environment_id) is not None

    def handmade(self, environment_id: str) -> bool:
        """Whether the settings map the environment to a sandbox made outside the worker."""
        return environment_id in self.handmade_sandboxes

    def busy(self, environment_id: str) -> bool:
        """Whether a change to the environment is under way or waiting to start."""
        return self._users.get(environment_id, 0) > 0

    def get(self, environment_id: str) -> EnvironmentInfo:
        info = self.records.get(environment_id)
        if info is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return info

    def governing(self, environment_id: str) -> Governing | None:
        """What decides the environment's packages, for provisioning: its stored policy
        revision, the pool's ceiling and floor, and its sandbox's ID, by which the policy
        middleware knows it. None for an environment the worker did not create, or one
        that is not ready or stopped."""
        info = self.records.get(environment_id)
        if (
            info is None
            or info.state not in (EnvironmentState.READY, EnvironmentState.STOPPED)
            or info.sandbox_id is None
        ):
            return None
        try:
            policy = self.store.policy(info.policy)
        except StoreError:
            return None
        return Governing(policy, self.ceiling, self.floor, info.sandbox_id)

    @contextlib.asynccontextmanager
    async def _lock(self, environment_id: str) -> AsyncGenerator[None]:
        """Hold the environment's lock, counted from the moment a caller starts waiting."""
        self._users[environment_id] = self._users.get(environment_id, 0) + 1
        try:
            async with self._locks.setdefault(environment_id, asyncio.Lock()):
                yield
        finally:
            self._users[environment_id] -= 1
            if not self._users[environment_id]:
                del self._users[environment_id]

    async def create(self, request: EnvironmentRequest) -> tuple[EnvironmentInfo, bool]:
        """Create the environment's sandbox; return the existing one for the same request."""
        # One answer for both, so that a client cannot tell sandboxes made by hand
        # from other projects' environments.
        taken = WorkerError(
            ErrorKind.CONFLICT,
            f"environment {request.environment_id!r} is taken; choose another ID",
        )
        if self.handmade(request.environment_id):
            raise taken
        async with self._lock(request.environment_id):
            existing = self.records.get(request.environment_id)
            if existing is not None and existing.project_id != request.project_id:
                # An ID stays with the project that first created it, deleted or not, so
                # that nothing checked for one project can reach another's (D78).
                raise taken
            if existing is not None and existing.state is EnvironmentState.DELETED:
                await self._forget(request.environment_id, reuse=True)
            if existing is not None and existing.state in (
                EnvironmentState.READY,
                EnvironmentState.STOPPED,
            ):
                same = (existing.project_id, existing.profile, existing.policy) == (
                    request.project_id,
                    request.profile,
                    request.policy,
                )
                if same:
                    return await self._start(existing), False
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"environment {request.environment_id!r} exists for another project, profile"
                    " or policy; revise its policy or delete it first",
                )
            image = self.settings.profiles.get(request.profile)
            if image is None:
                raise WorkerError(
                    ErrorKind.UNSUPPORTED,
                    f"this pool offers no profile {request.profile!r}"
                    f" (it offers {', '.join(sorted(self.settings.profiles))})",
                )
            policy = self._policy(request.policy)
            compiled = self._compile(policy)
            providers = await self._check_providers(policy)
            name = new_sandbox_name()
            while self.records.sandbox_taken(name):
                name = new_sandbox_name()
            now = datetime.now(UTC)
            info = EnvironmentInfo(
                environment_id=request.environment_id,
                project_id=request.project_id,
                profile=request.profile,
                image=image,
                sandbox=name,
                policy=request.policy,
                providers=providers,
                state=EnvironmentState.CREATING,
                findings=_findings(compiled),
                created_at=now,
                updated_at=now,
            )
            # Recorded first, so that a sandbox left by a crash is known to be ours. Only
            # this record's own leftover is ever deleted: a name is never another's.
            self.records.save(info)
            if existing is not None and existing.state is EnvironmentState.CREATING:
                await self._discard(existing.sandbox)
            try:
                sandbox_id = await self._create_sandbox(name, image, compiled, providers)
                self.store.bind(sandbox_id, request.policy)
            except BaseException:
                await self._discard(name)
                self.records.save(
                    info.model_copy(
                        update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
                    )
                )
                raise
            ready = info.model_copy(
                update={
                    "sandbox_id": sandbox_id,
                    "state": EnvironmentState.READY,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.records.save(ready)
            _log.info("created sandbox %s for environment %s", name, request.environment_id)
            return ready, True

    async def revise(self, environment_id: str, revision: str) -> EnvironmentInfo:
        """Move a ready environment to another policy revision."""
        async with self._lock(environment_id):
            info = await self._start(self.get(environment_id))
            if info.state is not EnvironmentState.READY or info.sandbox_id is None:
                raise WorkerError(
                    ErrorKind.CONFLICT, f"environment {environment_id!r} is {info.state}"
                )
            if info.policy == revision:
                return info
            policy = self._policy(revision)
            # Hosts decided under the old revision are decided again under the new one.
            recorded = [(old.host, old.port) for old in self.records.admissions(environment_id)]
            decisions = await asyncio.to_thread(self._decide, policy, recorded)
            compiled, decisions = await asyncio.to_thread(
                self._settle, policy, info.admitted_hosts, decisions
            )
            admitted = compiled.admitted
            providers = await self._check_providers(policy)
            if providers != info.providers:
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    "the revision attaches other providers than the sandbox has; OpenShell attaches"
                    " providers only when it creates a sandbox, so create a new environment",
                )
            await self._set_policy(info, revision, compiled)
            self.store.bind(info.sandbox_id, revision)
            self.records.decide(environment_id, decisions)
            revised = info.model_copy(
                update={
                    "policy": revision,
                    "findings": _findings(compiled),
                    "admitted_hosts": admitted,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.records.save(revised)
            _log.info("environment %s now runs under %s", environment_id, revision)
            return revised

    async def admit(
        self, environment_id: str, requests: Sequence[tuple[str, int]], *, wait: bool = True
    ) -> list[Admission] | None:
        """Decide hosts a sandbox could not reach, and admit those its policy could allow (D69).

        Each request is the host and port of a refused connection. Admitted hosts the
        sandbox cannot resolve yet are named in one new compilation of the
        environment's policy revision. Every decision is recorded and returned. Without
        ``wait``, nothing is decided while another change to the environment is under
        way or waiting, and the result is None.
        """
        if not wait and self.busy(environment_id):
            return None
        async with self._lock(environment_id):
            info = self.get(environment_id)
            if info.state is not EnvironmentState.READY or info.sandbox_id is None:
                raise WorkerError(
                    ErrorKind.CONFLICT, f"environment {environment_id!r} is {info.state}"
                )
            policy = self._policy(info.policy)
            decisions = await asyncio.to_thread(self._decide, policy, requests)
            compiled, decisions = await asyncio.to_thread(
                self._settle, policy, info.admitted_hosts, decisions
            )
            admitted = compiled.admitted
            if set(admitted) != set(info.admitted_hosts):
                new = sorted(set(admitted) - set(info.admitted_hosts))
                await self._set_policy(info, info.policy, compiled)
                info = info.model_copy(
                    update={
                        "findings": _findings(compiled),
                        "admitted_hosts": admitted,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self.records.save(info)
                _log.info("environment %s admitted %s", environment_id, ", ".join(new) or "none")
            self.records.decide(environment_id, decisions)
            return decisions

    def refuse(
        self, environment_id: str, requests: Sequence[tuple[str, int]], reason: str
    ) -> list[Admission]:
        """Record hosts as refused without deciding them, after admission kept failing."""
        now = datetime.now(UTC)
        decisions = {
            (_name(host), port): Admission(_name(host), port, False, reason, now)
            for host, port in requests
        }
        self.records.decide(environment_id, decisions.values())
        return list(decisions.values())

    async def delete(self, environment_id: str) -> EnvironmentInfo:
        """Stop the environment's executor and delete its sandbox and binding."""
        async with self._lock(environment_id):
            return await _owned(self._delete(environment_id))

    async def _delete(self, environment_id: str) -> EnvironmentInfo:
        info = self.get(environment_id)
        if info.state is EnvironmentState.DELETED and self.records.deletion_confirmed(info):
            await self._forget(environment_id)
            return info
        if not self.records.deletion_confirmed(info):
            document = await self.cli.sandbox(info.sandbox)
            if document is not None and (
                info.sandbox_id is None or document.get("id") != info.sandbox_id
            ):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox identity changed; replacement deletion refused"
                )
            if self.release is not None:
                await self.release(environment_id)
            # Executor shutdown is an await. A fresh identity check precedes the
            # name-addressed gateway mutation; a replacement is never our target.
            document = await self.cli.sandbox(info.sandbox)
            if document is not None:
                if info.sandbox_id is None or document.get("id") != info.sandbox_id:
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE, "sandbox identity changed before deletion"
                    )
                await self.cli.run("sandbox", "delete", info.sandbox)
            if await self.cli.sandbox(info.sandbox) is not None:
                raise WorkerError(ErrorKind.UNAVAILABLE, "sandbox deletion was not confirmed")
            self.records.confirm_deletion(info)
        if info.sandbox_id is not None:
            self.store.unbind(info.sandbox_id)
        self.records.forget(environment_id)
        self._start_failures.pop(environment_id, None)
        deleted = info.model_copy(
            update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
        )
        self.records.save(deleted)
        _log.info("deleted sandbox %s of environment %s", info.sandbox, environment_id)
        await self._forget(environment_id)
        return deleted

    async def _forget(self, environment_id: str, *, reuse: bool = False) -> None:
        """Tell each of ``deleted`` that the environment is deleted."""
        await _owned(self._finish_forget(environment_id, reuse))

    async def _finish_forget(self, environment_id: str, reuse: bool) -> None:
        for told in self.required_deleted:
            await told(environment_id)
        if self.records.deletion_confirmed(self.get(environment_id)):
            for told in self.confirmed_deleted:
                await told(environment_id)
        if reuse:
            for told in self.reusing:
                await told(environment_id)
        for told in self.deleted:
            try:
                await told(environment_id)
            except Exception:
                _log.exception("forgetting environment %s failed", environment_id)

    def notices(self, environment_id: str, since: datetime) -> list[tuple[datetime, str]]:
        """What admission decided for the environment after ``since``, for the agent (D69).

        Each notice comes with the time its decision was recorded. A host admitted
        again for another program is no news, and is left out.
        """
        notices: list[tuple[datetime, str]] = []
        for decision in self.records.decided_since(environment_id, since):
            host = printable(decision.host)
            if decision.admitted and decision.reason == "admitted":
                text = f"{host} was admitted on demand: connections to it work now"
            elif not decision.admitted:
                text = f"{host}:{decision.port} was not admitted: {printable(decision.reason, 300)}"
            else:
                continue
            notices.append((decision.decided_at, text))
        return notices[-MAX_NOTICES:]

    async def suspend(self, environment_id: str, idle_since: float) -> bool:
        """Stop a ready environment's sandbox, unless the environment was used since then.

        ``idle_since`` is a time by the monotonic clock. Returns whether it stopped.
        """
        async with self._lock(environment_id):
            info = self.records.get(environment_id)
            if info is None or info.state is not EnvironmentState.READY:
                return False
            if self.release_idle is not None and not await self.release_idle(
                environment_id, idle_since
            ):
                return False
            # Recorded first: if the stop fails, or the worker stops during it, the next
            # use reads the sandbox's actual phase, and starts it or carries on.
            self.records.save(
                info.model_copy(
                    update={"state": EnvironmentState.STOPPED, "updated_at": datetime.now(UTC)}
                )
            )
            try:
                await self.cli.run("sandbox", "stop", info.sandbox)
            except WorkerError as error:
                _log.warning(
                    "could not stop sandbox %s (%s); its next use checks it",
                    info.sandbox,
                    error.message,
                )
                return False
            _log.info("suspended environment %s (sandbox %s)", environment_id, info.sandbox)
            return True

    async def resume(self, environment_id: str) -> None:
        """Start a stopped environment's sandbox again; the backend calls it before an executor.

        It waits for the environment's lock even when the sandbox looks ready, so that
        a request arriving while a suspension stops the sandbox starts it again after.
        """
        if self.records.get(environment_id) is None:
            return  # a sandbox made by hand, from the static map
        async with self._lock(environment_id):
            info = self.records.get(environment_id)
            if info is not None:
                await self._start(info)

    async def recycle_executor(self, environment_id: str, sandbox: str, sandbox_id: str) -> None:
        """Stop/start the same managed VM to fence a dead daemon's unknown descendants.

        Stop preserves the writable disk and policy binding. No executor-manager
        callback runs under this lifecycle lock: its unused replacement is already
        shut down, and its persisted old endpoint fences admission until we return.
        """
        async with self._lock(environment_id):
            info = self.records.get(environment_id)
            if info is None or info.state not in (EnvironmentState.READY, EnvironmentState.STOPPED):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "executor incarnation lost; recycle the unmanaged sandbox before retrying",
                )
            if (info.sandbox, info.sandbox_id) != (sandbox, sandbox_id):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "environment binding changed; original sandbox cleanup must be confirmed",
                )
            document = await self.cli.sandbox(info.sandbox)
            if document is None or document.get("id") != info.sandbox_id:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "original sandbox is gone or its identity changed"
                )
            await self.cli.run("sandbox", "stop", info.sandbox)
            # A command acknowledgement alone is not proof the old processes stopped.
            try:
                async with asyncio.timeout(min(self.settings.create_timeout, _RESUME_TIMEOUT)):
                    while True:
                        document = await self.cli.sandbox(info.sandbox)
                        if document is None or document.get("id") != info.sandbox_id:
                            raise WorkerError(
                                ErrorKind.UNAVAILABLE,
                                f"sandbox {info.sandbox} changed while stopping",
                            )
                        if document.get("phase") == "Stopped":
                            break
                        if document.get("phase") in _DEAD_PHASES:
                            raise WorkerError(
                                ErrorKind.UNAVAILABLE, "sandbox failed while stopping"
                            )
                        await asyncio.sleep(_POLL_SECONDS)
            except TimeoutError as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox did not confirm stopping; replacement refused"
                ) from exc
            # Record only a confirmed stop: the startup loss check treats STOPPED as
            # proof cleanup happened. A crash before this is recovered from live phase.
            stopped = info.model_copy(
                update={"state": EnvironmentState.STOPPED, "updated_at": datetime.now(UTC)}
            )
            self.records.save(stopped)
            self._start_failures.pop(environment_id, None)
            await self._start(stopped, pinned=True)

    # Steps.

    async def _start(self, info: EnvironmentInfo, *, pinned: bool = False) -> EnvironmentInfo:
        """Start a stopped environment's sandbox; return any other environment unchanged.

        The sandbox's actual phase decides: a stop that failed left it ready. A start
        that fails is not tried again for a while, so that queued requests fail fast.
        """
        if info.state is EnvironmentState.READY:
            # Its sandbox must still be the one the worker created, not another under its name.
            document = await self.cli.sandbox(info.sandbox)
            if pinned and (document is None or document.get("id") != info.sandbox_id):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox identity changed before replacement start"
                )
            if document is None or document.get("id") != info.sandbox_id:
                found = None if document is None else document.get("id")
                await self._abandon(
                    info, f"sandbox {info.sandbox} is {found or 'gone'}, not {info.sandbox_id}"
                )
            if document.get("phase") != "Stopped":
                return info
            # A worker may have stopped after the VM stopped but before persisting
            # STOPPED. Its live phase recovers that gap without starting another VM.
            info = info.model_copy(
                update={"state": EnvironmentState.STOPPED, "updated_at": datetime.now(UTC)}
            )
            self.records.save(info)
        if info.state is not EnvironmentState.STOPPED:
            return info
        failed = self._start_failures.get(info.environment_id)
        if failed is not None and time.monotonic() - failed[0] < _START_BACKOFF:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"sandbox {info.sandbox} failed to start a moment ago ({failed[1]}); "
                "try again later",
            )
        started = datetime.now(UTC)
        timeout = min(self.settings.create_timeout, _RESUME_TIMEOUT)
        try:
            document = await self.cli.sandbox(info.sandbox)
            if pinned and (document is None or document.get("id") != info.sandbox_id):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox identity changed before replacement start"
                )
            if document is None:
                await self._abandon(info, f"sandbox {info.sandbox} is gone")
            elif document.get("id") != info.sandbox_id:
                await self._abandon(info, f"sandbox {info.sandbox} has another identity")
            elif document.get("phase") != "Ready":
                await self.cli.run("sandbox", "start", info.sandbox, timeout=timeout)
            sandbox_id = await self._wait_ready(info.sandbox, timeout)
        except WorkerError as error:
            self._start_failures[info.environment_id] = (time.monotonic(), error.message)
            raise
        self._start_failures.pop(info.environment_id, None)
        if sandbox_id != info.sandbox_id:
            if pinned:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox came back as another; replacement refused"
                )
            # OpenShell keeps a stopped sandbox's identity; another one is not this environment's.
            await self._abandon(
                info, f"sandbox {info.sandbox} came back as {sandbox_id}, not {info.sandbox_id}"
            )
        ready = info.model_copy(
            update={"state": EnvironmentState.READY, "updated_at": datetime.now(UTC)}
        )
        self.records.save(ready)
        seconds = (ready.updated_at - started).total_seconds()
        _log.info("resumed environment %s in %.1f s", info.environment_id, seconds)
        return ready

    async def _abandon(self, info: EnvironmentInfo, reason: str) -> NoReturn:
        """Give up an environment whose sandbox is gone or not its own, so it is created again."""
        # A missing/replaced binding is not authority to stop whatever now has its
        # old name. Preserve the original package gate; only confirmed deletion clears it.
        if info.sandbox_id is not None:
            with contextlib.suppress(StoreError):
                self.store.unbind(info.sandbox_id)
        self.records.forget(info.environment_id)
        self.records.save(
            info.model_copy(
                update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
            )
        )
        # This can be the provisioning task itself, through backend.prepare. It
        # must return to classification rather than wait for its own deletion drain.
        # Keep original revision/lockfile evidence until explicit deletion or reuse.
        raise WorkerError(ErrorKind.UNAVAILABLE, f"{reason}; create the environment again")

    def _policy(self, revision: str) -> LoadedPolicy:
        try:
            return self.store.policy(revision)
        except StoreError as exc:
            raise WorkerError(
                ErrorKind.NOT_FOUND,
                f"{exc}; an operator adds revisions with `sandbox-science-worker policy add`",
            ) from exc

    def _decide(self, policy: LoadedPolicy, requests: Iterable[tuple[str, int]]) -> list[Admission]:
        """Whether the policy could allow a request to each host and port, once each."""
        now = datetime.now(UTC)
        decisions: dict[tuple[str, int], Admission] = {}
        for host, port in requests:
            name = _name(host)
            if (name, port) in decisions:
                continue
            refusal = admission_refusal(
                policy, self.targets.sandbox, name, port, ceiling=self.ceiling, floor=self.floor
            )
            decisions[name, port] = Admission(
                host=name,
                port=port,
                admitted=refusal is None,
                reason="admitted" if refusal is None else refusal,
                decided_at=now,
            )
        return list(decisions.values())

    def _settle(
        self, policy: LoadedPolicy, current: Sequence[str], decisions: Sequence[Admission]
    ) -> tuple[Compiled, list[Admission]]:
        """Compile with the hosts in use and the newly admissible ones, within the limits.

        Hosts in use come first, so a revision or a new batch never displaces them.
        Hosts the compiled policy names already need no endpoint of their own. New
        hosts beyond ``max_admitted_hosts``, or that would make the compiled policy
        too large, are refused.
        """
        limit = self.settings.max_admitted_hosts
        new = [d.host for d in decisions if d.admitted and d.host not in current]
        candidates = list(dict.fromkeys([*current, *new]))
        compiled = self._compile(policy, candidates)
        emitted = set(compiled.admitted)
        keep = [host for host in candidates if host in emitted]
        refusals: dict[str, str] = {}
        if len(keep) > limit:
            for host in keep[limit:]:
                refusals[host] = f"the environment has {limit} hosts admitted on demand, its limit"
            keep = keep[:limit]
        if refusals or len(compiled.policy.to_yaml()) > MAX_POLICY_YAML:
            compiled = self._compile(policy, keep)
            if len(compiled.policy.to_yaml()) > MAX_POLICY_YAML:
                fresh = [host for host in keep if host not in current]
                for host in fresh:
                    refusals[host] = "the sandbox's policy would exceed OpenShell's size limit"
                keep = [host for host in keep if host in current]
                compiled = self._compile(policy, keep)
        settled: list[Admission] = []
        for decision in decisions:
            host = decision.host
            if not decision.admitted:
                pass
            elif host in refusals:
                decision = replace(decision, admitted=False, reason=refusals[host])
            elif host in compiled.admitted:
                if host in current:
                    decision = replace(decision, reason="admitted already")
            elif compiled.names(host):
                decision = replace(decision, reason="admitted already")
            else:
                reason = "the compiler did not admit it; see the environment's findings"
                decision = replace(decision, admitted=False, reason=reason)
            settled.append(decision)
        return compiled, settled

    async def _set_policy(self, info: EnvironmentInfo, revision: str, compiled: Compiled) -> None:
        """Replace the sandbox's network sections, keeping the filesystem section it reports."""
        live = await self._live_filesystem(info.sandbox)
        document = compiled.policy.with_filesystem(live)
        path = self._write(info.environment_id, revision, document.to_yaml())
        await self.cli.run(
            "policy", "set", info.sandbox, "--policy", str(path),
            "--wait", "--timeout", str(_POLICY_TIMEOUT),
            timeout=_POLICY_TIMEOUT + 30,
        )  # fmt: skip

    def _compile(self, policy: LoadedPolicy, admitted: Sequence[str] = ()) -> Compiled:
        try:
            return compile_sandbox(
                policy,
                self.targets.sandbox,
                ceiling=self.ceiling,
                floor=self.floor,
                admitted=admitted,
            )
        except CompileError as exc:
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, f"the policy cannot be compiled: {exc}"
            ) from exc

    async def _check_providers(self, policy: LoadedPolicy) -> tuple[str, ...]:
        """The policy's providers, each registered with a profile that names no endpoints."""
        wanted = policy.policy.credentials.providers
        if not wanted:
            return ()
        listing = await self.cli.json("provider", "list")
        registered = {
            str(entry.get("name")): str(entry.get("type"))
            for entry in _objects(listing, "providers")
        }
        profiles = {
            str(entry.get("id")): entry.get("endpoints")
            for entry in _objects(await self.cli.json("profile", "list"))
        }
        for ref in wanted:
            kind = registered.get(ref.name)
            if kind is None:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, f"provider {ref.name!r} is not registered in the gateway"
                )
            if kind != ref.profile:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"provider {ref.name!r} has profile {kind!r}; the policy says {ref.profile!r}",
                )
            if profiles.get(kind) != []:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"provider profile {kind!r} names endpoints, or is missing; OpenShell would"
                    " bind the credential there as well as where the policy binds it",
                )
        return tuple(sorted(ref.name for ref in wanted))

    async def _create_sandbox(
        self, name: str, image: str, compiled: Compiled, providers: tuple[str, ...]
    ) -> str:
        path = self._write(name, compiled.policy_digest, compiled.policy.to_yaml())
        args = ["sandbox", "create", "--name", name, "--from", image, "--policy", str(path)]
        for provider in providers:
            args += ["--provider", provider]
        args += ["--no-auto-providers", "--detach", "--", *_SANDBOX_COMMAND]
        await self.cli.run(*args, timeout=self.settings.create_timeout)
        return await self._wait_ready(name)

    async def _wait_ready(self, name: str, timeout: float | None = None) -> str:
        """Wait until the sandbox is ready; return its ID. Phases that cannot lead there fail."""
        loop = asyncio.get_running_loop()
        limit = self.settings.create_timeout if timeout is None else timeout
        deadline = loop.time() + limit
        while True:
            document = await self.cli.sandbox(name)
            if document is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {name} vanished")
            phase = document.get("phase")
            if phase in _DEAD_PHASES:
                raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {name} is {phase}")
            admission = document.get("configuration_admission")
            if isinstance(admission, dict):
                state = cast(dict[str, object], admission).get("state")
                if state not in (None, "", "accepted", "pending"):
                    error = cast(dict[str, object], admission).get("error")
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE,
                        f"OpenShell did not accept the policy: {error or state}",
                    )
            sandbox_id = document.get("id")
            if document.get("phase") == "Ready" and isinstance(sandbox_id, str) and sandbox_id:
                return sandbox_id
            if loop.time() > deadline:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, f"sandbox {name} was not ready after {limit:.0f} s"
                )
            await asyncio.sleep(_POLL_SECONDS)

    async def _live_filesystem(self, name: str) -> FilesystemPolicy:
        document = await self.cli.json("policy", "get", name, "--base")
        policy = (
            cast(dict[str, object], document).get("policy") if isinstance(document, dict) else None
        )
        section = (
            cast(dict[str, object], policy).get("filesystem_policy")
            if isinstance(policy, dict)
            else None
        )
        try:
            return FilesystemPolicy.model_validate(section)
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"sandbox {name} reported no usable filesystem section"
            ) from exc

    async def _discard(self, name: str) -> None:
        try:
            await self.cli.run("sandbox", "delete", name)
        except WorkerError as error:
            _log.warning(
                "could not delete sandbox %s after a failed creation: %s", name, error.message
            )

    def _write(self, owner: str, revision: str, text: str) -> Path:
        directory = self.root / owner
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"openshell-{revision.removeprefix('sha256:')[:16]}.yaml"
        path.write_text(text)
        return path


def _name(host: str) -> str:
    """A host as admission records it: normalized, or printable when it is no name at all."""
    try:
        return normalize_host(host)
    except ValueError:
        return printable(host)


def _findings(compiled: Compiled) -> tuple[str, ...]:
    return tuple(
        f"{finding.severity}: {finding.subject}: {finding.message}" for finding in compiled.findings
    )


def _objects(document: object, key: str | None = None) -> list[dict[str, object]]:
    """The JSON objects in a list, or in ``document[key]``; anything else is skipped."""
    items = (
        cast(dict[str, object], document).get(key)
        if key and isinstance(document, dict)
        else document
    )
    if not isinstance(items, list):
        return []
    return [
        cast(dict[str, object], item)
        for item in cast(list[object], items)
        if isinstance(item, dict)
    ]
