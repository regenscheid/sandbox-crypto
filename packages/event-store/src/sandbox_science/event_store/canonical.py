# SPDX-License-Identifier: MIT
"""Canonical JSON: RFC 8785 (JCS), restricted to values every implementation writes alike.

Values are objects with string keys, arrays, strings, integers within
±(2**53 - 1) (the I-JSON range, RFC 7493), booleans and null. Floats are
refused: JCS writes them with ECMAScript's number formatting, which is easy to
get subtly wrong in other languages, and records have no need for them.

Within that subset the canonical text is compact JSON in UTF-8: no whitespace,
object keys sorted by their UTF-16 code units, and strings escaped only where
JSON requires it (``"``, ``\\`` and control characters, the latter as ``\\b``,
``\\t``, ``\\n``, ``\\f``, ``\\r`` or lowercase ``\\u00xx``). Python's ``json``
module escapes exactly so; it sorts keys by code point, which differs from
UTF-16 order above U+FFFF, so keys are sorted here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, cast

from .errors import RecordError

MAX_SAFE_INTEGER: Final = 2**53 - 1
MAX_DEPTH: Final = 64
MAX_PAYLOAD_BYTES: Final = 1024 * 1024


def canonical_json(value: object) -> str:
    """Return the canonical JSON text of ``value``; raise RecordError if it has none."""
    return json.dumps(
        _normalize(value, 0), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def canonical_payload(payload: Mapping[str, object]) -> str:
    """Return the canonical text of a record payload, which must be a JSON object."""
    if not isinstance(payload, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise RecordError("a payload must be a JSON object")
    text = canonical_json(payload)
    if len(text.encode()) > MAX_PAYLOAD_BYTES:
        raise RecordError(f"the payload is larger than {MAX_PAYLOAD_BYTES} bytes")
    return text


def parse_payload(text: str) -> dict[str, object]:
    """Parse a stored payload, which must be a JSON object in canonical form."""
    payload = parse_json_object(text)
    if canonical_json(payload) != text:
        raise RecordError("the payload is not in canonical form")
    return payload


def parse_json_object(text: str) -> dict[str, object]:
    """Parse a JSON object strictly: no repeated keys, no floats, NaN or infinities."""
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_keys,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise RecordError(f"not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RecordError("not a JSON object")
    return cast("dict[str, object]", value)


def _normalize(value: object, depth: int) -> object:
    """Check ``value`` and copy it with plain types and object keys in canonical order."""
    if depth > MAX_DEPTH:
        raise RecordError(f"the value is nested more than {MAX_DEPTH} levels deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        number = int.__int__(value)  # a plain int, also for enums and other subclasses
        if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
            raise RecordError(f"integer {number} is outside ±(2**53 - 1)")
        return number
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, Mapping):
        items: list[tuple[str, object]] = []
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise RecordError(f"object keys must be strings, not {type(key).__name__}")
            items.append((_text(key), _normalize(item, depth + 1)))
        items.sort(key=lambda item: item[0].encode("utf-16-be"))
        return dict(items)
    if isinstance(value, list | tuple):
        return [_normalize(item, depth + 1) for item in cast("list[object]", value)]
    raise RecordError(f"{type(value).__name__} values have no canonical JSON form")


def _text(value: str) -> str:
    text = str.__str__(value)  # a plain str, also for enums and other subclasses
    try:
        text.encode()
    except UnicodeEncodeError as exc:
        raise RecordError("strings must not contain lone surrogates") from exc
    return text


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_float(text: str) -> object:
    raise ValueError(f"number {text} is not an integer")


def _no_constant(text: str) -> object:
    raise ValueError(f"{text} is not JSON")
