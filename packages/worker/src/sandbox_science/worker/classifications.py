# SPDX-License-Identifier: MIT
"""MCP servers' classifications, read and accepted for the workspace's review view (D96,
CL-1 to CL-6).

For an operator client's project, the worker shows each configured server's classification
in force (the project's accepted revision in the policy store, else the checked-in
catalog's), tool by tool with its pin state against the policy middleware's latest drift
record; the drafts a model wrote for the project, beside it, with the widenings marked;
and relays the user's two acceptances:

- **a draft**, edited or not: the YAML is read as the loader reads a classification file,
  its tools must be the server's (the draft's, the classification's in force or the latest
  listing's) with their known digests, and it becomes the project's revision in force, with
  its acceptance record and an ``mcp-classification-accepted`` event;
- **a drifted tool list**: an ``mcp-tools-accepted`` event naming the project, which ends
  the middleware's hold of the server's non-read calls for it (D103).

It also takes, from any client for its own projects, the listings the agent runtime's
direct client read and the middleware could not (event streams, D98): it compares each
with the pin of the project's classification in force, with the library's code
(``mcp_listings``), and appends the same ``mcp-tools-drift`` record the middleware would,
with source ``runtime``, unless that listing is the drift in force already. The
middleware's ``DriftLedger`` then holds the server's non-read calls as for its own reads.
It keeps the latest listing it took of each project's server in its store
(``listing_reports``), for the workspace's MCP services view (``listings``), which shows
each tool as the classification and the policy in force decide a call of it by name.

For the host's connectors it answers the class the project's classification in force gives
each tool a listing named, by name and the digest the host computed (``tool_classes``,
DA-7): only a tool the classification names with that very pin has a class, so neither a
drifted nor an unclassified tool is taken for a read.

The project's policy is the revision the policy store binds the project to, else the one
granted to it last (``policy add --project``). The single workspace user holds the operator
token, so the user is the actor (``user``); the workspace has no roles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.classifications import (
    MAX_DRIFT_LISTINGS,
    MAX_DRIFT_TOOLS,
    ClassificationAccepted,
    ClassificationDraft,
    ClassificationInForce,
    ClassificationSource,
    ClassifiedTool,
    DraftTool,
    DriftAccepted,
    DriftListing,
    DriftSummary,
    DriftTool,
    EffectName,
    ListedToolView,
    ListingReport,
    ListingReported,
    PinState,
    ReportedListing,
    RuntimeClassification,
    RuntimeClassifications,
    ToolAsk,
    ToolClass,
    ToolClasses,
    ToolClassQuery,
    ToolRule,
)
from sandbox_science.policy import (
    Action,
    HardFloor,
    LoadedPolicy,
    PolicyLoadError,
    PoolCeiling,
    evaluate_mcp_message,
)
from sandbox_science.policy.classification import (
    ServerClassification,
    changed_tools,
    class_action,
    classified_names,
    listed_effect,
    possible_classes,
    resolve,
    restrictiveness,
    revision_classification,
    server_classification,
    widening_reasons,
)
from sandbox_science.policy.detection import (
    ACCEPTED_RECORD,
    CLASSIFICATION_ACCEPTED,
    LISTING_REPORTED_RECORD,
    RUNTIME_SOURCE,
    DriftLedger,
    DriftSeen,
    pin_digest,
)
from sandbox_science.policy.effects import EffectClass, ToolEntry
from sandbox_science.policy.evaluate import DEFAULT_FLOOR
from sandbox_science.policy.mcp_listings import drift_record, listing_digest, listing_drift
from sandbox_science.policy.mcp_wire import ListedTool
from sandbox_science.policy.model import McpServer
from sandbox_science.policy.store import Acceptance, Draft, PolicyStore, StoreError

from .classification_receipts import ClassificationReceipts, ReceiptConflictError
from .environments import Environments
from .listing_reports import ListingReports
from .permissions import Permissions, PermissionStoreError

_log = logging.getLogger(__name__)

MAX_REPORTS: Final = 10
"""Listing reports one caller may make of one project's server a minute, counted apart for
runs' listings (a report naming a session or run) and for those naming neither, the
canaries' and the workspace user's availability checks, so that checks never spend the
runs' budget."""
REPORT_WINDOW: Final = timedelta(minutes=1)
MAX_REPORTERS: Final = 4096
"""Callers, projects and servers whose recent reports are counted, the oldest forgotten."""
RUNTIME_CALLER: Final = "agent-runtime"
"""The caller a reported listing's record names: the agent runtime's direct client."""
ACTOR: Final = "user"
"""Who accepts: the workspace's one user, who holds the worker's operator token."""
_EFFECTS: Final = frozenset(effect.value for effect in EffectClass)


