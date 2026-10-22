# SPDX-License-Identifier: MIT
"""Ownership of executor work, kernel retirement and process cleanup (stdlib only).

Protocol validation and result assembly stay with Executor. This owner receives
resource operations, never the coordinator or a mutable backreference to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .executor import Clock, Kernel, Running
    from .sandbox_processes import Leader, Process


async def owned[T](work: Awaitable[T], cancelled: Callable[[], None] | None = None) -> T:
    """Retain a launch or cleanup until completion through repeated cancellation."""
    task = asyncio.ensure_future(work)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if cancelled is not None:
                cancelled()
    return task.result()


class Lifecycle:
    """One connection's accepted work, kernels and owned control operations."""

    def __init__(
        self,
        clock: Clock,
        *,
        make_kernel: Callable[[str, str, str | None], Kernel],
        error: Callable[[str, str], Exception],
        signal: Callable[[str, Leader | None], Awaitable[None]],
        leftovers: Callable[[str, Leader | None], tuple[list[Process], bool]],
        collect: Callable[[str, Leader | None], Awaitable[int]],
        end_descendants: Callable[[], Awaitable[None]],
        close_resources: Callable[[], Awaitable[None]],
        languages: tuple[str, ...],
        max_run_kernels: int,
        max_ended_runs: int,
        term_grace: float,
        package_grace: float,
    ) -> None:
        self.clock = clock
        self.make_kernel = make_kernel
        self.error = error
        self.signal = signal
        self.leftovers = leftovers
        self.collect = collect
        self.end_descendants = end_descendants
        self.close_resources = close_resources
        self.languages = languages
        self.max_run_kernels = max_run_kernels
        self.max_ended_runs = max_ended_runs
        self.term_grace = term_grace
        self.package_grace = package_grace
        self.running: dict[str, Running] = {}
        self.kernels: dict[tuple[str, str, str], Kernel] = {}
        self.ended_runs: dict[str, dict[str, None]] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.controls: set[asyncio.Task[Any]] = set()
        self.reaper: asyncio.Task[None] | None = None
        self.closing = False

    def accept(self, running: Running) -> None:
        """Register work before its acceptance reply can be sent."""
        self.running[running.execution] = running

    def finish(self, execution: str) -> None:
        self.running.pop(execution, None)

    def request_cancel(self, execution: str) -> Running | None:
        """Mark accepted work before its cancellation acknowledgement is sent."""
        running = self.running.get(execution)
        if running is not None:
            running.cancel_requested = True
        return running

    def cancel(self, running: Running) -> None:
        """Apply cancellation after the protocol coordinator acknowledged it."""
        if running.kind in ("shell", "job"):
            if running.process is not None:
                self.spawn(self.terminate(running))
        elif running.queued and running.task is not None:
            running.task.cancel()
        # Launch checks the flag before starting; an active cell interrupts itself.

    def start_idle_reaper(self, idle: float) -> None:
        if self.reaper is None:
            self.reaper = self.spawn(self.reap_idle_kernels(idle))

    def spawn(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @contextlib.contextmanager
    def control(self):
        """Keep close/restart visible to shutdown until their owned work drains."""
        task = asyncio.current_task()
        if task is not None:
            self.controls.add(task)
        try:
            yield
        finally:
            if task is not None:
                self.controls.discard(task)

    async def shutdown(self) -> None:
        await owned(self.finish_shutdown(asyncio.current_task()))

    async def finish_shutdown(self, requester: asyncio.Task[Any] | None) -> None:
        self.closing = True
        await self.close_resources()
        if self.reaper is not None:
            self.reaper.cancel()
        running = list(self.running.values())
        await asyncio.gather(
            *(self.terminate(item) for item in running if item.process is not None),
            return_exceptions=True,
        )
        for item in running:
            item.cancel_requested = True
            if item.task is not None:
                item.task.cancel()
        await asyncio.gather(
            *(item.task for item in running if item.task is not None), return_exceptions=True
        )
        controls = [
            task for task in self.controls if task not in (requester, asyncio.current_task())
        ]
        for task in controls:
            task.cancel()
        await asyncio.gather(*controls, return_exceptions=True)
        await asyncio.gather(
            *(kernel.stop() for kernel in self.kernels.values()), return_exceptions=True
        )
        await self.end_descendants()

    async def terminate(self, running: Running) -> None:
        """End one command tree, allowing a package wrapper time to roll back."""
        process = running.process
        if process is None:
            return
        owner, leader = running.owner, running.leader
        await self.signal(owner, leader)
        started = self.clock.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), self.term_grace)
        while True:
            left, in_turn = await asyncio.to_thread(self.leftovers, owner, leader)
            grace = self.package_grace if in_turn else self.term_grace
            if not left or self.clock.monotonic() - started >= grace:
                break
            await self.clock.sleep(0.1)
        if left or process.returncode is None:
            await self.collect(owner, leader)
        await process.wait()

    async def restart(
        self,
        kernel: Kernel,
        *,
        only_if_absent: bool,
        permit: Callable[[], contextlib.AbstractAsyncContextManager[None]],
        start: Callable[[], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
    ) -> None:
        """Replace a generation under control, stopping inside the admission permit."""
        async with kernel.control:
            async with permit():
                if only_if_absent and await kernel.alive():
                    raise self.error(
                        "conflict", "kernel became live before conditional recovery restart"
                    )
                for running in self.running.values():
                    if running.kernel is kernel and running.started:
                        running.restarted = True
                if kernel.manager is not None:
                    await stop()
                    kernel.state_lost = True
            if kernel.retired:
                raise self.error("conflict", f"run {kernel.run} has ended, and its kernels with it")
            await start()

    def kernel(self, session: str, language: str, run: str | None = None) -> Kernel:
        """The session's kernel of ``language``, or the run's own, made on first use."""
        if language not in self.languages:
            raise self.error("unsupported", f"no {language!r} kernel here")
        key = (session, run or "", language)
        if run is not None and run in self.ended_runs.get(session, {}):
            raise self.error("conflict", f"run {run} has ended, and its kernels with it")
        if key not in self.kernels:
            in_use = {id(running.kernel) for running in self.running.values()}
            runs = {
                kernel.run
                for kernel in self.kernels.values()
                if kernel.session == session
                and kernel.run not in (None, run)
                and (kernel.manager is not None or id(kernel) in in_use)
            }
            if run is not None and len(runs) >= self.max_run_kernels:
                raise self.error(
                    "unavailable",
                    f"{self.max_run_kernels} runs of this session have kernels of their own; "
                    "one must end first",
                )
            kernel = self.make_kernel(session, language, run)
            self.kernels[key] = kernel
        return self.kernels[key]

    async def reap_idle_kernels(self, idle: float) -> None:
        """Stop kernels unused for ``idle`` seconds; the session's next cell reports lost state."""
        interval = max(0.2, min(30.0, idle / 2))
        while True:
            await self.clock.sleep(interval)
            for kernel in tuple(self.kernels.values()):
                if kernel.manager is None or self.clock.monotonic() - kernel.last_used < idle:
                    continue
                async with kernel.control:
                    busy = any(running.kernel is kernel for running in self.running.values())
                    if kernel.manager is None or busy:
                        continue
                    if self.clock.monotonic() - kernel.last_used < idle:
                        continue
                    await kernel.stop()
                    kernel.state_lost = True
                print(
                    f"executor: stopped kernel {kernel.kernel_id} after {idle:.0f} s unused",
                    file=sys.stderr,
                )

    async def close_session(self, session: str, run: str | None) -> list[dict[str, Any]]:
        """Stop all session kernels; reply with the kernels it stopped.

        Its running cells end cancelled and its queued ones never start. Each kernel
        keeps its generation, so the session's next cell starts a new one and reports
        that earlier state is gone. Shell calls are not affected. Runs' own kernels are
        retired and forgotten, and their runs remembered as ended, so a late cell is
        refused rather than starting one again. A request naming a run stops only that
        run's own kernels; the session's kernels and jobs stay.
        """
        stopped: list[dict[str, Any]] = []
        jobs = [
            running
            for running in self.running.values()
            if running.kind == "job" and running.session == session and run is None
        ]
        for running in jobs:
            running.cancel_requested = True
        await asyncio.gather(
            *(self.terminate(running) for running in jobs if running.process is not None)
        )
        await asyncio.gather(
            *(running.task for running in jobs if running.task is not None), return_exceptions=True
        )
        for key, kernel in list(self.kernels.items()):
            if kernel.session != session or (run is not None and kernel.run != run):
                continue
            for running in self.running.values():
                if running.kernel is kernel:
                    running.cancel_requested = True
                    if running.started:
                        running.closing = True
            async with kernel.control:
                if kernel.manager is not None:
                    stopped.append(kernel.info())
                    await kernel.stop()
                    kernel.state_lost = True
                if kernel.run is not None:
                    kernel.retired = True
                    self.kernels.pop(key, None)
                    self.end_run(session, kernel.run)
        if run is not None:
            self.end_run(session, run)
        return stopped

    def end_run(self, session: str, run: str) -> None:
        """Remember ``run`` as ended, within ``self.max_ended_runs`` per session."""
        ended = self.ended_runs.setdefault(session, {})
        ended.pop(run, None)
        ended[run] = None
        while len(ended) > self.max_ended_runs:
            del ended[next(iter(ended))]

    async def session_kernels(self, session: str) -> list[dict[str, Any]]:
        """Only this session's live kernels, its own and its runs', in the order of their
        runs and then the fixed supported-language order."""
        found: list[dict[str, Any]] = []
        order = {language: number for number, language in enumerate(self.languages)}
        keys = sorted(
            (key for key in self.kernels if key[0] == session),
            key=lambda key: (key[1], order.get(key[2], len(order))),
        )
        for key in keys:
            kernel = self.kernels[key]
            if await kernel.alive():
                found.append(kernel.info())
        return found
