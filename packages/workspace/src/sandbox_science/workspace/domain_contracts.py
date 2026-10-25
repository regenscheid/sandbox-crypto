# SPDX-License-Identifier: MIT
"""Wire contracts for the workspace's documents, annotations and background jobs.

Dictionary responses are described, not reserialized: absent legacy/conditional keys
stay absent. Requests reuse the domain validators; their defaults stay optional.
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, with_config
from typing_extensions import TypedDict

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.operations import Operation


class _Change(Contract):
    base: int = Field(ge=1, le=999999999, strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    reason: str = Field(min_length=1, max_length=1000)


class SourceEdit(_Change):
    action: Literal["source"]
    text: str


class BlockEdit(_Change):
    action: Literal["patch"]
    operations: tuple[Operation, ...] = Field(min_length=1, max_length=64)


class Revert(_Change):
    action: Literal["revert"]
    target: int = Field(ge=1, le=999999999, strict=True)


class Promote(_Change):
    action: Literal["promote"]
    name: str = Field(min_length=1, max_length=67)


class ApplyPatch(_Change):
    action: Literal["apply_patch"]
    patch_id: str = Field(pattern=r"^patch-[0-9a-f]{24}$")


class Decline(Contract):
    """The user declines an agent's suggestion (LD-20): the document is not changed."""

    action: Literal["decline"]
    patch_id: str = Field(pattern=r"^patch-[0-9a-f]{24}$")


DocumentChangeRequest = Annotated[
    SourceEdit | BlockEdit | Revert | Promote | ApplyPatch | Decline,
    Field(discriminator="action"),
]

DOCUMENT_CHANGE = TypeAdapter(DocumentChangeRequest)


@with_config(ConfigDict(extra="forbid"))
class DocumentAuthor(TypedDict):
    """Full provenance or the compact form stored in history and older annotations."""

    kind: Literal["user", "agent", "worker", "import", "platform"]
    user: NotRequired[str | None]
    session_id: NotRequired[str | None]
    run_id: NotRequired[str | None]
    role: NotRequired[str | None]
    model: NotRequired[str | None]
    alias: NotRequired[str | None]
    tool_call: NotRequired[str | None]
    import_id: NotRequired[str | None]
    service: NotRequired[str | None]
    worker_binding: NotRequired[str | None]
    environment_id: NotRequired[str | None]
    incarnation: NotRequired[str | None]
    execution_id: NotRequired[str | None]
    attempt_id: NotRequired[str | None]


@with_config(ConfigDict(extra="forbid"))
class DocumentInfo(TypedDict):
    document_id: str
    project_id: str
    scope: Literal["project", "session"]
    session_id: str | None
    name: str
    title: str
    created_at: str
    created_by: DocumentAuthor
    created_session: str | None
    takes_suggestions: bool
    head: int
    deleted: bool


@with_config(ConfigDict(extra="forbid"))
class DocumentListing(TypedDict):
    documents: list[DocumentInfo]


@with_config(ConfigDict(extra="forbid"))
class DocumentWarning(TypedDict):
    path: NotRequired[str]
    kind: str
    message: str
    block: NotRequired[str]
    math: NotRequired[str]


@with_config(ConfigDict(extra="forbid"))
class DocumentNormalization(TypedDict):
    normalized: NotRequired[bool]
    converted: NotRequired[dict[str, int]]
    kept: NotRequired[list[dict[str, str]]]


@with_config(ConfigDict(extra="forbid"))
class RevisionInfo(TypedDict):
    document_id: str
    number: int
    created_at: str
    author: DocumentAuthor
    accepted_by: DocumentAuthor | None
    session_id: str | None
    base: int | None
    parent: int | None
    reason: str
    operations: list[JsonValue]
    patch_id: str | None
    sha256: str
    tombstone: bool
    normalized: DocumentNormalization
    warnings: list[DocumentWarning]
    parser: dict[str, str]


@with_config(ConfigDict(extra="forbid"))
class DocumentMath(TypedDict):
    id: str
    kind: Literal["inline", "display"]
    status: str
    tex: str | None


@with_config(ConfigDict(extra="forbid"))
class DocumentBlock(TypedDict):
    id: str
    kind: str
    source: str | None
    parent: str | None
    depth: int
    lines: tuple[int, int]
    status: str
    structural: str | None
    attrs: dict[str, JsonValue]
    math: list[DocumentMath]


