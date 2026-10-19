# SPDX-License-Identifier: MIT
"""Bounded automatic agent replacement, preserving the original research sandbox."""

from __future__ import annotations

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
    """

    def __init__(
        self,
        config: LauncherConfig,
        layout: Layout,
        supervisor: Supervisor,
        *,
        clock: Clock = SYSTEM_CLOCK,
        poll: float = 1,
        steady: float = 60,
        delays: tuple[float, ...] = (2, 4, 8, 16, 32, 60),
    ) -> None:
        self.config, self.layout, self.supervisor = config, layout, supervisor
        self.clock, self.poll, self.steady, self.delays = clock, poll, steady, delays
        self.path = layout.root / "orchestrator" / "infrastructure-recovery.json"
        self.enabled = bool(config.orchestrator and config.orchestrator.recover_infrastructure)
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
        healthy_pid: int | None = None
        healthy_since = self.clock.monotonic()
        while not self.supervisor.stop_requested.is_set():
            agent = next(
                (p for p in self.supervisor.running if p.part.name == "agent-runtime"), None
            )
            if self.enabled and not self.state.blocked:
                if agent is not None and agent.process.returncode is None:
                    if healthy_pid != agent.process.pid:
                        healthy_pid, healthy_since = agent.process.pid, self.clock.monotonic()
                    if (
                        self.state.attempts
                        and self.clock.monotonic() - healthy_since >= self.steady
                    ):
                        self.save(RecoveryState())
                elif agent is not None or self.state.attempts:
                    if self.clock.now().timestamp() >= self.state.retry_after:
                        return
            if await wait_or_sleep(self.clock, self.supervisor.stop_requested, self.poll):
                return

    async def replace(self) -> None:
        """Reconcile only the owned agent VM; never issue a research tool call."""
        if not self.enabled or self.state.blocked or self.supervisor.stop_requested.is_set():
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
            await orchestrator.replace(self.config, self.layout, self.supervisor)
        except (LauncherError, OSError, ValueError) as exc:
            await self.supervisor.stop_part("agent-runtime")
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
