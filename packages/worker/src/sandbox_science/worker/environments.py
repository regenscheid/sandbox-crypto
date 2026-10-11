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
import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

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
from sandbox_science.policy_middleware.errors import StoreError
from sandbox_science.policy_middleware.store import PolicyStore

from .errors import WorkerError
from .settings import EnvironmentSettings, OpenShellBackendSettings

_log = logging.getLogger(__name__)

SANDBOX_PREFIX: Final = "ss-"
_UNSAFE: Final = re.compile(r"[^a-z0-9]+")
_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SANDBOX_COMMAND: Final = ("sleep", "infinity")
_POLL_SECONDS: Final = 1.0
_POLICY_TIMEOUT: Final = 120
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


def sandbox_name(environment_id: str) -> str:
    """A stable OpenShell sandbox name for an environment: lowercase, short and unique."""
    slug = _UNSAFE.sub("-", environment_id.lower()).strip("-")[:40] or "env"
    digest = hashlib.sha256(environment_id.encode()).hexdigest()[:8]
    return f"{SANDBOX_PREFIX}{slug}-{digest}"


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
        self._lock = threading.Lock()

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
        self._locks: dict[str, asyncio.Lock] = {}

    def close(self) -> None:
        self.records.close()

    def sandbox(self, environment_id: str) -> str | None:
        """The sandbox a ready environment runs in, for the backend."""
        info = self.records.get(environment_id)
        return info.sandbox if info is not None and info.state is EnvironmentState.READY else None

    def get(self, environment_id: str) -> EnvironmentInfo:
        info = self.records.get(environment_id)
        if info is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return info

    def _lock(self, environment_id: str) -> asyncio.Lock:
        return self._locks.setdefault(environment_id, asyncio.Lock())

    async def create(self, request: EnvironmentRequest) -> tuple[EnvironmentInfo, bool]:
        """Create the environment's sandbox; return the existing one for the same request."""
        async with self._lock(request.environment_id):
            existing = self.records.get(request.environment_id)
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
            name = sandbox_name(request.environment_id)
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
            # Recorded first, so that a sandbox left by a crash is known to be ours.
            self.records.save(info)
            if existing is not None or await self.cli.sandbox(name) is not None:
                await self.cli.run("sandbox", "delete", name)
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
        self, environment_id: str, requests: Sequence[tuple[str, int]]
    ) -> list[Admission]:
        """Decide hosts a sandbox could not reach, and admit those its policy could allow (D69).

        Each request is the host and port of a refused connection. Admitted hosts the
        sandbox cannot resolve yet are named in one new compilation of the
        environment's policy revision. Every decision is recorded and returned.
        """
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
            info = self.get(environment_id)
            if info.state is EnvironmentState.DELETED:
                return info
            if self.release is not None:
                await self.release(environment_id)
            await self.cli.run("sandbox", "delete", info.sandbox)
            if info.sandbox_id is not None:
                self.store.unbind(info.sandbox_id)
            self.records.forget(environment_id)
            deleted = info.model_copy(
                update={"state": EnvironmentState.DELETED, "updated_at": datetime.now(UTC)}
            )
            self.records.save(deleted)
            _log.info("deleted sandbox %s of environment %s", info.sandbox, environment_id)
            return deleted

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
            await self.cli.run("sandbox", "stop", info.sandbox)
            self.records.save(
                info.model_copy(
                    update={"state": EnvironmentState.STOPPED, "updated_at": datetime.now(UTC)}
                )
            )
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

    # Steps.

    async def _start(self, info: EnvironmentInfo) -> EnvironmentInfo:
        """Start a stopped environment's sandbox; return any other environment unchanged."""
        if info.state is not EnvironmentState.STOPPED:
            return info
        started = datetime.now(UTC)
        await self.cli.run("sandbox", "start", info.sandbox, timeout=self.settings.create_timeout)
        sandbox_id = await self._wait_ready(info.sandbox)
        if sandbox_id != info.sandbox_id:
            # OpenShell keeps a stopped sandbox's identity; another one is not this environment's.
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"sandbox {info.sandbox} came back as {sandbox_id}, not {info.sandbox_id}",
            )
        ready = info.model_copy(
            update={"state": EnvironmentState.READY, "updated_at": datetime.now(UTC)}
        )
        self.records.save(ready)
        seconds = (ready.updated_at - started).total_seconds()
        _log.info("resumed environment %s in %.1f s", info.environment_id, seconds)
        return ready

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

    async def _wait_ready(self, name: str) -> str:
        """Wait until the sandbox is ready; return its ID."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.create_timeout
        while True:
            document = await self.cli.sandbox(name)
            if document is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {name} vanished after creation")
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
                    ErrorKind.UNAVAILABLE,
                    f"sandbox {name} was not ready after {self.settings.create_timeout:.0f} s",
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
