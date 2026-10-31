# SPDX-License-Identifier: MIT
"""Base models, identifiers and the primitive types shared by every contract.

Contracts hold shapes, per-field bounds and the cross-field rules that define a value
(such as exactly one of two fields). They do not recompute what the server computed.
"""

from __future__ import annotations

import re
import secrets
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Contract(BaseModel):
    """Base for contract models: unknown fields are errors; values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrictContract(Contract):
    """A contract that also refuses coercion: ``"1"`` is not an int, nor 1 a bool."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
"""Project, session, environment, operation and generated IDs: printable, path-safe, short."""

MAX_PROJECT_ID: Final = 63
"""The longest ID a new project gets: the agent runtime's sandbox is labelled with it on the
direct MCP path, and a Kubernetes label value is at most 63 characters."""

_LABEL_VALUE: Final = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")

PROJECT_ID_RULE: Final = (
    f"a project ID is at most {MAX_PROJECT_ID} characters of letters, digits, '.', '_' and '-', "
    "starting and ending with a letter or digit, because the direct MCP path labels the "
    "agent runtime's sandbox with it and a Kubernetes label value allows no more"
)


def project_id_label_problem(project_id: str) -> str | None:
    """Why ``project_id`` cannot be a Kubernetes label value (the direct MCP path labels
    the agent runtime's sandbox with the project), or None when it can."""
    if _LABEL_VALUE.fullmatch(project_id) is not None:
        return None
    return f"project ID {project_id!r} cannot label the agent runtime's sandbox: {PROJECT_ID_RULE}"


MAX_COUNTER: Final = 2**62
"""The largest count, size or sequence number a contract carries: below SQLite's integer
limit with room to add two of them."""
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Revision = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
"""A revision number of a stored record; the first is 1."""
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A SHA-256 digest in lowercase hex."""


def check_page(offset: int, count: int, limit: int, total: int, next_offset: int | None) -> None:
    """The one paging rule: ``count`` rows from ``offset`` of ``total``, at most ``limit``,
    with ``next_offset`` naming where the next page starts, or None after the last.

    Raises ValueError for a page that does not bind its range or makes no progress.
    """
    end = offset + count
    if (
        offset > total
        or count > limit
        or end > total
        or next_offset != (end if end < total else None)
    ):
        raise ValueError("page does not bind its exact range")
    if end < total and not count:
        raise ValueError("a page before the end must make progress")


def new_id(prefix: str) -> str:
    """Return a new random identifier such as ``exe-3f2a…`` (96 random bits)."""
    return f"{prefix}-{secrets.token_hex(12)}"
