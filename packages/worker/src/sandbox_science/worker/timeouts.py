# SPDX-License-Identifier: MIT
"""The execution manager's grace periods, deadlines and polling intervals.

The defaults are the production values. Tests pass shorter ones instead of
patching module constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ManagerTimeouts:
    """Seconds the execution manager waits; see each field."""

    watchdog_grace: float = 30.0
    """Past an execution's own limit before the worker cancels it."""
    lost_grace: float = 60.0
    """After that cancel, before an execution that never reported a result is lost."""
    cancel_grace: float = 30.0
    """For an executor to confirm a cancel before its execution is lost."""
    retain_live: float = 600.0
    """An ended execution stays in memory this long; later reads come from the store."""
    kernel_observation: float = 2.0
    """The whole budget of a passive kernel observation, the transport write lock included."""
    lockfile: float = 120.0
    """For the executor to send an environment's lockfile."""
    shared: float = 120.0
    """For syncing the project's shared files before or after an execution (D79)."""
    poll_interval: float = 0.1
    """Between replays of a daemon's running executions."""
    poll_retry_max: float = 10.0
    """The longest wait between replays while they keep failing."""
    recovery_retry: float = 0.2
    """The first wait before retrying a daemon recovery; it doubles each time."""
    recovery_retry_max: float = 30.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not value > 0:
                raise ValueError(f"{name} must be a positive number of seconds")


MANAGER_TIMEOUTS: Final = ManagerTimeouts()
"""The production values."""
