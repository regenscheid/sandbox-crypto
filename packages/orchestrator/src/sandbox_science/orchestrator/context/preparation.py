# SPDX-License-Identifier: MIT
"""Prepare an agent's archive scope, reconstructed view, folding and retrieval together.

Model selection and execution authority belong to the caller. This boundary receives
an already admitted scope and resolved models, and keeps all history consumers tied
to that scope without importing the harness or its tool assembly.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ErrorKind, ExecutionContext
from sandbox_science.contracts.worker import KernelObservation
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.digest import Digest, Rendered, constraints_block, render
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.proposals import FoldProposals
from sandbox_science.orchestrator.model import ChatModel
from sandbox_science.orchestrator.notices import DIGEST, TASK_CONSTRAINTS
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.tokens import chars_per_token

from .history import (
    Archive,
    Folder,
    HistoryTool,
    Judge,
    Register,
    RunArchive,
    View,
    geometry,
    ledger_cites,
    ledger_entries,
    ledger_works,
    recorded_words,
    register_reads,
    session_view,
    settle,
)
from .history.delegated import delegated_folder
from .history.geometry import Shares

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryScope:
    """The trusted execution and exact attempt identity shared by history consumers."""

    context: ExecutionContext
    run: str
    parent: str | None
    message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FoldLimits:
    """Resolved context geometry, independent of model-role selection policy."""

    ceiling: int
    chunk: int | None
    shares: Shares
    consolidate: bool = False


@dataclass(frozen=True)
class PreparedContext:
    """One attempt's reconstructed context and its archive-backed capabilities."""

    folder: Folder
    log: RunArchive
    retrieval: HistoryTool

    @property
    def view(self) -> View:
        """The initial view, before system prompts and tool schemas are assembled."""
        return self.folder.view


async def prepare_context(
    scope: HistoryScope,
    *,
    projects: ProjectStore,
    documents: DocumentStore,
    run_state: RunState,
    limits: FoldLimits,
    alias: str,
    brief: str,
    delegation: str | None,
    narrator: ChatModel | None,
    former_model: Callable[[str], ChatModel | None],
    learned: MutableMapping[str, int],
    observe_kernels: Callable[[ExecutionContext], Awaitable[KernelObservation]],
    clock: Clock = SYSTEM_CLOCK,
) -> PreparedContext:
    """Reconstruct scoped history and give folding, logging and retrieval one owner."""
    context, name = scope.context, scope.run
    if scope.parent is not None:
        block = await threaded(task_block, documents, scope, delegation, clock=clock)
        folder = await delegated_folder(
            Archive(projects.control, clock=clock, chain=True),
            context,
            run=name,
            parent=scope.parent,
            brief=brief,
            constraints=block,
            ceiling=limits.ceiling,
            chunk=limits.chunk,
            shares=limits.shares,
            clock=clock,
            model=narrator,
        )
    else:
        folder = await _main_folder(
            scope,
            projects=projects,
            documents=documents,
            run_state=run_state,
            limits=limits,
            alias=alias,
            narrator=narrator,
            former_model=former_model,
            learned=learned,
            observe_kernels=observe_kernels,
            clock=clock,
        )
    return PreparedContext(
        folder=folder,
        log=RunArchive(
            Archive(projects.control, clock=clock),
            context,
            run=name,
            parent=scope.parent,
            message_ids=scope.message_ids,
            clock=clock,
        ),
        retrieval=HistoryTool(
            Archive(projects.control, clock=clock, chain=True),
            context,
            run=name,
            delegated=scope.parent is not None,
        ),
    )


async def _main_folder(
    scope: HistoryScope,
    *,
    projects: ProjectStore,
    documents: DocumentStore,
    run_state: RunState,
    limits: FoldLimits,
    alias: str,
    narrator: ChatModel | None,
    former_model: Callable[[str], ChatModel | None],
    learned: MutableMapping[str, int],
    observe_kernels: Callable[[ExecutionContext], Awaitable[KernelObservation]],
    clock: Clock,
) -> Folder:
    context, name = scope.context, scope.run
    project, session = context.project_id, context.session_id
    archive = Archive(projects.control, clock=clock)
    calls = await threaded(projects.usage, project, session, alias)
    runs = await threaded(projects.unarchived_runs, project, session)
    earlier = functools.partial(recorded_words, projects, run_state, project, session)
    top = limits.ceiling
    start = geometry(top, 0, limits.chunk, limits.shares)  # no system message yet
    per_token = chars_per_token(calls)
    ledger = Ledger(documents)
    digest = functools.partial(research_digest, documents, project, session, clock)
    found = await threaded(digest, int(start.digest * per_token))
    # Which folded markers are retired, by the ledger and the read register (CM-21a).
    judge = Judge(
        ledger_entries(ledger, project),
        register_reads(Register(archive.control), project),
        ledger_works(ledger, project),
        ledger_cites(ledger, project),
    )
    view = await threaded(
        session_view,
        archive,
        context,
        geometry=start,
        per_token=per_token,
        runs=runs,
        earlier=earlier,
        judge=judge,
        digest=found,
        consolidating=limits.consolidate,
    )
    kernels = None
    if view.cells:  # whether the kernels its cells ran in still run (CM-29c)
        try:
            kernels = (await observe_kernels(context)).kernels
        except Exception as error:  # unknown, then: the run goes on
            _log.warning("the kernels could not be observed for the view: %r", error)
    view = await settle(view, archive, context, run=name, kernels=kernels, clock=clock)

    def exists(handle: str) -> bool:
        try:
            ledger.entry(project, handle)
        except (DocumentError, ValueError):
            return False
        return True

    # The session's last main run on another model: this one refits to its own
    # geometry first, that model writing the refit's narratives (CM-31).
    switched = await threaded(_switched, projects, project, session, alias)
    former = former_model(switched[0]) if switched is not None else None
    made = Folder(
        archive,
        context,
        run=name,
        view=view,
        start=start,
        ceiling=top,
        chunk=limits.chunk,
        calls=calls,
        runs=runs,
        earlier=earlier,
        kernels=kernels,
        digest=digest,
        model=narrator,
        exists=exists,
        judge=judge,
        consolidating=limits.consolidate,
        switched=switched,
        former=former,
        carried=await threaded(_carried, projects, project, session),
        clock=clock,
    )
    # The ceiling a context error capped, carried to later runs: this service's, or
    # the session's last main run's on the alias, as its record keeps it (CM-43).
    made.learned = learned.get(alias)
    if made.learned is None:
        made.learned = await threaded(_learned, projects, project, session, alias)

    def learn(cap: int) -> None:
        learned[alias] = cap

    made.on_cap = learn
    # A fold's proposals are checked against its span and kept apart (CM-15).
    made.propose = functools.partial(FoldProposals(ledger).propose, project, session, name)
    return made


