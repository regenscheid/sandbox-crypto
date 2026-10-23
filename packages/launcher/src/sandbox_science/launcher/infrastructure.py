# SPDX-License-Identifier: MIT
"""Bounded automatic agent replacement, preserving the original research sandbox."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep
from sandbox_science.contracts.model import Contract

from . import orchestrator
from .config import LauncherConfig, LauncherError
from .state import Layout, write_private
from .supervise import Supervisor


class RecoveryState(Contract):
    attempts: int = Field(default=0, ge=0, le=10)
    retry_after: float = Field(default=0, ge=0)
    pending: bool = False
    blocked: bool = False
    error: str | None = Field(default=None, max_length=512)


class InfrastructureRecovery:
    """Persist each replacement decision before effect, with a ten-attempt bound.

    An interrupted replacement has an unknown outcome and is never silently
    repeated after launcher restart. Explicit launcher startup reconciles its
    recorded VM; this journal remains available for operator diagnosis.

    ``alive`` says whether the runtime its owner holds is running (True), has ended
    (False) or is not held at all (None); ``replace`` is the owner's replacement, which
    leaves no runtime held when it fails; ``stopping`` is set when the owner stops.
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool,
        alive: Callable[[], bool | None],
        replace: Callable[[], Awaitable[None]],
        stopping: asyncio.Event,
        clock: Clock = SYSTEM_CLOCK,
        poll: float = 1,
        steady: float = 60,
        delays: tuple[float, ...] = (2, 4, 8, 16, 32, 60),
    ) -> None:
        self.path, self.enabled, self.alive = path, enabled, alive
        self.replacement, self.stopping = replace, stopping
        self.clock, self.poll, self.steady, self.delays = clock, poll, steady, delays
        self.state = RecoveryState()
        if self.path.exists():
            if self.path.stat().st_size > 4096:
                raise LauncherError("infrastructure recovery journal exceeds its byte bound")
            self.state = RecoveryState.model_validate_json(self.path.read_bytes())
            if self.state.pending:
                self.save(
                    self.state.model_copy(
                        update={
                            "blocked": True,
                            "error": "prior replacement outcome is unknown",
                        }
                    )
                )

    def save(self, state: RecoveryState) -> None:
        self.state = state
        write_private(self.path, state.model_dump_json() + "\n")

    async def wait(self) -> None:
        """Return only when a dead agent is eligible for the next audited attempt."""
        # The runtime is never restarted under this wait, only replaced after it
        # returns, so its steady time runs from when it is first seen alive.
        was_alive = False
        healthy_since = self.clock.monotonic()
        while not self.stopping.is_set():
            alive = self.alive()
            if self.enabled and not self.state.blocked:
                if alive:
                    if not was_alive:
                        healthy_since = self.clock.monotonic()
                    if (
                        self.state.attempts
                        and self.clock.monotonic() - healthy_since >= self.steady
                    ):
                        self.save(RecoveryState())
                elif alive is not None or self.state.attempts:
                    if self.clock.now().timestamp() >= self.state.retry_after:
                        return
            was_alive = alive is True
            if await wait_or_sleep(self.clock, self.stopping, self.poll):
                return

    async def replace(self) -> None:
        """Reconcile only the owned agent VM; never issue a research tool call."""
        if not self.enabled or self.state.blocked or self.stopping.is_set():
            return
        attempts = self.state.attempts + 1
        if attempts > 10:
            self.save(self.state.model_copy(update={"blocked": True}))
            return
        delay = self.delays[min(attempts, len(self.delays)) - 1]
        self.save(
            RecoveryState(
                attempts=attempts, pending=True, retry_after=self.clock.now().timestamp() + delay
            )
        )
        print(
            f"automatic agent replacement attempt {attempts}/10; research VM retained", flush=True
        )
        error: str | None = None
        try:
            await self.replacement()
        except (LauncherError, OSError, ValueError) as exc:
            error = str(exc)[:512]
            print("automatic agent replacement failed: " + error, flush=True)
        self.save(
            RecoveryState(
                attempts=attempts,
                retry_after=self.clock.now().timestamp() + delay,
                blocked=error is not None and attempts == 10,
                error=error,
            )
        )


def launcher_recovery(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, *, clock: Clock = SYSTEM_CLOCK
) -> InfrastructureRecovery:
    """Recovery of the host launcher's ``agent-runtime`` part, journaled in its state."""

    def alive() -> bool | None:
        agent = next((p for p in supervisor.running if p.part.name == "agent-runtime"), None)
        return None if agent is None else agent.process.returncode is None

    async def replace() -> None:
        try:
            await orchestrator.replace(config, layout, supervisor)
        except (LauncherError, OSError, ValueError):
            await supervisor.stop_part("agent-runtime")
            raise

    return InfrastructureRecovery(
        layout.root / "orchestrator" / "infrastructure-recovery.json",
        enabled=bool(config.orchestrator and config.orchestrator.recover_infrastructure),
        alive=alive,
        replace=replace,
        stopping=supervisor.stop_requested,
        clock=clock,
    )
