# SPDX-License-Identifier: MIT
"""Loading policy and pool-ceiling files.

Policy files are read as a strict YAML dialect so that what a reviewer sees in
the file is exactly what is enforced: no explicit tags, aliases, merge keys or
duplicate keys; mapping keys are text; every key has a value; numbers are plain
decimals; booleans are true or false; double-quoted strings have no backslash
escapes; lines are narrow enough to see and have no runs of blanks inside them;
and no invisible characters, unusual whitespace or hidden line breaks.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, Final, Protocol, cast

import yaml
from pydantic import BaseModel, ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode

from .ceiling import PoolCeiling
from .errors import PolicyLoadError
from .model import ProjectPolicy

MAX_FILE_BYTES: Final = 1024 * 1024
"""Policy and ceiling files larger than this are rejected."""

MAX_LINE_COLUMNS: Final = 160
"""Wider lines could hide an entry beyond the edge of an editor or diff view."""

MAX_INDENT_COLUMNS: Final = 48
"""Deeper indentation could push an entry off-screen on an otherwise blank-looking line."""

# A run of four or more spaces inside a line could push the rest of the line out
# of view; it is allowed only to align a comment.
_BLANK_RUN_RE: Final = re.compile(r" {4,}")

# The stubs leave yaml.scan and token marks untyped.
_scan = cast("Callable[..., Iterable[object]]", yaml.scan)  # pyright: ignore[reportUnknownMemberType]

_TAG: Final = "tag:yaml.org,2002:"
# Scalar forms accepted for each implicitly resolved type. YAML 1.1 would also read
# 0443 as octal 291, 1:20 as 80, 1_000 as 1000 and yes/on as true.
_SCALAR_FORMS: Final = {
    _TAG + "str": None,
    _TAG + "int": re.compile(r"-?(?:0|[1-9][0-9]{0,30})"),
    _TAG + "float": re.compile(r"-?(?:0|[1-9][0-9]{0,30})\.[0-9]{1,30}(?:[eE][-+]?[0-9]{1,3})?"),
    _TAG + "bool": re.compile(r"true|false|True|False|TRUE|FALSE"),
}
_COLLECTION_TAGS: Final = frozenset({_TAG + "seq", _TAG + "map"})
_SCALAR_HINTS: Final = {
    "int": "write numbers in plain decimal, ",
    "float": "write numbers in plain decimal, ",
    "bool": "write booleans as true or false, ",
}
# Control characters (tab included: use spaces), NEL, Unicode line and paragraph
# separators (which YAML treats as line breaks), zero-width and bidirectional
# formatting characters, a byte-order mark after the start, and a CR that is not
# part of CRLF.
_FORBIDDEN_TEXT_RE: Final = re.compile(
    "[\x00-\x08\x09\x0b\x0c\x0e-\x1f\x7f\x85\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e"
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\u2800\u3164"
    "\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f\U000e0100-\U000e01ef"
    # Whitespace other than ASCII space, which can pad a line invisibly.
    "\xa0\u1680\u2000-\u200a\u202f\u205f\u3000]|\r(?!\n)"
)


@dataclass(frozen=True, slots=True)
class LoadedPolicy:
    """A validated policy plus the digest that identifies its exact revision."""

    policy: ProjectPolicy
    digest: str
    """``sha256:`` followed by the hex SHA-256 of the file's bytes."""
    source: str | None = None


class _Mark(Protocol):
    line: int
    column: int


class _IndexedMark(_Mark, Protocol):
    index: int


class _Token(Protocol):
    start_mark: _IndexedMark
    end_mark: _IndexedMark