def research_digest(
    documents: DocumentStore, project: str, session: str, clock: Clock, budget: int
) -> Rendered | None:
    """The research-state digest a main run's state block holds (CM-16), within
    ``budget`` characters, its third of the state block (CM-27), framed, with the
    handles and lines it shows, which the fold region's short forms are checked
    against (CM-21b); ``None`` when the research state is empty."""
    ledger = Ledger(documents)
    digest = Digest(
        ledger,
        proposals=FoldProposals(ledger),
        plans=SessionPlans(documents),
        questions=Questions(documents, clock=clock),
        delegations=DelegationRecords(documents, clock=clock),
        findings=Findings(ReviewStore(documents)),
    )
    try:
        found = render(digest.sections(project, session), budget)
    except DocumentError as error:  # a project the ledger does not know yet
        _log.warning("no digest for %s/%s: %s", project, session, error)
        return None
    if not found.text:
        return None
    return dataclasses.replace(found, text=DIGEST.text + "\n" + found.text)


def task_block(
    documents: DocumentStore, scope: HistoryScope, delegation: str | None, *, clock: Clock
) -> str | None:
    """A delegated task's state block: the user's standing constraints, each with the
    user's words beside it (CM-37a). A check gets none, nor anything else."""
    project, session = scope.context.project_id, scope.context.session_id
    if delegation is None:
        return None
    records = DelegationRecords(documents, clock=clock)
    try:
        if records.get(project, session, delegation).kind != "task":
            return None
        lines = Digest(Ledger(documents)).constraints(project, session)
    except (ControlError, DocumentError) as error:  # no record of it: no constraints
        _log.warning("no state block for delegated run %s: %s", scope.run, error)
        return None
    return TASK_CONSTRAINTS.text + "\n" + constraints_block(lines) if lines else None


def _learned(projects: ProjectStore, project: str, session: str, alias: str) -> int | None:
    """The ceiling the session's last main run on ``alias`` capped after a context
    error, as its record keeps it (``summaries.folds.capped``), if it did."""
    for entry in projects.iter_runs(project, session, newest_first=True):
        if entry.role != "main":
            continue
        try:
            record = projects.run(project, session, entry.name)
        except ProjectError as error:
            if error.kind is ErrorKind.UNSUPPORTED:
                raise  # Do not discard persisted fold/model constraints at a read bound.
            return None
        folds = record.summaries.get("folds")
        capped = folds.get("capped") if isinstance(folds, dict) else None
        return capped if record.alias == alias and type(capped) is int else None
    return None


def _carried(projects: ProjectStore, project: str, session: str) -> tuple[str, str] | None:
    """The session's last main run that began folding, and the fold it kept for the next
    run's start (``summaries.folds.kept``, CM-24a), if it kept one."""
    for entry in projects.iter_runs(project, session, newest_first=True):
        if entry.role != "main":
            continue
        try:
            record = projects.run(project, session, entry.name)
        except ProjectError as error:
            if error.kind is ErrorKind.UNSUPPORTED:
                raise  # Do not discard persisted fold/model constraints at a read bound.
            return None
        folds = record.summaries.get("folds")
        if not isinstance(folds, dict):  # it stopped before its first call
            continue
        kept = folds.get("kept")
        return (entry.name, kept) if isinstance(kept, str) else None
    return None


def _switched(
    projects: ProjectStore, project: str, session: str, alias: str
) -> tuple[str, int] | None:
    """The alias and the ceiling of the session's last main run that folded, when it was
    on another alias than ``alias``, as its record keeps them (CM-31); or, when that run
    refused a switch, the alias and ceiling it refused to switch from, so that every run
    names the switch until one on the new alias goes on."""
    for entry in projects.iter_runs(project, session, newest_first=True):
        if entry.role != "main":
            continue
        try:
            record = projects.run(project, session, entry.name)
        except ProjectError as error:
            if error.kind is ErrorKind.UNSUPPORTED:
                raise  # Do not discard persisted fold/model constraints at a read bound.
            return None
        folds = record.summaries.get("folds")
        shape = folds.get("geometry") if isinstance(folds, dict) else None
        found = shape.get("ceiling") if isinstance(shape, dict) else None
        if type(found) is not int:
            continue
        refit = cast(dict[str, Any], folds).get("refit") if isinstance(folds, dict) else None
        if isinstance(refit, dict) and cast(dict[str, Any], refit).get("refused"):
            earlier = cast(dict[str, Any], refit).get("from")
            size = cast(dict[str, Any], refit).get("ceiling")
            if isinstance(earlier, str) and type(size) is int and earlier != alias:
                return earlier, size
        return (record.alias, found) if record.alias and record.alias != alias else None
    return None
