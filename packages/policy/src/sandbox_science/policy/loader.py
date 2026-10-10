# SPDX-License-Identifier: MIT
"""Loading policy and pool-ceiling files.

YAML is read with a safe loader that also rejects duplicate keys, aliases and
merge keys, so that what a reader sees in the file is exactly what is enforced.
"""

from __future__ import annotations

import hashlib
from collections.abc import Hashable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, Final, Protocol, cast

import yaml
from pydantic import BaseModel, ValidationError
from yaml.nodes import MappingNode, Node

from .ceiling import PoolCeiling
from .errors import PolicyLoadError
from .model import ProjectPolicy

MAX_FILE_BYTES: Final = 1024 * 1024
"""Policy and ceiling files larger than this are rejected."""

_MERGE_TAG: Final = "tag:yaml.org,2002:merge"


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
    """A SafeLoader that rejects aliases, merge keys and duplicate keys."""

    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        if self.check_event(yaml.AliasEvent):
            # The stubs leave peek_event untyped.
            event = cast("yaml.Event", self.peek_event())  # pyright: ignore[reportUnknownMemberType]
            raise PolicyLoadError(f"YAML aliases are not allowed ({_where(event.start_mark)})")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                raise PolicyLoadError(
                    f"YAML merge keys are not allowed ({_where(key_node.start_mark)})"
                )
            key: object = self.construct_object(key_node, deep=deep)  # pyright: ignore[reportUnknownMemberType]
            if not isinstance(key, Hashable):
                raise PolicyLoadError(
                    f"mapping keys must be scalars ({_where(key_node.start_mark)})"
                )
            if key in seen:
                raise PolicyLoadError(f"duplicate key {key!r} ({_where(key_node.start_mark)})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _label(source: str | None, kind: str) -> str:
    return f"{kind} {source}" if source else kind


def _load_mapping(data: bytes, source: str | None, kind: str) -> dict[str, object]:
    label = _label(source, kind)
    if len(data) > MAX_FILE_BYTES:
        raise PolicyLoadError(f"{label} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyLoadError(f"{label} is not valid UTF-8") from exc
    loader = _StrictLoader(text)
    try:
        document: object = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"{label} is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise PolicyLoadError(f"{label} is nested too deeply") from exc
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
