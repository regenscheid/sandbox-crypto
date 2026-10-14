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

Each run of a session is kept too, as the record ``run --transcript`` writes, in
``runs/<project>/<session>/`` beside ``projects/`` (W4). A tree of its own keeps a
project's runs clear of other projects' files, whose names may contain dots. A run's
record is named by when the run started and its run ID, so a run ID used again adds a
record rather than replacing one, and it is written once: complete, synced, and never
over another.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    Identifier,
    PolicyRevision,
    new_id,
)
from sandbox_science.contracts.model import Contract

from .kernel_notice_receipts import KernelNoticeDelivery, validate_delivery
from .prompt_bundle import PromptProvenance, verify_retained
from .worker_client import WorkerAPIError, WorkerClient

_MAX_ID: Final = 128
_IDENTIFIER: Final = TypeAdapter(Identifier)
_TAG_BYTES: Final = 6
_STARTED: Final = "%Y%m%dT%H%M%S%fZ"
_RUN_NAME: Final = re.compile(r"([0-9]{8}T[0-9]{12}Z)-([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
MAX_SESSION_RECORD_BYTES: Final = 1024 * 1024
"""A polling lookup's project JSON cap, independent of ordinary record APIs."""


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
    kernel_notice_lifetime: Identifier | None = None
    """Opaque control-owned open lifetime; missing legacy state proves no past lifetime."""


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


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class SessionSnapshot:
    """A bounded trusted session read, retaining its original inode until closed.

    Supported registry writes replace files. Holding this descriptor prevents inode
    reuse from hiding a complete close/reopen ABA between two reads. Any project
    replacement is conservatively a different snapshot, including unrelated edits.
    This is receipt-time continuity, not a lock over future registry changes.
    """

    def __init__(
        self,
        descriptor: int,
        root: int,
        identity: tuple[int, int, int, int, int],
        digest: bytes,
        context: ExecutionContext,
        session: SessionRecord,
    ) -> None:
        self.context = context
        self.session = session
        self._descriptor = descriptor
        self._root = root
        self._identity = identity
        self._digest = digest

    @property
    def descriptor(self) -> int:
        """The retained descriptor of the session's project record."""
        return self._descriptor

    @property
    def root(self) -> int:
        """The retained descriptor of the projects directory."""
        return self._root

    @property
    def identity(self) -> tuple[int, int, int, int, int]:
        """The project record's file identity when it was read."""
        return self._identity

    @property
    def digest(self) -> bytes:
        """The SHA-256 of the project record as read."""
        return self._digest

    def matches(self, other: SessionSnapshot) -> bool:
        """Whether both bounded reads still name the same unchanged open binding."""
        before, after = os.fstat(self.root), os.fstat(other.root)
        return (
            (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            and self.identity == other.identity
            and _file_identity(os.fstat(self.descriptor)) == self.identity
            and _file_identity(os.fstat(other.descriptor)) == other.identity
            and self.digest == other.digest
            and self.context == other.context
            and self.session == other.session
        )

    def close(self) -> None:
        """Release both retained descriptors; the caller owns their actual lifetime."""
        os.close(self.descriptor)
        os.close(self.root)


FailurePhase = Literal["model", "tool", "harness", "cleanup"]
"""A fixed local classification, never exception text or a provider response."""


class RunRecord(Contract):
    """One run of the harness: what ``run --transcript`` writes, and what a session keeps."""

    run_id: str
    """The run's ID, which starts every operation ID of its tool calls."""
    prompt_provenance: PromptProvenance | None = None
    """Recorded MAIN system/tool input pins; missing legacy provenance stays unknown."""
    kernel_notice_delivery: KernelNoticeDelivery | None = None
    """Exact successfully delivered historical input; no future inbox authority."""
    context: ExecutionContext
    task: str
    started_at: datetime
    ended_at: datetime
    stopped: str
    """``answered``, ``turn_limit``, ``user_message``, or ``error``."""
    failure: FailurePhase | None = None
    """Failed phase; old records omit it. Partial messages are observations, not an answer."""
    job_notice_ids: list[Identifier] = Field(default_factory=list[Identifier])
    """Completion notices represented by this durable run; acknowledged only after save."""
    work_request_ids: list[Identifier] = Field(default_factory=list[Identifier])
    """User work requests represented in this saved transcript, acknowledged after save."""
    review_notice_ids: list[Identifier] = Field(default_factory=list[Identifier])
    """Finding notices sent to a successful model call and acknowledged after save."""
    math_notice_ids: list[Identifier] = Field(default_factory=list[Identifier])
    math_summary: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    review_summary: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    final_text: str | None = None
    messages: list[dict[str, JsonValue]]
    """The conversation in the OpenAI chat format the harness uses, system prompt first."""
    tools: list[dict[str, JsonValue]]
    """One record per tool call, in order: its name, arguments and outcome."""

    @field_validator("kernel_notice_delivery", mode="before")
    @classmethod
    def _kernel_notice_pin(cls, value: object) -> KernelNoticeDelivery | None:
        if value is None:
            return None
        return validate_delivery(value)

    @model_validator(mode="after")
    def _failure_is_not_an_answer(self) -> RunRecord:
        if self.prompt_provenance is not None:
            verify_retained(self.prompt_provenance, self.messages[0] if self.messages else None)
        if self.kernel_notice_delivery is not None:
            claim = self.kernel_notice_delivery.claim
            if claim.run_id != self.run_id or any(
                (
                    notice.observation.scope.project_id,
                    notice.observation.scope.environment_id,
                    notice.observation.scope.session_id,
                )
                != (self.context.project_id, self.context.environment_id, self.context.session_id)
                for notice in claim.notices
            ):
                raise ValueError("kernel delivery does not match the recorded run scope")
            injected = self.kernel_notice_delivery.text()
            if (
                sum(
                    message.get("role") == "user" and message.get("content") == injected
                    for message in self.messages
                )
                != 1
            ):
                raise ValueError("kernel delivery needs exactly one retained historical user input")
        if self.failure is not None and (self.stopped != "error" or self.final_text is not None):
            raise ValueError("a failed run must have error status and no final answer")
        return self

    def to_json(self) -> str:
        """The record as JSON text.

        The standard library's encoder escapes what it cannot write as UTF-8, such as a
        lone surrogate in a model's reply, where pydantic's would fail at the end of a run.
        """
        return json.dumps(self.model_dump(mode="json"), indent=2)


class RunEntry(Contract):
    """A run a session keeps, named by when it started and its run ID."""

    name: str
    run_id: Identifier
    started_at: datetime


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


def _is_identifier(value: str) -> bool:
    try:
        _IDENTIFIER.validate_python(value)
    except ValidationError:
        return False
    return True


def _run_entry(name: str) -> RunEntry | None:
    """The run a record's name stands for; None for any other name, which is never a path."""
    match = _RUN_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        started = datetime.strptime(match[1], _STARTED).replace(tzinfo=UTC)
    except ValueError:
        return None
    return RunEntry(name=name, run_id=match[2], started_at=started)


class ProjectStore:
    """The orchestrator's project records, in ``<state_dir>/projects``, and the runs of
    their sessions, in ``<state_dir>/runs``."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "projects"
        self.runs_root = state_dir / "runs"
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

    def session_snapshot(self, project_id: str, session_id: str) -> SessionSnapshot:
        """Bounded open-session lookup for passive polling; ordinary reads are unchanged.

        Reject special files without blocking on their open, refuse final symlinks,
        bound bytes before JSON materialization, and retain the file identity. A
        regular file read can still block in the filesystem; its caller must own
        this work until it returns, even after its response deadline expires.
        """
        if not _is_identifier(project_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid project ID")
        name = self._path(project_id).name
        if not _is_identifier(session_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid session ID")
        root = descriptor = -1
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 <= before.st_size <= MAX_SESSION_RECORD_BYTES
            ):
                raise ProjectError(
                    ErrorKind.UNAVAILABLE, "project registry is not a bounded record"
                )
            data = bytearray()
            while len(data) <= MAX_SESSION_RECORD_BYTES:
                chunk = os.read(
                    descriptor, min(64 * 1024, MAX_SESSION_RECORD_BYTES + 1 - len(data))
                )
                if not chunk:
                    break
                data.extend(chunk)
            if (
                len(data) > MAX_SESSION_RECORD_BYTES
                or len(data) != before.st_size
                or _file_identity(os.fstat(descriptor)) != _file_identity(before)
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "project registry changed during read")
            record = ProjectRecord.model_validate_json(data, strict=True)
            found = record.sessions.get(session_id)
            if record.project_id != project_id or (
                found is not None and found.session_id != session_id
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "project registry has inconsistent scope")
            if found is None or found.closed_at is not None:
                raise ProjectError(ErrorKind.NOT_FOUND, "no open session in this project")
            if found.opened_at.tzinfo is None or found.opened_at.utcoffset() is None:
                raise ProjectError(ErrorKind.UNAVAILABLE, "session registry time is not known")
            result = SessionSnapshot(
                descriptor,
                root,
                _file_identity(before),
                hashlib.sha256(data).digest(),
                ExecutionContext(
                    project_id=project_id,
                    session_id=session_id,
                    environment_id=found.environment_id,
                ),
                found,
            )
            descriptor = root = -1
            return result
        except FileNotFoundError:
            raise ProjectError(ErrorKind.NOT_FOUND, "no such recorded project") from None
        except (OSError, ValidationError, ValueError, RecursionError) as exc:
            raise ProjectError(ErrorKind.UNAVAILABLE, "project registry is unreadable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if root >= 0:
                os.close(root)

    def create(self, project_id: str, profile: str, policy: str) -> tuple[ProjectRecord, bool]:
        """Record a new project; return the existing one when it has the same settings."""
        with self.locked(project_id):
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
        with self.locked(project_id):
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
        with self.locked(project_id):
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
                    kernel_notice_lifetime=new_id("kns"),
                )
            elif session.dedicated != dedicated:
                where = "its own environment" if session.dedicated else "the project's"
                raise ProjectError(
                    ErrorKind.CONFLICT,
                    f"session {session_id!r} runs in {where}; a session keeps its environment, "
                    "since its kernels and installed tools live there",
                )
            elif session.closed_at is not None:
                session = session.model_copy(
                    update={
                        "closed_at": None,
                        "opened_at": now,
                        "kernel_notice_lifetime": new_id("kns"),
                    }
                )
            else:
                return session
            self._write(
                record.model_copy(
                    update={"sessions": {**record.sessions, session_id: session}, "updated_at": now}
                )
            )
            return session

    def kernel_notice_baseline(self, project_id: str, session_id: str) -> SessionRecord:
        """Persist a present-time legacy baseline before any dependent snapshot/RPC.

        Only this new path uses the bounded nofollow lock/read and durable write;
        existing project lifecycle and logical-session plan CAS are unchanged.
        """
        _IDENTIFIER.validate_python(project_id, strict=True)
        _IDENTIFIER.validate_python(session_id, strict=True)
        root = lock = -1
        snapshot: SessionSnapshot | None = None
        partial: str | None = None
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            lock = os.open(
                project_id + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root,
            )
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise ProjectError(ErrorKind.UNAVAILABLE, "notification registry lock is unsafe")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            snapshot = self.session_snapshot(project_id, session_id)
            if (os.fstat(root).st_dev, os.fstat(root).st_ino) != (
                os.fstat(snapshot.root).st_dev,
                os.fstat(snapshot.root).st_ino,
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "notification registry changed")
            if snapshot.session.kernel_notice_lifetime is not None:
                os.fsync(snapshot.descriptor)
                os.fsync(root)
                return snapshot.session
            raw = os.pread(snapshot.descriptor, MAX_SESSION_RECORD_BYTES + 1, 0)
            if (
                len(raw) > MAX_SESSION_RECORD_BYTES
                or hashlib.sha256(raw).digest() != snapshot.digest
                or _file_identity(os.fstat(snapshot.descriptor)) != snapshot.identity
            ):
                raise ProjectError(ErrorKind.UNAVAILABLE, "notification registry changed")
            record = ProjectRecord.model_validate_json(raw, strict=True)
            current = self.session_snapshot(project_id, session_id)
            try:
                if not snapshot.matches(current) or (
                    os.fstat(lock).st_dev,
                    os.fstat(lock).st_ino,
                ) != (
                    os.stat(project_id + ".lock", dir_fd=root, follow_symlinks=False).st_dev,
                    os.stat(project_id + ".lock", dir_fd=root, follow_symlinks=False).st_ino,
                ):
                    raise ProjectError(ErrorKind.UNAVAILABLE, "notification registry changed")
            finally:
                current.close()
            session = snapshot.session.model_copy(update={"kernel_notice_lifetime": new_id("kns")})
            updated = record.model_copy(
                update={
                    "sessions": {**record.sessions, session_id: session},
                    "updated_at": datetime.now(UTC),
                }
            )
            data = updated.model_dump_json(indent=2).encode("utf-8")
            if len(data) > MAX_SESSION_RECORD_BYTES:
                raise ProjectError(ErrorKind.UNAVAILABLE, "notification registry exceeds its bound")
            partial = "." + project_id + "." + secrets.token_hex(12) + ".notice-partial"
            descriptor = os.open(
                partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root
            )
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(partial, project_id + ".json", src_dir_fd=root, dst_dir_fd=root)
            partial = None
            os.fsync(root)
            return session
        except (OSError, ValidationError, ValueError, RecursionError):
            raise ProjectError(ErrorKind.UNAVAILABLE, "notification baseline unavailable") from None
        finally:
            if partial is not None and root >= 0:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(partial, dir_fd=root)
            if snapshot is not None:
                snapshot.close()
            if lock >= 0:
                os.close(lock)
            if root >= 0:
                os.close(root)

    def close_session(self, project_id: str, session_id: str) -> SessionRecord:
        with self.locked(project_id):
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

    def project_ids(self) -> list[str]:
        """The IDs of the projects recorded here, sorted."""
        found = (path.name.removesuffix(".json") for path in self.root.glob("*.json"))
        return sorted(
            project_id
            for project_id in found
            if _is_identifier(project_id) and self._path(project_id).is_file()
        )

    def save_run(self, record: RunRecord) -> RunEntry:
        """Keep a run of one of the project's sessions; the record is written once."""
        project_id, session_id = record.context.project_id, record.context.session_id
        if session_id not in self.get(project_id).sessions:
            raise ProjectError(
                ErrorKind.NOT_FOUND, f"project {project_id!r} has no session {session_id!r}"
            )
        started = record.started_at.astimezone(UTC).strftime(_STARTED)
        entry = _run_entry(f"{started}-{record.run_id}")
        if entry is None:
            raise ProjectError(
                ErrorKind.INVALID_REQUEST,
                f"run ID {record.run_id!r} cannot name a record: use letters, digits, '.', "
                "'_' and '-', starting with a letter or digit",
            )
        directory = self._runs(project_id, session_id)
        directory.mkdir(parents=True, exist_ok=True)
        # A partial file of its own, synced, then linked to its name, which fails rather
        # than replace a record that has the name already.
        descriptor, partial = tempfile.mkstemp(dir=directory, prefix=".", suffix=".partial")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(record.to_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.link(partial, directory / f"{entry.name}.json")
            # A completion notice may be acknowledged after this returns. Sync the
            # directory entry too, so a power loss cannot keep the ack and lose the run.
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            raise ProjectError(
                ErrorKind.CONFLICT, f"session {session_id!r} has a run {entry.name!r} already"
            ) from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(partial)
        return entry

    def runs(self, project_id: str, session_id: str) -> list[RunEntry]:
        """The runs a session keeps, oldest first."""
        directory = self._runs(project_id, session_id)
        entries = (_run_entry(path.name.removesuffix(".json")) for path in directory.glob("*.json"))
        return sorted((entry for entry in entries if entry is not None), key=lambda e: e.name)

    def run(
        self, project_id: str, session_id: str, name: str, *, max_bytes: int | None = None
    ) -> RunRecord:
        """A run the session keeps, by its name; larger than ``max_bytes`` is refused."""
        if _run_entry(name) is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid run name {name!r}")
        path = self._runs(project_id, session_id) / f"{name}.json"
        try:
            with path.open("rb") as handle:
                data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        except FileNotFoundError:
            raise ProjectError(
                ErrorKind.NOT_FOUND, f"session {session_id!r} of {project_id!r} has no run {name!r}"
            ) from None
        if max_bytes is not None and len(data) > max_bytes:
            raise ProjectError(
                ErrorKind.UNSUPPORTED, f"run {name!r} has a record larger than {max_bytes} bytes"
            )
        try:
            return RunRecord.model_validate(json.loads(data))
        except (ValueError, RecursionError) as exc:  # ValidationError is a ValueError
            raise ProjectError(
                ErrorKind.INTERNAL, f"run record {path} is damaged: {str(exc)[:300]}"
            ) from exc

    def _runs(self, project_id: str, session_id: str) -> Path:
        """Where a session's runs are kept; both IDs are checked, so it is a safe path."""
        self._path(project_id)
        if not _is_identifier(session_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid session ID {session_id!r}")
        return self.runs_root / project_id / session_id

    def _path(self, project_id: str) -> Path:
        # An identifier is a safe file name.
        if not _is_identifier(project_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid project ID {project_id!r}")
        return self.root / f"{project_id}.json"

    @contextlib.contextmanager
    def locked(self, project_id: str) -> Generator[None]:
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
