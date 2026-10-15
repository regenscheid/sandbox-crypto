# SPDX-License-Identifier: MIT
"""Writing the TOML of the parts' settings: tables of strings, numbers, booleans and lists.

The standard library reads TOML but does not write it. This writes only what the
launcher makes: a table's plain values first, then its tables, then its arrays of
tables. ``tomllib`` reads it back as the same values (``test_toml.py``).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

_BARE = re.compile(r"[A-Za-z0-9_-]+")


def dumps(table: Mapping[str, Any]) -> str:
    """``table`` as a TOML document."""
    lines: list[str] = []
    _table(table, (), lines)
    return "\n".join(lines).lstrip("\n") + "\n"


def _table(table: Mapping[str, Any], path: tuple[str, ...], lines: list[str]) -> None:
    tables = {
        key: cast(Mapping[str, Any], value)
        for key, value in table.items()
        if isinstance(value, Mapping)
    }
    arrays = {key: value for key, value in table.items() if _is_array_of_tables(value) and value}
    for key, value in table.items():
        if key not in tables and key not in arrays and value is not None:
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in tables.items():
        lines.extend(["", f"[{_dotted((*path, key))}]"])
        _table(value, (*path, key), lines)
    for key, value in arrays.items():
        for item in cast(Sequence[Mapping[str, Any]], value):
            lines.extend(["", f"[[{_dotted((*path, key))}]]"])
            _table(item, (*path, key), lines)


def _is_array_of_tables(value: object) -> bool:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return False
    return all(isinstance(item, Mapping) for item in cast(Sequence[object], value))


def _key(key: str) -> str:
    return key if _BARE.fullmatch(key) else _string(key)


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(_key(key) for key in path)


def _string(value: str) -> str:
    # JSON's escapes are TOML's too; TOML also wants DEL escaped.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _value(value: object) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float() if math.isfinite(value):
            return repr(value)
        case str():
            return _string(value)
        case Mapping():
            # An inline table, as in an array of names and tables.
            items = cast(Mapping[str, object], value).items()
            return "{ " + ", ".join(f"{_key(key)} = {_value(item)}" for key, item in items) + " }"
        case Sequence():
            items = cast(Sequence[object], value)
            return "[" + ", ".join(_value(item) for item in items) + "]"
        case _:
            raise TypeError(f"cannot write {type(value).__name__} {value!r} as TOML")
