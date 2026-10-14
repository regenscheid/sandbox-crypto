# SPDX-License-Identifier: MIT
"""Canonical JSON, and the request digest that binds an operation ID to its request.

A client names each operation with an ID of its own; the worker keeps the request's
digest and refuses the same ID with a different request. The digest covers only the
fields set to something other than their default, in sorted-key JSON, so a field
added later with a default leaves every stored identity unchanged. A domain names
what kind of request is hashed, so equal fields in different kinds never collide.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def canonical_json(value: object) -> bytes:
    """``value`` as compact JSON with sorted keys and ASCII escapes; NaN is an error."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def request_digest(request: BaseModel, domain: str) -> str:
    """The SHA-256 (hex) of ``domain``, a NUL, and the request's non-default fields."""
    if not domain or "\x00" in domain:
        raise ValueError("a digest domain is a non-empty string without NUL")
    fields = request.model_dump(mode="json", exclude_defaults=True)
    return hashlib.sha256(domain.encode() + b"\x00" + canonical_json(fields)).hexdigest()
