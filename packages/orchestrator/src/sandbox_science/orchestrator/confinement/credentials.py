# SPDX-License-Identifier: MIT
"""Control-issued runtime generations and tokens for the cluster's runtime manager.

Control is the only authority that mints them, so "never an older value, never while a
runtime holds the connection" is enforced where generations are already fenced. The
token itself lives only in the memory-volume file that the per-frame check rereads;
the durable journal keeps request IDs, generations and token digests.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from sandbox_science.contracts.model import Contract, Identifier, Sha256, StrictContract

JOURNAL: Final = "runtime-credentials.json"
"""The issue journal's name in control's state directory."""
MAX_REQUEST_BYTES: Final = 1024
MAX_JOURNAL_BYTES: Final = 16 * 1024 * 1024

RequestId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class CredentialsRequest(StrictContract):
    """The manager's journaled request ID; it chooses no value control issues."""

    request: RequestId


class Issued(Contract):
    generation: Identifier
    token: str = Field(min_length=64, max_length=64)


RefusalKind = Literal["invalid_request", "superseded", "occupied", "unavailable"]


class Refused(Contract):
    error: RefusalKind
    message: str = Field(default="", max_length=512)


class Issue(Contract):
    """One request. ``revoked``: the previous token is replaced, no value issued yet."""

    request: RequestId
    state: Literal["revoked", "issued", "superseded"]
    generation: Identifier | None = None
    token_sha256: Sha256 | None = None


class Journal(Contract):
    issues: tuple[Issue, ...] = ()


class CredentialsError(Exception):
    """A refused issue; ``kind`` is the wire error kind."""

    def __init__(self, kind: Literal["superseded", "occupied"], message: str) -> None:
        super().__init__(message)
        self.kind: Literal["superseded", "occupied"] = kind


def _publish(path: Path, data: bytes) -> None:
    """Fsync a private file and atomically replace ``path``; readers see old or new."""
    descriptor, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _private_text(path: Path, maximum: int) -> str | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise ValueError("runtime credential state must be a private regular file")
    if path.stat().st_size > maximum:
        raise ValueError("runtime credential state exceeds its size bound")
    return path.read_text(encoding="utf-8").strip()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RuntimeCredentials:
    """The KRB-7 issue states over the journal, the token file and the generation file.

    Every method is synchronous and called from the event loop, so requests are
    serialized and the occupancy observed by ``issue`` cannot change while it runs.
    """

    def __init__(
        self,
        journal: Path,
        token_file: Path,
        generation_file: Path,
        *,
        placeholder: str,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._journal_file = journal
        self._token_file = token_file
        self._generation_file = generation_file
        self._placeholder = placeholder
        self._entropy = entropy
        self._journal = Journal()

    def start(self) -> None:
        """Revoke at process start: a fresh unissued token, the last issue superseded.

        A request left ``revoked`` by a crash stays so: its retry mints fresh values.
        """
        self._journal_file.parent.mkdir(parents=True, exist_ok=True)
        text = _private_text(self._journal_file, MAX_JOURNAL_BYTES)
        self._journal = Journal.model_validate_json(text) if text is not None else Journal()
        self._revoke()
        latest = self._journal.issues[-1] if self._journal.issues else None
        if latest is not None and latest.state == "issued":
            self._save_latest(latest.model_copy(update={"state": "superseded"}))

    def issue(self, request: str, *, occupied: bool) -> Issued:
        """Replay the latest issue, refuse an older one, or revoke and mint new values.

        ``occupied`` is whether a runtime is connected or draining; it is checked after
        the revocation, so a connected runtime is refused at its next frame either way.
        """
        issues = self._journal.issues
        latest = issues[-1] if issues else None
        if latest is not None and latest.request == request and latest.state == "issued":
            current = self._current(latest)
            if current is not None:
                return current
            self._save_latest(latest.model_copy(update={"state": "superseded"}))
            raise CredentialsError("superseded", "the issued credentials are no longer current")
        retry = latest is not None and latest.request == request and latest.state == "revoked"
        if not retry:
            if any(issue.request == request for issue in issues):
                raise CredentialsError("superseded", "the request was superseded")
            if latest is not None:
                issues = (*issues[:-1], latest.model_copy(update={"state": "superseded"}))
            self._save((*issues, Issue(request=request, state="revoked")))
        self._revoke()
        if occupied:
            raise CredentialsError("occupied", "a runtime is connected or draining")
        generation = self._mint_generation()
        token = self._entropy(32).hex()
        # A crash before the journal write leaves this request revoked, and control's
        # restart replaces the token, so these values are never handed out later.
        _publish(self._generation_file, generation.encode() + b"\n")
        _publish(self._token_file, token.encode() + b"\n")
        self._save_latest(
            Issue(
                request=request, state="issued", generation=generation, token_sha256=_digest(token)
            )
        )
        return Issued(generation=generation, token=token)

    def _current(self, latest: Issue) -> Issued | None:
        token = _private_text(self._token_file, 258)
        generation = _private_text(self._generation_file, 130)
        if token is None or generation is None:
            return None
        if _digest(token) != latest.token_sha256 or generation != latest.generation:
            return None
        return Issued(generation=generation, token=token)

    def _mint_generation(self) -> str:
        used = {issue.generation for issue in self._journal.issues if issue.generation}
        used.add(self._placeholder)
        stray = _private_text(self._generation_file, 130)
        if stray is not None:
            used.add(stray)
        while (generation := f"runtime-{self._entropy(12).hex()}") in used:
            pass
        return generation

    def _revoke(self) -> None:
        _publish(self._token_file, self._entropy(32).hex().encode() + b"\n")

    def _save_latest(self, issue: Issue) -> None:
        self._save((*self._journal.issues[:-1], issue))

    def _save(self, issues: tuple[Issue, ...]) -> None:
        journal = Journal(issues=issues)
        _publish(self._journal_file, journal.model_dump_json().encode())
        self._journal = journal
