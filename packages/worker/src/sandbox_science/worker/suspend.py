# SPDX-License-Identifier: MIT
"""Idle suspension (W3): stop the sandboxes of environments nobody is using.

A project's environments persist, but need not keep running (docs/PROJECTS-AND-
SESSIONS.md, "Provisioning and persistence"). An environment is idle when the worker
has run nothing in it, and been asked nothing of it, for ``suspend_after`` seconds,
and its kernels have used almost no CPU meanwhile, so that a computation left running
in a kernel thread keeps it up. The suspender then stops its executor and its sandbox
(``Environments.suspend``). OpenShell keeps a stopped sandbox's writable disk, so
files and installed tools survive; kernels do not, and the next cell reports that its
earlier state is gone. The next use starts the sandbox again.

Idle time counts from the environment's last change as well (created, resumed,
revised, given admitted hosts), so a new or just-resumed environment gets its full
``suspend_after``. Requests in flight (files, publication, kernel restarts) count as
use. The CPU check misses work that waits (a download, a remote job being polled)
and children reaped automatically under ``SIGCHLD`` ignored; such work ends with the
suspension, and the next cell reports the lost state.

The executor reports the CPU time, and research code may take the executor over. It
can keep its own environment running, or let it be suspended: each sample has a time
limit and environments are sampled concurrently, so a stopped or lying executor
holds up no other environment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo

from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager
from .timeouts import seconds

_log = logging.getLogger(__name__)

BUSY_SHARE: Final = 0.02
"""CPU seconds per second, summed over an environment's kernels, that count as use."""
CONCURRENCY: Final = 8
"""Environments sampled at once."""


class Suspender:
    """Stops idle environments' sandboxes, checking every ``interval`` seconds."""

    def __init__(
        self,
        environments: Environments,
        manager: ExecutionManager,
        after: float,
        *,
        interval: float | None = None,
        sample_timeout: float = 15.0,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.environments = environments
        self.manager = manager
        self.after = after
        self.interval = seconds(
            "interval", interval if interval is not None else max(5.0, min(60.0, after / 4))
        )
        self.sample_timeout = seconds("sample_timeout", sample_timeout)
        """How long one environment's sample may take."""
        self.clock = clock
        """The manager's clock, which its activity reports use too."""
        self.started = clock.monotonic()
        self._samples: dict[str, tuple[float, float, str | None]] = {}
        """Environment to the last (time, CPU seconds, executor) its executor reported."""
        self._busy: dict[str, float] = {}
        """Environment to when it was last seen in use or computing."""

    async def run(self) -> None:
        """Check every ready environment until cancelled."""
        while True:
            await self.clock.sleep(self.interval)
            try:
                await self.check()
            except Exception:
                # Never let one bad round end suspension for good.
                _log.exception("idle suspension check failed")

    async def check(self) -> list[str]:
        """Suspend the environments idle for ``after`` seconds; return their IDs.

        Environments are sampled concurrently, each within ``sample_timeout``, so that
        an executor that stopped answering holds up no other environment.
        """
        ready = self.environments.records.ready()
        names = {info.environment_id for info in ready}
        self._samples = {key: value for key, value in self._samples.items() if key in names}
        self._busy = {key: value for key, value in self._busy.items() if key in names}
        limit = asyncio.Semaphore(CONCURRENCY)

        async def idle(info: EnvironmentInfo) -> bool:
            async with limit:
                try:
                    return await asyncio.wait_for(self._idle(info), self.sample_timeout)
                except TimeoutError:
                    return False
                except Exception:
                    _log.exception("could not sample environment %s", info.environment_id)
                    return False

        verdicts = await asyncio.gather(*(idle(info) for info in ready))
        suspended: list[str] = []
        for info, verdict in zip(ready, verdicts, strict=True):
            environment_id = info.environment_id
            if not verdict:
                continue
            try:
                if not await self.environments.suspend(
                    environment_id, self.clock.monotonic() - self.after
                ):
                    continue
            except WorkerError as error:
                # Its executor is gone, but it stays ready; the next use starts another.
                _log.warning("could not suspend environment %s: %s", environment_id, error.message)
                continue
            except Exception:
                # Logged in full; the other environments still get their turn.
                _log.exception("could not suspend environment %s", environment_id)
                continue
            suspended.append(environment_id)
            self._samples.pop(environment_id, None)
            self._busy.pop(environment_id, None)
        return suspended

    async def _idle(self, info: EnvironmentInfo) -> bool:
        environment_id = info.environment_id
        activity = await self.manager.activity(environment_id)
        now = self.clock.monotonic()
        if activity is None:
            return False  # its executor could not say what it is doing
        if activity.running:
            self._busy[environment_id] = now
            return False
        if activity.cpu_seconds is None:
            self._samples.pop(environment_id, None)
        else:
            previous = self._samples.get(environment_id)
            self._samples[environment_id] = (now, activity.cpu_seconds, activity.executor)
            if (
                previous is None
                or previous[2] != activity.executor
                or activity.cpu_seconds < previous[1]
            ):
                return False  # a first sample, a new executor or a counter that went down
            elapsed = max(now - previous[0], 1e-6)
            if activity.cpu_seconds - previous[1] > BUSY_SHARE * elapsed:
                self._busy[environment_id] = now
                return False
        # Idle time counts from the environment's last change too: created, resumed,
        # revised or given admitted hosts.
        age = (self.clock.now() - info.updated_at).total_seconds()
        last = max(
            self.started,
            now - max(age, 0.0),
            activity.last_used or 0.0,
            self._busy.get(environment_id, 0.0),
        )
        return now - last >= self.after
