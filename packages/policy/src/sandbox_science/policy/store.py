# SPDX-License-Identifier: MIT
"""Project policies by revision, and bindings that name the revision a subject is decided under.

A store is a directory that the policy middleware, the worker and the MCP proxy share
the layout of:

- ``policies/<hex>.yaml`` holds a project policy file, named by the hex SHA-256 of its
  bytes, so that a file cannot stand in for another revision;
- ``sandboxes/<sandbox id>.json`` binds an OpenShell sandbox to one revision, for the
  policy middleware, which the worker writes when it creates or revises a sandbox;
- ``projects/<project>.json`` binds a project's MCP calls to one revision, for the MCP
  proxy;
- ``classifications/<project>/<server>/<hex>.yaml`` holds a project's accepted revision of
  one MCP server's classification (D96, CL-3), named by the hex SHA-256 of its bytes, with
  ``<hex>.json`` beside it recording who accepted it, when, why, the draft it came from and
  its difference from the classification it replaced, and ``current.json``
  (``{"revision": "<hex>"}``) naming the revision in force for that project and server;
- ``classification-drafts/<project>/<server>/<hex>.yaml`` holds a draft a model proposed
  (``classify-draft --project``), with ``<hex>.json`` saying when and which model.

A binding is ``{"policy": "sha256:<hex>"}``. Bindings, ``current.json`` included, are read on
every use, so a new revision applies without restarting a reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

from .classification import ServerClassification, revision_classification
from .errors import PolicyLoadError
from .loader import LoadedPolicy, parse_policy, read_limited

DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
"""A policy revision: ``sha256:`` and the hex SHA-256 of the policy file."""

NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""Sandbox and project IDs (the contracts' ``Identifier``); also safe as file names."""

SERVER_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
"""An MCP server's name in a policy (``mcp.servers``)."""

HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
"""A classification revision or draft: the hex SHA-256 of its file."""

MAX_DRAFTS: Final = 64
"""Drafts listed per project and server, newest first."""

Subjects = Literal["sandboxes", "projects"]
"""What a store's bindings bind: sandboxes for the policy middleware, projects for the MCP proxy."""

_MAX_BINDING_BYTES: Final = 4096
_MAX_RECORD_BYTES: Final = 256 * 1024
_MAX_RECORD_NAMES: Final = 1024
_CURRENT: Final = "current.json"
_MAX_CACHED_POLICIES: Final = 256
_SUBJECT: Final[dict[str, str]] = {"sandboxes": "sandbox", "projects": "project"}


class StoreError(Exception):
    """A policy or a binding could not be read, validated or written."""


@dataclass(frozen=True, slots=True)
class Acceptance:
    """Who accepted a project's classification revision, when and why, and what changed."""

    accepted_by: str
    accepted_at: datetime
    reason: str
    draft: str | None = None
    """The draft it was accepted from (hex), edited or not."""
    replaces: str | None = None
    """The project revision it replaced (hex); None when it replaced the catalog's."""
    diff: Mapping[str, Sequence[str]] | None = None
    """Tools ``added``, ``removed`` and ``changed`` against the replaced classification,
    and the ``widenings`` (``classification.widenings``)."""

    def document(self) -> dict[str, object]:
        return {
            "accepted_by": self.accepted_by,
            "accepted_at": self.accepted_at.isoformat(),
            "reason": self.reason,
            "draft": self.draft,
            "replaces": self.replaces,
            "diff": {
                key: list(names)[:_MAX_RECORD_NAMES] for key, names in (self.diff or {}).items()
            },
        }


@dataclass(frozen=True, slots=True)
class Draft:
    """A classification a model drafted for a project's server: its text, never in force."""

    draft: str
    """The hex SHA-256 of ``data``."""
    server: str
    created_at: datetime
    model: str | None
    data: bytes
    classification: ServerClassification


