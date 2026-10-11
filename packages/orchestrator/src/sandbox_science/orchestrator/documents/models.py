# SPDX-License-Identifier: MIT
"""The records of living documents: authors, documents, revisions, patches, results."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Literal, Self

from pydantic import Field, JsonValue, model_validator

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.model import Contract

NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
OPERATION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MAX_TITLE: Final = 200
MAX_REASON: Final = 1000

AuthorKind = Literal["user", "agent", "import", "platform"]
PatchState = Literal[
    "applied", "unchanged", "conflicted", "refused", "suggested", "accepted", "declined"
]
Outcome = Literal[
    "created",
    "applied",
    "unchanged",
    "conflict",
    "refused",
    "suggested",
    "accepted",
    "declined",
    "deleted",
]


class Author(Contract):
    """Who made a change, and how (LD-10).

    - ``user``: the workspace's user, directly or through a fixture.
    - ``agent``: an agent run, with its session, role, model and tool call.
    - ``import``: an import the user started, by its ID.
    - ``platform``: a platform service, such as the math repair (later).
    """

    kind: AuthorKind
    user: str | None = None
    session_id: Identifier | None = None
    run_id: str | None = None
    role: str | None = None
    model: str | None = None
    """The model that answered, as the gateway reported it."""
    alias: str | None = None
    """The model alias the run asked for."""
    tool_call: str | None = None
    import_id: str | None = None
    service: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        required = {
            "user": ("user",),
            "agent": ("session_id", "run_id", "role"),
            "import": ("user", "import_id"),
            "platform": ("service",),
        }[self.kind]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"an author of kind {self.kind} needs {', '.join(missing)}")
        return self

    @property
    def actor(self) -> str:
        """Whose operation IDs these are: a session's agents share them, as retries cross runs."""
        if self.kind == "agent":
            return f"session:{self.session_id}"
        if self.kind == "platform":
            return f"platform:{self.service}"
        return f"user:{self.user}"


USER: Final = Author(kind="user", user="user")
"""The workspace's one user (W4 has one user per installation)."""


class DocumentInfo(Contract):
    document_id: str
    project_id: Identifier
    scope: Literal["project", "session"]
    session_id: Identifier | None
    """The session whose workbook holds it; None for a project document."""
    name: str
    title: str
    created_at: datetime
    created_by: Author
    created_session: Identifier | None
    """The session whose agent created it, which alone among agents may delete it (LD-12)."""
    takes_suggestions: bool
    """Agent patches wait as suggestions for the user's decision (LD-20)."""
    head: int
    deleted: bool


class RevisionInfo(Contract):
    document_id: str
    number: int
    created_at: datetime
    author: Author
    accepted_by: Author | None = None
    session_id: Identifier | None
    base: int | None
    """The revision the change was made from."""
    parent: int | None
    """The revision it was applied to: the base, or a later one when it was rebased."""
    reason: str
    operations: list[JsonValue]
    patch_id: str | None
    sha256: str
    tombstone: bool
    normalized: dict[str, JsonValue]
    warnings: list[dict[str, JsonValue]]
    parser: dict[str, str]


class Revision(Contract):
    info: RevisionInfo
    text: str
    block_map: dict[str, JsonValue]


class PatchInfo(Contract):
    patch_id: str
    document_id: str
    created_at: datetime
    author: Author
    session_id: Identifier | None
    base: int
    reason: str
    operations: list[JsonValue]
    state: PatchState
    revision: int | None
    details: dict[str, JsonValue]
    decided_at: datetime | None = None
    decided_by: Author | None = None


class ChangeResult(Contract):
    """What a change did, as the tool and the API report it."""

    outcome: Outcome
    document_id: str
    revision: int | None = None
    """The revision made, or the current one when none was."""
    base: int | None = None
    applied_to: int | None = None
    rebased: bool = False
    patch_id: str | None = None
    changes: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    normalized: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    warnings: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    conflicts: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    unnamed: list[str] = Field(default_factory=list[str])
    """Blocks a refused patch would have changed without naming them (LD-18)."""
    message: str | None = None
    replayed: bool = False


class EventRecord(Contract):
    seq: int
    time: datetime
    kind: str
    producer: str
    project_id: str
    session_id: str | None
    document_id: str
    payload: dict[str, JsonValue]
