# SPDX-License-Identifier: MIT
"""Projects and their sessions, as the orchestrator records them (W3).

A project names a base profile (D46) and a policy revision, and owns a default
environment. Each session (workbook) runs in the default environment, with its own
kernels and workspace, unless it was opened with a dedicated environment of its own,
which starts from the same base profile ([projects and sessions](../../../../docs/
PROJECTS-AND-SESSIONS.md)). The worker creates the environments (D73); the
orchestrator asks for them by ID and keeps this record of which session uses which.

Records are JSON files under the orchestrator's state directory, one per project.
Each change rewrites its file atomically under a lock, so concurrent harness
processes cannot lose one another's sessions.

Environment IDs end with a random tag, kept in the record. A worker shared with other
projects' orchestrators keeps each ID for the project that first creates it (D78), so
an ID another orchestrator could predict, such as ``p1-default``, could be taken first.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    Identifier,
    PolicyRevision,
)
from sandbox_science.contracts.model import Contract

from .worker_client import WorkerAPIError, WorkerClient

_MAX_ID: Final = 128
_IDENTIFIER: Final = TypeAdapter(Identifier)
_TAG_BYTES: Final = 6


class ProjectError(Exception):
    """A project or session that does not exist, or a request that conflicts with one."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class SessionRecord(Contract):
    session_id: Identifier
    environment_id: Identifier
    dedicated: bool = False
    """The session has an environment of its own instead of the project's default."""
    opened_at: datetime
    closed_at: datetime | None = None
    """When the session was last closed: its kernels were stopped. Running it reopens it."""


class ProjectRecord(Contract):
    project_id: Identifier
    profile: Identifier
    policy: PolicyRevision
    default_environment_id: Identifier
    sessions: dict[str, SessionRecord] = Field(default_factory=dict[str, SessionRecord])
    created_at: datetime
    updated_at: datetime

    def environments(self) -> list[str]:
        """Every environment the project uses: the default one and the sessions' own."""
        dedicated = {s.environment_id for s in self.sessions.values() if s.dedicated}
        return [self.default_environment_id, *sorted(dedicated - {self.default_environment_id})]


def environment_id(*parts: str) -> str:
    """An environment ID from its parts, such as a project's and a session's IDs, short
    enough to be one."""
    joined = "-".join(parts)
    if len(joined) <= _MAX_ID:
        return joined
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{joined[: _MAX_ID - len(digest) - 1]}-{digest}"


def _new_environment_id(project_id: str, name: str) -> str:
    """A new environment's ID: readable, and unpredictable to other projects' clients."""
    return environment_id(project_id, name, secrets.token_hex(_TAG_BYTES))


