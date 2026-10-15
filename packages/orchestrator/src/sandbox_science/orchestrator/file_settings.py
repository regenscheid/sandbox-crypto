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
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

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
_PATTERN: Final = TypeAdapter[str](Pattern)


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
        patterns = cast(dict[str, object], value).get("ignore") if isinstance(value, dict) else None
        if isinstance(patterns, list):
            for line, pattern in enumerate(cast(list[object], patterns), 1):
                try:
                    _PATTERN.validate_python(pattern)
                    compile_pattern(cast(str, pattern))
                except (ValidationError, DocumentError):
                    raise DocumentError(
                        INVALID_REQUEST, f"line {line}: invalid ignore pattern {pattern!r}"[:300]
                    ) from None
        try:
            change = FileSettingsChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid file settings change") from None
        if len(json.dumps(change.ignore).encode()) > 65536:
            raise DocumentError(INVALID_REQUEST, "the ignore patterns exceed 64 KiB")
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


MAX_NAME_BYTES: Final = 255
"""The longest name of a path's segment that is matched; a longer one is refused."""
_Token = tuple[str, str]
"""``("*", "")``, ``("?", "")``, ``("[", class)`` or ``("=", character)``."""


@dataclass(frozen=True)
class Glob:
    """One ignore pattern, matched segment by segment: no regular expression, so a name
    built to make a matcher backtrack costs at most its length times the pattern's."""

    segments: tuple[tuple[_Token, ...] | None, ...]
    """Each segment's tokens; ``None`` for ``**``."""
    anchored: bool
    directory: bool

    def matches(self, names: Sequence[str]) -> bool:
        """Whether the path, or a directory on its way, matches."""
        pattern = self.segments if self.anchored else (None, *self.segments)
        count = len(names)
        reached = {0}
        for index, segment in enumerate(pattern):
            if segment is None:
                # ** stands for any number of segments; at the end, at least one.
                low = min(reached) + (1 if index == len(pattern) - 1 else 0)
                reached = set(range(low, count + 1))
            else:
                reached = {j + 1 for j in reached if j < count and _name(segment, names[j])}
            if not reached:
                return False
        return any(1 <= m < count if self.directory else m >= 1 for m in reached)


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> Glob:
    """One ignore pattern's matcher, over a path inside its area."""
    directory = pattern.endswith("/")
    body = pattern.rstrip("/")
    anchored = "/" in body
    parts = body.lstrip("/").split("/")
    if not body or any(part == "" or part in (".", "..") for part in parts):
        raise DocumentError(INVALID_REQUEST, f"invalid ignore pattern {pattern!r}")
    segments = tuple(None if part == "**" else _tokens(part, pattern) for part in parts)
    return Glob(segments, anchored, directory)


def _tokens(part: str, pattern: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(part):
        char = part[index]
        if char == "[":
            end = part.find("]", index + 2)
            inner = part[index + 1 : end] if end > 0 else ""
            body = inner[1:] if inner.startswith("!") else inner
            pairs = [body[i : i + 3] for i in range(len(body)) if body[i + 1 : i + 2] == "-"]
            if end < 0 or not body or any(len(r) == 3 and r[0] > r[2] for r in pairs):
                raise DocumentError(INVALID_REQUEST, f"invalid ignore pattern {pattern!r}")
            tokens.append(("[", inner))
            index = end
        elif char in "*?":
            if not (char == "*" and tokens and tokens[-1][0] == "*"):  # ** within a name is *
                tokens.append((char, ""))
        else:
            tokens.append(("=", char))
        index += 1
    return tuple(tokens)


def _one(token: _Token, char: str) -> bool:
    kind, value = token
    if kind == "?":
        return True
    if kind == "=":
        return value == char
    negated = value.startswith("!")
    body = value[1:] if negated else value
    found, index = False, 0
    while index < len(body):
        if index + 2 < len(body) and body[index + 1] == "-":
            found = found or body[index] <= char <= body[index + 2]
            index += 3
        else:
            found = found or body[index] == char
            index += 1
    return found != negated


def _name(tokens: tuple[_Token, ...], name: str) -> bool:
    """Whether a segment's tokens match a name: the usual wildcard walk that goes back
    only to the last ``*``, in at most ``len(name) * len(tokens)`` steps."""
    t = n = 0
    star, mark = -1, 0
    while n < len(name):
        if t < len(tokens) and tokens[t][0] == "*":
            star, mark = t, n
            t += 1
        elif t < len(tokens) and _one(tokens[t], name[n]):
            t += 1
            n += 1
        elif star >= 0:
            t, mark = star + 1, mark + 1
            n = mark
        else:
            return False
    while t < len(tokens) and tokens[t][0] == "*":
        t += 1
    return t == len(tokens)


def names(path: str) -> list[str]:
    """A workspace path's segments inside its area (``shared/`` left out); a path that
    is not a plain relative one, or has a name over 255 bytes, is refused."""
    inside = path.removeprefix("shared/") if path.startswith("shared/") else path
    parts = inside.split("/")
    if (
        not inside
        or any(part in ("", ".", "..") for part in parts)
        or any(len(part.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES for part in parts)
        or "\0" in inside
    ):
        raise ValueError("not a path the ignore patterns match")
    return parts


class IgnoreRules:
    """The compiled ignore patterns of a project's settings."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns: Sequence[Glob] = tuple(compile_pattern(p) for p in patterns)

    def ignores(self, path: str) -> bool:
        """Whether a workspace path (``shared/…`` or a session path) is left out.
        Raises ``ValueError`` for a path ``names`` refuses."""
        parts = names(path)
        return any(pattern.matches(parts) for pattern in self.patterns)
