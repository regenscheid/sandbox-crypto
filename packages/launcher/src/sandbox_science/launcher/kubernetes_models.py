# SPDX-License-Identifier: MIT
"""Nonsecret workspace role catalog, using the workspace CLI's public options."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Effort = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    alias: Annotated[str, Field(min_length=1, max_length=200)]
    api: Literal["chat", "responses", "messages"] = "responses"
    effort: Effort | None = None

    @model_validator(mode="after")
    def messages_effort(self) -> Self:
        if self.api == "messages" and self.effort is not None:
            raise ValueError("Messages models do not accept a requested effort")
        return self


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    main: RoleModel
    delegated: RoleModel
    reviewer: RoleModel | None = None

    @model_validator(mode="after")
    def consistent_adapters(self) -> Self:
        adapters: dict[str, str] = {}
        for profile in (self.main, self.delegated, self.reviewer):
            if profile is not None:
                if profile.alias in adapters and adapters[profile.alias] != profile.api:
                    raise ValueError("one alias must use the same API in every role")
                adapters[profile.alias] = profile.api
        return self

    def workspace_arguments(self) -> list[str]:
        """Render explicit main/delegated/reviewer defaults without model credentials."""
        result: list[str] = []
        adapters: dict[str, str] = {}
        for role, profile in (
            ("main", self.main),
            ("delegated", self.delegated),
            ("reviewer", self.reviewer),
        ):
            if profile is None:
                continue
            result += ["--agent-model", f"{role}={profile.alias}"]
            if profile.api != "messages":
                effort = profile.effort or ("medium" if role == "reviewer" else "max")
                result += ["--agent-effort", f"{role}={effort}"]
            adapters[profile.alias] = profile.api
        for alias, api in adapters.items():
            result += ["--agent-api", f"{alias}={api}"]
        return result
