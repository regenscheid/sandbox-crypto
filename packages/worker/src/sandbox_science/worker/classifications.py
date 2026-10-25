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

The project's policy is the revision the policy store binds the project to, else the one
granted to it last (``policy add --project``). The single workspace user holds the operator
token, so the user is the actor (``user``); the workspace has no roles.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.classifications import (
    MAX_DRIFT_TOOLS,
    ClassificationAccepted,
    ClassificationDraft,
    ClassificationInForce,
    ClassificationSource,
    ClassifiedTool,
    DraftTool,
    DriftAccepted,
    DriftSummary,
    DriftTool,
    EffectName,
    PinState,
    ToolRule,
)
from sandbox_science.policy import Action, LoadedPolicy, PolicyLoadError
from sandbox_science.policy.classification import (
    ServerClassification,
    changed_tools,
    class_action,
    resolve,
    restrictiveness,
    revision_classification,
    server_classification,
    widening_reasons,
)
from sandbox_science.policy.detection import (
    ACCEPTED_RECORD,
    CLASSIFICATION_ACCEPTED,
    DriftLedger,
    DriftSeen,
    pin_digest,
)
from sandbox_science.policy.effects import EffectClass, ToolEntry
from sandbox_science.policy.model import McpServer
from sandbox_science.policy.store import Acceptance, Draft, PolicyStore, StoreError

from .classification_receipts import ClassificationReceipts, ReceiptConflictError
from .environments import Environments
from .permissions import Permissions, PermissionStoreError

_log = logging.getLogger(__name__)

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
    ) -> None:
        self.store = store
        self.permissions = permissions
        self.policies = policies
        self.receipts = receipts
        self.clock = clock

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
        hints = _hints(self._drift(self._drifts(), server, current))
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
        self, project: str, server: str, reason: str, operation_id: str
    ) -> DriftAccepted:
        """Re-accept a server's drifted tool list for the project (CL-4), once per
        ``operation_id``. Blocking."""
        asked = {"accept": "drift", "server": server, "reason": reason}

        def act() -> str:
            return self._accept_drift(project, server, reason, operation_id).model_dump_json()

        return DriftAccepted.model_validate_json(self._once(project, operation_id, asked, act))

    def _once(
        self, project: str, operation_id: str, asked: Mapping[str, str], act: Callable[[], str]
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
        seen = self._drift(drifts, server, current)
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
        self, project: str, server: str, reason: str, operation_id: str
    ) -> DriftAccepted:
        _, entry = self._server(project, server)
        current = self._current(project, server, entry)
        seen = self._drift(self._drifts(), server, current)
        if seen is None:
            raise ClassificationReviewError(f"{server}'s tool list has no drift to accept")
        now = self.clock.now()
        # The acceptance names the pin and the listing it accepts: a later listing that
        # differs is a new drift, held again (D103).
        payload = {
            "project": project,
            "server": server,
            "pin": seen.pin,
            "listing": seen.listing,
            "reason": reason,
            "actor": ACTOR,
            "operation_id": operation_id,
        }
        self.permissions.record_acceptance(ACCEPTED_RECORD, payload)
        return DriftAccepted(accepted_at=now)

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
        drifts: DriftLedger, server: str, current: ServerClassification | None
    ) -> DriftSeen | None:
        """The server's latest drift from the pin in force, if any."""
        if current is None:
            return None
        return drifts.latest(server, pin_digest(current.pins))

    def _view(
        self,
        loaded: LoadedPolicy,
        project: str,
        server: str,
        entry: McpServer,
        current: ServerClassification,
        drifts: DriftLedger,
    ) -> ClassificationInForce:
        seen = self._drift(drifts, server, current)
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
    digests = _strings(seen.data.get("digests"))
    return DriftSummary(
        at=at,
        pin=pin,
        added=[
            DriftTool(name=name, sha256=_hex(digests.get(name)))
            for name in _names(seen.data.get("added"))
        ],
        removed=_names(seen.data.get("removed")),
        changed=[
            DriftTool(name=name, sha256=_hex(digests.get(name)))
            for name in _names(seen.data.get("changed"))
        ],
        held=drifts.drifted(server, pin_digest(current.pins), project) is not None,
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
