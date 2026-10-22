# SPDX-License-Identifier: MIT
"""Shared review JSON values, independent of the review lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import Receipts
from sandbox_science.orchestrator.documents.agent_text import agent_text as checked_agent_text
from sandbox_science.orchestrator.documents.blocks import check_text
from sandbox_science.orchestrator.documents.errors import INVALID_REQUEST, DocumentError

# Heterogeneous evidence JSON is validated by its producing boundary.
Json = dict[str, Any]

RECEIPTS: Final = Receipts("review", "operation ID already names another finding mutation")
"""Finding and response changes share one receipt scope."""


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stored_json(value: str) -> Json:
    """A JSON object the store holds, as it was written (decision 3: no second check)."""
    return cast(Json, json.loads(value))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def identifier(value: object, prefix: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{24}", value) is None
    ):
        raise DocumentError(INVALID_REQUEST, f"invalid {prefix} identifier")


def text(value: str, limit: int = 8192) -> str:
    check_text(value)
    if not value or len(value) > limit or len(value.encode()) > limit * 4:
        raise DocumentError(INVALID_REQUEST, "review text exceeds its bound")
    return value


def agent_text(value: str, what: str, limit: int = 8192) -> str:
    """Review text an agent wrote, which the user decides on and agents read again, as it
    is kept: its line ends line feeds, within its bound, and with no character the user
    may not see (threat model, A3)."""
    kept = checked_agent_text(value, what)
    return text(kept, limit)
