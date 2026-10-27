# SPDX-License-Identifier: MIT
"""MCP servers' classifications as the workspace's review view reads and accepts them
(D96, CL-1 to CL-6).

The worker serves them to an operator client: per configured server of a project, the
classification in force (the checked-in catalog's, or the project's accepted revision in
the policy store), each tool with its class, rules and pin state against the policy
middleware's latest drift record; the drafts a model wrote for the project; and the two
acceptances, of a draft (edited or not) and of a drifted tool list. What the worker
computes (pin states, widenings) it computes; these models only bound the shapes.
docs/MCP-CONNECTORS.md, "Classification files", has the rules.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints

from .model import Contract, Identifier, Sha256

MAX_TOOLS: Final = 1024
"""Tools one classification or draft lists."""
MAX_DRIFT_TOOLS: Final = 100
"""Tools a drift summary lists of those added, removed and changed (the middleware's
record lists as many)."""
MAX_DRIFT_LISTINGS: Final = 64
"""Unaccepted drifted listings a review shows and a re-acceptance names; past them, the
operator's ``accept-tools`` ends the drifts."""
MAX_DRAFTS: Final = 64
"""Drafts one listing carries, newest first."""
MAX_SERVERS: Final = 256
MAX_YAML_CHARS: Final = 1024 * 1024
"""A classification file's length, as the policy loader bounds a file's bytes."""
MAX_REASON_CHARS: Final = 600

Text = Annotated[str, StringConstraints(max_length=600)]
ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
OperationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
"""The workspace's name for one acceptance, which a retry repeats (``operation_id``)."""
EffectName = Literal["read", "write", "communicate", "browse", "admin", "unclassified"]
PinState = Literal["current", "changed", "added", "removed", "unpinned"]
"""A tool against the latest listing the middleware compared: as pinned (``current``),
listed with another digest (``changed``), listed but not pinned (``added``), pinned but no
longer listed (``removed``), or classified without a pin (``unpinned``)."""


class ClassificationSource(Contract):
    """Where the classification in force comes from: the checked-in catalog (its name and
    the day its pinned listing was captured) or the project's accepted revision."""

    kind: Literal["catalog", "project"]
    name: Text | None = None
    """The catalog's name, as the policy names it."""
    revision: Sha256 | None = None
    """A project revision: the hex SHA-256 of its file."""
    pinned: date | None = None
    accepted_at: AwareDatetime | None = None
    accepted_by: Text | None = None
    reason: Text | None = None


class ToolRule(Contract):
    """An argument rule: the call's class when the argument is set (``present``) or has a
    value (``equals``, as JSON text, so ``true`` and ``"true"`` differ)."""

    argument: Text
    present: bool | None = None
    equals: Text | None = None
    effect: EffectName


class ClassifiedTool(Contract):
    """One tool as the view shows it."""

    name: ToolName
    effect: EffectName
    """Its base class; for a tool the classification does not mention, its hints' class."""
    reason: Text | None = None
    rules: list[ToolRule] = Field(default_factory=list[ToolRule], max_length=64)
    significant: dict[Text, Text] | None = None
    """The arguments an approval card summarizes a call by, by kind (D97)."""
    sha256: Sha256 | None = None
    pin_state: PinState
    mentioned: bool
    """False for a tool the classification does not name (one the listing added)."""
    escalates: bool
    """Whether a call of it without arguments asks first under the project's policy: by its
    base class, or for a tool the classification does not mention by its hints' class (a
    ``readOnlyHint`` tool is a read)."""


class DriftTool(Contract):
    name: ToolName
    sha256: Sha256 | None = None
    """The digest the listing gave it; None in a record from before digests were kept."""


class DriftListing(Contract):
    """One listing a server's tools drifted to from the pin in force: its digest (None in a
    record from before listings were digested), when and by whom it was recorded (the
    middleware, which read it, or ``runtime``, from the agent runtime's report), and how it
    differs from the pin."""

    listing: Sha256 | None
    at: AwareDatetime
    source: Literal["middleware", "runtime"]
    added: list[DriftTool] = Field(max_length=MAX_DRIFT_TOOLS)
    removed: list[ToolName] = Field(max_length=MAX_DRIFT_TOOLS)
    changed: list[DriftTool] = Field(max_length=MAX_DRIFT_TOOLS)