class ProjectStore:
    """The orchestrator's project records, in ``<state_dir>/projects``."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, project_id: str) -> ProjectRecord:
        path = self._path(project_id)
        try:
            return ProjectRecord.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            raise ProjectError(ErrorKind.NOT_FOUND, f"no project {project_id!r}") from None
        except ValidationError as exc:
            raise ProjectError(
                ErrorKind.INTERNAL, f"project record {path} is damaged: {str(exc)[:300]}"
            ) from exc

    def create(self, project_id: str, profile: str, policy: str) -> tuple[ProjectRecord, bool]:
        """Record a new project; return the existing one when it has the same settings."""
        with self._locked(project_id):
            try:
                existing: ProjectRecord | None = self.get(project_id)
            except ProjectError as error:
                if error.kind is not ErrorKind.NOT_FOUND:
                    raise
                existing = None
            if existing is not None:
                if (existing.profile, existing.policy) == (profile, policy):
                    return existing, False
                raise ProjectError(
                    ErrorKind.CONFLICT,
                    f"project {project_id!r} exists with profile {existing.profile} and policy "
                    f"{existing.policy}; revise its policy instead",
                )
            now = datetime.now(UTC)
            record = self._validated(
                project_id=project_id,
                profile=profile,
                policy=policy,
                default_environment_id=_new_environment_id(project_id, "default"),
                created_at=now,
                updated_at=now,
            )
            self._write(record)
            return record, True

    def set_policy(self, project_id: str, policy: str) -> ProjectRecord:
        """Move the project to another policy revision; its environments follow on next use."""
        with self._locked(project_id):
            record = self.get(project_id)
            if record.policy == policy:
                return record
            updated = self._validated(
                **{**record.model_dump(), "policy": policy, "updated_at": datetime.now(UTC)}
            )
            self._write(updated)
            return updated

    def open_session(self, project_id: str, session_id: str, *, dedicated: bool) -> SessionRecord:
        """The session's record, created on first use; a closed session opens again."""
        with self._locked(project_id):
            record = self.get(project_id)
            session = record.sessions.get(session_id)
            now = datetime.now(UTC)
            if session is None:
                session = self._session(
                    session_id=session_id,
                    environment_id=_new_environment_id(project_id, session_id)
                    if dedicated
                    else record.default_environment_id,
                    dedicated=dedicated,
                    opened_at=now,
                )
            elif session.dedicated != dedicated:
                where = "its own environment" if session.dedicated else "the project's"
                raise ProjectError(
                    ErrorKind.CONFLICT,
                    f"session {session_id!r} runs in {where}; a session keeps its environment, "
                    "since its kernels and installed tools live there",
                )
            elif session.closed_at is not None:
                session = session.model_copy(update={"closed_at": None, "opened_at": now})
            else:
                return session
            self._write(
                record.model_copy(
                    update={"sessions": {**record.sessions, session_id: session}, "updated_at": now}
                )
            )
            return session

    def close_session(self, project_id: str, session_id: str) -> SessionRecord:
        with self._locked(project_id):
            record = self.get(project_id)
            session = record.sessions.get(session_id)
            if session is None:
                raise ProjectError(
                    ErrorKind.NOT_FOUND, f"project {project_id!r} has no session {session_id!r}"
                )
            now = datetime.now(UTC)
            closed = session.model_copy(update={"closed_at": now})
            self._write(
                record.model_copy(
                    update={"sessions": {**record.sessions, session_id: closed}, "updated_at": now}
                )
            )
            return closed

    def _path(self, project_id: str) -> Path:
        # An identifier is a safe file name.
        try:
            _IDENTIFIER.validate_python(project_id)
        except ValidationError:
            raise ProjectError(
                ErrorKind.INVALID_REQUEST, f"invalid project ID {project_id!r}"
            ) from None
        return self.root / f"{project_id}.json"

    @contextlib.contextmanager
    def _locked(self, project_id: str) -> Generator[None]:
        lock = self._path(project_id).with_suffix(".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write(self, record: ProjectRecord) -> None:
        path = self._path(record.project_id)
        partial = path.with_suffix(".json.partial")
        partial.write_text(record.model_dump_json(indent=2))
        os.replace(partial, path)

    @staticmethod
    def _validated(**fields: object) -> ProjectRecord:
        try:
            return ProjectRecord.model_validate(fields)
        except ValidationError as exc:
            raise ProjectError(ErrorKind.INVALID_REQUEST, str(exc)[:500]) from exc

    @staticmethod
    def _session(**fields: object) -> SessionRecord:
        try:
            return SessionRecord.model_validate(fields)
        except ValidationError as exc:
            raise ProjectError(ErrorKind.INVALID_REQUEST, str(exc)[:500]) from exc


async def ensure_environment(
    client: WorkerClient, project: ProjectRecord, environment_id: str
) -> EnvironmentInfo | None:
    """Have the worker run the environment under the project's profile and policy.

    Creates it when it does not exist, and revises it when the project's policy has
    changed. Returns None when the worker does not manage environments (a local
    worker, or sandboxes made by hand): the ID is then used as it is.
    """
    try:
        info: EnvironmentInfo | None = await client.environment(environment_id)
    except WorkerAPIError as error:
        if error.kind is ErrorKind.UNSUPPORTED:
            return None
        if error.kind is not ErrorKind.NOT_FOUND:
            raise
        info = None
    if info is None or info.state is EnvironmentState.DELETED:
        return await client.create_environment(
            EnvironmentRequest(
                environment_id=environment_id,
                project_id=project.project_id,
                profile=project.profile,
                policy=project.policy,
            )
        )
    if (info.project_id, info.profile) != (project.project_id, project.profile):
        raise WorkerAPIError(
            ErrorKind.CONFLICT,
            f"environment {environment_id} belongs to project {info.project_id} with profile "
            f"{info.profile}",
        )
    if info.policy != project.policy:
        return await client.revise_environment(environment_id, project.policy)
    return info