@with_config(ConfigDict(extra="forbid"))
class DocumentMacroContext(TypedDict):
    project: str
    revision: int
    sha256: str
    macros: dict[str, str]
    known: bool
    anchors: list[dict[str, JsonValue]]
    problems: list[str]
    held: bool
    key: str


@with_config(ConfigDict(extra="forbid"))
class RemovedBlock(TypedDict):
    id: str
    kind: str


@with_config(ConfigDict(extra="forbid"))
class RemovedMath(TypedDict):
    id: str
    block: str


@with_config(ConfigDict(extra="forbid"))
class DocumentRevision(TypedDict):
    document: DocumentInfo
    revision: RevisionInfo
    text: str
    title: str | None
    macros: dict[str, str]
    macro_context: DocumentMacroContext
    matched: bool
    blocks: list[DocumentBlock]
    removed: list[RemovedBlock]
    removed_math: list[RemovedMath]
    pins: list[dict[str, JsonValue]]
    figure_checks: list[dict[str, JsonValue]]


@with_config(ConfigDict(extra="forbid"))
class HistoryRevision(TypedDict):
    number: int
    created_at: str
    author: DocumentAuthor
    accepted_by: DocumentAuthor | None
    reason: str
    base: int | None
    parent: int | None
    patch_id: str | None
    tombstone: bool
    normalized: bool
    warnings: int
    changed: list[str]
    added: list[str]
    moved: list[str]
    removed: list[str]


@with_config(ConfigDict(extra="forbid"))
class PatchRecord(TypedDict):
    """History excludes nulls; previews carry the same fields with explicit nulls."""

    patch_id: str
    document_id: str
    created_at: str
    author: DocumentAuthor
    session_id: NotRequired[str | None]
    base: int
    reason: str
    state: str
    operations: list[JsonValue]
    revision: NotRequired[int | None]
    details: dict[str, JsonValue]
    decided_at: NotRequired[str | None]
    decided_by: NotRequired[DocumentAuthor | None]


@with_config(ConfigDict(extra="forbid"))
class BlockHistory(TypedDict):
    revision: int
    status: str
    author: DocumentAuthor


@with_config(ConfigDict(extra="forbid"))
class DocumentHistory(TypedDict):
    document: DocumentInfo
    revisions: list[HistoryRevision]
    blocks: dict[str, list[BlockHistory]]
    patches: list[PatchRecord]


@with_config(ConfigDict(extra="forbid"))
class ConflictAuthor(TypedDict):
    revision: int
    author: DocumentAuthor


@with_config(ConfigDict(extra="forbid"))
class DocumentConflict(TypedDict, total=False):
    block: str
    math: str
    now: str | None
    at_base: str | None
    kind: str | None
    why: str
    changed_by: list[ConflictAuthor]


@with_config(ConfigDict(extra="forbid"))
class DocumentChange(TypedDict):
    outcome: str
    document_id: str
    revision: int | None
    base: int | None
    applied_to: int | None
    rebased: bool
    patch_id: str | None
    changes: dict[str, JsonValue]
    normalized: DocumentNormalization
    warnings: list[DocumentWarning]
    conflicts: list[DocumentConflict]
    unnamed: list[str]
    message: str | None
    replayed: bool


@with_config(ConfigDict(extra="forbid"))
class PatchPreview(TypedDict):
    patch: PatchRecord
    base: int
    current: str
    proposed: str | None
    allowed: bool
    message: str | None
    unnamed: list[str]


@with_config(ConfigDict(extra="forbid"))
class CommentAnchor(TypedDict):
    sha256: str
    block_sha256: str
    revision: int
    block: str
    math: str | None
    start: int | None
    end: int | None
    kind: Literal["block", "math", "range"]
    quote: str
    quote_truncated: bool
    before: str
    after: str


@with_config(ConfigDict(extra="forbid"))
class CommentBodySource(TypedDict):
    entry_id: str
    revision: NotRequired[int]
    sha256: NotRequired[str]
    body: NotRequired[str]
    author: NotRequired[DocumentAuthor | None]
    original_author: NotRequired[DocumentAuthor]
    held: NotRequired[str]


@with_config(ConfigDict(extra="forbid"))
class CommentPayload(TypedDict, total=False):
    revision: int
    changed_blocks: list[str]
    outside_anchor: list[str]
    changed_blocks_total: int
    changed_blocks_truncated: bool
    outside_anchor_total: int
    outside_anchor_truncated: bool
    addressed_to: str | None
    request_id: str | None
    anchor: dict[str, JsonValue]
    entry: str
    base: int
    sha256: str
    body_revision: int


