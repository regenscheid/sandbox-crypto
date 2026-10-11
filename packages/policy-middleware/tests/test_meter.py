# SPDX-License-Identifier: MIT
"""The per-sandbox egress meter behind the policy monitors."""

from sandbox_science.policy_middleware.meter import EgressMeter


def test_counts_cover_the_last_hour_per_sandbox() -> None:
    now = [0.0]
    meter = EgressMeter(clock=lambda: now[0])
    assert meter.add("a", 100) == 100
    now[0] = 30 * 60
    assert meter.add("a", 50) == 150
    assert meter.add("b", 7) == 7
    now[0] = 60 * 60
    # The first minute's bucket has left the window; the half-hour one has not.
    assert meter.add("a", 1) == 51
    now[0] = 2 * 60 * 60
    assert meter.add("a", 0) == 0


def test_requests_in_the_same_minute_share_a_bucket() -> None:
    now = [0.0]
    meter = EgressMeter(clock=lambda: now[0])
    for second in range(60):
        now[0] = float(second)
        meter.add("a", 1)
    assert meter.add("a", 0) == 60
