# SPDX-License-Identifier: MIT
"""A project's settings for the files its executions write (research artifacts, RA-29,
RA-39): whether every written file is recorded (``all``, the default) or only results
(``results``), and the ignore patterns that leave files out.

Patterns follow ``.gitignore``: ``*`` matches within a name, ``**`` across directories,
``?`` one character and ``[…]`` a class; a pattern with a ``/`` before its end is
matched from the area's top, one without from any directory; a trailing ``/`` matches
a directory and everything in it, and a pattern that names a directory leaves out
everything in it too. Paths are matched inside their area: the session workspace, or
``shared/`` without that prefix. Negation (``!``) is not supported. Only the user
changes the settings; a change names the revision it read and an operation ID.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import Field, StringConstraints, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.control import Receipts

from .documents.errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .documents.models import Author

if TYPE_CHECKING:
    from .documents.store import DocumentStore

Recording = Literal["all", "results"]
DEFAULT_IGNORE: Final = (
    ".jobs/",
    ".cache/",
    "__pycache__/",
    "*.pyc",
    ".ipynb_checkpoints/",
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "build/",
    "dist/",
    "*.egg-info/",
    "tmp/",
    "*.tmp",
    "*.swp",
    "*~",
    ".DS_Store",
)
"""RA-29's defaults: caches, logs of background jobs, version control, environments,
build output and editors' temporary files."""
MAX_PATTERNS: Final = 200
Pattern = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=256, pattern=r"^[^!\x00-\x1f\x7f][^\x00-\x1f\x7f]*$"
    ),
]
_RECEIPTS = Receipts("files", "operation ID already names another file settings change")


class FileSettings(Contract):
    """A project's file settings, at a revision (0: the defaults, never changed)."""

    project: str
    revision: int
    recording: Recording = "all"
    ignore: tuple[str, ...] = DEFAULT_IGNORE


class FileSettingsChange(Contract):
    expected: int = Field(ge=0, le=999999999, strict=True)
    recording: Recording
    ignore: list[Pattern] = Field(max_length=MAX_PATTERNS)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProjectFiles:
    """The file settings of the control store's projects."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def get(self, project: str) -> FileSettings:
        _scope(project)
        if self.documents.control.missing:
            return FileSettings(project=project, revision=0)
        with self.documents.control.read() as tx:
            return _read(tx.sql, project)

    def change(self, project: str, value: object, author: Author) -> tuple[FileSettings, bool]:
        """Apply a USER change; a retried operation returns the first result and
        ``True``."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user changes a project's file settings")
        try:
            change = FileSettingsChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid file settings change") from None
        for pattern in change.ignore:
            compile_pattern(pattern)
        fingerprint = hashlib.sha256(
            json.dumps(change.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        with self.documents.control.write() as tx:
            db = tx.sql
            replay = _RECEIPTS.find(db, project, author.actor, change.operation_id, fingerprint)
            if replay is not None:
                return _read(db, project), True
            current = _read(db, project)
            if current.revision != change.expected:
                raise DocumentError(CONFLICT, "the project's file settings changed meanwhile")
            if (current.recording, list(current.ignore)) != (change.recording, change.ignore):
                db.execute(
                    "INSERT INTO project_file_settings VALUES(?,?,?,?,?) ON CONFLICT(project) "
                    "DO UPDATE SET revision=excluded.revision,recording=excluded.recording,"
                    "ignore=excluded.ignore,author=excluded.author",
                    (
                        project,
                        current.revision + 1,
                        change.recording,
                        json.dumps(change.ignore),
                        author.model_dump_json(),
                    ),
                )
            if _RECEIPTS.count(db, project) >= 4096:
                raise DocumentError(CONFLICT, "the file settings' operation quota is reached")
            result = _read(db, project)
            _RECEIPTS.record(
                db, project, author.actor, change.operation_id, fingerprint,
                {"revision": result.revision},
            )  # fmt: skip
            return result, False


def _read(db: sqlite3.Connection, project: str) -> FileSettings:
    row = db.execute(
        "SELECT revision,recording,ignore FROM project_file_settings WHERE project=?", (project,)
    ).fetchone()
    if row is None:
        return FileSettings(project=project, revision=0)
    try:
        return FileSettings(
            project=project, revision=row[0], recording=row[1], ignore=tuple(json.loads(row[2]))
        )
    except (ValidationError, ValueError, TypeError):
        raise DocumentError(INTERNAL, "invalid stored file settings") from None


def _scope(project: object) -> None:
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
    ):
        raise DocumentError(INVALID_REQUEST, "invalid project")


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """The regular expression of one ignore pattern, over a path inside its area."""
    directory = pattern.endswith("/")
    body = pattern.rstrip("/")
    anchored = "/" in body
    parts = body.lstrip("/").split("/")
    if not body or any(part == "" or part in (".", "..") for part in parts):
        raise DocumentError(INVALID_REQUEST, f"invalid ignore pattern {pattern!r}")
    regex = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            regex += ".*" if last else "(?:[^/]+/)*"
        else:
            regex += _component(part, pattern) + ("" if last else "/")
    lead = "" if anchored else "(?:[^/]+/)*"
    tail = "/.+" if directory else "(?:/.+)?"
    return re.compile(f"{lead}{regex}{tail}")


def _component(part: str, pattern: str) -> str:
    out, index = "", 0
    while index < len(part):
        char = part[index]
        if char == "*":
            out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "[":
            end = part.find("]", index + 2)
            if end < 0:
                raise DocumentError(INVALID_REQUEST, f"invalid ignore pattern {pattern!r}")
            inner = part[index + 1 : end]
            negated = inner.startswith("!")
            inner = inner[1:] if negated else inner
            out += "[" + ("^" if negated else "") + inner.replace("\\", "\\\\") + "]"
            index = end
        else:
            out += re.escape(char)
        index += 1
    return out


class IgnoreRules:
    """The compiled ignore patterns of a project's settings."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns: Sequence[re.Pattern[str]] = tuple(compile_pattern(p) for p in patterns)

    def ignores(self, path: str) -> bool:
        """Whether a workspace path (``shared/…`` or a session path) is left out."""
        inside = path.removeprefix("shared/") if path.startswith("shared/") else path
        return any(pattern.fullmatch(inside) for pattern in self.patterns)
