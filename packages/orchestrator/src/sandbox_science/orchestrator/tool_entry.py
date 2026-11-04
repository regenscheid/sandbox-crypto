# SPDX-License-Identifier: MIT
"""Tool dispatch values shared by the registry and ordinary capability builders, and the
tool groups a run loads when it needs them (docs/specs/tools.md, tool groups)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final

from .hooks import Effect, Notice
from .model import ToolCall, ToolSpec
from .recording import Written


@dataclass
class ToolOutcome:
    """What the model sees, and what the transcript keeps."""

    text: str
    record: dict[str, Any] = field(default_factory=dict[str, Any])
    whole: bool = False
    """The result must reach the model as it is, with no notices riding on it."""
    written: Written | None = None
    """The files the call changed, which the run's recorder records (RA-27)."""
    saved: dict[str, str] = field(default_factory=dict[str, str])
    """The texts saved whole behind a sample, by path (Shell 5), which the session's
    archive keeps beside the result (docs/specs/context-management.md, CM-6)."""
    image: tuple[Notice, str] | None = None
    """An image ``view_image`` gives the model, and its token: the loop adds its framed
    message after the turn's results (docs/specs/figures.md, FG-37)."""


@dataclass(frozen=True)
class Request:
    """One tool call as its handler gets it; ``args`` is empty for a raw entry."""

    call: ToolCall
    args: dict[str, Any]
    operation: str
    model: str | None


Handler = Callable[[Request], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolEntry:
    """An offered tool: its spec, its handler, and the effect class a gate reads."""

    spec: ToolSpec
    effect: Effect
    handler: Handler
    raw: bool = False
    """The handler parses the call's arguments itself."""
    bound: bool = False
    """The result is bounded like a file's or an execution's text."""
    whole: bool = False
    """The result reaches the model as it is, with no notices riding on it."""


def group(
    specs: Sequence[ToolSpec],
    run: Callable[[str, Request], Awaitable[tuple[str, dict[str, Any]]]],
    effects: Mapping[str, Effect],
    **flags: bool,
) -> list[ToolEntry]:
    """A group's entries, whose ``run`` answers a call with text and a record. A tool
    ``effects`` does not name is an effect, so a new tool is gated until classified."""

    async def handler(name: str, request: Request) -> ToolOutcome:
        return ToolOutcome(*await run(name, request))

    return [
        ToolEntry(spec, effects.get(spec.name, "effect"), partial(handler, spec.name), **flags)
        for spec in specs
    ]


def plain(handler: Callable[[dict[str, Any], str], Awaitable[ToolOutcome]]) -> Handler:
    return lambda request: handler(request.args, request.operation)


@dataclass(frozen=True)
class ToolGroup:
    """Tools a run is not offered at its start, which it loads for the rest of the run
    when it needs them: with ``load_tools``, by calling one of them, or when a notice
    from one of ``notices`` enters its conversation (docs/specs/tools.md, tool groups)."""

    name: str
    description: str
    """What the group is for, in one line of ``load_tools``' description."""
    tools: tuple[str, ...]
    notices: tuple[str, ...] = ()
    """The notice sources whose notices make the group relevant, and load it."""


LOAD_TOOLS: Final = "load_tools"
GROUPS: Final = (
    ToolGroup(
        "documents",
        "create, edit and delete living documents, and their comment threads, where a "
        "document work request is answered",
        (
            "document_create",
            "document_patch",
            "document_delete",
            "document_comments",
            "document_comment",
            "document_reply",
            "document_comment_read",
            "document_comment_edit",
            "document_resolve",
        ),
        ("requests", "math"),
    ),
    ToolGroup(
        "reviews",
        "consistency review findings: read them and their evidence, respond with a fix "
        "or a dismissal, and ask for a review",
        (
            "review_findings",
            "review_finding_read",
            "review_evidence_read",
            "review_respond",
            "request_review",
            "review_response_read",
            "review_response_edit",
        ),
        ("reviews",),
    ),
    ToolGroup(
        "ledger_admin",
        "the ledger's frontier and its categories, imports of results files your code "
        "wrote, and an entry's revisions and work records",
        ("ledger_work", "ledger_history", "ledger_import", "ledger_frontier", "ledger_category"),
    ),
    ToolGroup(
        "jobs",
        "background jobs: computations that run for hours while you work, which you list, "
        "read, wait on and cancel",
        ("start_job", "list_jobs", "job_status", "cancel_job", "wait_jobs"),
        ("jobs",),
    ),
    ToolGroup(
        "provisioning",
        "install packages and capabilities in the environment, and follow their progress",
        ("provision", "provision_status"),
    ),
    ToolGroup(
        "resources",
        "answer a child's check-in at a soft cap, and read the session's recorded spend",
        ("resource_decide", "resource_usage"),
        ("resources",),
    ),
)
"""The deferred groups, in the order ``load_tools`` lists them. Every other tool is core:
offered to every run from its start. A tool stays core when a prompt tells the model to
use it unprompted, and a deferred one is loaded by the notice that asks for it."""
GROUP_OF: Final = {tool: group.name for group in GROUPS for tool in group.tools}
"""Each deferred tool's group."""
NOTICE_GROUPS: Final = {source: group.name for group in GROUPS for source in group.notices}
"""The one mapping from a notice's source to the group its notices load."""


def deferred(
    entries: Sequence[ToolEntry],
) -> tuple[list[ToolEntry], dict[str, tuple[ToolEntry, ...]]]:
    """``entries`` as a run with tool groups is offered them: the core ones from its
    start, in their order, and the others by group, in ``GROUPS``' order, each group's
    in their order; a group none of ``entries`` is in is left out."""
    core = [entry for entry in entries if entry.spec.name not in GROUP_OF]
    groups = {
        group.name: found
        for group in GROUPS
        if (found := tuple(e for e in entries if GROUP_OF.get(e.spec.name) == group.name))
    }
    return core, groups