class DriftSummary(Contract):
    """A server's drift from the pin in force: its newest unaccepted ``mcp-tools-drift``
    record (else its newest), in the top fields, and in ``drifts`` every listing the project
    has not accepted, newest first, each of which a re-acceptance must name."""

    at: AwareDatetime
    pin: Text
    """The digest of the pinned tool list the listing drifted from."""
    added: list[DriftTool] = Field(max_length=MAX_DRIFT_TOOLS)
    removed: list[ToolName] = Field(max_length=MAX_DRIFT_TOOLS)
    changed: list[DriftTool] = Field(max_length=MAX_DRIFT_TOOLS)
    held: bool
    """Whether the server's non-read calls still ask (D103): not re-accepted since."""
    drifts: list[DriftListing] = Field(max_length=MAX_DRIFT_LISTINGS)
    """Every listing the project has not accepted from this pin, newest first; the newest
    drift alone when none is unaccepted."""


class ClassificationInForce(Contract):
    """A configured server's classification in force for a project."""

    server: ServerName
    source: ClassificationSource
    digest: Text
    """``sha256:`` over the catalog's files, or over a project revision's file."""
    tools: list[ClassifiedTool] = Field(max_length=MAX_TOOLS)
    drift: DriftSummary | None = None


class ClassificationsInForce(RootModel[list[ClassificationInForce]]):
    """``GET /v1/classifications``: one per configured server that names a classification."""

    model_config = ConfigDict(frozen=True)
    root: list[ClassificationInForce] = Field(max_length=MAX_SERVERS)


class DraftTool(ClassifiedTool):
    """A draft's tool beside the one in force."""

    current_effect: EffectName | None = None
    """Its base class in force, if the classification in force names it."""
    widening: bool
    """Whether accepting would widen what some call of it gets (``widening_reasons``)."""
    widening_reasons: list[Text] = Field(default_factory=list[str], max_length=16)
    """Why, in words: a call that would pass without asking where it asks or is denied now,
    a wider grant ceiling, or a class whose grants (session or project) would cover it.
    Computed against the project's policy as the policy store binds it to the project
    (``projects/<project>.json``), else the revision granted to it last."""


class ClassificationDraft(Contract):
    """A draft a model wrote for a project's server: the model's text, never in force."""

    draft: Sha256
    server: ServerName
    created_at: AwareDatetime
    model: Text | None = None
    """The model alias that drafted it."""
    yaml: Annotated[str, StringConstraints(max_length=MAX_YAML_CHARS)]
    tools: list[DraftTool] = Field(max_length=MAX_TOOLS)


class ClassificationDrafts(RootModel[list[ClassificationDraft]]):
    """``GET /v1/classifications/{server}/drafts``: newest first."""

    model_config = ConfigDict(frozen=True)
    root: list[ClassificationDraft] = Field(max_length=MAX_DRAFTS)


class ClassificationAcceptance(Contract):
    """The user's acceptance of a draft, edited or not: the file to put in force. A retry
    with the same ``operation_id`` gets the first answer; another request with it is a
    conflict."""

    project: Identifier
    operation_id: OperationId
    yaml: Annotated[str, StringConstraints(min_length=1, max_length=MAX_YAML_CHARS)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REASON_CHARS)]


class ClassificationAccepted(Contract):
    """The revision now in force and what accepting it widened, computed against the
    project's policy as the policy store binds it to the project, else the revision
    granted to it last."""

    revision: Sha256
    accepted_at: AwareDatetime
    widenings: list[ToolName] = Field(max_length=MAX_TOOLS)
    """The tools whose acceptance widened what their calls get (``DraftTool.widening``)."""
    widening_reasons: dict[ToolName, list[Text]] = Field(
        default_factory=dict[str, list[str]], max_length=MAX_TOOLS
    )
    """Why, by tool, as ``DraftTool.widening_reasons`` says."""


class DriftAcceptance(Contract):
    """The user's re-acceptance of a drifted server's tool list for a project, kept by
    ``operation_id`` as a draft's acceptance is. ``listings`` are the digests of the drifted
    listings the review showed; the worker refuses them unless they are exactly those the
    project has not accepted, so nothing the user did not see is accepted."""

    project: Identifier
    operation_id: OperationId
    reason: Annotated[str, StringConstraints(min_length=1, max_length=MAX_REASON_CHARS)]
    listings: list[Sha256] = Field(max_length=MAX_DRIFT_LISTINGS)


