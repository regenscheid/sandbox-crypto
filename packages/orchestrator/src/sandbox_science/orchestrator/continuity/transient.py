# SPDX-License-Identifier: MIT
"""Which failures a later attempt can get past, and how long to wait before one (D100).

The classifier reads an exception's type, HTTP status and the fixed messages the model
client and the confined runtime raise; it never reads model or tool output. A gateway
budget refusal, a context limit, a policy denial or any unrecognized error is not
transient, so automatic recovery stops on it with a durable reason. The retry policy is
pure: the caller persists each decision it returns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, Literal

import aiohttp

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.confinement.transport import TransportError
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.model import (
    RETRIED,
    ContextLimitError,
    GatewayBudgetError,
    IncompleteStreamError,
    ModelError,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerProtocolError

Transient = Literal[
    "network",
    "gateway_error",
    "gateway_timeout",
    "model_timeout",
    "incomplete_stream",
    "worker_loss",
    "bridge_loss",
    "persistence",
    "model",
]
"""``model`` is a legacy model-phase failure saved before failures were classified."""

TRANSIENT: Final[frozenset[str]] = frozenset(
    {
        "network",
        "gateway_error",
        "gateway_timeout",
        "model_timeout",
        "incomplete_stream",
        "worker_loss",
        "bridge_loss",
        "persistence",
        "model",
    }
)
REASON_PREFIX: Final = "transient:"
"""A work's reason when a Resume branch ended on a transient failure, before its detail."""
_GATEWAY_TIMEOUTS: Final = frozenset({504, 524})
_MESSAGES: Final[tuple[tuple[str, Transient], ...]] = (
    ("confined model runtime unavailable", "bridge_loss"),
    ("cannot reach the model gateway", "network"),
    ("gave no answer within", "model_timeout"),
    ("stream ended without a final response", "incomplete_stream"),
    ("response body was incomplete", "incomplete_stream"),
    ("did not stream its responses answer", "incomplete_stream"),
)
"""Fixed texts of ``model.py`` and ``confinement/models.py``, which survive the confined
runtime's wire (its error reply keeps the message and status, not the type)."""


def _one(error: BaseException) -> Transient | None:
    if isinstance(error, GatewayBudgetError | ContextLimitError | WorkerProtocolError):
        return None
    if isinstance(error, IncompleteStreamError):
        return "incomplete_stream"
    if isinstance(error, UnknownEffectError):
        return "worker_loss" if error.kind == "worker_unknown_effect" else None
    if isinstance(error, WorkerAPIError):
        return "worker_loss" if error.kind is ErrorKind.UNAVAILABLE else None
    if isinstance(error, TransportError):
        if error.kind != "unavailable":
            return None
        # The host's reply for a worker lost mid-operation names the adapter's kind.
        return "worker_loss" if str(error).startswith("worker_unknown_effect") else "bridge_loss"
    if isinstance(error, ModelError):
        text = str(error).lower()
        for marker, kind in _MESSAGES:
            if marker in text:
                return kind
        status = error.status
        if status is not None and status in _GATEWAY_TIMEOUTS:
            return "gateway_timeout"
        if status is not None and (status in RETRIED or status >= 500):
            return "gateway_error"
        return None
    if isinstance(error, sqlite3.OperationalError):
        return "persistence"  # a locked or busy store; integrity errors are not
    if isinstance(error, aiohttp.ClientConnectionError | ConnectionError | TimeoutError):
        return "network"
    return None


def classify(error: BaseException) -> Transient | None:
    """The transient kind of ``error`` or of one of its causes; ``None`` when permanent.

    A permanent error anywhere first in the chain wins: a budget refusal wrapped in a
    connection error is still a refusal.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if isinstance(current, GatewayBudgetError | ContextLimitError | WorkerProtocolError):
            return None
        if (kind := _one(current)) is not None:
            return kind
        current = current.__cause__ or current.__context__
    return None


@dataclass(frozen=True)
class RetryPolicy:
    """Increasing backoff, a bounded attempt count and a no-progress stop.

    ``attempts`` counts consecutive automatic attempts in one episode: a failure more than
    ``episode_seconds`` after the previous automatic attempt starts a new episode. The
    ``streak`` counts consecutive attempts that recorded no progress (no new answer,
    evidence, completed work or plan change).
    """

    base_seconds: float = 30.0
    cap_seconds: float = 900.0
    max_attempts: int = 10
    max_no_progress: int = 3
    episode_seconds: float = 3600.0

    def delay(self, streak: int) -> float:
        """The wait after the previous attempt: 30, 30, 60, 120 ... s, capped."""
        return min(self.base_seconds * 2 ** max(streak - 2, 0), self.cap_seconds)


@dataclass(frozen=True)
class Prior:
    """The last automatic attempt's persisted decision."""

    observed: float
    progress: tuple[int, ...]
    streak: int
    attempts: int


@dataclass(frozen=True)
class Decision:
    """``retry`` now, ``wait`` for the backoff, or ``stop`` with ``reason``."""

    action: Literal["retry", "wait", "stop"]
    streak: int
    attempts: int
    delay: float
    reason: str | None = None


def decide(
    policy: RetryPolicy, prior: Prior | None, progress: tuple[int, ...], now: float
) -> Decision:
    """The next automatic step for a tree whose failure was classified transient."""
    if prior is None:
        return Decision("retry", 1, 1, 0.0)
    streak = prior.streak + 1 if tuple(prior.progress) == progress else 1
    fresh = now - prior.observed > policy.episode_seconds
    attempts = 1 if fresh else prior.attempts + 1
    delay = policy.delay(streak)
    if streak > policy.max_no_progress:
        return Decision(
            "stop",
            streak,
            attempts,
            delay,
            f"no progress across {policy.max_no_progress} automatic retries",
        )
    if attempts > policy.max_attempts:
        return Decision(
            "stop",
            streak,
            attempts,
            delay,
            f"{policy.max_attempts} automatic retries within {policy.episode_seconds:.0f} s",
        )
    if now - prior.observed < delay:
        return Decision("wait", streak, attempts, delay)
    return Decision("retry", streak, attempts, delay)
