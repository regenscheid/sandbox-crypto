# SPDX-License-Identifier: MIT
"""Immutable worker edit identity; authority and paths never come from this payload."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from .model import Contract, Identifier

SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DirectEditCandidate(Contract):
    """A captured edit, whose reported base is never inferred from current content.

    ``catalog_seq`` is provenance, not a document revision or permission grant.
    Complete raw bytes and writer closure must be established independently.
    """

    protocol: Literal[1] = 1
    edit_id: Identifier
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    document_id: str = Field(pattern=r"^doc-[0-9a-f]{24}$")
    base_revision: int | None = Field(default=None, ge=1, le=2**63 - 1, strict=True)
    base_written_sha256: SHA256 | None = None
    raw_sha256: SHA256
    raw_bytes: int = Field(ge=0, le=2**40, strict=True)
    capture_id: Identifier
    catalog_seq: int = Field(ge=1, le=2**63 - 1, strict=True)

    @field_validator("protocol", mode="before")
    @classmethod
    def _protocol(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol must be the exact integer 1")
        return value

    @model_validator(mode="after")
    def _base(self) -> Self:
        if (self.base_revision is None) != (self.base_written_sha256 is None):
            raise ValueError("base revision and written digest must both be present or absent")
        return self