def _where(mark: _Mark | None) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown"


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader for the strict policy dialect described in the module docstring."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._text = text

    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        # The stubs leave peek_event untyped.
        event = cast("yaml.Event", self.peek_event())  # pyright: ignore[reportUnknownMemberType]
        if isinstance(event, yaml.AliasEvent):
            raise PolicyLoadError(f"YAML aliases are not allowed ({_where(event.start_mark)})")
        if cast("str | None", getattr(event, "tag", None)) is not None:
            raise PolicyLoadError(f"YAML tags are not allowed ({_where(event.start_mark)})")
        return super().compose_node(parent, index)

    def construct_object(self, node: Node, deep: bool = False) -> Any:
        tag = node.tag
        if isinstance(node, ScalarNode):
            if node.style in ("|", ">"):
                raise PolicyLoadError(
                    "block scalars are not allowed; use a quoted string "
                    f"({_where(node.start_mark)})"
                )
            if tag == _TAG + "null":
                raise PolicyLoadError(
                    f"a value is missing; give one or remove the key ({_where(node.start_mark)})"
                )
            if (
                node.style == '"'
                and "\\" in self._text[node.start_mark.index : node.end_mark.index]
            ):
                raise PolicyLoadError(
                    "backslash escapes are not allowed in double-quoted strings; write the "
                    f"characters themselves ({_where(node.start_mark)})"
                )
            if tag not in _SCALAR_FORMS:
                raise PolicyLoadError(
                    f"value {node.value!r} would be read as {tag.removeprefix(_TAG)}; "
                    f"quote it if it is text ({_where(node.start_mark)})"
                )
            form = _SCALAR_FORMS[tag]
            if form is not None and not form.fullmatch(node.value):
                kind = tag.removeprefix(_TAG)
                hint = _SCALAR_HINTS.get(kind, "")
                raise PolicyLoadError(
                    f"{node.value!r} would be read as a {kind}; {hint}quote it if it is text "
                    f"({_where(node.start_mark)})"
                )
        elif tag not in _COLLECTION_TAGS:
            raise PolicyLoadError(f"YAML type {tag} is not allowed ({_where(node.start_mark)})")
        return super().construct_object(node, deep=deep)  # pyright: ignore[reportUnknownMemberType]

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise PolicyLoadError(f"expected a mapping ({_where(node.start_mark)})")
        seen: set[str] = set()
        for key_node, _value_node in node.value:
            key: object = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise PolicyLoadError(f"mapping keys must be text ({_where(key_node.start_mark)})")
            if key in seen:
                raise PolicyLoadError(f"duplicate key {key!r} ({_where(key_node.start_mark)})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _label(source: str | None, kind: str) -> str:
    return f"{kind} {source}" if source else kind


def _display_width(line: str) -> int:
    width = 0
    for char in line:
        if char == "\t":
            width += 8 - width % 8
        elif unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _token_coverage(text: str, label: str) -> bytearray:
    """Mark the characters that belong to YAML tokens (everything but blanks and comments).

    Positions come from PyYAML's scanner, which is what parses the file, so
    quoted strings and comments are located exactly as YAML sees them. Also
    rejects values that span lines, which could swallow lines that look like
    live settings.
    """
    covered = bytearray(len(text))
    try:
        scanned = list(_scan(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"{label} is not valid YAML: {exc}") from exc
    for raw in scanned:
        token = cast("_Token", raw)
        start, end = token.start_mark.index, token.end_mark.index
        if isinstance(raw, yaml.ScalarToken) and token.start_mark.line != token.end_mark.line:
            raise PolicyLoadError(
                f"{label}: every value must fit on one line ({_where(token.start_mark)})"
            )
        covered[start:end] = b"\x01" * (end - start)
    return covered


def _check_layout(text: str, label: str) -> None:
    """Reject layouts that could hide content from someone reading the file.

    Limits width and indentation, keeps enforced values in ASCII, and allows a
    run of spaces inside a line only to align a comment.
    """
    covered = _token_coverage(text, label)
    offset = 0
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        base = offset
        offset += len(raw) + 1
        if _display_width(line) > MAX_LINE_COLUMNS:
            raise PolicyLoadError(f"{label} line {number} is wider than {MAX_LINE_COLUMNS} columns")
        indent = len(line) - len(line.lstrip(" "))
        if indent > MAX_INDENT_COLUMNS and line.strip():
            raise PolicyLoadError(
                f"{label} line {number} is indented by more than {MAX_INDENT_COLUMNS} columns"
            )
        comment = next(
            (
                index
                for index, char in enumerate(line)
                if char == "#"
                and not covered[base + index]
                and (index == 0 or line[index - 1] == " ")
            ),
            len(line),
        )
        if any(not line[index].isascii() for index in range(comment)):
            raise PolicyLoadError(
                f"{label} line {number}: write settings in ASCII; other characters are allowed "
                "only in comments"
            )
        for run in _BLANK_RUN_RE.finditer(line, indent, comment):
            inside_value = any(covered[base + run.start() : base + run.end()])
            if run.end() in (len(line), comment) and not inside_value:
                continue  # trailing blanks, or aligning a comment
            raise PolicyLoadError(
                f"{label} line {number} has a run of blanks that could push content out of "
                "view; use single spaces"
            )


def _check_text(text: str, label: str) -> None:
    match = _FORBIDDEN_TEXT_RE.search(text)
    if match is not None:
        index = match.start()
        line = text.count("\n", 0, index) + 1
        column = index - text.rfind("\n", 0, index)
        raise PolicyLoadError(
            f"{label} contains an invisible or line-breaking character "
            f"U+{ord(match.group()[0]):04X} at line {line}, column {column}"
        )
    _check_layout(text, label)


def _load_mapping(data: bytes, source: str | None, kind: str) -> dict[str, object]:
    label = _label(source, kind)
    if len(data) > MAX_FILE_BYTES:
        raise PolicyLoadError(f"{label} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyLoadError(f"{label} is not valid UTF-8") from exc
    text = text.removeprefix("\ufeff")
    _check_text(text, label)
    loader = _StrictLoader(text)
    try:
        document: object = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"{label} is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise PolicyLoadError(f"{label} is nested too deeply") from exc
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        # PyYAML's constructors raise these on malformed values; never let them escape.
        raise PolicyLoadError(f"{label} has a value that cannot be read: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(document, dict):
        raise PolicyLoadError(f"{label} must contain a mapping at the top level")
    return cast("dict[str, object]", document)


def _validate[M: BaseModel](model: type[M], document: dict[str, object], label: str) -> M:
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        details = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc']) or '<top level>'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise PolicyLoadError(f"{label} is invalid:\n{details}") from exc


def _read(path: str | PathLike[str]) -> bytes:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise PolicyLoadError(f"cannot read {path}: {exc}") from exc


def _as_bytes(text: str | bytes, label: str) -> bytes:
    if isinstance(text, bytes):
        return text
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PolicyLoadError(f"{label} cannot be encoded as UTF-8") from exc


def parse_policy(text: str | bytes, *, source: str | None = None) -> LoadedPolicy:
    """Parse and validate a policy file's contents. Raises PolicyLoadError."""
    data = _as_bytes(text, _label(source, "policy"))
    document = _load_mapping(data, source, "policy")
    policy = _validate(ProjectPolicy, document, _label(source, "policy"))
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return LoadedPolicy(policy=policy, digest=digest, source=source)


def load_policy(path: str | PathLike[str]) -> LoadedPolicy:
    """Read, parse and validate a policy file. Raises PolicyLoadError."""
    return parse_policy(_read(path), source=str(path))


def parse_ceiling(text: str | bytes, *, source: str | None = None) -> PoolCeiling:
    """Parse and validate a pool-ceiling file's contents. Raises PolicyLoadError."""
    data = _as_bytes(text, _label(source, "pool ceiling"))
    document = _load_mapping(data, source, "pool ceiling")
    return _validate(PoolCeiling, document, _label(source, "pool ceiling"))


def load_ceiling(path: str | PathLike[str]) -> PoolCeiling:
    """Read, parse and validate a pool-ceiling file. Raises PolicyLoadError."""
    return parse_ceiling(_read(path), source=str(path))