class ClassificationReviewError(Exception):
    """A server, draft or acceptance the review cannot serve: ``kind`` is ``missing``,
    ``conflict`` or ``invalid``."""

    def __init__(self, message: str, kind: str = "conflict") -> None:
        super().__init__(message)
        self.kind = kind


class ClassificationDesk:
    """The review view's side of the policy store and the middleware's event store."""

    def __init__(
        self,
        store: PolicyStore,
        permissions: Permissions,
        policies: Callable[[str], LoadedPolicy | None],
        receipts: ClassificationReceipts,
        clock: Clock = SYSTEM_CLOCK,
        *,
        listings: ListingReports,
        ceiling: PoolCeiling | None = None,
        floor: HardFloor = DEFAULT_FLOOR,
    ) -> None:
        self.store = store
        self.permissions = permissions
        self.policies = policies
        self.receipts = receipts
        self.clock = clock
        self.ceiling = ceiling if ceiling is not None else PoolCeiling.unrestricted()
        """The pool's ceiling and floor, which decide the listed tools' calls with the
        policy, as the middleware does."""
        self.floor = floor
        self.kept = listings
        """The latest listing taken of each project's server: the report, when, and whether
        it drifted, in the worker's store."""
        self._reports = threading.RLock()
        """One report's check of the drifts and its records at a time."""
        self._recent: dict[tuple[str, str, str, str], deque[datetime]] = {}
        """Each caller's recent reports by project and server, for the rate."""

    # Reads.

    def in_force(self, project: str) -> list[ClassificationInForce]:
        """Each configured server's classification in force for the project. Blocking."""
        loaded = self.policies(project)
        if loaded is None:
            return []
        drifts = self._drifts()
        found: list[ClassificationInForce] = []
        for name in sorted(loaded.policy.mcp.servers):
            entry = loaded.policy.mcp.servers[name]
            current = self._current(project, name, entry)
            if current is not None:
                found.append(self._view(loaded, project, name, entry, current, drifts))
        return found

    def drafts(self, project: str, server: str) -> list[ClassificationDraft]:
        """The project's drafts for a server, newest first, beside its classification in
        force. Blocking."""
        loaded, entry = self._server(project, server)
        current = self._current(project, server, entry)
        hints = _hints(self._drift(self._drifts(), server, current, project))
        try:
            stored = self.store.classification_drafts(project, server)
        except StoreError as exc:
            raise PermissionStoreError(str(exc)) from exc
        return [self._draft(loaded, server, entry, current, draft, hints) for draft in stored]

    def export(self, project: str, server: str, revision: str) -> tuple[bytes, Acceptance | None]:
        """An accepted revision of the project's, its file exactly as stored, to check in
        (CL-3), and its acceptance record. Blocking."""
        try:
            data = self.store.classification_revision_bytes(project, server, revision)
        except StoreError as exc:
            raise ClassificationReviewError(str(exc), "missing") from exc
        try:
            return data, self.store.acceptance(project, server, revision)
        except StoreError as exc:
            raise PermissionStoreError(str(exc)) from exc

    # Acceptances.

    def accept_draft(
        self, project: str, server: str, draft: str, text: str, reason: str, operation_id: str
    ) -> ClassificationAccepted:
        """Put the user's edit of a draft in force for the project (CL-3), once per
        ``operation_id``. Blocking."""
        asked = {
            "accept": "draft",
            "server": server,
            "draft": draft,
            "yaml": text,
            "reason": reason,
        }

        def act() -> str:
            accepted = self._accept_draft(project, server, draft, text, reason, operation_id)
            return accepted.model_dump_json()

        answer = self._once(project, operation_id, asked, act)
        return ClassificationAccepted.model_validate_json(answer)

    def accept_drift(
        self,
        project: str,
        server: str,
        reason: str,
        operation_id: str,
        listings: Sequence[str] = (),
    ) -> DriftAccepted:
        """Re-accept a server's drifted tool list for the project (CL-4), once per
        ``operation_id``: exactly the drifted ``listings`` the review showed. Blocking."""
        shown = sorted(set(listings))
        asked = {"accept": "drift", "server": server, "reason": reason, "listings": shown}

        def act() -> str:
            accepted = self._accept_drift(project, server, reason, operation_id, shown)
            return accepted.model_dump_json()

        return DriftAccepted.model_validate_json(self._once(project, operation_id, asked, act))

    def _once(
        self,
        project: str,
        operation_id: str,
        asked: Mapping[str, object],
        act: Callable[[], str],
    ) -> str:
        text = json.dumps(asked, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(text.encode()).hexdigest()
        try:
            return self.receipts.once(project, operation_id, digest, act)
        except ReceiptConflictError as exc:
            raise ClassificationReviewError(str(exc)) from None

    def _accept_draft(
        self, project: str, server: str, draft: str, text: str, reason: str, operation_id: str
    ) -> ClassificationAccepted:
        loaded, entry = self._server(project, server)
        if entry.classification is None:
            raise ClassificationReviewError(
                f"the project's policy names no classification for {server}: a revision would "
                "not apply"
            )
        try:
            stored = self.store.classification_draft(project, server, draft)
        except StoreError as exc:
            raise ClassificationReviewError(str(exc), "missing") from exc
        if stored is None:
            raise ClassificationReviewError(f"no draft {draft} for {server}", "missing")
        data = text.encode()
        try:
            proposed = revision_classification(data, server, source="the edited classification")
        except PolicyLoadError as exc:
            raise ClassificationReviewError(str(exc), "invalid") from exc
        drifts = self._drifts()
        current = self._current(project, server, entry)
        seen = self._drift(drifts, server, current, project)
        _check_tools(proposed, stored.classification, current, seen)
        reasons = widening_reasons(loaded.policy, server, current, proposed, _hints(seen))
        widened = sorted(reasons)
        diff = {**changed_tools(current, proposed), "widenings": widened}
        now = self.clock.now()
        previous = self._binding(project, server)
        acceptance = Acceptance(ACTOR, now, reason, draft, previous, diff)
        try:
            revision = cast(
                str, self.store.accept_classification(project, server, data, acceptance).revision
            )
        except StoreError as exc:
            raise PermissionStoreError(str(exc)) from exc
        payload: dict[str, object] = {
            "project": project,
            "server": server,
            "revision": revision,
            "draft": draft,
            "replaces": previous,
            "reason": reason,
            "widenings": widened,
            "widening_reasons": reasons,
            "diff": diff,
            "actor": ACTOR,
            "operation_id": operation_id,
        }
        try:
            self.permissions.record_acceptance(CLASSIFICATION_ACCEPTED, payload)
        except PermissionStoreError:
            # Unrecorded, the acceptance did not happen: the revision it replaced stays.
            try:
                self.store.restore_classification(project, server, previous)
            except StoreError:
                _log.exception("revision %s of %s stays in force unrecorded", revision, server)
            raise
        return ClassificationAccepted(
            revision=revision, accepted_at=now, widenings=widened, widening_reasons=reasons
        )

    def _accept_drift(
        self, project: str, server: str, reason: str, operation_id: str, shown: list[str]
    ) -> DriftAccepted:
        _, entry = self._server(project, server)
        current = self._current(project, server, entry)
        drifts = self._drifts()
        seen = self._drift(drifts, server, current, project)
        if seen is None:
            raise ClassificationReviewError(f"{server}'s tool list has no drift to accept")
        now = self.clock.now()
        # The acceptance names the pin and every listing it accepts, those the project has
        # not accepted from that pin: a later listing that differs is a new drift, held
        # again (D103), and a record never ends a hold by itself.
        held = drifts.unaccepted(server, seen.pin, project)
        unaccepted = sorted({drift.listing for drift in held if drift.listing is not None})
        if shown != unaccepted:
            # Accept nothing the user did not see: a listing recorded since the review, or
            # one it did not show.
            raise ClassificationReviewError("the drift changed; review again")
        payload = {
            "project": project,
            "server": server,
            "pin": seen.pin,
            "listing": seen.listing,
            "listings": unaccepted,
            "reason": reason,
            "actor": ACTOR,
            "operation_id": operation_id,
        }
        self.permissions.record_acceptance(ACCEPTED_RECORD, payload)
        return DriftAccepted(accepted_at=now)

    # Listings the agent runtime's direct client read (D98).

    def report_listing(self, report: ListingReport, caller: str) -> ListingReported:
        """Compare a reported listing with the pin of the project's classification in force
        and record its drift once (see the module). The caller checked the project is its
        own; the server must be one of the project's policy. A report may start or keep a
        hold, never end one: one whose listing the project accepted while another drift of
        the server holds it is refused. At most ``MAX_REPORTS`` a minute per caller, project
        and server, for runs and for checks apart. Blocking."""
        _, entry = self._server(report.project_id, report.server)
        seen = {
            tool.name: ListedTool(tool.name, tool.digest, EffectClass(tool.hint))
            for tool in report.tools
        }
        if len(seen) != len(report.tools):
            raise ClassificationReviewError("a reported listing names a tool twice", "invalid")
        if listing_digest(seen) != report.listing:
            raise ClassificationReviewError("the listing's digest is not its tools'", "invalid")
        scope = "check" if report.session is None and report.run is None else "run"
        self._admit(caller, report.project_id, report.server, scope)
        current = self._current(report.project_id, report.server, entry)
        pin = pin_digest(current.pins) if current is not None else None
        drift = listing_drift(report.server, current.pins, seen) if current is not None else None
        subject = {
            "project": report.project_id,
            "caller": RUNTIME_CALLER,
            "sandbox_id": None,
            "session": report.session,
            "run": report.run,
            "server": report.server,
            "method": "tools/list",
            "tool": None,
        }
        seq: int | None = None
        with self._reports:
            drifts = self._drifts()
            if drift is not None and drifts.known(report.server, pin, report.listing):
                held = drifts.drifted(report.server, pin, report.project_id)
                if held is not None and drifts.accepted_listing(
                    report.server, pin, report.project_id, report.listing
                ):
                    # A replay of an accepted listing while another drift holds: refused.
                    raise ClassificationReviewError(
                        "this listing was accepted, and another drift of the server holds it"
                    )
            elif drift is not None:
                seq = self.permissions.record_drift(drift_record(drift, pin, subject, None))
            # Every report taken is recorded, drifted or not: it answers an unread listing.
            self.permissions.record_drift(
                {**subject, "pin": pin, "listing": report.listing, "drifted": drift is not None},
                kind=LISTING_REPORTED_RECORD,
            )
            self.kept.put(report, drift is not None, self.clock.now())
        return ListingReported(drifted=drift is not None, record=seq)

    def listings(self, project: str) -> list[ReportedListing]:
        """The latest listing taken of each of the project's servers that has one, each
        tool as the policy and classification in force decide a call of it by name alone.
        Blocking."""
        loaded = self.policies(project)
        if loaded is None:
            return []
        found: list[ReportedListing] = []
        for kept in self.kept.of(project):
            report, server = kept.report, kept.report.server
            entry = loaded.policy.mcp.servers.get(server)
            if entry is None:
                continue  # a server the project's policy no longer declares
            try:
                revision = self.store.classification(project, server)
            except (StoreError, PolicyLoadError) as exc:
                raise PermissionStoreError(f"the classification of {server}: {exc}") from exc
            found.append(
                ReportedListing(
                    server=server,
                    reported_at=kept.reported_at,
                    session=report.session,
                    run=report.run,
                    listing=report.listing,
                    drifted=kept.drifted,
                    tools=[
                        self._listed(loaded, server, entry, revision, tool.name, tool.hint)
                        for tool in report.tools
                    ],
                )
            )
        return found

    def _listed(
        self,
        loaded: LoadedPolicy,
        server: str,
        entry: McpServer,
        revision: ServerClassification | None,
        name: str,
        hint: EffectName,
    ) -> ListedToolView:
        """A listed tool as a call of it by name, without arguments, is decided: by the
        policy, the pool's ceiling and floor, the classification in force and its hint, as
        the middleware decides one (D96, D98)."""
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
        decision = evaluate_mcp_message(
            server,
            message,
            loaded,
            origin="orchestrator",
            ceiling=self.ceiling,
            floor=self.floor,
            hints={name: EffectClass(hint)},
            classification=revision,
        )
        effect = listed_effect(entry, name, decision, revision)
        action = (
            "deny"
            if decision.action is Action.DENY
            else "escalate"
            if decision.action is Action.ESCALATE
            else "allow"
        )
        stripped = name.removeprefix(entry.tool_prefix or "")
        return ListedToolView(
            name=name,
            hint=hint,
            effect=cast(EffectName, effect if effect in _EFFECTS else "unclassified"),
            classified=stripped in classified_names(entry, revision),
            action=action,
        )

    def tool_classes(self, query: ToolClassQuery) -> ToolClasses:
        """Each named tool's class, for the host's connectors (DA-7), as the policy
        middleware decides the agent runtime's calls: under the policy revision the
        project's runtime sandboxes are bound to and the project's classification in force.
        Only a tool the classification names with a pin equal to the digest the host
        computed has a class: the class a call of it by name without arguments resolves to
        (``read`` only when no argument can make a call more), whether every call of it asks
        the user, and the classes and ceilings a grant would have to name. Nothing the
        listing said beyond the name and the digest is taken. When the runtime sandboxes are
        bound to none or several revisions, no tool has a class: no answer could match the
        middleware's for every call. The caller checked the project is its own. Blocking."""
        names = [tool.name for tool in query.tools]
        if len(set(names)) != len(names):
            raise ClassificationReviewError("a tool is named twice", "invalid")
        project, server = query.project_id, query.server
        self._server(project, server)  # the project's own server, or not found
        none = {"project_id": project, "server": server}
        try:
            revisions = set(self.store.runtime_bindings(project).values())
            loaded = self.store.policy(revisions.pop()) if len(revisions) == 1 else None
            revision = self.store.classification(project, server)
        except (StoreError, PolicyLoadError) as exc:
            raise PermissionStoreError(f"the classes of {server}'s tools: {exc}") from exc
        if loaded is None:
            return ToolClasses(
                **none, tools=[ToolClass(name=name, state="unbound") for name in names]
            )
        entry = loaded.policy.mcp.servers.get(server)
        current = server_classification(entry, revision) if entry is not None else None
        if entry is None or current is None:
            return ToolClasses(
                **none,
                policy=loaded.digest,
                tools=[ToolClass(name=name, state="unclassified") for name in names],
            )
        return ToolClasses(
            **none,
            policy=loaded.digest,
            classification=current.digest,
            tools=[
                self._class(loaded, server, entry, current, tool.name, tool.digest)
                for tool in query.tools
            ],
        )

    def _class(
        self,
        loaded: LoadedPolicy,
        server: str,
        entry: McpServer,
        current: ServerClassification,
        name: str,
        digest: str,
    ) -> ToolClass:
        stripped = name.removeprefix(entry.tool_prefix or "")
        found = current.tools.get(stripped)
        if found is None:
            return ToolClass(name=name, state="unclassified")
        if found.sha256 is None:
            return ToolClass(name=name, state="unpinned")
        if found.sha256 != digest:
            return ToolClass(name=name, state="changed")
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
        decision = evaluate_mcp_message(
            server,
            message,
            loaded,
            origin="orchestrator",
            ceiling=self.ceiling,
            floor=self.floor,
            # The project's revision, else none: the catalog's is the policy's own.
            classification=current if current.revision is not None else None,
        )
        if decision.action is Action.DENY:
            return ToolClass(name=name, state="denied")
        effect = listed_effect(entry, name, decision, current)
        if effect not in _EFFECTS:
            return ToolClass(name=name, state="unclassified")
        # Every call asks only when every class its arguments can resolve it to asks or is
        # denied: one argument that makes it an allowed class lets that call out unasked.
        terms = [
            (possible, class_action(loaded.policy, server, possible))
            for possible in sorted(possible_classes(entry, stripped, current))
        ]
        escalates = decision.action is Action.ESCALATE and all(
            action.action in (Action.ESCALATE, Action.DENY) for _, action in terms
        )
        asks = (
            [
                ToolAsk(effect=possible.value, max_grant=action.max_grant.value)
                for possible, action in terms
                if action.action is Action.ESCALATE
            ]
            if escalates
            else []
        )
        return ToolClass(
            name=name,
            effect=cast(EffectName, effect),
            escalates=escalates,
            asks=asks,
            state="classified",
        )

    def _admit(self, caller: str, project: str, server: str, scope: str) -> None:
        """Count one report of the caller's for the project's server, of runs' or checks'
        listings (``scope``); refuse past the rate."""
        now = self.clock.now()
        with self._reports:
            recent = self._recent.setdefault((caller, project, server, scope), deque())
            while recent and now - recent[0] >= REPORT_WINDOW:
                recent.popleft()
            if len(recent) >= MAX_REPORTS:
                raise ClassificationReviewError(
                    f"more than {MAX_REPORTS} listing reports of {server} a minute"
                )
            recent.append(now)
            if len(self._recent) > MAX_REPORTERS:
                self._recent.pop(next(iter(self._recent)))

    # Parts.

    def _server(self, project: str, server: str) -> tuple[LoadedPolicy, McpServer]:
        loaded = self.policies(project)
        if loaded is None:
            raise ClassificationReviewError(f"project {project!r} has no policy", "missing")
        entry = loaded.policy.mcp.servers.get(server)
        if entry is None:
            raise ClassificationReviewError(
                f"the project's policy has no server {server}", "missing"
            )
        return loaded, entry

    def _current(self, project: str, server: str, entry: McpServer) -> ServerClassification | None:
        try:
            return server_classification(entry, self.store.classification(project, server))
        except (StoreError, PolicyLoadError) as exc:
            raise PermissionStoreError(f"the classification of {server}: {exc}") from exc

    def _binding(self, project: str, server: str) -> str | None:
        try:
            return self.store.classification_binding(project, server)
        except StoreError as exc:
            raise PermissionStoreError(str(exc)) from exc

    def _drifts(self) -> DriftLedger:
        return self.permissions.drifts()

    @staticmethod
    def _drift(
        drifts: DriftLedger,
        server: str,
        current: ServerClassification | None,
        project: str | None = None,
    ) -> DriftSeen | None:
        """The server's newest drift from the pin in force that holds ``project``, else its
        newest drift from that pin, if any."""
        if current is None:
            return None
        pin = pin_digest(current.pins)
        held = drifts.unaccepted(server, pin, project) if project is not None else []
        return held[0] if held else drifts.latest(server, pin)

    def _view(
        self,
        loaded: LoadedPolicy,
        project: str,
        server: str,
        entry: McpServer,
        current: ServerClassification,
        drifts: DriftLedger,
    ) -> ClassificationInForce:
        seen = self._drift(drifts, server, current, project)
        summary = _summary(seen, drifts, project, server, current) if seen is not None else None
        tools = [
            _tool(loaded, server, entry, current, name, tool, _pin_state(tool, name, seen))
            for name, tool in sorted(current.tools.items())
        ]
        if seen is not None:
            digests, hints = _strings(seen.data.get("digests")), _strings(seen.data.get("hints"))
            for name in _names(seen.data.get("added")):
                if name not in current.tools:
                    hint = hints.get(name)
                    effect = cast(EffectName, hint) if hint in _EFFECTS else "unclassified"
                    tools.append(
                        ClassifiedTool(
                            name=name,
                            effect=effect,
                            sha256=_hex(digests.get(name)),
                            pin_state="added",
                            mentioned=False,
                            escalates=_escalates(
                                loaded, server, entry, current, name, EffectClass(effect)
                            ),
                        )
                    )
        acceptance = self._acceptance(project, server, current)
        source = ClassificationSource(
            kind="project" if current.revision is not None else "catalog",
            name=entry.classification,
            revision=current.revision,
            pinned=date.fromisoformat(current.pinned) if current.pinned else None,
            accepted_at=acceptance.accepted_at if acceptance else None,
            accepted_by=acceptance.accepted_by if acceptance else None,
            reason=acceptance.reason[:600] if acceptance else None,
        )
        return ClassificationInForce(
            server=server, source=source, digest=current.digest, tools=tools, drift=summary
        )

    def _acceptance(
        self, project: str, server: str, current: ServerClassification
    ) -> Acceptance | None:
        if current.revision is None:
            return None
        try:
            return self.store.acceptance(project, server, current.revision)
        except StoreError as exc:
            raise PermissionStoreError(str(exc)) from exc

    def _draft(
        self,
        loaded: LoadedPolicy,
        server: str,
        entry: McpServer,
        current: ServerClassification | None,
        draft: Draft,
        hints: Mapping[str, EffectClass],
    ) -> ClassificationDraft:
        proposed = draft.classification
        reasons = widening_reasons(loaded.policy, server, current, proposed, hints)
        tools: list[DraftTool] = []
        for name, tool in sorted(proposed.tools.items()):
            was = current.tools.get(name) if current is not None else None
            state: PinState
            if tool.sha256 is None:
                state = "unpinned"
            elif was is None:
                state = "added"
            else:
                state = "current" if was.sha256 == tool.sha256 else "changed"
            shown = _tool(loaded, server, entry, proposed, name, tool, state)
            tools.append(
                DraftTool(
                    **shown.model_dump(),
                    current_effect=was.effect.value if was is not None else None,
                    widening=name in reasons,
                    widening_reasons=reasons.get(name, [])[:16],
                )
            )
        return ClassificationDraft(
            draft=draft.draft,
            server=server,
            created_at=draft.created_at,
            model=draft.model,
            yaml=draft.data.decode("utf-8", "replace"),
            tools=tools,
        )


def runtime_classifications(
    store: PolicyStore, project: str, servers: Sequence[str]
) -> RuntimeClassifications:
    """The project's classification revisions in force for ``servers``, each file exactly
    as stored, for the Kubernetes runtime manager's compile of the agent runtime's fence
    (``GET /v1/runtime-classifications``). A binding or revision that cannot be read is
    reported with why, never as none, so the manager can say it fell back. Blocking."""
    found: list[RuntimeClassification] = []
    for server in servers:
        try:
            revision = store.classification_binding(project, server)
        except StoreError as exc:
            _log.warning(
                "the classification binding of %s/%s cannot be read: %s", project, server, exc
            )
            found.append(
                RuntimeClassification(
                    server=server, error_kind="binding", error="the binding cannot be read"
                )
            )
            continue
        text: str | None = None
        if revision is not None:
            try:
                text = store.classification_revision_bytes(project, server, revision).decode()
            except (StoreError, UnicodeDecodeError) as exc:
                _log.warning(
                    "classification revision %s of %s/%s cannot be read: %s",
                    revision, project, server, exc,
                )  # fmt: skip
                found.append(
                    RuntimeClassification(
                        server=server,
                        error_kind="revision",
                        error=f"the bound revision {revision} cannot be read",
                    )
                )
                continue
        found.append(RuntimeClassification(server=server, revision=revision, classification=text))
    return RuntimeClassifications(project_id=project, servers=found)


def project_policy(environments: Environments, project: str) -> LoadedPolicy | None:
    """The project's policy as the policy store binds it to the project
    (``projects/<project>.json``, the MCP proxy's binding), else the revision granted to it
    last (``policy add --project``): what its classifications and widenings are read under."""
    try:
        revision = PolicyStore(environments.settings.policy_store, "projects").binding(project)
        revision = revision or environments.records.latest_grant(project)
        return environments.store.policy(revision) if revision is not None else None
    except StoreError as exc:
        raise PermissionStoreError(str(exc)) from exc


def _check_tools(
    proposed: ServerClassification,
    draft: ServerClassification,
    current: ServerClassification | None,
    seen: DriftSeen | None,
) -> None:
    """Refuse a tool the server is not known to have, or a pin no listing gave it."""
    known: dict[str, set[str | None]] = {}
    for source in (draft, current):
        tools: Mapping[str, ToolEntry] = source.tools if source is not None else {}
        for name, tool in tools.items():
            known.setdefault(name, {None}).add(tool.sha256)
    if seen is not None:
        digests = _strings(seen.data.get("digests"))
        for name in (*_names(seen.data.get("added")), *_names(seen.data.get("changed"))):
            known.setdefault(name, {None}).add(digests.get(name))
    unknown = sorted(set(proposed.tools) - set(known))
    if unknown:
        raise ClassificationReviewError(
            f"tools the server is not known to have: {', '.join(unknown[:10])}", "invalid"
        )
    repinned = sorted(
        name for name, tool in proposed.tools.items() if tool.sha256 not in known[name]
    )
    if repinned:
        raise ClassificationReviewError(
            f"pins no listing gave: {', '.join(repinned[:10])}", "invalid"
        )


def _tool(
    loaded: LoadedPolicy,
    server: str,
    entry: McpServer,
    classification: ServerClassification,
    name: str,
    tool: ToolEntry,
    state: PinState,
) -> ClassifiedTool:
    escalates = _escalates(loaded, server, entry, classification, name)
    return ClassifiedTool(
        name=name,
        effect=tool.effect.value,
        reason=tool.reason,
        rules=[
            ToolRule(
                argument=rule.argument,
                present=rule.present,
                equals=json.dumps(rule.equals) if rule.equals is not None else None,
                effect=rule.effect.value,
            )
            for rule in tool.rules
        ],
        significant={str(kind): path for kind, path in tool.significant.items()} or None,
        sha256=tool.sha256,
        pin_state=state,
        mentioned=True,
        escalates=escalates,
    )


def _escalates(
    loaded: LoadedPolicy,
    server: str,
    entry: McpServer,
    classification: ServerClassification | None,
    name: str,
    hint: EffectClass | None = None,
) -> bool:
    """Whether a call of the tool without arguments asks first, as decision time says."""
    rank = restrictiveness(loaded.policy, server)
    base = resolve(entry, name, {}, rank, hint=hint, project=classification).effect
    return class_action(loaded.policy, server, base).action is Action.ESCALATE


def _hints(seen: DriftSeen | None) -> dict[str, EffectClass]:
    """The hint classes a drift record gives the tools its listing added."""
    if seen is None:
        return {}
    hints = _strings(seen.data.get("hints"))
    return {name: EffectClass(hint) for name, hint in hints.items() if hint in _EFFECTS}


def _pin_state(tool: ToolEntry, name: str, seen: DriftSeen | None) -> PinState:
    if seen is not None:
        if name in _names(seen.data.get("removed")):
            return "removed"
        if name in _names(seen.data.get("changed")):
            return "changed"
        if name in _names(seen.data.get("added")):
            return "added"
    return "current" if tool.sha256 is not None else "unpinned"


def _summary(
    seen: DriftSeen,
    drifts: DriftLedger,
    project: str,
    server: str,
    current: ServerClassification,
) -> DriftSummary | None:
    pin = seen.pin or pin_digest(current.pins)
    try:
        at = datetime.fromisoformat(seen.time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    newest = _listing(seen, at)
    if newest is None:
        return None
    held = drifts.unaccepted(server, pin_digest(current.pins), project)
    shown = [
        found
        for drift in (held or [seen])[:MAX_DRIFT_LISTINGS]
        if (found := _listing(drift, None)) is not None
    ]
    return DriftSummary(
        at=at,
        pin=pin,
        added=newest.added,
        removed=newest.removed,
        changed=newest.changed,
        held=bool(held),
        drifts=shown,
    )


def _listing(seen: DriftSeen, at: datetime | None) -> DriftListing | None:
    """One drifted listing as the review shows it; None when its record's time does not
    read as one."""
    if at is None:
        try:
            at = datetime.fromisoformat(seen.time.replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is None:
            return None
    digests = _strings(seen.data.get("digests"))
    return DriftListing(
        listing=_hex(seen.listing),
        at=at,
        source="runtime" if seen.source == RUNTIME_SOURCE else "middleware",
        added=[
            DriftTool(name=name, sha256=_hex(digests.get(name)))
            for name in _names(seen.data.get("added"))
        ],
        removed=_names(seen.data.get("removed")),
        changed=[
            DriftTool(name=name, sha256=_hex(digests.get(name)))
            for name in _names(seen.data.get("changed"))
        ],
    )


def _names(value: object) -> list[str]:
    """A record's list of tool names, its malformed entries dropped."""
    if not isinstance(value, list):
        return []
    names = [name for name in cast("list[object]", value) if isinstance(name, str)]
    return [name for name in names if 0 < len(name) <= 256][:MAX_DRIFT_TOOLS]


def _strings(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        return {}
    fields = cast("dict[object, object]", value)
    return {
        key: item for key, item in fields.items() if isinstance(key, str) and isinstance(item, str)
    }


def _hex(value: str | None) -> str | None:
    if value is None or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value
