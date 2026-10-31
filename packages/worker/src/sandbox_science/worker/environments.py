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
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
)
from sandbox_science.contracts.child_environments import is_child_environment
from sandbox_science.contracts.environment_claims import EnvironmentClaim, EnvironmentClaimResult
from sandbox_science.policy import (
    HardFloor,
    LoadedPolicy,
    PolicyLoadError,
    effective_resources,
    load_ceiling,
    normalize_host,
)
from sandbox_science.policy.model import Resources
from sandbox_science.policy.openshell import (
    Compiled,
    CompileError,
    FilesystemPolicy,
    LandlockPolicy,
    admission_entry,
    admission_refusal,
    compile_sandbox,
    load_targets,
)
from sandbox_science.policy.store import PolicyStore, StoreError

from .checkpoint_records import HeldError, begin_start, end_start
from .claims import EnvironmentClaims
from .errors import WorkerError
from .kubernetes import check_pinned_policy
from .provisioner import Governing
from .recovery_fence import check_dispatch
from .settings import EnvironmentSettings, OpenShellBackendSettings
from .timeouts import seconds
from .unknown_creations import (
    CREATION_ROLLED_BACK,
    ENVIRONMENT_LABEL,
    LIST_PAGE_SIZE,
    MAX_LIST_PAGES,
    MAX_OBSERVATIONS,
    PROJECT_LABEL,
    UNKNOWN_OUTCOME,
    Observation,
    Outcome,
    Reconciliation,
    confirms,
    examine,
    label_value,
    labels,
    observation,
    page,
    refusal,
    when,
)

if TYPE_CHECKING:
    from .store import Store

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
OPENSHELL_SEPARATOR: Final = "--"
"""What OpenShell puts between the segments of a routable name: a sandbox's Kubernetes
objects are ``<OpenShell workspace>--<sandbox name>``. A sandbox name never contains it."""
CREATION_UNKNOWN: Final = "environment_creation_unknown"
"""The log event of a creation still unknown past the grace (B3, D103)."""
MAX_SANDBOX_NAME: Final = 19
"""OpenShell's limit on a sandbox's name (``MAX_ROUTABLE_NAME_LEN``)."""
_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FRAME: Final = re.compile(r"[\s\u2500-\u257f\u00d7]+")
"""Whitespace and the box drawing miette frames and wraps a report with."""
_MISSING: Final = re.compile(r'(?:message|self): "sandbox not found"')
"""The gateway's NotFound for a sandbox name, as tonic shows a status: ``message:`` in
the OpenShell 0.1.3 labs, ``self:`` from tonic 0.14 (OpenShell 0.1.5). Other NotFound
answers, such as a missing service endpoint, gateway or file, do not match."""
_SANDBOX_COMMAND: Final = ("sleep", "infinity")
_POLICY_TIMEOUT: Final = 120
_DEAD_PHASES: Final = frozenset({"Error", "Deleting", "Completed"})
"""OpenShell sandbox phases from which no sandbox becomes ready."""
MAX_POLICY_YAML: Final = 320 * 1024
"""The largest compiled policy, as YAML, that admission grows to. OpenShell limits a
sandbox's policy to 256 KiB as protobuf, which is smaller than its YAML."""
MAX_NOTICES: Final = 10
"""Admission notices one result carries at most; the newest are kept."""
MAX_RECORDED: Final = 512
"""Decisions kept per environment: every admitted one, then the newest refusals."""

JOB_ENVIRONMENT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,109}-job-[0-9a-f]{12}$")
"""A job sandbox's environment ID (D93, JS-1): ``<project>-job-<12 hex>``. No client may
create an environment by such an ID."""


def job_environment_id(project_id: str) -> str:
    """A new job environment ID for the project, within the 128 characters of an ID."""
    return f"{project_id[:110]}-job-{secrets.token_hex(6)}"


class NotReadyError(WorkerError):
    """A sandbox that was still not ready when its time to become ready ran out."""


class ScheduleTimeoutError(WorkerError):
    """A job sandbox found no node with room within its profile's schedule timeout (JS-23)."""


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

    OpenShell v0.1.5 takes at most 19 characters, a DNS label without the ``--``
    (``OPENSHELL_SEPARATOR``) it uses between the segments of routable names, so the
    sandbox is what follows the last ``--`` of its Kubernetes objects' names (the
    operator's cleanup reads them so). A name derived from the environment ID left
    room for only 32 bits of a hash, so a client could choose an ID whose name was
    another project's sandbox (review of D78). The record keeps the name.
    """
    return f"{SANDBOX_PREFIX}{secrets.token_hex((MAX_SANDBOX_NAME - len(SANDBOX_PREFIX)) // 2)}"


class SandboxMissingError(WorkerError):
    """The gateway answered that no sandbox has the name: proof of absence, unlike any
    other failure, which says nothing about whether the sandbox exists."""


class OpenShellCli:
    """Runs ``openshell`` commands for the worker, with the backend's CLI environment."""

    def __init__(self, settings: OpenShellBackendSettings, env: dict[str, str]) -> None:
        self.cli = settings.cli
        self.env = env

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        """Run a command and return its output; raise ``unavailable`` if it fails."""
        check_dispatch()
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
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await _owned(process.wait())
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {' '.join(args[:2])} took over {timeout:.0f} s"
            ) from exc
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            # Reap even an already-exited process. Later cancellation cannot
            # abandon this wait or replace the original caller interruption.
            with contextlib.suppress(asyncio.CancelledError):
                await _owned(process.wait())
            raise
        if process.returncode != 0:
            text = _ANSI.sub("", (errors or output).decode("utf-8", "replace")).strip()
            # Classify the whole report: the status leads it, and the end is what is kept.
            missing = _MISSING.search(_FRAME.sub(" ", text)) is not None
            error = SandboxMissingError if missing else WorkerError
            raise error(
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
        except SandboxMissingError:
            return None
        if not isinstance(document, dict):
            raise WorkerError(ErrorKind.UNAVAILABLE, "openshell sandbox get returned no object")
        return cast(dict[str, object], document)

    async def sandboxes(self) -> tuple[list[dict[str, object]], bool]:
        """Every sandbox the gateway lists, page after page up to ``MAX_LIST_PAGES``, and
        whether the listing ended there. An answer the worker cannot read raises."""
        entries: list[dict[str, object]] = []
        token = ""
        for _ in range(MAX_LIST_PAGES):
            args = ["sandbox", "list", "--page-size", str(LIST_PAGE_SIZE)]
            if token:
                args += ["--page-token", token]
            read = page(await self.json(*args))
            if read is None:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "openshell sandbox list returned no sandbox listing"
                )
            entries += read[0]
            token = read[1]
            if not token:
                return entries, True
        return entries, False


