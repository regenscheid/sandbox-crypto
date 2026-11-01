# SPDX-License-Identifier: MIT
"""USER project settings for the three agent model roles.

The deployment supplies the trusted alias catalog and the adapters it names. A project
may choose an alias from that catalog and an effort value, which every adapter takes; it
cannot supply an API, gateway address or credential. A fresh research tree snapshots the
effective profiles, so changing these settings never mutates an admitted tree.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from sandbox_science.contracts import Contract

from .control import Receipts
from .documents import DocumentStore
from .documents.errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .documents.models import Author
from .model_profiles import ApiKind, Effort, ModelProfile

AgentModelRole = Literal["main", "delegated", "reviewer"]
AGENT_MODEL_ROLES: Final[tuple[AgentModelRole, ...]] = ("main", "delegated", "reviewer")
KNOWN_EFFORTS: Final[tuple[str, ...]] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
_RECEIPTS = Receipts("models", "operation ID already names another model settings change")


class AgentModelSelection(Contract):
    alias: str = Field(min_length=1, max_length=200)
    reasoning_effort: Effort | None = None
    deployment_default: bool = False
    """The deployment's default effort for the role on the API the alias is called
    through, which the run service resolves when the API is its own choice
    (docs/specs/context-management.md, CM-41), rather than ``reasoning_effort``."""

    @model_validator(mode="after")
    def one_effort(self) -> AgentModelSelection:
        if self.deployment_default and self.reasoning_effort is not None:
            raise ValueError("a deployment default names no effort of its own")
        return self


class RoleDefault(Contract):
    """The deployment's default effort for a role, and, where it differs, on Messages."""

    effort: Effort | None = None
    messages: Effort | None = None


class ModelOption(Contract):
    alias: str = Field(min_length=1, max_length=200)
    api: ApiKind | None
    """The adapter the deployment names for the alias, or ``None`` when the run service
    chooses it from the gateway's model information (docs/specs/context-management.md,
    CM-41)."""
    effort_supported: bool = True
    """Whether a requested effort applies: every adapter takes one."""


class ModelSettings(Contract):
    project: str
    revision: int = Field(ge=0)
    roles: dict[AgentModelRole, AgentModelSelection] = Field(max_length=3)
    defaults: dict[AgentModelRole, AgentModelSelection] = Field(max_length=3)
    default_efforts: dict[AgentModelRole, RoleDefault] = Field(
        default_factory=dict[AgentModelRole, RoleDefault], max_length=3
    )
    available: tuple[ModelOption, ...] = Field(max_length=64)
    known_efforts: tuple[str, ...] = KNOWN_EFFORTS


class ModelSettingsChange(Contract):
    expected: int = Field(ge=0, le=999999999, strict=True)
    roles: dict[AgentModelRole, AgentModelSelection] = Field(min_length=2, max_length=3)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    @model_validator(mode="after")
    def required_roles(self) -> ModelSettingsChange:
        if "main" not in self.roles or "delegated" not in self.roles:
            raise ValueError("main and delegated model settings are required")
        return self


