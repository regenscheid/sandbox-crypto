# SPDX-License-Identifier: MIT
"""The transient-failure classifier and the automatic retry policy (D100)."""

from __future__ import annotations

import sqlite3

import aiohttp
import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.confinement.transport import TransportError
from sandbox_science.orchestrator.continuity.transient import (
    Prior,
    RetryPolicy,
    classify,
    decide,
)
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.model import (
    ContextLimitError,
    GatewayBudgetError,
    IncompleteStreamError,
    ModelError,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerProtocolError


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ModelError("the model gateway returned 524: <html>", status=524), "gateway_timeout"),
        (ModelError("the model gateway returned 504: gateway", status=504), "gateway_timeout"),
        (ModelError("the model gateway returned 502: bad", status=502), "gateway_error"),
        (ModelError("the model gateway returned 500: oops", status=500), "gateway_error"),
        (ModelError("the model gateway returned 429: slow down", status=429), "gateway_error"),
        (ModelError("cannot reach the model gateway: ClientOSError: reset"), "network"),
        (ModelError("the model gateway gave no answer within 960 s"), "model_timeout"),
        (
            IncompleteStreamError(
                "the model gateway's Responses stream ended without a final response"
            ),
            "incomplete_stream",
        ),
        # The confined runtime keeps only the message of a model error.
        (
            ModelError("the model gateway's Responses stream ended without a final response"),
            "incomplete_stream",
        ),
        (
            ModelError("the model gateway's response body was incomplete; its charge is unknown"),
            "incomplete_stream",
        ),
        (ModelError("confined model runtime unavailable"), "bridge_loss"),
        (UnknownEffectError("worker_unknown_effect", "the shell operation..."), "worker_loss"),
        (WorkerAPIError(ErrorKind.UNAVAILABLE, "cannot reach the worker"), "worker_loss"),
        (TransportError("unavailable", "control connection closed"), "bridge_loss"),
        (TransportError("unavailable", "worker_unknown_effect: the shell..."), "worker_loss"),
        (aiohttp.ServerDisconnectedError(), "network"),
        (ConnectionResetError(), "network"),
        (TimeoutError(), "network"),
        (sqlite3.OperationalError("database is locked"), "persistence"),
    ],
)
def test_transient_failures_are_classified(error: BaseException, kind: str) -> None:
    assert classify(error) == kind


@pytest.mark.parametrize(
    "error",
    [
        GatewayBudgetError("budget exceeded", status=429),
        ContextLimitError("context window exceeded", status=400),
        ModelError("the model gateway returned 400: bad request", status=400),
        ModelError("the model gateway returned 401: no key", status=401),
        ModelError("pinned model alias/API differs from the configured caller"),
        ModelError("unexpected completion: {}"),
        UnknownEffectError("mcp_unknown_effect", "remote write outcome unknown"),
        WorkerAPIError(ErrorKind.POLICY_DENIED, "denied"),
        WorkerAPIError(ErrorKind.CONFLICT, "lost_operation"),
        WorkerProtocolError(ErrorKind.UNSUPPORTED, "protocol 8"),
        TransportError("denied", "runtime capability was revoked"),
        sqlite3.IntegrityError("constraint failed"),
        ValueError("anything else"),
        RuntimeError("anything else"),
    ],
)
def test_permanent_failures_are_not_transient(error: BaseException) -> None:
    assert classify(error) is None


def test_a_cause_is_classified_but_a_refusal_anywhere_wins() -> None:
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = ConnectionResetError("reset")
    assert classify(wrapped) == "network"
    refusal = GatewayBudgetError("budget exceeded", status=400)
    outer = RuntimeError("wrapped")
    outer.__cause__ = refusal
    assert classify(outer) is None


def test_backoff_increases_and_is_capped() -> None:
    policy = RetryPolicy(base_seconds=30, cap_seconds=900)
    assert [policy.delay(streak) for streak in range(1, 8)] == [30, 30, 60, 120, 240, 480, 900]


def test_first_retry_is_immediate_then_waits_for_its_backoff() -> None:
    policy = RetryPolicy()
    first = decide(policy, None, (0, 1), 1000.0)
    assert (first.action, first.streak, first.attempts) == ("retry", 1, 1)
    prior = Prior(observed=1000.0, progress=(0, 1), streak=1, attempts=1)
    assert decide(policy, prior, (0, 1), 1010.0).action == "wait"
    later = decide(policy, prior, (0, 1), 1030.0)
    assert (later.action, later.streak, later.attempts) == ("retry", 2, 2)


def test_retries_without_progress_stop_with_a_reason() -> None:
    policy = RetryPolicy(max_no_progress=3)
    prior = Prior(observed=0.0, progress=(1, 2, 3, 4), streak=3, attempts=3)
    stopped = decide(policy, prior, (1, 2, 3, 4), 10_000.0)
    assert stopped.action == "stop"
    assert stopped.reason == "no progress across 3 automatic retries"


def test_progress_resets_the_streak_but_attempts_stay_bounded() -> None:
    policy = RetryPolicy(max_attempts=4, max_no_progress=3)
    prior = Prior(observed=0.0, progress=(1,), streak=3, attempts=3)
    progressed = decide(policy, prior, (2,), 60.0)
    assert (progressed.action, progressed.streak, progressed.attempts) == ("retry", 1, 4)
    prior = Prior(observed=60.0, progress=(2,), streak=1, attempts=4)
    bounded = decide(policy, prior, (3,), 120.0)
    assert bounded.action == "stop"
    assert bounded.reason is not None
    assert "4 automatic retries" in bounded.reason


def test_a_failure_long_after_the_last_retry_starts_a_new_episode() -> None:
    policy = RetryPolicy(max_attempts=4, episode_seconds=3600)
    prior = Prior(observed=0.0, progress=(2,), streak=1, attempts=4)
    fresh = decide(policy, prior, (3,), 7200.0)
    assert (fresh.action, fresh.attempts) == ("retry", 1)
