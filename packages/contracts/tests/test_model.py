# SPDX-License-Identifier: MIT
"""The shared primitives: one counter bound, one page rule, strict and aware values."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.model import MAX_COUNTER, Counter, StrictContract, check_page
from sandbox_science.contracts.provision_observations import ProvisionObservation


class Numbers(StrictContract):
    count: Counter


@pytest.mark.parametrize(
    ("offset", "count", "limit", "total", "next_offset"),
    [
        (0, 0, 8, 0, None),  # an empty result
        (0, 8, 8, 20, 8),
        (16, 4, 8, 20, None),  # the last page
        (20, 0, 8, 20, None),  # past the last row, at the end
    ],
)
def test_pages_that_bind_their_range(
    offset: int, count: int, limit: int, total: int, next_offset: int | None
) -> None:
    check_page(offset, count, limit, total, next_offset)


@pytest.mark.parametrize(
    ("offset", "count", "limit", "total", "next_offset", "message"),
    [
        (21, 0, 8, 20, None, "exact range"),  # offset past the total
        (0, 9, 8, 20, 9, "exact range"),  # more rows than the limit
        (16, 8, 8, 20, None, "exact range"),  # rows past the total
        (0, 8, 8, 20, None, "exact range"),  # a missing continuation
        (16, 4, 8, 20, 20, "exact range"),  # a continuation after the end
        (0, 4, 8, 20, 8, "exact range"),  # a continuation that skips rows
        (4, 0, 8, 20, 4, "make progress"),  # an empty page before the end
    ],
)
def test_pages_that_do_not(
    offset: int, count: int, limit: int, total: int, next_offset: int | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check_page(offset, count, limit, total, next_offset)


def test_one_counter_bound_and_no_coercion() -> None:
    assert Numbers(count=MAX_COUNTER).count == MAX_COUNTER
    for value in (MAX_COUNTER + 1, -1, "1", True):
        with pytest.raises(ValidationError):
            Numbers.model_validate({"count": value})


def test_observation_times_need_a_timezone() -> None:
    fields = {
        "project_id": "p",
        "environment_id": "e",
        "gate": {
            "generation": 0,
            "mode": "ready",
            "operation": None,
            "revision": None,
            "recorded": False,
        },
        "current_revision": None,
        "queue_total": 0,
        "operations": (),
    }
    aware = ProvisionObservation.model_validate({**fields, "observed_at": datetime.now(UTC)})
    assert aware.observed_at.tzinfo is not None
    with pytest.raises(ValidationError):
        ProvisionObservation.model_validate({**fields, "observed_at": datetime(2026, 10, 14)})
