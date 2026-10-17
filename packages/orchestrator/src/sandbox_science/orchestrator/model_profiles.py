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


class ModelProfile(Contract):
    alias: str = Field(min_length=1, max_length=200)
    api: Literal["chat", "responses", "messages"] = "chat"
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


def requested(alias: str, api: str = "chat", reasoning_effort: str | None = None) -> ModelProfile:
    """Validate a deployment-selected profile before use."""
    return ModelProfile.model_validate(
        {"alias": alias, "api": api, "reasoning_effort": reasoning_effort}
    )
