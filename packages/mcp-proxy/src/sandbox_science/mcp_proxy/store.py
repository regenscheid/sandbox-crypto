# SPDX-License-Identifier: MIT
"""Project policies by revision, and the revision each project's MCP calls are decided under.

The store has the policy middleware's layout (docs/POLICY-MIDDLEWARE.md,
"Policy store"), so one directory can serve both:

- ``policies/<hex>.yaml`` holds a project policy file, named by the hex
  SHA-256 of its bytes, so that a file cannot stand in for another revision;
- ``projects/<project>.json`` binds a project to one revision, as
  ``sandboxes/<sandbox id>.json`` binds a sandbox for the middleware.

The binding is read for every call, so a new revision applies to the next
call without restarting the proxy.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Final, cast

from sandbox_science.policy import LoadedPolicy, PolicyLoadError, parse_policy
from sandbox_science.policy.loader import read_limited

from .errors import StoreError

DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
"""A policy revision: ``sha256:`` and the hex SHA-256 of the policy file."""

PROJECT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""Project ids (the contracts' ``Identifier``); also safe as file names."""

_MAX_BINDING_BYTES: Final = 4096
_MAX_CACHED_POLICIES: Final = 256


def check_digest(digest: str) -> str:
    """Return ``digest`` if it is a well-formed revision, else raise StoreError."""
    if not DIGEST_RE.fullmatch(digest):
        raise StoreError(f"{digest!r} is not a policy revision (sha256:<64 hex digits>)")
    return digest


def check_project(project: str) -> str:
    """Return ``project`` if it is a well-formed project id, else raise StoreError."""
    if not PROJECT_RE.fullmatch(project):
        raise StoreError(f"{project!r} is not a valid project id")
    return project


class PolicyStore:
    """Read and write policies and project bindings under one directory."""

    def __init__(self, root: Path) -> None:
        self._policies = root / "policies"
        self._projects = root / "projects"
        self._cache: dict[str, LoadedPolicy] = {}
        self._lock = threading.Lock()

    def policy(self, digest: str) -> LoadedPolicy:
        """Load the policy with this revision; raise StoreError if it is missing or invalid."""
        check_digest(digest)
        with self._lock:
            cached = self._cache.get(digest)
        if cached is not None:
            return cached
        path = self._policies / f"{digest.removeprefix('sha256:')}.yaml"
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

    def binding(self, project: str) -> str | None:
        """Return the revision a project is bound to, or None if it is not bound."""
        path = self._projects / f"{check_project(project)}.json"
        try:
            with path.open("rb") as handle:
                data = handle.read(_MAX_BINDING_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"binding for project {project} cannot be read: {exc}") from exc
        if len(data) > _MAX_BINDING_BYTES:
            raise StoreError(f"binding for project {project} is too large")
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(f"binding for project {project} is not JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise StoreError(f"binding for project {project} must be a JSON object")
        fields = cast("dict[str, object]", document)
        digest = fields.get("policy")
        if set(fields) != {"policy"} or not isinstance(digest, str):
            raise StoreError(f'binding for project {project} must be {{"policy": "<revision>"}}')
        return check_digest(digest)

    def add_policy(self, data: bytes) -> LoadedPolicy:
        """Validate a policy file's bytes and store them under their revision."""
        try:
            loaded = parse_policy(data)
        except PolicyLoadError as exc:
            raise StoreError(f"policy is invalid: {exc}") from exc
        _write_atomic(self._policies / f"{loaded.digest.removeprefix('sha256:')}.yaml", data)
        return loaded

    def bind(self, project: str, digest: str) -> None:
        """Bind a project to a stored revision, replacing any earlier binding."""
        check_project(project)
        self.policy(digest)
        document = json.dumps({"policy": digest}, sort_keys=True) + "\n"
        _write_atomic(self._projects / f"{project}.json", document.encode())

    def unbind(self, project: str) -> bool:
        """Remove a project's binding; return whether there was one."""
        path = self._projects / f"{check_project(project)}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


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
