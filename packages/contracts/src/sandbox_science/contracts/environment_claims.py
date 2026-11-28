# SPDX-License-Identifier: MIT
"""Bounded claims that retain active research environments across idle intervals."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .context import ExecutionContext
from .model import Contract, Identifier

CLAIM_SECONDS: Final = 120
MAX_CLAIM_SECONDS: Final = 300
CLAIM_RENEW_SECONDS: Final = 30
"""How often a root renews its claim; it asks for ``MAX_CLAIM_SECONDS`` each time."""
WORKER_OUTAGE_SECONDS: Final = MAX_CLAIM_SECONDS - CLAIM_RENEW_SECONDS
"""How long a run waits for a worker that does not answer, its claim renewals and an
execution's event stream retrying, before it stops as a transient failure: longer than a
worker Pod's restart, and short enough that the worker still holds the claim last renewed
when the run gives up, so that it never releases what the run is still waiting to renew."""
MAX_ENVIRONMENT_CLAIMS: Final = 32
ClaimHolder = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")]
"""A run record's timestamp and separator plus its at-most-128-character run ID."""


class EnvironmentClaim(Contract):
    """Grant/renew a holder's claim, or release it with zero seconds.

    Renewals and releases pin the sandbox identity returned by the first grant.
    """

    context: ExecutionContext
    holder: ClaimHolder
    seconds: int = Field(default=CLAIM_SECONDS, strict=True, ge=0, le=MAX_CLAIM_SECONDS)
    sandbox_id: str | None = Field(default=None, min_length=1, max_length=128)


class EnvironmentClaimResult(Contract):
    environment_id: Identifier
    state: Literal["held", "released", "unmanaged"]
    sandbox_id: str | None = Field(default=None, min_length=1, max_length=128)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.state == "held":
            if self.sandbox_id is None or self.expires_at is None:
                raise ValueError("a held claim names its sandbox and expiry")
        elif self.expires_at is not None:
            raise ValueError("only a held claim has an expiry")
        if self.state == "unmanaged" and self.sandbox_id is not None:
            raise ValueError("an unmanaged environment has no managed sandbox claim")
        return self
