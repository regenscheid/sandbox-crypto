# SPDX-License-Identifier: MIT
"""Internal original run/kernel enrollment observations, without execution grants."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from .model import Contract, Identifier

MAX_RUN_KERNEL_RECORD = 4096
_SCOPE_FIELDS = (
    "project_id",
    "session_id",
    "environment_id",
    "parent_run_id",
    "delegation_id",
    "child_run_id",
)


class RunKernelScope(Contract):
    """Explicit trusted controller attribution, never authentication or a grant.

    The caller must supply the genuine recorded parent/delegation/child identity.
    No actor, execution or synthetic session can substitute for that identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: Identifier
    session_id: Identifier
    environment_id: Identifier
    parent_run_id: Identifier
    delegation_id: Identifier
    child_run_id: Identifier

    @model_validator(mode="after")
    def _runs(self) -> Self:
        if (
            self.parent_run_id.casefold() == "user"
            or self.child_run_id.casefold() == "user"
            or self.parent_run_id == self.child_run_id
        ):
            raise ValueError("a child requires distinct original non-USER run identities")
        return self


class RunKernelBinding(Contract):
    """Original managed endpoint observation; no kernel birth or revision claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scope: RunKernelScope
    sandbox: Identifier
    sandbox_id: Identifier
    incarnation: Annotated[str, Field(pattern=r"^inc-[0-9a-f]{24}$")]


class RunKernelEnrollment(Contract):
    """One immutable historical receipt, not dispatch or current eligibility."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[1]
    operation_id: Identifier
    request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    binding_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    binding: RunKernelBinding
    recorded_at: Annotated[str, Field(max_length=40)]

    @field_validator("format_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("enrollment version must be an exact integer")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _utc(cls, value: str) -> str:
        stamp = datetime.fromisoformat(value)
        offset = stamp.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("enrollment receipt time needs a UTC offset")
        return value


def _fields(value: object, model: type[Contract]) -> dict[str, object]:
    if type(value) is not model:
        raise ValueError("invalid run/kernel model type")
    fields = object.__getattribute__(value, "__dict__")
    extras = object.__getattribute__(value, "__pydantic_extra__")
    if type(fields) is not dict or extras is not None:
        raise ValueError("undeclared run/kernel model fields")
    declared = cast(dict[object, object], fields)
    if (
        len(declared) != len(model.model_fields)
        or any(type(key) is not str for key in declared)
        or set(declared) != set(model.model_fields)
    ):
        raise ValueError("undeclared run/kernel model fields")
    return cast(dict[str, object], declared)


def _text(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or not value.isascii():
        raise ValueError("invalid run/kernel scalar")
    return value


def checked_run_kernel_scope(value: RunKernelScope) -> RunKernelScope:
    """Copy and strictly validate bounded built-in fields before aggregate encoding."""
    fields = _fields(value, RunKernelScope)
    return RunKernelScope.model_validate(
        {name: _text(fields[name]) for name in _SCOPE_FIELDS}, strict=True
    )


def checked_run_kernel_binding(value: RunKernelBinding) -> RunKernelBinding:
    """Reject copied extras/subtypes and unknown original VM/incarnation fields."""
    fields = _fields(value, RunKernelBinding)
    scope = checked_run_kernel_scope(cast(RunKernelScope, fields["scope"]))
    return RunKernelBinding(
        scope=scope,
        sandbox=_text(fields["sandbox"]),
        sandbox_id=_text(fields["sandbox_id"]),
        incarnation=_text(fields["incarnation"]),
    )


def run_kernel_binding_sha256(value: RunKernelBinding) -> str:
    """Canonical v1 original identity key; neither current state nor grants enter it."""
    binding = checked_run_kernel_binding(value)
    raw = json.dumps(
        binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(b"run-kernel-binding-v1\0" + raw).hexdigest()
