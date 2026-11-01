# SPDX-License-Identifier: MIT
"""Requested model profiles; provider defaults remain an explicit absence."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import Field

from sandbox_science.contracts import Contract

ModelRole = Literal[
    "main", "delegated", "reviewer", "watcher", "fold", "overview", "skills_metadata"
]
Effort = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
ApiKind = Literal["chat", "responses", "messages"]
"""The adapters an alias may be called through (CM-41)."""


class ModelProfile(Contract):
    alias: str = Field(min_length=1, max_length=200)
    api: ApiKind = "chat"
    reasoning_effort: Effort | None = None


PROFILES: ContextVar[Mapping[ModelRole, ModelProfile] | None] = ContextVar(
    "model_profiles", default=None
)


@contextmanager
def profiles(values: Mapping[ModelRole, ModelProfile]) -> Generator[None]:
    token = PROFILES.set(values)
    try:
        yield
    finally:
        PROFILES.reset(token)


def default_effort(
    role: ModelRole,
    api: str,
    efforts: Mapping[ModelRole, str],
    messages_efforts: Mapping[ModelRole, str],
) -> str | None:
    """A role's requested effort as the deployment sets it: ``messages_efforts``' for an
    alias called through Messages when it names the role (a default that differs there,
    as the Kubernetes bundle's ``high`` for research roles), else ``efforts``'."""
    if api == "messages" and role in messages_efforts:
        return messages_efforts[role]
    return efforts.get(role)


def requested(alias: str, api: str = "chat", reasoning_effort: str | None = None) -> ModelProfile:
    """Validate a deployment-selected profile before use."""
    return ModelProfile.model_validate(
        {"alias": alias, "api": api, "reasoning_effort": reasoning_effort}
    )
