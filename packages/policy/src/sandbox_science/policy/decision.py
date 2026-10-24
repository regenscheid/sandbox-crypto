# SPDX-License-Identifier: MIT
"""Policy actions and the decisions the evaluator returns."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal


class Action(StrEnum):
    """What happens to an operation (docs/POLICY.md, "Actions")."""

    ALLOW = "allow"
    ALERT = "alert"
    ESCALATE = "escalate"
    DENY = "deny"

    @property
    def restrictiveness(self) -> int:
        """Rank used for ties: ``deny`` > ``escalate`` > ``alert`` > ``allow``."""
        return _RESTRICTIVENESS[self]


_RESTRICTIVENESS: dict[Action, int] = {
    Action.ALLOW: 0,
    Action.ALERT: 1,
    Action.ESCALATE: 2,
    Action.DENY: 3,
}


def most_restrictive(actions: Iterable[Action]) -> Action:
    """Return the most restrictive of a non-empty collection of actions."""
    return max(actions, key=lambda action: action.restrictiveness)


Layer = Literal["classification", "floor", "ceiling", "policy", "default"]
"""Which layer produced a decision. ``default`` means no project rule matched."""


@dataclass(frozen=True, slots=True)
class EscalationTerms:
    """How long an escalated operation waits, and what happens if nobody answers.

    ``on_timeout`` is ``DENY``, or ``ALERT`` when a read-only rule permits the
    operation to proceed with a recorded finding.
    """

    timeout: timedelta
    on_timeout: Action

    def __post_init__(self) -> None:
        if self.on_timeout not in (Action.DENY, Action.ALERT):
            raise ValueError("on_timeout must be DENY or ALERT")


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of evaluating one operation."""

    action: Action
    layer: Layer
    reason: str
    rule: str | None = None
    policy_digest: str | None = None
    escalation: EscalationTerms | None = None
    effect: str | None = None
    """For an MCP tool call, the effect class it resolved to (D96)."""
    max_grant: str | None = None
    """For an escalated MCP tool call, the widest grant a user may give (D97)."""

    def __post_init__(self) -> None:
        if (self.action is Action.ESCALATE) != (self.escalation is not None):
            raise ValueError("escalation terms accompany exactly the ESCALATE decisions")


@dataclass(frozen=True, slots=True)
class Alert:
    """A monitor finding. Monitors never change access decisions."""

    rule: str
    reason: str