class EnvironmentRecords:
    """The worker's record of the environments it manages, in its own SQLite file."""

    def __init__(self, path: Path, clock: Clock = SYSTEM_CLOCK) -> None:
        self.clock = clock
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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sandbox_launch_settings ("
            " sandbox TEXT PRIMARY KEY, driver_config TEXT NOT NULL)"
        )
        # Old allocations retain their original behavior; current config never upgrades
        # a recovered sandbox's storage promise.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(sandbox_launch_settings)")}
        if "require_workspace_prefix" not in columns:
            self._db.execute(
                "ALTER TABLE sandbox_launch_settings ADD COLUMN require_workspace_prefix"
                " INTEGER NOT NULL DEFAULT 0 CHECK (require_workspace_prefix IN (0,1))"
            )
        # Reads that found no trace of a creation with an unknown outcome, and the
        # environments marked deleted on that evidence (B3). Created when missing, so
        # opening an older file adds them and opening it again changes nothing.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS creation_observations ("
            " environment_id TEXT NOT NULL, sandbox TEXT NOT NULL, observed_at TEXT NOT NULL,"
            " listed INTEGER NOT NULL, names TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS creation_reconciliations ("
            " environment_id TEXT NOT NULL, sandbox TEXT NOT NULL, reason TEXT NOT NULL,"
            " reconciled_at TEXT NOT NULL, observations TEXT NOT NULL,"
            " hooks_pending INTEGER NOT NULL DEFAULT 0 CHECK (hooks_pending IN (0,1)),"
            " PRIMARY KEY (environment_id, sandbox))"
        )
        # When each sandbox's create command ended, whatever its result: the grace of a
        # creation with an unknown outcome counts from then (B3).
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS creation_commands ("
            " environment_id TEXT NOT NULL, sandbox TEXT NOT NULL, ended_at TEXT NOT NULL,"
            " PRIMARY KEY (environment_id, sandbox))"
        )
        self._lock = threading.Lock()

    def observe(self, info: EnvironmentInfo, seen: Observation) -> tuple[Observation, ...]:
        """Record a read that found no trace of the environment's original sandbox; return
        the recorded ones for that name, oldest first, the newest ``MAX_OBSERVATIONS``."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO creation_observations VALUES (?,?,?,?,?)",
                    (
                        info.environment_id,
                        info.sandbox,
                        seen.observed_at.isoformat(),
                        seen.listed,
                        json.dumps(list(seen.names)),
                    ),
                )
                self._db.execute(
                    "DELETE FROM creation_observations WHERE environment_id=? AND sandbox=?"
                    " AND rowid NOT IN (SELECT rowid FROM creation_observations"
                    " WHERE environment_id=? AND sandbox=? ORDER BY rowid DESC LIMIT ?)",
                    (info.environment_id, info.sandbox) * 2 + (MAX_OBSERVATIONS,),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self.observations(info)

    def observations(self, info: EnvironmentInfo) -> tuple[Observation, ...]:
        """The recorded absences of the environment's original sandbox, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT observed_at, listed, names FROM creation_observations"
                " WHERE environment_id=? AND sandbox=? ORDER BY rowid",
                (info.environment_id, info.sandbox),
            ).fetchall()
        return tuple(
            Observation(datetime.fromisoformat(str(at)), int(listed), tuple(json.loads(names)))
            for at, listed, names in rows
        )

    def create_ended(self, info: EnvironmentInfo, at: datetime) -> None:
        """Record that the create command of the environment's sandbox has ended."""
        with self._lock:
            self._db.execute(
                "INSERT INTO creation_commands VALUES (?,?,?) ON CONFLICT"
                " (environment_id, sandbox) DO UPDATE SET ended_at=excluded.ended_at",
                (info.environment_id, info.sandbox, at.isoformat()),
            )

    def creation_ended(self, info: EnvironmentInfo) -> datetime | None:
        """When the create command of the environment's sandbox ended; None when no end
        is recorded, as when the worker stopped while it ran."""
        with self._lock:
            row = self._db.execute(
                "SELECT ended_at FROM creation_commands WHERE environment_id=? AND sandbox=?",
                (info.environment_id, info.sandbox),
            ).fetchone()
        return None if row is None else datetime.fromisoformat(str(row[0]))

    def identified(self) -> list[EnvironmentInfo]:
        """The environments, of every kind, that are not deleted and have a sandbox
        identity: those a complete sandbox listing must show."""
        with self._lock:
            rows = self._db.execute("SELECT document FROM environments").fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [
            info
            for info in infos
            if info.state is not EnvironmentState.DELETED and info.sandbox_id is not None
        ]

    def hooks_pending(self) -> list[tuple[str, str]]:
        """Creations rolled back with the worker stopped, whose deletion hooks the next
        running worker owes: (environment ID, sandbox name)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT environment_id, sandbox FROM creation_reconciliations"
                " WHERE hooks_pending = 1 ORDER BY reconciled_at"
            ).fetchall()
        return [(str(environment_id), str(sandbox)) for environment_id, sandbox in rows]

    def hooks_done(self, environment_id: str, sandbox: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE creation_reconciliations SET hooks_pending = 0"
                " WHERE environment_id=? AND sandbox=?",
                (environment_id, sandbox),
            )

    def roll_back(
        self, info: EnvironmentInfo, reason: str, at: datetime, *, hooks_pending: bool = False
    ) -> None:
        """Record why a creation is deleted without a sandbox, with its evidence, and
        confirm its deletion, in one transaction. ``hooks_pending`` says that the
        deletion hooks did not run here, and the running worker owes them."""
        evidence = json.dumps(
            [
                {"observed_at": o.observed_at.isoformat(), "listed": o.listed, "names": o.names}
                for o in self.observations(info)
            ]
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO creation_reconciliations VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT (environment_id, sandbox) DO UPDATE SET reason=excluded.reason,"
                    " reconciled_at=excluded.reconciled_at, observations=excluded.observations,"
                    " hooks_pending=excluded.hooks_pending",
                    (
                        info.environment_id,
                        info.sandbox,
                        reason,
                        at.isoformat(),
                        evidence,
                        int(hooks_pending),
                    ),
                )
                self._db.execute(
                    "INSERT INTO confirmed_deletions VALUES (?,?,?) ON CONFLICT(environment_id)"
                    " DO UPDATE SET sandbox=excluded.sandbox,sandbox_id=excluded.sandbox_id",
                    (info.environment_id, info.sandbox, info.sandbox_id),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def reconciliation(self, environment_id: str) -> tuple[str, str, datetime, int] | None:
        """The latest creation of the environment deleted without a sandbox: its sandbox
        name, the reason, when, and how many recorded reads found it absent."""
        with self._lock:
            row = self._db.execute(
                "SELECT sandbox, reason, reconciled_at, observations FROM creation_reconciliations"
                " WHERE environment_id=? ORDER BY reconciled_at DESC LIMIT 1",
                (environment_id,),
            ).fetchone()
        if row is None:
            return None
        sandbox, reason, at, evidence = row
        count = len(cast(list[object], json.loads(evidence)))
        return str(sandbox), str(reason), datetime.fromisoformat(str(at)), count

    def creating(self) -> list[EnvironmentInfo]:
        """The environments still being created, of every kind."""
        with self._lock:
            rows = self._db.execute(
                "SELECT document FROM environments ORDER BY environment_id"
            ).fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [info for info in infos if info.state is EnvironmentState.CREATING]

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

    def save(
        self,
        info: EnvironmentInfo,
        *,
        driver_config: str | None = None,
        require_workspace_prefix: bool = False,
    ) -> None:
        """Save ownership and, at initial allocation, immutable trusted launch settings."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if driver_config is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO sandbox_launch_settings VALUES (?,?,?)",
                        (info.sandbox, driver_config, int(require_workspace_prefix)),
                    )
                    row = self._db.execute(
                        "SELECT driver_config,require_workspace_prefix FROM sandbox_launch_settings"
                        " WHERE sandbox=?",
                        (info.sandbox,),
                    ).fetchone()
                    if row != (driver_config, int(require_workspace_prefix)):
                        raise WorkerError(ErrorKind.CONFLICT, "sandbox launch settings changed")
                self._db.execute(
                    "INSERT INTO environments (environment_id, sandbox, document) VALUES (?, ?, ?)"
                    " ON CONFLICT (environment_id) DO UPDATE SET"
                    " sandbox = excluded.sandbox, document = excluded.document",
                    (info.environment_id, info.sandbox, info.model_dump_json()),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def driver_config(self, sandbox: str) -> str | None:
        """Original allocation settings; legacy allocations inherit gateway defaults."""
        with self._lock:
            row = self._db.execute(
                "SELECT driver_config FROM sandbox_launch_settings WHERE sandbox=?", (sandbox,)
            ).fetchone()
        return None if row is None else str(row[0])

    def requires_workspace_prefix(self, sandbox: str) -> bool:
        """The original storage requirement, false for allocations predating the pin."""
        with self._lock:
            row = self._db.execute(
                "SELECT require_workspace_prefix FROM sandbox_launch_settings WHERE sandbox=?",
                (sandbox,),
            ).fetchone()
        return row == (1,)

    def live(self, projects: Iterable[str]) -> list[str]:
        """The IDs of the projects' session environments that are not deleted. Job
        sandboxes count against the pool's job limits instead (JS-9)."""
        wanted = set(projects)
        with self._lock:
            rows = self._db.execute("SELECT document FROM environments").fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [
            info.environment_id
            for info in infos
            if info.project_id in wanted
            and info.state is not EnvironmentState.DELETED
            and info.kind == "session"
        ]

    def listed(self, projects: Iterable[str] | None) -> list[EnvironmentInfo]:
        """The environments that are not deleted, of ``projects`` or of every project."""
        wanted = None if projects is None else set(projects)
        with self._lock:
            rows = self._db.execute(
                "SELECT document FROM environments ORDER BY environment_id"
            ).fetchall()
        infos = [EnvironmentInfo.model_validate_json(row[0]) for row in rows]
        return [
            info
            for info in infos
            if (wanted is None or info.project_id in wanted)
            and info.state is not EnvironmentState.DELETED
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
            now = self.clock.now()
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

    def latest_grant(self, project_id: str) -> str | None:
        """The policy revision granted to the project last, if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT revision FROM policy_grants WHERE project_id = ? ORDER BY rowid DESC"
                " LIMIT 1",
                (project_id,),
            ).fetchone()
        return cast(str, row[0]) if row is not None else None

    def granted(self, project_id: str, revision: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM policy_grants WHERE project_id = ? AND revision = ?",
                (project_id, revision),
            ).fetchone()
        return row is not None

    def forget(self, environment_id: str) -> None:
        """Drop an environment's decisions and creation reads, when its sandbox is deleted."""
        with self._lock:
            self._db.execute("DELETE FROM admissions WHERE environment_id = ?", (environment_id,))
            self._db.execute(
                "DELETE FROM creation_observations WHERE environment_id = ?", (environment_id,)
            )

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
        *,
        clock: Clock = SYSTEM_CLOCK,
        poll_seconds: float = 1.0,
        resume_timeout: float = 180.0,
        start_backoff: float = 60.0,
    ) -> None:
        self.settings = settings
        self.clock = clock
        """Stamps the environments' records and times the backoff after a failed start."""
        self.poll_seconds = seconds("poll_seconds", poll_seconds)
        """Between looks at a sandbox that is starting or stopping."""
        self.resume_timeout = seconds("resume_timeout", resume_timeout)
        """The longest a stopped sandbox may take to start or stop, within ``create_timeout``."""
        self.start_backoff = seconds("start_backoff", start_backoff)
        """After a failed start, uses fail at once for this long instead of starting again."""
        self.handmade_sandboxes = frozenset(backend.sandboxes)
        self.isolation_tier = backend.isolation_tier
        """The tier the gateway's driver declares; a job sandbox's probe can only lower it."""
        self.cli = OpenShellCli(backend, cli_env)
        self.store = PolicyStore(settings.policy_store)
        self.floor = HardFloor(frozenset(settings.admin_hosts))
        try:
            self.targets = load_targets(settings.targets)
            self.ceiling = load_ceiling(settings.ceiling)
        except PolicyLoadError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"environment settings: {exc}") from exc
        self.root = state_dir / "environments"
        self.state_dir = state_dir
        self.records = EnvironmentRecords(state_dir / "environments.sqlite3", clock)
        self.claims = EnvironmentClaims(state_dir / "environment-claims.sqlite3", clock=clock)
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
        self.required_deleting: list[Release] = []
        """Required original-binding proof before any explicit deletion mutation."""
        self.reusing: list[Release] = []
        """Required original-binding checks before allocating a replacement sandbox."""
        self.given_up: list[Callable[[str], None]] = []
        """Told at once of each environment the worker gives up because its sandbox went
        or was replaced; they must not block: the execution manager ends its job
        sandboxes in the background (JS-25)."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        """Callers holding or waiting for each environment's lock."""
        self._start_failures: dict[str, tuple[float, str]] = {}
        self.serving_since: datetime | None = None
        """When the running worker started serving; set by ``serve``. A creation whose
        command's end is not recorded (the worker stopped during it) counts as ended
        then, since the stopped worker's commands ended with it. None elsewhere, where
        such a creation is refused."""
        self.hooks_deferred = False
        """Set by the offline command, whose deletion hooks are the running worker's:
        a creation rolled back here records them as pending (``run_pending_hooks``)."""
        self._absent_here: dict[tuple[str, str], tuple[datetime, float]] = {}
        """This process's first read finding each creation absent: wall and monotonic time."""

    def close(self) -> None:
        self.records.close()
        self.claims.close()

    async def claim(self, request: EnvironmentClaim) -> EnvironmentClaimResult:
        """Retain a ready environment for active research, within bounded expiry."""
        context = request.context
        async with self._lock(context.environment_id):
            check_dispatch()
            info = self.get(context.environment_id)
            if info.project_id != context.project_id:
                raise WorkerError(ErrorKind.NOT_FOUND, "no environment in this project")
            if request.seconds == 0:
                self.claims.release(request)
                return EnvironmentClaimResult(
                    environment_id=info.environment_id,
                    state="released",
                    sandbox_id=request.sandbox_id,
                )
            if info.state is not EnvironmentState.READY or info.sandbox_id is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, "only a ready sandbox can be retained")
            if request.sandbox_id is not None and request.sandbox_id != info.sandbox_id:
                raise WorkerError(ErrorKind.CONFLICT, "environment claim's sandbox was replaced")
            if context.policy_revision is not None and context.policy_revision != info.policy:
                raise WorkerError(ErrorKind.POLICY_DENIED, "environment claim's policy is stale")
            return EnvironmentClaimResult(
                environment_id=info.environment_id,
                state="held",
                sandbox_id=info.sandbox_id,
                expires_at=self.claims.grant(request, info),
            )

    def protected(self, environment_id: str) -> bool:
        """Whether active research protects this environment against idle suspension."""
        return self.claims.held(self.get(environment_id))

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

    def daemon_identity(self, environment_id: str) -> tuple[str, str] | None:
        """The sandbox a managed environment is bound to, by name and ID; None for an
        environment the worker did not create, or one without a sandbox ID yet."""
        if not self.manages(environment_id):
            return None
        info = self.get(environment_id)
        return (info.sandbox, info.sandbox_id) if info.sandbox_id else None

    def requires_workspace_prefix(self, environment_id: str) -> bool:
        """Required storage is pinned at allocation, never inferred from today's profile."""
        return self.manages(environment_id) and self.records.requires_workspace_prefix(
            self.get(environment_id).sandbox
        )

    def job_resources(self, environment_id: str) -> Resources:
        """The job limits of the environment's policy, within the pool's ceiling."""
        if self.busy(environment_id):
            raise WorkerError(ErrorKind.UNAVAILABLE, "the environment is changing")
        governing = self.governing(environment_id)
        if governing is None:
            if self.handmade(environment_id):
                return self.ceiling.resources
            raise WorkerError(ErrorKind.POLICY_DENIED, "no ready environment policy for jobs")
        return effective_resources(governing.policy.policy, governing.ceiling)[0]

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
        if JOB_ENVIRONMENT.fullmatch(request.environment_id):
            raise WorkerError(
                ErrorKind.INVALID_REQUEST,
                f"environment IDs ending in -job- and 12 hex digits are the worker's own, for"
                f" job sandboxes; choose another than {request.environment_id!r}",
            )
        if is_child_environment(request.environment_id):
            raise WorkerError(
                ErrorKind.INVALID_REQUEST,
                f"environment IDs ending in -child- and 12 hex digits are the worker's own,"
                f" for child environments; choose another than {request.environment_id!r}",
            )
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
            placement = self.settings.kubernetes.get(request.profile)
            try:
                launch = (
                    placement.driver_json(effective_resources(policy.policy, self.ceiling)[0])
                    if placement is not None
                    else "{}"
                )
            except ValueError as exc:
                raise WorkerError(ErrorKind.POLICY_DENIED, str(exc)) from exc
            providers = await self._check_providers(policy)
            if existing is not None and existing.state is EnvironmentState.CREATING:
                # Validate the replacement request before changing the old allocation.
                # Never overwrite the only ownership record for a possibly live VM.
                await _owned(self._cleanup_creation(existing))
                await self._forget(request.environment_id, reuse=True)
            name = new_sandbox_name()
            while self.records.sandbox_taken(name):
                name = new_sandbox_name()
            now = self.clock.now()
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
            self.records.save(
                info,
                driver_config=launch,
                require_workspace_prefix=placement.require_workspace_prefix if placement else False,
            )
            try:
                sandbox_id = await self._create_sandbox(info, compiled)
                self.store.bind(sandbox_id, request.policy)
            except BaseException:
                try:
                    await _owned(self._cleanup_creation(self.get(request.environment_id)))
                except Exception as error:
                    _log.warning("creation cleanup remains pending for sandbox %s: %s", name, error)
                raise
            ready = info.model_copy(
                update={
                    "sandbox_id": sandbox_id,
                    "state": EnvironmentState.READY,
                    "updated_at": self.clock.now(),
                }
            )
            self.records.save(ready)
            _log.info("created sandbox %s for environment %s", name, request.environment_id)
            return ready, True

    def architecture(self, environment_id: str) -> str | None:
        """The ``kubernetes.io/arch`` an environment's sandbox was pinned to, if any."""
        launch = self.records.driver_config(self.get(environment_id).sandbox)
        if launch in (None, "{}"):
            return None
        try:
            document: object = json.loads(launch)
        except json.JSONDecodeError:
            return None
        for key in ("kubernetes", "pod", "node_selector", "kubernetes.io/arch"):
            if not isinstance(document, dict):
                return None
            document = cast(dict[str, object], document).get(key)
        return document if isinstance(document, str) else None

    async def create_job(
        self,
        session_environment: str,
        environment_id: str,
        job_id: str,
        launch: str,
        *,
        require_workspace_prefix: bool,
        schedule_timeout: float,
    ) -> EnvironmentInfo:
        """Create a job sandbox (D93, JS-1, JS-3, JS-14): a new environment of kind ``job``
        for one job, from the session environment's recorded image, policy revision and
        providers, seeded with its admitted hosts. ``launch`` is the job profile's driver
        configuration. Recorded before creation, so a crash leaves a sandbox known to be
        the job's; a sandbox not placed within ``schedule_timeout`` raises
        ``ScheduleTimeoutError`` and is deleted by identity, as any failed creation is."""
        if JOB_ENVIRONMENT.fullmatch(environment_id) is None:
            raise WorkerError(ErrorKind.INTERNAL, f"{environment_id!r} is no job environment ID")
        return await self._create_derived(
            session_environment,
            environment_id,
            {"kind": "job", "job_id": job_id},
            launch,
            require_workspace_prefix=require_workspace_prefix,
            schedule_timeout=schedule_timeout,
        )

    async def create_child(
        self,
        session_environment: str,
        environment_id: str,
        delegation_id: str,
        session_id: str,
        launch: str,
        *,
        require_workspace_prefix: bool,
        schedule_timeout: float,
    ) -> EnvironmentInfo:
        """Create a child environment (CE-1, CE-3, CE-10): a new environment of kind
        ``child`` for one delegated run, made as a job sandbox is, from the session
        environment's image, policy revision, providers and admitted hosts. ``launch`` is
        the child profile's driver configuration, narrowed by the policy's caps."""
        if not is_child_environment(environment_id):
            raise WorkerError(ErrorKind.INTERNAL, f"{environment_id!r} is no child environment ID")
        return await self._create_derived(
            session_environment,
            environment_id,
            {"kind": "child", "delegation_id": delegation_id, "session_id": session_id},
            launch,
            require_workspace_prefix=require_workspace_prefix,
            schedule_timeout=schedule_timeout,
        )

    async def _create_derived(
        self,
        session_environment: str,
        environment_id: str,
        identity: dict[str, str],
        launch: str,
        *,
        require_workspace_prefix: bool,
        schedule_timeout: float,
    ) -> EnvironmentInfo:
        """An environment of another kind made from a session environment, under the new
        environment's lock: its record first, then its sandbox."""
        what = "a job sandbox" if identity["kind"] == "job" else "a child environment"
        async with self._lock(environment_id):
            if self.records.get(environment_id) is not None:
                raise WorkerError(ErrorKind.CONFLICT, f"environment {environment_id} exists")
            session = self.get(session_environment)
            if session.kind != "session" or session.state not in (
                EnvironmentState.READY,
                EnvironmentState.STOPPED,
            ):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"environment {session_environment!r} is {session.state}; {what}"
                    " starts from a ready session environment",
                )
            policy = self._policy(session.policy)
            compiled = self._compile(policy, session.admitted_hosts)
            providers = await self._check_providers(policy)
            if providers != session.providers:
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    "the policy now attaches other providers than the session environment has",
                )
            name = new_sandbox_name()
            while self.records.sandbox_taken(name):
                name = new_sandbox_name()
            now = self.clock.now()
            info = EnvironmentInfo.model_validate(
                {
                    "environment_id": environment_id,
                    "project_id": session.project_id,
                    "profile": session.profile,
                    "image": session.image,
                    "sandbox": name,
                    "policy": session.policy,
                    "providers": providers,
                    "state": EnvironmentState.CREATING,
                    "findings": _findings(compiled),
                    "admitted_hosts": compiled.admitted,
                    "created_at": now,
                    "updated_at": now,
                    **identity,
                }
            )
            self.records.save(
                info, driver_config=launch, require_workspace_prefix=require_workspace_prefix
            )
            try:
                sandbox_id = await self._create_sandbox(info, compiled, timeout=schedule_timeout)
                self.store.bind(sandbox_id, session.policy)
            except BaseException:
                try:
                    await _owned(self._cleanup_creation(self.get(environment_id)))
                except Exception as error:
                    _log.warning("creation cleanup remains pending for sandbox %s: %s", name, error)
                raise
            ready = self.get(environment_id).model_copy(
                update={
                    "sandbox_id": sandbox_id,
                    "state": EnvironmentState.READY,
                    "updated_at": self.clock.now(),
                }
            )
            self.records.save(ready)
            _log.info("created %s %s as environment %s", what, name, environment_id)
            return ready

    async def stop_job(self, environment_id: str) -> bool:
        """Stop a job sandbox or a child environment to retain it (JS-25, CE-4): its
        executor goes, its disk stays, and its CPU and memory are freed. Returns whether
        OpenShell says it is stopped."""
        async with self._lock(environment_id):
            info = self.get(environment_id)
            if info.kind == "session" or info.state is not EnvironmentState.READY:
                return info.state is EnvironmentState.STOPPED
            if self.release is not None:
                await self.release(environment_id)
            if not await self._own_sandbox(info, "stop"):
                return False  # gone: nothing to stop, and its deletion confirms it
            self.records.save(
                info.model_copy(
                    update={"state": EnvironmentState.STOPPED, "updated_at": self.clock.now()}
                )
            )
            try:
                await self.cli.run("sandbox", "stop", info.sandbox)
            except WorkerError as error:
                _log.warning("could not stop sandbox %s: %s", info.sandbox, error.message)
                return False
            return await self._stopped(info, wait=True)

    async def present(self, environment_id: str) -> bool | None:
        """Whether a job sandbox's own sandbox still runs: False when the gateway says it is
        gone, another under its name, or in a phase it cannot come back from (pod loss,
        JS-20); None when the gateway cannot say."""
        info = self.get(environment_id)
        try:
            document = await self.cli.sandbox(info.sandbox)
        except WorkerError:
            return None
        if document is None or document.get("id") != info.sandbox_id:
            return False
        return document.get("phase") not in _DEAD_PHASES

    async def runtime_sandbox_of(self, project_id: str, sandbox_id: str) -> None:
        """Refuse unless the gateway shows ``sandbox_id`` as ``project_id``'s agent runtime
        sandbox: the sandbox the listing names by that ID, read with ``sandbox get``, labelled
        ``PROJECT_LABEL`` with the project, as the launcher and the runtime manager create
        it. A project-scoped client binds only such a sandbox; the refusal names only its
        own project."""
        self._not_an_environment(sandbox_id)  # a research environment's: a conflict
        refused = WorkerError(
            ErrorKind.POLICY_DENIED,
            f"sandbox {sandbox_id!r} is not a runtime sandbox labelled for {project_id}",
        )
        entries, _ = await self.cli.sandboxes()
        names = [entry.get("name") for entry in entries if entry.get("id") == sandbox_id]
        if len(names) != 1 or not isinstance(names[0], str):
            raise refused
        document = await self.cli.sandbox(names[0])
        if document is None or document.get("id") != sandbox_id:
            raise refused
        if labels(document).get(PROJECT_LABEL) != project_id:
            raise refused

    def bind_runtime(
        self, project_id: str, sandbox_id: str, projects: frozenset[str] | None = None
    ) -> str:
        """Bind the agent runtime's sandbox to the project's policy revision: the one the
        policy store binds to the project (the MCP proxy's binding), else the one granted to
        it last. The policy middleware decides only sandboxes bound to a stored revision.

        A research environment's sandbox is bound by its own creation and revision, so
        this refuses one of those.
        """
        self._not_an_environment(sandbox_id)
        try:
            entry = self.store.binding_entry(sandbox_id)
            if projects is not None and entry is not None and entry[1] != project_id:
                # Another project's runtime sandbox (or a binding of no recorded project) is
                # never taken over by a client limited to some projects; its own may move to
                # a new revision.
                raise WorkerError(
                    ErrorKind.POLICY_DENIED,
                    f"sandbox {sandbox_id!r} is bound, and not for {project_id}",
                )
            revision = PolicyStore(self.settings.policy_store, "projects").binding(project_id)
            revision = revision or self.records.latest_grant(project_id)
            if revision is None:
                raise WorkerError(
                    ErrorKind.NOT_FOUND,
                    f"project {project_id!r} has no policy revision; an operator grants one"
                    " with `sandbox-science-worker policy add --project`",
                )
            self.store.bind(sandbox_id, revision, project=project_id)
        except StoreError as error:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"policy store: {error}") from None
        return revision

    def unbind_runtime(self, sandbox_id: str, projects: frozenset[str] | None = None) -> bool:
        """Remove the runtime sandbox's binding, if it has one; never an environment's.

        A client limited to ``projects`` may unbind only a runtime sandbox bound for one of
        them: a sandbox with no runtime binding is ``not_found`` to it, and one bound for
        another project ``policy_denied``, naming none but its own. The pool's own token
        (``projects`` None) may unbind any, and none is no error for it."""
        self._not_an_environment(sandbox_id)
        try:
            entry = self.store.binding_entry(sandbox_id)
            if projects is not None:
                if entry is None or entry[1] is None:
                    raise WorkerError(ErrorKind.NOT_FOUND, f"no runtime sandbox {sandbox_id!r}")
                if entry[1] not in projects:
                    raise WorkerError(
                        ErrorKind.POLICY_DENIED,
                        f"runtime sandbox {sandbox_id!r} is not bound for "
                        f"{', '.join(sorted(projects))}",
                    )
            return self.store.unbind(sandbox_id)
        except StoreError as error:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"policy store: {error}") from None

    def _not_an_environment(self, sandbox_id: str) -> None:
        if any(info.sandbox_id == sandbox_id for info in self.records.listed(None)):
            raise WorkerError(ErrorKind.CONFLICT, "that sandbox belongs to a research environment")

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
            self._check_launch_policy(info, policy)
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
                    "updated_at": self.clock.now(),
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
            check_dispatch()
            decisions = await asyncio.to_thread(self._decide, policy, requests)
            compiled, decisions = await asyncio.to_thread(
                self._settle, policy, info.admitted_hosts, decisions
            )
            admitted = compiled.admitted
            check_dispatch()
            if set(admitted) != set(info.admitted_hosts):
                new = sorted(set(admitted) - set(info.admitted_hosts))
                await self._set_policy(info, info.policy, compiled)
                info = info.model_copy(
                    update={
                        "findings": _findings(compiled),
                        "admitted_hosts": admitted,
                        "updated_at": self.clock.now(),
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
        now = self.clock.now()
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
            if info.state is EnvironmentState.CREATING:
                return await _owned(self._cleanup_creation(info))
            return await _owned(self._delete(environment_id))

    async def _cleanup_creation(self, info: EnvironmentInfo) -> EnvironmentInfo:
        """Reconcile one creation, retaining its record until deletion is confirmed.

        A creation without an identity is deleted without a sandbox only on confirmed
        absence (``unknown_creations``); otherwise its deletion is refused."""
        if info.sandbox_id is None:
            outcome = await self._creation_outcome(info)
            if outcome.sandbox_id is None:
                if not outcome.confirmed:
                    raise refusal(outcome)
                return await self._roll_back(info)
            # The worker allocated this random name before dispatch. Once an object
            # appears, keep its identity before a delete or another fallible await.
            info = self._creation_identity(info, outcome.sandbox_id)
        return await self._delete(info.environment_id)

    async def _creation_outcome(self, info: EnvironmentInfo) -> Outcome:
        """One reconciliation read of a creation without an identity (B3).

        The sandbox's own name first: any sandbox there gives its identity. Past the
        grace, the listing too, and a read with no trace is recorded; two such reads
        ``creation_recheck`` apart confirm absence. Gateway failures raise, and count
        as nothing."""
        document = await self.cli.sandbox(info.sandbox)
        if document is not None:
            identity = document.get("id")
            if isinstance(identity, str) and identity:
                return Outcome(sandbox_id=identity)
            return Outcome(
                message=f"{UNKNOWN_OUTCOME}: the gateway shows {info.sandbox} without an identity"
            )
        now = self.clock.now()
        grace = self.settings.creation_grace
        ended = self.records.creation_ended(info)
        if ended is None:
            if self.serving_since is None:
                return Outcome(
                    message=f"{UNKNOWN_OUTCOME}: the end of its create command is not"
                    " recorded, as when a worker stopped while it ran; start the worker,"
                    " which reconciles it"
                )
            ended = max(
                self.serving_since,
                info.created_at + timedelta(seconds=self.settings.create_timeout),
            )
        absent_from = max(info.created_at, ended) + timedelta(seconds=grace)
        if now < absent_from:
            return Outcome(
                message=f"{UNKNOWN_OUTCOME}: its create command ended less than the creation"
                f" grace ({grace:g} s) ago; absence can be observed from {when(absent_from)}"
            )
        entries, complete = await self.cli.sandboxes()
        listing = examine(
            entries,
            complete,
            info,
            self.records.identified(),
            labelled=self.settings.label_environments,
        )
        if listing.sandbox_id is not None:
            return Outcome(sandbox_id=listing.sandbox_id)
        if listing.blocker is not None:
            return Outcome(message=f"{UNKNOWN_OUTCOME}: {listing.blocker}")
        monotonic = self.clock.monotonic()
        local = self._absent_here.setdefault((info.environment_id, info.sandbox), (now, monotonic))
        seen = self.records.observe(info, observation(now, listing.names))
        recheck = self.settings.creation_recheck
        if confirms(seen, now, recheck, local, monotonic):
            return Outcome(
                confirmed=True,
                message=f"{info.sandbox} was absent on {len(seen)} reads from"
                f" {when(seen[0].observed_at)} to {when(now)}",
            )
        first = when(seen[0].observed_at)
        times = f"once, at {first}" if len(seen) == 1 else f"{len(seen)} times since {first}"
        retry_at = seen[0].observed_at + timedelta(seconds=recheck)
        if local[0] == seen[0].observed_at:
            # Due by the monotonic clock too, whatever the wall clock did meanwhile.
            retry_at = max(retry_at, now + timedelta(seconds=local[1] + recheck - monotonic))
        return Outcome(
            message=f"{UNKNOWN_OUTCOME}: seen absent {times};"
            f" a second read after {when(retry_at)} confirms it",
            retry_at=retry_at,
        )

    async def _roll_back(self, info: EnvironmentInfo) -> EnvironmentInfo:
        """Mark a creation confirmed absent as deleted (``creation_rolled_back``), its
        deletion confirmed, so the ordinary path forgets its record and frees its ID."""
        self.records.roll_back(
            info, CREATION_ROLLED_BACK, self.clock.now(), hooks_pending=self.hooks_deferred
        )
        self._absent_here.pop((info.environment_id, info.sandbox), None)
        _log.warning(
            "environment %s: sandbox %s never appeared; marked deleted (%s)",
            info.environment_id,
            info.sandbox,
            CREATION_ROLLED_BACK,
        )
        return await self._delete(info.environment_id)

    async def reconcile_creation(self, environment_id: str) -> Reconciliation:
        """Make one reconciliation read of a creation with an unknown outcome (B3), under
        the environment's lock, and act on it: a sandbox that appeared gives the
        environment its identity, and confirmed absence marks it deleted. Nothing here
        deletes a sandbox. When only a later read is missing, ``retry_at`` says when."""
        async with self._lock(environment_id):
            info = self.get(environment_id)
            if info.state is not EnvironmentState.CREATING:
                return Reconciliation(
                    environment_id, "skipped", f"{environment_id} is {info.state}, not creating"
                )
            if info.sandbox_id is not None:
                return Reconciliation(
                    environment_id,
                    "skipped",
                    f"{environment_id}'s sandbox {info.sandbox} has identity"
                    f" {info.sandbox_id}; deleting the environment deletes it by identity",
                )
            outcome = await self._creation_outcome(info)
            if outcome.sandbox_id is not None:
                self._creation_identity(info, outcome.sandbox_id)
                return Reconciliation(
                    environment_id,
                    "adopted",
                    f"sandbox {info.sandbox} exists with identity {outcome.sandbox_id};"
                    " the environment keeps it, and deleting the environment deletes it",
                )
            if outcome.confirmed:
                await _owned(self._roll_back(info))
                return Reconciliation(
                    environment_id,
                    "rolled_back",
                    f"{outcome.message}; {environment_id} is deleted"
                    f" ({CREATION_ROLLED_BACK}) and its ID may be created again",
                )
            return Reconciliation(
                environment_id, "inconclusive", outcome.message, retry_at=outcome.retry_at
            )

    async def run_pending_hooks(self) -> list[str]:
        """Run the deletion hooks the offline command left pending, once the running
        worker has registered them, before it serves; return the environments' IDs.

        Only for a record still deleted under the same sandbox name: an ID created
        again since then already ran them when it was reused."""
        done: list[str] = []
        for environment_id, sandbox in self.records.hooks_pending():
            async with self._lock(environment_id):
                info = self.records.get(environment_id)
                if (
                    info is not None
                    and info.state is EnvironmentState.DELETED
                    and info.sandbox == sandbox
                ):
                    await self._forget(environment_id)
                    done.append(environment_id)
                self.records.hooks_done(environment_id, sandbox)
        return done

    async def reconcile_creations(self) -> list[Reconciliation]:
        """The running worker's pass: one read for each creation without an identity
        past the grace, skipping environments whose lock is held or awaited."""
        done: list[Reconciliation] = []
        grace = timedelta(seconds=self.settings.creation_grace)
        for info in self.records.creating():
            if info.sandbox_id is not None or self.clock.now() < info.created_at + grace:
                continue  # no read is due yet; _creation_outcome counts from the command's end
            if self.busy(info.environment_id):
                continue  # a creation or deletion under way decides it
            # Each read of a creation past the grace is a record of its own (D103): one
            # still unknown is ``environment_creation_unknown``, which the rendered Loki
            # alerts read (docs/KUBERNETES-BUNDLE.md, "Alert rules").
            fields = {"environment": info.environment_id, "project": info.project_id}
            try:
                result = await self.reconcile_creation(info.environment_id)
            except WorkerError as error:
                _log.warning(
                    "could not reconcile environment %s: %s",
                    info.environment_id,
                    error.message,
                    extra={"event": CREATION_UNKNOWN, "action": "error", **fields},
                )
                continue
            if result.action in ("rolled_back", "adopted"):
                _log.info(
                    "environment %s: %s",
                    info.environment_id,
                    result.message,
                    extra={"event": "environment_creation_reconciled", "action": result.action,
                           **fields},
                )  # fmt: skip
            elif result.action == "inconclusive":
                _log.warning(
                    "environment %s is still creating past the grace: %s",
                    info.environment_id,
                    result.message,
                    extra={"event": CREATION_UNKNOWN, "action": result.action, **fields},
                )
            done.append(result)
        return done

    def _creation_identity(self, info: EnvironmentInfo, sandbox_id: str) -> EnvironmentInfo:
        if info.sandbox_id is not None and info.sandbox_id != sandbox_id:
            raise WorkerError(ErrorKind.UNAVAILABLE, "sandbox identity changed during creation")
        if info.sandbox_id is None:
            info = info.model_copy(
                update={"sandbox_id": sandbox_id, "updated_at": self.clock.now()}
            )
            self.records.save(info)
        return info

    async def _delete(self, environment_id: str) -> EnvironmentInfo:
        info = self.get(environment_id)
        for told in self.required_deleting:
            await told(environment_id)
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
            update={"state": EnvironmentState.DELETED, "updated_at": self.clock.now()}
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
            if self.claims.held(info):
                return False
            if self.release_idle is not None and not await self.release_idle(
                environment_id, idle_since
            ):
                return False
            if not await self._own_sandbox(info, "stop"):
                return False  # gone: its next use finds out and gives the environment up
            # Recorded first: if the stop fails, or the worker stops during it, the next
            # use reads the sandbox's actual phase, and starts it or carries on.
            self.records.save(
                info.model_copy(
                    update={"state": EnvironmentState.STOPPED, "updated_at": self.clock.now()}
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

    async def stop_now(self, environment_id: str, idle_since: float) -> bool:
        """Suspend the environment now, as ``suspend`` does, and say whether OpenShell
        has its sandbox stopped.

        ``suspend`` records ``stopped`` before it stops the sandbox, so a stop that failed
        or was cut short leaves the record saying stopped while the sandbox runs. Here
        the sandbox's own phase decides: a sandbox recorded stopped is stopped again
        unless OpenShell says it is, and the answer is OpenShell's.
        """
        info = self.records.get(environment_id)
        if info is not None and info.state is EnvironmentState.READY:
            await self.suspend(environment_id, idle_since)
        async with self._lock(environment_id):
            info = self.records.get(environment_id)
            if info is None or info.state is not EnvironmentState.STOPPED:
                return False  # in use, being created, or gone
            if await self._stopped(info, wait=False):
                return True
            if not await self._own_sandbox(info, "stop"):
                return False
            try:
                await self.cli.run("sandbox", "stop", info.sandbox)
            except WorkerError as error:
                _log.warning("could not stop sandbox %s again: %s", info.sandbox, error.message)
                return False
            return await self._stopped(info, wait=True)

    async def _own_sandbox(self, info: EnvironmentInfo, action: str) -> bool:
        """Before a command that names the environment's sandbox (OpenShell's lifecycle
        commands take a name, not an ID): whether the gateway shows the sandbox the
        record names, by its recorded identity. False when no sandbox has the name.
        Another sandbox under the name, or a record without an identity, refuses: the
        worker never acts on a sandbox it cannot show is its own. Read just before the
        command, after any other await."""
        document = await self.cli.sandbox(info.sandbox)
        if document is None:
            return False
        found = document.get("id")
        if info.sandbox_id is None or found != info.sandbox_id:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"sandbox {info.sandbox} is {found or 'without an identity'}, not environment"
                f" {info.environment_id}'s {info.sandbox_id or '(identity unknown)'}; refusing"
                f" to {action} another sandbox under its name",
            )
        return True

    async def _stopped(self, info: EnvironmentInfo, *, wait: bool) -> bool:
        """Whether OpenShell has the environment's own sandbox stopped; with ``wait``,
        once it says so, within the resume timeout."""
        deadline = self.clock.monotonic() + self.resume_timeout
        while True:
            try:
                document = await self.cli.sandbox(info.sandbox)
            except WorkerError:
                return False
            if document is None or document.get("id") != info.sandbox_id:
                return False  # gone, or another sandbox under its name
            phase = document.get("phase")
            if phase == "Stopped":
                return True
            if not wait or phase in _DEAD_PHASES or self.clock.monotonic() > deadline:
                return False
            await self.clock.sleep(self.poll_seconds)

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
                async with asyncio.timeout(min(self.settings.create_timeout, self.resume_timeout)):
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
                        await asyncio.sleep(self.poll_seconds)
            except TimeoutError as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "sandbox did not confirm stopping; replacement refused"
                ) from exc
            # Record only a confirmed stop: the startup loss check treats STOPPED as
            # proof cleanup happened. A crash before this is recovered from live phase.
            stopped = info.model_copy(
                update={"state": EnvironmentState.STOPPED, "updated_at": self.clock.now()}
            )
            self.records.save(stopped)
            self._start_failures.pop(environment_id, None)
            await self._start(stopped, pinned=True)

    # Steps.

    def _check_launch_policy(self, info: EnvironmentInfo, policy: LoadedPolicy) -> None:
        if info.kind == "job":
            return  # its size is its job profile's, which the policy grants (JS-9)
        try:
            check_pinned_policy(
                self.records.driver_config(info.sandbox),
                effective_resources(policy.policy, self.ceiling)[0],
            )
        except ValueError as exc:
            raise WorkerError(ErrorKind.POLICY_DENIED, str(exc)) from exc

    async def _start(self, info: EnvironmentInfo, *, pinned: bool = False) -> EnvironmentInfo:
        """Start a stopped environment's sandbox; return any other environment unchanged.

        The sandbox's actual phase decides: a stop that failed left it ready. A start
        that fails is not tried again for a while, so that queued requests fail fast.
        """
        if info.state in (EnvironmentState.READY, EnvironmentState.STOPPED):
            self._check_launch_policy(info, self._policy(info.policy))
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
                update={"state": EnvironmentState.STOPPED, "updated_at": self.clock.now()}
            )
            self.records.save(info)
        if info.state is not EnvironmentState.STOPPED:
            return info
        failed = self._start_failures.get(info.environment_id)
        if failed is not None and self.clock.monotonic() - failed[0] < self.start_backoff:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"sandbox {info.sandbox} failed to start a moment ago ({failed[1]}); "
                "try again later",
            )
        started = self.clock.now()
        await self._begin_start(info.environment_id, info.sandbox)
        try:
            return await self._run_start(info, started, pinned=pinned)
        finally:
            with contextlib.suppress(sqlite3.Error, OSError):
                await asyncio.to_thread(
                    end_start, self.state_dir, info.environment_id, self.clock.now()
                )

    async def _begin_start(self, environment_id: str, sandbox: str) -> None:
        """Refuse to start a sandbox a checkpoint holds stopped, or one a restore is writing
        into, and record the start in the same transaction, so a plan placed meanwhile sees
        it (checkpoint_records). A records error refuses the start too: the check never
        fails open."""
        try:
            await asyncio.to_thread(
                begin_start, self.state_dir, environment_id, self.clock.now(), sandbox
            )
        except HeldError as held:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"environment {environment_id} is {held}; try again after that",
            ) from None
        except (sqlite3.Error, OSError, ValueError) as error:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"cannot check checkpoint holds ({type(error).__name__}); not starting",
            ) from None

    async def _run_start(
        self, info: EnvironmentInfo, started: datetime, *, pinned: bool
    ) -> EnvironmentInfo:
        timeout = min(self.settings.create_timeout, self.resume_timeout)
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
                check_dispatch()
                await self.cli.run("sandbox", "start", info.sandbox, timeout=timeout)
            sandbox_id = await self._wait_ready(info.sandbox, timeout)
        except WorkerError as error:
            self._start_failures[info.environment_id] = (self.clock.monotonic(), error.message)
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
            update={"state": EnvironmentState.READY, "updated_at": self.clock.now()}
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
                update={"state": EnvironmentState.DELETED, "updated_at": self.clock.now()}
            )
        )
        for told in self.given_up:
            try:
                told(info.environment_id)
            except Exception:
                _log.exception("giving up environment %s: a hook failed", info.environment_id)
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
        now = self.clock.now()
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
        """Compile with the entries in use and the newly admissible ones, within the limits.

        An entry is a host, or a host with a port that only a ``ports: any`` rule reaches
        (``admission_entry``). Entries in use come first, so a revision or a new batch never
        displaces them. Hosts the compiled policy admits already need no entry of their
        own. New entries beyond ``max_admitted_hosts``, or that would make the compiled
        policy too large, are refused.
        """
        limit = self.settings.max_admitted_hosts

        def entry(decision: Admission) -> str:
            return admission_entry(policy, decision.host, decision.port)

        new = [entry(d) for d in decisions if d.admitted and entry(d) not in current]
        candidates = list(dict.fromkeys([*current, *new]))
        compiled = self._compile(policy, candidates)
        emitted = set(compiled.admitted)
        keep = [item for item in candidates if item in emitted]
        refusals: dict[str, str] = {}
        if len(keep) > limit:
            for item in keep[limit:]:
                refusals[item] = f"the environment has {limit} hosts admitted on demand, its limit"
            keep = keep[:limit]
        if refusals or len(compiled.policy.to_yaml()) > MAX_POLICY_YAML:
            compiled = self._compile(policy, keep)
            if len(compiled.policy.to_yaml()) > MAX_POLICY_YAML:
                fresh = [item for item in keep if item not in current]
                for item in fresh:
                    refusals[item] = "the sandbox's policy would exceed OpenShell's size limit"
                keep = [item for item in keep if item in current]
                compiled = self._compile(policy, keep)
        settled: list[Admission] = []
        for decision in decisions:
            key = entry(decision) if decision.admitted else ""
            if not decision.admitted:
                pass
            elif key in refusals:
                decision = replace(decision, admitted=False, reason=refusals[key])
            elif key in compiled.admitted:
                if key in current:
                    decision = replace(decision, reason="admitted already")
            elif compiled.admits(decision.host, decision.port):
                decision = replace(decision, reason="admitted already")
            else:
                reason = "the compiler did not admit it; see the environment's findings"
                decision = replace(decision, admitted=False, reason=reason)
            settled.append(decision)
        return compiled, settled

    async def _set_policy(self, info: EnvironmentInfo, revision: str, compiled: Compiled) -> None:
        """Replace the sandbox's network sections, keeping the static sections it reports."""
        filesystem, landlock = await self._live_static(info.sandbox)
        check_dispatch()
        if not await self._own_sandbox(info, "change the policy of"):
            raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {info.sandbox} is gone")
        check_dispatch()  # again: the identity read was an await
        document = compiled.policy.with_static(filesystem, landlock)
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
        self, info: EnvironmentInfo, compiled: Compiled, *, timeout: float | None = None
    ) -> str:
        name, image = info.sandbox, info.image
        try:
            path = self._write(name, compiled.policy_digest, compiled.policy.to_yaml())
            args = ["sandbox", "create", "--name", name, "--from", image, "--policy", str(path)]
            launch = self.records.driver_config(name)
            if launch not in (None, "{}"):
                args += ["--driver-config-json", launch]
            for provider in info.providers:
                args += ["--provider", provider]
            label = label_value(info.environment_id)
            if self.settings.label_environments and label is not None:
                args += ["--label", f"{ENVIRONMENT_LABEL}={label}"]
            args += ["--no-auto-providers", "--detach", "--", *_SANDBOX_COMMAND]
            await self.cli.run(*args, timeout=self.settings.create_timeout)
        finally:
            # Ended, whatever the result: a timeout or cancellation kills and reaps the
            # command first. The grace of an unknown outcome counts from here (B3).
            self.records.create_ended(info, self.clock.now())
        if timeout is None:
            return await self._wait_ready(name, creating=info.environment_id)
        try:
            return await self._wait_ready(name, timeout, creating=info.environment_id)
        except NotReadyError as exc:
            raise ScheduleTimeoutError(
                ErrorKind.UNAVAILABLE,
                f"the sandbox was not placed within {timeout:g} s; no node had room",
            ) from exc

    async def _wait_ready(
        self, name: str, timeout: float | None = None, *, creating: str | None = None
    ) -> str:
        """Wait until the sandbox is ready; return its ID. Phases that cannot lead there fail."""
        loop = asyncio.get_running_loop()
        limit = self.settings.create_timeout if timeout is None else timeout
        deadline = loop.time() + limit
        while True:
            document = await self.cli.sandbox(name)
            if document is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, f"sandbox {name} vanished")
            sandbox_id = document.get("id")
            if creating is not None and isinstance(sandbox_id, str) and sandbox_id:
                self._creation_identity(self.get(creating), sandbox_id)
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
            if document.get("phase") == "Ready" and isinstance(sandbox_id, str) and sandbox_id:
                return sandbox_id
            if loop.time() > deadline:
                raise NotReadyError(
                    ErrorKind.UNAVAILABLE, f"sandbox {name} was not ready after {limit:.0f} s"
                )
            await asyncio.sleep(self.poll_seconds)

    async def _live_static(self, name: str) -> tuple[FilesystemPolicy, LandlockPolicy | None]:
        """The filesystem and Landlock sections OpenShell fixed when it created the sandbox."""
        document = await self.cli.json("policy", "get", name, "--base")
        policy = (
            cast(dict[str, object], document).get("policy") if isinstance(document, dict) else None
        )
        sections = cast(dict[str, object], policy) if isinstance(policy, dict) else {}
        try:
            filesystem = FilesystemPolicy.model_validate(sections.get("filesystem_policy"))
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"sandbox {name} reported no usable filesystem section"
            ) from exc
        landlock = sections.get("landlock")
        try:
            return filesystem, None if landlock is None else LandlockPolicy.model_validate(landlock)
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"sandbox {name} reported an unusable Landlock section"
            ) from exc

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