class ProjectModels:
    """Revisioned model selections for projects in one control store."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def selections(self, project: str) -> tuple[int, dict[AgentModelRole, AgentModelSelection]]:
        """Return persisted selections; revision zero means deployment defaults."""
        _scope(project)
        if self.documents.control.missing:
            return 0, {}
        with self.documents.control.read() as tx:
            return _read(tx.sql, project)

    def get(
        self,
        project: str,
        defaults: Mapping[AgentModelRole, ModelProfile],
        available: Mapping[str, ApiKind | None],
        messages_defaults: Mapping[AgentModelRole, str] | None = None,
    ) -> ModelSettings:
        _scope(project)
        revision, stored = self.selections(project)
        return _view(project, revision, stored, defaults, available, messages_defaults)

    def change(
        self,
        project: str,
        value: object,
        author: Author,
        *,
        defaults: Mapping[AgentModelRole, ModelProfile],
        available: Mapping[str, ApiKind | None],
        messages_defaults: Mapping[AgentModelRole, str] | None = None,
    ) -> tuple[ModelSettings, bool]:
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user changes project model settings")
        try:
            change = ModelSettingsChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid project model settings change") from None
        if set(change.roles) != set(defaults):
            raise DocumentError(INVALID_REQUEST, "model settings must name every configured role")
        for role, selection in change.roles.items():
            if selection.alias not in available:
                raise DocumentError(
                    INVALID_REQUEST, f"{role} model is outside the deployment catalog"
                )
        fingerprint = hashlib.sha256(
            json.dumps(change.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with self.documents.control.write() as tx:
            db = tx.sql
            replay = _RECEIPTS.find(db, project, author.actor, change.operation_id, fingerprint)
            if replay is not None:
                revision, stored = _read(db, project)
                view = _view(project, revision, stored, defaults, available, messages_defaults)
                return view, True
            revision, current = _read(db, project)
            if revision != change.expected:
                raise DocumentError(CONFLICT, "the project's model settings changed meanwhile")
            if current != change.roles:
                db.execute(
                    "INSERT INTO project_model_settings VALUES(?,?,?,?) ON CONFLICT(project) "
                    "DO UPDATE SET revision=excluded.revision,selections=excluded.selections,"
                    "author=excluded.author",
                    (
                        project,
                        revision + 1,
                        json.dumps(
                            {
                                role: selection.model_dump(mode="json")
                                for role, selection in change.roles.items()
                            },
                            sort_keys=True,
                        ),
                        author.model_dump_json(),
                    ),
                )
            if _RECEIPTS.count(db, project) >= 4096:
                raise DocumentError(CONFLICT, "the model settings' operation quota is reached")
            revision, stored = _read(db, project)
            result = _view(project, revision, stored, defaults, available, messages_defaults)
            _RECEIPTS.record(
                db, project, author.actor, change.operation_id, fingerprint,
                {"revision": result.revision},
            )  # fmt: skip
            return result, False


def _view(
    project: str,
    revision: int,
    stored: Mapping[AgentModelRole, AgentModelSelection],
    defaults: Mapping[AgentModelRole, ModelProfile],
    available: Mapping[str, ApiKind | None],
    messages_defaults: Mapping[AgentModelRole, str] | None = None,
) -> ModelSettings:
    on_messages = {
        role: effort
        for role, effort in (messages_defaults or {}).items()
        if role in defaults and effort != defaults[role].reasoning_effort
    }
    base: dict[AgentModelRole, AgentModelSelection] = {
        role: _default(profile, available.get(profile.alias), on_messages.get(role))
        for role, profile in defaults.items()
    }
    return ModelSettings(
        project=project,
        revision=revision,
        roles=dict(stored) if stored else base,
        defaults=base,
        default_efforts={
            role: RoleDefault(effort=profile.reasoning_effort, messages=on_messages.get(role))
            for role, profile in defaults.items()
        },
        available=tuple(
            ModelOption(alias=alias, api=api) for alias, api in sorted(available.items())
        ),
    )


def _default(
    profile: ModelProfile, api: ApiKind | None, on_messages: str | None
) -> AgentModelSelection:
    """A role's default selection: its effort on the alias's API, or, when that default
    differs on Messages and the run service chooses the API, the deployment's default."""
    if on_messages is None:
        effort = profile.reasoning_effort
    elif api == "messages":
        effort = on_messages
    elif api is None:
        return AgentModelSelection(alias=profile.alias, deployment_default=True)
    else:
        effort = profile.reasoning_effort
    return AgentModelSelection(alias=profile.alias, reasoning_effort=effort)


def _read(
    db: sqlite3.Connection, project: str
) -> tuple[int, dict[AgentModelRole, AgentModelSelection]]:
    row = db.execute(
        "SELECT revision,selections FROM project_model_settings WHERE project=?", (project,)
    ).fetchone()
    if row is None:
        return 0, {}
    try:
        roles = TypeAdapter(dict[AgentModelRole, AgentModelSelection]).validate_python(
            json.loads(row[1])
        )
        return int(row[0]), roles
    except (ValidationError, ValueError, TypeError):
        raise DocumentError(INTERNAL, "invalid stored project model settings") from None


def _scope(project: object) -> None:
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
    ):
        raise DocumentError(INVALID_REQUEST, "invalid project")
