# SPDX-License-Identifier: MIT
"""Project policies by revision, and bindings that name the revision a subject is decided under.

A store is a directory that the policy middleware, the worker and the MCP proxy share
the layout of:

- ``policies/<hex>.yaml`` holds a project policy file, named by the hex SHA-256 of its
  bytes, so that a file cannot stand in for another revision;
- ``sandboxes/<sandbox id>.json`` binds an OpenShell sandbox to one revision, for the
  policy middleware, which the worker writes when it creates or revises a sandbox;
- ``projects/<project>.json`` binds a project's MCP calls to one revision, for the MCP
  proxy.

A binding is ``{"policy": "sha256:<hex>"}``. Bindings are read on every use, so a new
revision applies without restarting a reader.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Final, Literal, cast

from .errors import PolicyLoadError
from .loader import LoadedPolicy, parse_policy, read_limited

DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
"""A policy revision: ``sha256:`` and the hex SHA-256 of the policy file."""

NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""Sandbox and project IDs (the contracts' ``Identifier``); also safe as file names."""

Subjects = Literal["sandboxes", "projects"]
"""What a store's bindings bind: sandboxes for the policy middleware, projects for the MCP proxy."""

_MAX_BINDING_BYTES: Final = 4096
_MAX_CACHED_POLICIES: Final = 256
_SUBJECT: Final[dict[str, str]] = {"sandboxes": "sandbox", "projects": "project"}


class StoreError(Exception):
    """A policy or a binding could not be read, validated or written."""


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

    def _policy_path(self, digest: str) -> Path:
        return self._policies / f"{digest.removeprefix('sha256:')}.yaml"

    def _binding_path(self, name: str) -> Path:
        return self._bindings / f"{check_name(name, self._subject)}.json"


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