async def confirmed_daemon_loss(
    cli: OpenShellCli,
    store: Store,
    sandbox_of: Callable[[str], str | None],
    environments: Environments | None,
    environment_id: str,
) -> bool:
    """Actual sandbox loss, never a recycle intent or an unreachable gateway.

    ``sandbox_of`` is the backend's sandbox for the environment; ``environments`` the
    managed ones, if the worker manages any.
    """
    pending_recycle = store.recycle_needed(environment_id)
    binding = store.recycle_binding(environment_id) if pending_recycle else None
    if pending_recycle and binding is None:
        return False  # legacy identity is unknown; cleanup needs explicit operator evidence
    sandbox = sandbox_of(environment_id)
    if environments is not None and environments.manages(environment_id):
        info = environments.get(environment_id)
        if info.state is EnvironmentState.DELETED and not pending_recycle:
            return True
        if info.state is EnvironmentState.STOPPED and not pending_recycle:
            return True  # ordinary suspension already shut down its executor
        sandbox = info.sandbox
    if binding is not None:
        sandbox = binding[0]  # prove termination of the original VM, not a new current binding
    if sandbox is None:
        return not pending_recycle  # removing a binding cannot prove a fenced VM stopped
    try:
        description = await cli.sandbox(sandbox)
    except WorkerError:
        return False  # gateway/transport failure is not evidence of sandbox loss
    if pending_recycle:
        if description is None:
            return True
        if binding is not None and description.get("id") != binding[1]:
            return False
        return description.get("phase") == "Stopped"
    if description is None or description.get("phase") in (
        "Stopped",
        "Completed",
        "Error",
        "Deleting",
    ):
        return True
    if environments is not None and environments.manages(environment_id):
        return description.get("id") != environments.get(environment_id).sandbox_id
    return False
