# SPDX-License-Identifier: MIT
"""Nonsecret workspace role catalog, using the workspace CLI's public options."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Effort = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
MESSAGES_RESEARCH_EFFORT: Final = "high"
"""A research role's default effort on Messages."""
DEFAULT_API: Final = "responses"
"""The API of a role whose alias the catalog names none for and the gateway does not list
as Anthropic's: the run service chooses ``messages`` for those it does (CM-41)."""


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    alias: Annotated[str, Field(min_length=1, max_length=200)]
    api: Literal["chat", "responses", "messages"] | None = None
    """The API, or, unset, the run service's choice: ``messages`` when the gateway lists
    the alias's provider as ``anthropic``, else ``responses``."""
    effort: Effort | None = None

    @field_validator("alias")
    @classmethod
    def cli_alias(cls, value: str) -> str:
        if (
            value.startswith("-")
            or "=" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(
                "model alias cannot contain CLI delimiters, options or control characters"
            )
        return value

    def requested_effort(self, role: str) -> str:
        """The role's effort: its own, else ``medium`` for the reviewer, and for a research
        role ``high`` on Messages (Anthropic's advice for agentic work; ``max`` is Claude
        Opus 5.5's costliest level) and ``max`` on the other APIs. A role whose API the run
        service chooses gets ``max`` here and ``messages_effort``'s on Messages."""
        if self.effort:
            return self.effort
        if role == "reviewer":
            return "medium"
        return MESSAGES_RESEARCH_EFFORT if self.api == "messages" else "max"

    def messages_effort(self, role: str) -> str | None:
        """The role's default on Messages, where it differs from ``requested_effort``'s
        and the run service chooses the API, so only it can apply it (CM-41)."""
        if self.effort or role == "reviewer" or self.api is not None:
            return None
        return MESSAGES_RESEARCH_EFFORT


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    main: RoleModel
    delegated: RoleModel
    reviewer: RoleModel | None = None

    @model_validator(mode="after")
    def consistent_adapters(self) -> Self:
        adapters: dict[str, str | None] = {}
        for profile in (self.main, self.delegated, self.reviewer):
            if profile is not None:
                if profile.alias in adapters and adapters[profile.alias] != profile.api:
                    raise ValueError("one alias must use the same API in every role")
                adapters[profile.alias] = profile.api
        return self

    def apis(self) -> dict[str, str]:
        """The APIs the catalog names, by alias; an alias it names none for is the run
        service's choice."""
        return {
            profile.alias: profile.api
            for profile in (self.main, self.delegated, self.reviewer)
            if profile is not None and profile.api is not None
        }

    def workspace_arguments(self) -> list[str]:
        """Render explicit main/delegated/reviewer defaults without model credentials."""
        result: list[str] = []
        for role, profile in (
            ("main", self.main),
            ("delegated", self.delegated),
            ("reviewer", self.reviewer),
        ):
            if profile is None:
                continue
            result += ["--agent-model", f"{role}={profile.alias}"]
            result += ["--agent-effort", f"{role}={profile.requested_effort(role)}"]
            if (on_messages := profile.messages_effort(role)) is not None:
                result += ["--agent-messages-effort", f"{role}={on_messages}"]
        for alias, api in self.apis().items():
            result += ["--agent-api", f"{alias}={api}"]
        return result
