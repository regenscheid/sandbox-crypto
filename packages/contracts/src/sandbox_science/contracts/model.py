# SPDX-License-Identifier: MIT
"""Base model and identifiers shared by every contract."""

from __future__ import annotations

import secrets
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class Contract(BaseModel):
    """Base for contract models: unknown fields are errors; values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
"""Project, session, environment, operation and generated IDs: printable, path-safe, short."""


def new_id(prefix: str) -> str:
    """Return a new random identifier such as ``exe-3f2a…`` (96 random bits)."""
    return f"{prefix}-{secrets.token_hex(12)}"
