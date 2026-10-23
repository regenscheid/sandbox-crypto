# SPDX-License-Identifier: MIT
"""Model dispatch intents retain unknown paid-call outcomes across process loss."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, new_id
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.context.history import capture
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.model import (
    ChatModel,
    GatewayBudgetError,
    GatewayModel,
    Message,
    ModelTurn,
    ToolSpec,
)
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole
from sandbox_science.orchestrator.projects import ModelUsage

from .records import Ownership, check_owner
from .reservations import UNRESERVED


class ModelIntents:
    def __init__(self, control: ControlStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control, self.clock = control, clock

    def begin(
        self,
        owner: Ownership,
        *,
        kind: str,
        alias: str,
        profile: ModelProfile | None = None,
        role: ModelRole | None = None,
        captured: capture.CapturedRequest | None = None,
    ) -> str:
        """Commit before contacting the gateway; callers cannot provide another scope."""
        if kind not in {"main", "delegated", "reviewer", "watcher", "fold", "skills", "embedding"}:
            raise ValueError("unknown model intent kind")
        if not alias or len(alias) > 200:
            raise ValueError("invalid model alias")
        from sandbox_science.orchestrator.runstate import RunState

        if RunState(self.control).over_cost_limit(owner.project, self.clock.now()):
            raise ControlError(CONFLICT, "model_budget: project budget is exhausted")
        identity = new_id("model")
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            if (
                tx.sql.execute(
                    "SELECT 1 FROM research_work WHERE project=? AND session=? "
                    "AND work=? AND state='running'",
                    (owner.project, owner.session, owner.work),
                ).fetchone()
                is None
            ):
                raise ControlError(CONFLICT, "work is not running; no paid dispatch is admitted")
            tx.sql.execute(
                "INSERT INTO research_model_calls VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    owner.project,
                    owner.session,
                    owner.run,
                    identity,
                    kind,
                    alias,
                    self.clock.now().timestamp(),
                ),
            )
            if captured is not None:
                capture.save(tx.sql, (owner.project, owner.session, owner.run), identity, captured)
            if profile is not None:
                if profile.alias != alias:
                    raise ValueError("model intent profile alias differs")
                (last,) = tx.sql.execute(
                    "SELECT coalesce(max(seq),0) FROM run_events "
                    "WHERE project_id=? AND session_id=? AND run=?",
                    (owner.project, owner.session, owner.run),
                ).fetchone()
                tx.sql.execute(
                    "INSERT INTO run_events VALUES (?,?,?,?,?,'notice',?)",
                    (
                        owner.project,
                        owner.session,
                        owner.run,
                        last + 1,
                        self.clock.now().isoformat(),
                        json.dumps(
                            {
                                "source": "model_profile",
                                "identity": identity,
                                "kind": kind,
                                "role": role,
                                "profile": profile.model_dump(mode="json"),
                            }
                        ),
                    ),
                )
        return identity

    def observe(self, owner: Ownership, identity: str, usage: ModelUsage) -> None:
        """Auxiliary calls: persist observed usage before returning it to their caller."""
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            observed(tx.sql, owner.project, owner.session, owner.run, identity, usage)

    def pending(self, project: str, session: str, run: str, *, after: str = "") -> tuple[str, ...]:
        with self.control.read() as tx:
            return tuple(
                row[0]
                for row in tx.sql.execute(
                    "SELECT m.identity FROM research_model_calls m "  # noqa: S608 - fixed fragment
                    "WHERE m.project=? AND m.session=? "
                    "AND m.run=? AND m.identity>? AND m.usage IS NULL "
                    f"AND {UNRESERVED} ORDER BY m.identity LIMIT 100",
                    (project, session, run, after),
                )
            )


def observed(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    identity: str,
    usage: ModelUsage,
) -> None:
    """Join the assistant/usage archive transaction for a main or child response."""
    encoded = usage.model_dump_json()
    if len(encoded.encode()) > 16384:
        raise ValueError("model observation exceeds its 16 KiB bound")
    changed = db.execute(
        "UPDATE research_model_calls SET usage=? WHERE project=? AND session=? AND run=? "
        "AND identity=? AND usage IS NULL",
        (encoded, project, session, run, identity),
    ).rowcount
    if changed != 1:
        raise ControlError(CONFLICT, "model response has no pending intent in this attempt")


class IntentModel:
    """The local harness path; the confined runtime uses the same journal via its capability."""

    def __init__(
        self,
        model: ChatModel,
        intents: ModelIntents,
        owner: Ownership,
        alias: str,
        *,
        kind: str = "main",
    ) -> None:
        self.model, self.intents, self.owner, self.alias = model, intents, owner, alias
        self.kind = kind

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        if isinstance(self.model, GatewayModel):
            self.model.validate_profile()
        try:
            identity = await threaded(
                self.intents.begin,
                self.owner,
                kind=self.kind,
                alias=self.alias,
                profile=getattr(self.model, "requested_profile", None),
                role=getattr(self.model, "_model_role", None),
                captured=capture.request(messages, tools),
            )
        except ControlError as exc:
            if exc.message.startswith("model_budget:"):
                raise GatewayBudgetError(exc.message) from None
            raise
        reply = await self.model.complete(messages, tools)
        return replace(reply, intent_id=identity)