@with_config(ConfigDict(extra="forbid"))
class CommentEntry(TypedDict):
    entry_id: str
    number: int
    kind: Literal["create", "reply", "edit", "edit_body", "resolve", "reopen"]
    author: DocumentAuthor
    body: str
    created_at: float
    body_source: NotRequired[CommentBodySource]
    payload: CommentPayload


@with_config(ConfigDict(extra="forbid"))
class CommentRequest(TypedDict):
    request_id: str
    session_id: str
    entry_id: str
    state: Literal["queued", "delivered", "responded"]
    delivered_run: str | None
    responded_entry: str | None


@with_config(ConfigDict(extra="forbid"))
class CommentAttachment(TypedDict):
    start: NotRequired[int]
    end: NotRequired[int]
    status: Literal["current", "moved", "stale", "orphaned"]
    quote: str
    current: str | None


@with_config(ConfigDict(extra="forbid"))
class CommentThread(TypedDict):
    thread_id: str
    anchor: CommentAnchor
    opener: DocumentAuthor
    created_at: float
    revision: int
    status: Literal["open", "resolved"]
    entries_before: int | None
    entries: list[CommentEntry]
    requests: list[CommentRequest]
    view_revision: int
    attachment: CommentAttachment


@with_config(ConfigDict(extra="forbid"))
class CommentListing(TypedDict):
    threads: list[CommentThread]
    next_after: str | None


@with_config(ConfigDict(extra="forbid"))
class CommentChange(TypedDict):
    thread_id: str
    entry_id: str
    revision: int
    request_id: str | None
    outcome: str
    replayed: NotRequired[bool]


@with_config(ConfigDict(extra="forbid"))
class JobCheckin(TypedDict):
    job_id: str
    alias: str
    model: str | None
    at: str
    state: Literal["fine", "needs_attention"]
    note: NotRequired[str]
    input_tokens: NotRequired[int | None]
    output_tokens: NotRequired[int | None]


@with_config(ConfigDict(extra="forbid"))
class JobWait(TypedDict):
    """Persisted waits can predate mode and started_at."""

    run_id: str
    state: Literal["waiting"]
    job_ids: list[str]
    mode: NotRequired[Literal["all", "first"]]
    started_at: NotRequired[str]
    watcher: str | None
    checkins: list[JobCheckin]
    connected: bool


@with_config(ConfigDict(extra="forbid"))
class JobLogs(TypedDict, total=False):
    stdout: str
    stderr: str


@with_config(ConfigDict(extra="forbid"))
class JobSandboxView(TypedDict):
    """A job sandbox's platform record: the job's own sandbox, never agent text (JS-18)."""

    profile: str
    phase: Literal["creating", "preparing", "running", "collecting", "released", "retained"]
    environment_id: str
    cpu_millicores: int
    memory_mib: int
    collection: Literal["pending", "complete", "incomplete"]
    retained_until: str | None
    files: str
    manifest_sha256: str | None


@with_config(ConfigDict(extra="forbid"))
class JobNotCollected(TypedDict):
    path: str
    reason: str


@with_config(ConfigDict(extra="forbid"))
class JobManifestView(TypedDict):
    """What the job's immutable result manifest says, once the job has ended (JS-16)."""

    reason: str | None
    collected: int
    not_collected_total: int
    """How many it left out; ``not_collected`` names the first twenty."""
    not_collected: list[JobNotCollected]
    replayed_packages: int
    isolation_tier: int | None


@with_config(ConfigDict(extra="forbid"))
class BackgroundJob(TypedDict):
    job_id: str
    state: str
    purpose: str
    expected_progress: str
    elapsed_seconds: float
    exit_code: int | None
    error: str | None
    stdout: str
    stderr: str
    written_files: list[str]
    written_files_truncated: bool
    platform_notices: list[str]
    cpu_seconds: float | None
    memory_bytes: int | None
    logs: JobLogs
    worker_restart: str
    checkin: JobCheckin | None
    sandbox: NotRequired[JobSandboxView]
    manifest: NotRequired[JobManifestView]


@with_config(ConfigDict(extra="forbid"))
class JobListing(TypedDict):
    enabled: bool
    jobs: list[BackgroundJob]
    truncated: bool
    waits: list[JobWait]


@with_config(ConfigDict(extra="forbid"))
class JobRelease(TypedDict):
    job_id: str
    phase: Literal["released"]


@with_config(ConfigDict(extra="forbid"))
class JobCancellation(TypedDict):
    job_id: str
    state: str
    purpose: str
    submitted_at: str
    started_at: str | None
    ended_at: str | None
