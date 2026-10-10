# SPDX-License-Identifier: MIT
"""Loading policy and pool-ceiling files.

Policy files are read as a strict YAML dialect so that what a reviewer sees in
the file is exactly what is enforced: no explicit tags, aliases, merge keys or
duplicate keys; mapping keys are text; numbers are plain decimals; booleans are
true or false; and no invisible characters or hidden line breaks.
"""

from __future__ import annotations

import hashlib
import re
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

_TAG: Final = "tag:yaml.org,2002:"
# Scalar forms accepted for each implicitly resolved type. YAML 1.1 would also read
# 0443 as octal 291, 1:20 as 80, 1_000 as 1000 and yes/on as true.
_SCALAR_FORMS: Final = {
    _TAG + "str": None,
    _TAG + "int": re.compile(r"-?(?:0|[1-9][0-9]{0,30})"),
    _TAG + "float": re.compile(r"-?(?:0|[1-9][0-9]{0,30})\.[0-9]{1,30}(?:[eE][-+]?[0-9]{1,3})?"),
    _TAG + "bool": re.compile(r"true|false|True|False|TRUE|FALSE"),
    _TAG + "null": re.compile(r"|~|null|Null|NULL"),
}
_COLLECTION_TAGS: Final = frozenset({_TAG + "seq", _TAG + "map"})
_SCALAR_HINTS: Final = {
    "int": "write numbers in plain decimal, ",
    "float": "write numbers in plain decimal, ",
    "bool": "write booleans as true or false, ",
    "null": "write null, ",
}
# Control characters other than tab and newline, NEL, Unicode line and paragraph
# separators (which YAML treats as line breaks), zero-width and bidirectional
# formatting characters, a byte-order mark after the start, and a CR that is not
# part of CRLF.
_FORBIDDEN_TEXT_RE: Final = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\u200b-\u200f\u2028\u2029"
    "\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]|\r(?!\n)"
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


def _where(mark: _Mark | None) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown"


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader for the strict policy dialect described in the module docstring."""

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


def parse_policy(text: str | bytes, *, source: str | None = None) -> LoadedPolicy:
    """Parse and validate a policy file's contents. Raises PolicyLoadError."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    document = _load_mapping(data, source, "policy")
    policy = _validate(ProjectPolicy, document, _label(source, "policy"))
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return LoadedPolicy(policy=policy, digest=digest, source=source)


def load_policy(path: str | PathLike[str]) -> LoadedPolicy:
    """Read, parse and validate a policy file. Raises PolicyLoadError."""
    return parse_policy(_read(path), source=str(path))


def parse_ceiling(text: str | bytes, *, source: str | None = None) -> PoolCeiling:
    """Parse and validate a pool-ceiling file's contents. Raises PolicyLoadError."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    document = _load_mapping(data, source, "pool ceiling")
    return _validate(PoolCeiling, document, _label(source, "pool ceiling"))


def load_ceiling(path: str | PathLike[str]) -> PoolCeiling:
    """Read, parse and validate a pool-ceiling file. Raises PolicyLoadError."""
    return parse_ceiling(_read(path), source=str(path))