def check_server(name: str) -> str:
    """Return ``name`` if it is a well-formed MCP server name, else raise StoreError."""
    if not SERVER_RE.fullmatch(name):
        raise StoreError(f"{name!r} is not a valid MCP server name")
    return name


def check_hex(value: str, what: str = "classification revision") -> str:
    """Return ``value`` if it is a hex SHA-256, else raise StoreError."""
    if not HEX_RE.fullmatch(value):
        raise StoreError(f"{value!r} is not a {what} (64 hex digits)")
    return value


def check_digest(digest: str) -> str:
    """Return ``digest`` if it is a well-formed revision, else raise StoreError."""
    if not DIGEST_RE.fullmatch(digest):
        raise StoreError(f"{digest!r} is not a policy revision (sha256:<64 hex digits>)")
    return digest


def check_name(name: str, subject: str = "sandbox") -> str:
    """Return ``name`` if it is a well-formed sandbox or project ID, else raise StoreError."""
    if not NAME_RE.fullmatch(name):
        raise StoreError(f"{name!r} is not a valid {subject} id")
    return name


class PolicyStore:
    """Read and write policies, and one kind of binding, under one directory."""

    def __init__(self, root: Path, subjects: Subjects = "sandboxes") -> None:
        self._classifications = root / "classifications"
        self._drafts = root / "classification-drafts"
        self._policies = root / "policies"
        self._bindings = root / subjects
        self._subject = _SUBJECT[subjects]
        self._cache: dict[str, LoadedPolicy] = {}
        self._lock = threading.Lock()

    def policy(self, digest: str) -> LoadedPolicy:
        """Load the policy with this revision; raise StoreError if it is missing or invalid."""
        check_digest(digest)
        with self._lock:
            cached = self._cache.get(digest)
        if cached is not None:
            return cached
        path = self._policy_path(digest)
        try:
            loaded = parse_policy(read_limited(path), source=str(path))
        except PolicyLoadError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                raise StoreError(f"no policy with revision {digest}") from exc
            raise StoreError(f"policy revision {digest} cannot be loaded: {exc}") from exc
        if loaded.digest != digest:
            raise StoreError(f"{path} does not hold revision {digest}")
        with self._lock:
            if len(self._cache) >= _MAX_CACHED_POLICIES:
                self._cache.clear()
            self._cache[digest] = loaded
        return loaded

    def binding(self, name: str) -> str | None:
        """Return the revision a sandbox or project is bound to, or None if it is not bound."""
        what = f"binding for {self._subject} {name}"
        path = self._binding_path(name)
        try:
            with path.open("rb") as handle:
                data = handle.read(_MAX_BINDING_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"{what} cannot be read: {exc}") from exc
        if len(data) > _MAX_BINDING_BYTES:
            raise StoreError(f"{what} is too large")
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(f"{what} is not JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise StoreError(f"{what} must be a JSON object")
        fields = cast("dict[str, object]", document)
        digest = fields.get("policy")
        if set(fields) != {"policy"} or not isinstance(digest, str):
            raise StoreError(f'{what} must be {{"policy": "<revision>"}}')
        return check_digest(digest)

    def add_policy(self, data: bytes) -> LoadedPolicy:
        """Validate a policy file's bytes and store them under their revision."""
        try:
            loaded = parse_policy(data)
        except PolicyLoadError as exc:
            raise StoreError(f"policy is invalid: {exc}") from exc
        _write_atomic(self._policy_path(loaded.digest), data)
        return loaded

    def bind(self, name: str, digest: str) -> None:
        """Bind a sandbox or project to a stored revision, replacing any earlier binding."""
        path = self._binding_path(name)
        self.policy(digest)
        document = json.dumps({"policy": digest}, sort_keys=True) + "\n"
        _write_atomic(path, document.encode())

    def unbind(self, name: str) -> bool:
        """Remove a binding; return whether there was one."""
        try:
            self._binding_path(name).unlink()
        except FileNotFoundError:
            return False
        return True

    # Project classification revisions (D96, CL-3) and drafts (CL-2).

    def classification(self, project: str, server: str) -> ServerClassification | None:
        """The project's accepted revision of a server's classification in force, or None
        when it has none and the catalog's applies. Raises StoreError."""
        revision = self.classification_binding(project, server)
        return (
            self.classification_revision(project, server, revision)
            if revision is not None
            else None
        )

    def classification_binding(self, project: str, server: str) -> str | None:
        """The revision (hex) in force for the project and server, or None."""
        path = self._revisions(project, server) / _CURRENT
        document = _read_json(path, f"classification binding of {project}/{server}", 4096)
        if document is None:
            return None
        revision = document.get("revision")
        if set(document) != {"revision"} or not isinstance(revision, str):
            raise StoreError(f'{path} must be {{"revision": "<hex>"}}')
        return check_hex(revision)

    def classification_revision(
        self, project: str, server: str, revision: str
    ) -> ServerClassification:
        """A stored revision of the project's classification of a server. Raises StoreError."""
        path = self._revisions(project, server) / f"{check_hex(revision)}.yaml"
        return _load_classification(path, server, revision, "classification revision")

    def classification_revision_bytes(self, project: str, server: str, revision: str) -> bytes:
        """A stored revision's file, exactly as accepted, for export: StoreError when the
        project has no such revision of the server's classification."""
        path = self._revisions(project, server) / f"{check_hex(revision)}.yaml"
        try:
            data = read_limited(path)
        except PolicyLoadError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                raise StoreError(f"no classification revision {revision} of {server}") from exc
            raise StoreError(f"classification revision {revision} cannot be read: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != revision:
            raise StoreError(f"{path} does not hold classification revision {revision}")
        return data

    def acceptance(self, project: str, server: str, revision: str) -> Acceptance | None:
        """The record of a revision's acceptance, or None if it has none."""
        path = self._revisions(project, server) / f"{check_hex(revision)}.json"
        document = _read_json(path, f"acceptance of {revision}", _MAX_RECORD_BYTES)
        if document is None:
            return None
        try:
            diff = cast("dict[str, list[str]]", document.get("diff") or {})
            return Acceptance(
                accepted_by=str(document["accepted_by"]),
                accepted_at=datetime.fromisoformat(str(document["accepted_at"])),
                reason=str(document["reason"]),
                draft=cast("str | None", document.get("draft")),
                replaces=cast("str | None", document.get("replaces")),
                diff={str(key): [str(name) for name in names] for key, names in diff.items()},
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise StoreError(f"{path} is not an acceptance record: {exc}") from exc

    def accept_classification(
        self, project: str, server: str, data: bytes, acceptance: Acceptance
    ) -> ServerClassification:
        """Validate a classification file for the server, store it as a revision with its
        acceptance record, and put it in force for the project. Raises StoreError."""
        try:
            found = revision_classification(data, check_server(server))
        except PolicyLoadError as exc:
            raise StoreError(f"the classification is invalid: {exc}") from exc
        directory = self._revisions(project, server)
        revision = cast(str, found.revision)
        _write_atomic(directory / f"{revision}.yaml", data)
        record = json.dumps(acceptance.document(), sort_keys=True) + "\n"
        _write_atomic(directory / f"{revision}.json", record.encode())
        current = json.dumps({"revision": revision}, sort_keys=True) + "\n"
        _write_atomic(directory / _CURRENT, current.encode())
        return found

    def restore_classification(self, project: str, server: str, revision: str | None) -> None:
        """Put a stored revision back in force for the project and server, or, with None,
        none (the catalog's applies again). Raises StoreError."""
        path = self._revisions(project, server) / _CURRENT
        if revision is None:
            path.unlink(missing_ok=True)
            return
        self.classification_revision(project, server, revision)
        current = json.dumps({"revision": check_hex(revision)}, sort_keys=True) + "\n"
        _write_atomic(path, current.encode())

    def add_classification_draft(
        self, project: str, server: str, data: bytes, *, created_at: datetime, model: str | None
    ) -> Draft:
        """Store a model's draft for the project's server, once it loads as a classification
        file of that server would. Raises StoreError."""
        try:
            found = revision_classification(data, check_server(server))
        except PolicyLoadError as exc:
            raise StoreError(f"the draft is not a classification of {server}: {exc}") from exc
        directory = self._draft_directory(project, server)
        draft = cast(str, found.revision)
        _write_atomic(directory / f"{draft}.yaml", data)
        meta = {"created_at": created_at.isoformat(), "model": model}
        _write_atomic(
            directory / f"{draft}.json", (json.dumps(meta, sort_keys=True) + "\n").encode()
        )
        return Draft(draft, server, created_at, model, data, found)

    def classification_draft(self, project: str, server: str, draft: str) -> Draft | None:
        """One stored draft, or None if there is none by that name. Raises StoreError."""
        directory = self._draft_directory(project, server)
        path = directory / f"{check_hex(draft, 'classification draft')}.yaml"
        if not path.is_file():
            return None
        meta = _read_json(directory / f"{draft}.json", f"draft {draft}", 4096) or {}
        try:
            created_at = datetime.fromisoformat(str(meta["created_at"]))
        except (KeyError, ValueError) as exc:
            raise StoreError(f"draft {draft} has no creation time") from exc
        model = meta.get("model")
        found = _load_classification(path, server, draft, "classification draft")
        data = read_limited(path)
        return Draft(
            draft, server, created_at, model if isinstance(model, str) else None, data, found
        )

    def classification_drafts(self, project: str, server: str) -> list[Draft]:
        """The project's drafts for a server, newest first, at most ``MAX_DRAFTS``."""
        directory = self._draft_directory(project, server)
        if not directory.is_dir():
            return []
        drafts = [
            found
            for path in directory.glob("*.yaml")
            if HEX_RE.fullmatch(path.stem)
            and (found := self.classification_draft(project, server, path.stem)) is not None
        ]
        drafts.sort(key=lambda found: (found.created_at, found.draft), reverse=True)
        return drafts[:MAX_DRAFTS]

    def _revisions(self, project: str, server: str) -> Path:
        return self._classifications / check_name(project, "project") / check_server(server)

    def _draft_directory(self, project: str, server: str) -> Path:
        return self._drafts / check_name(project, "project") / check_server(server)

    def _policy_path(self, digest: str) -> Path:
        return self._policies / f"{digest.removeprefix('sha256:')}.yaml"

    def _binding_path(self, name: str) -> Path:
        return self._bindings / f"{check_name(name, self._subject)}.json"


def _read_json(path: Path, what: str, limit: int) -> dict[str, object] | None:
    """A small JSON object file, or None when there is none. Raises StoreError."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"{what} cannot be read: {exc}") from exc
    if len(data) > limit:
        raise StoreError(f"{what} is too large")
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise StoreError(f"{what} must be a JSON object")
    return cast("dict[str, object]", document)


def _load_classification(path: Path, server: str, name: str, what: str) -> ServerClassification:
    """A stored classification file that must hold exactly the bytes ``name`` digests."""
    try:
        data = read_limited(path)
    except PolicyLoadError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise StoreError(f"no {what} {name} of {server}") from exc
        raise StoreError(f"{what} {name} cannot be read: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != name:
        raise StoreError(f"{path} does not hold {what} {name}")
    try:
        return revision_classification(data, server, source=str(path))
    except PolicyLoadError as exc:
        raise StoreError(f"{what} {name} cannot be loaded: {exc}") from exc


def _write_atomic(path: Path, data: bytes) -> None:
    """Write a file so that readers see either the old or the complete new content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