class DriftAccepted(Contract):
    accepted_at: AwareDatetime


MAX_REPORTED_TOOLS: Final = 2048
"""Tools one reported listing carries."""


class ReportedTool(Contract):
    """One tool of a listing the agent runtime's client read: its name, its digest
    (``mcp_wire.tool_digest``) and the class its hints give it."""

    name: ToolName
    digest: Sha256
    hint: EffectName


class ListingReport(Contract):
    """A complete ``tools/list`` listing of a server the agent runtime's direct client read,
    for the worker to compare with the project's pin (D98, D103): the server's own tools,
    before the client removed the denied ones, and the listing's digest (``pin_digest`` of
    every tool's digest by name). The control service names the project, session and run of
    the attempt that listed, or of its canaries; the runtime names none."""

    project_id: Identifier
    server: Identifier
    session: Identifier | None = None
    run: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")] | None = (
        None
    )
    tools: tuple[ReportedTool, ...] = Field(max_length=MAX_REPORTED_TOOLS)
    listing: Sha256


class ListingReported(Contract):
    """The worker's answer: whether the listing drifted from the project's pin, and the
    ``mcp-tools-drift`` record it appended (none for a listing whose drift is the one in
    force already, or that matches its pin)."""

    drifted: bool
    record: int | None = Field(default=None, ge=1)


class ListedToolView(Contract):
    """One tool of the latest listing reported for a server, as the workspace's MCP
    services view shows it: the class its hints give it, its class as a listing tells it
    (``effect``: by the project's classification in force when it names the tool, else by
    the hint; ``read`` only when no argument can make a call of it more), and what a call
    of it by name alone, without arguments, is decided as (``allow``, ``escalate`` or
    ``deny``) under the project's policy and classification in force. Arguments can make a
    call stricter."""

    name: ToolName
    hint: EffectName
    effect: EffectName
    classified: bool
    """Whether the classification in force names the tool."""
    action: Literal["allow", "escalate", "deny"]


class ReportedListing(Contract):
    """The latest complete listing of a server that the agent runtime's client reported
    (``report_mcp_listing``, D98) and the worker took, with each tool as the classification
    in force sees it now. The worker keeps the latest one per project and server in memory:
    after it restarts there is none until a run or a canary lists the server again."""

    server: ServerName
    reported_at: AwareDatetime
    session: Identifier | None = None
    run: str | None = Field(default=None, max_length=160)
    listing: Sha256
    drifted: bool
    """Whether the listing drifted from the project's pin when it was reported."""
    tools: list[ListedToolView] = Field(max_length=MAX_REPORTED_TOOLS)


class ReportedListings(RootModel[list[ReportedListing]]):
    """``GET /v1/mcp-listings?project=``: one per server of the project that has one."""

    model_config = ConfigDict(frozen=True)
    root: list[ReportedListing] = Field(max_length=MAX_SERVERS)


MAX_RUNTIME_SERVERS: Final = 64
"""Servers one ``GET /v1/runtime-classifications`` may name: ``[mcp_direct]``'s bound."""


class RuntimeClassification(Contract):
    """One server's classification revision in force for a project, for compiling the agent
    runtime's fence: none (the checked-in file applies), the revision's file exactly as
    stored, or why the binding cannot be read (the fence then takes the checked-in file,
    and the policy middleware refuses the server's calls while it stays unreadable)."""

    server: ServerName
    revision: Sha256 | None = None
    classification: str | None = Field(default=None, max_length=MAX_YAML_CHARS)
    """The revision's YAML; the reader checks that it digests to ``revision``."""
    error_kind: Literal["binding", "revision"] | None = None
    """Why nothing is answered: the binding (``current.json``) or the revision it names
    cannot be read. The policy middleware, reading the same, refuses the server's calls."""
    error: Text | None = None
    """A short reason, without the worker's paths; the worker logs the details."""


class RuntimeClassifications(Contract):
    """``GET /v1/runtime-classifications?project=&server=``: the Kubernetes runtime manager's
    one read besides binding the runtime's sandbox (``binding_only``), for its own project
    and the servers it names, and nothing of drafts, acceptances or drift."""

    project_id: Identifier
    servers: list[RuntimeClassification] = Field(max_length=MAX_RUNTIME_SERVERS)
