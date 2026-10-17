# SPDX-License-Identifier: MIT
"""VM-owned bounded root admission and explicit recovery branch scheduling."""

import asyncio
from collections.abc import Callable

from pydantic import JsonValue, TypeAdapter

from .transport import Capability, Channel, TransportError
from .wire import Queue, ResumeBranch, ResumeReady, Waiting, payload, typed


class Scheduling:
    def __init__(
        self, channel: Channel, projects: frozenset[str], capacity: Callable[[], bool]
    ) -> None:
        self.channel, self.projects, self.capacity = channel, projects, capacity
        self.active: dict[tuple[str, str, str, str], asyncio.Task[JsonValue]] = {}

    async def poll(self) -> None:
        for key, task in tuple(self.active.items()):
            if task.done():
                self.active.pop(key)
                TypeAdapter(bool).validate_python(await task)
        queue = typed(Queue, await self.channel.request(Capability.QUEUE, None, timeout=30))
        for entry in queue.entries:
            if entry.project not in self.projects:
                raise TransportError("denied", "control returned work outside its scope")
            if not self.capacity():
                break
            await self.channel.request(Capability.ADMIT, payload(entry), timeout=30)
            if entry.request is not None:
                await self.resume(entry)

    async def resume(self, entry: Waiting) -> None:
        request = entry.request
        if request is None:
            raise TransportError("invalid_request", "Resume scheduling requires a request")
        ready = typed(
            ResumeReady,
            await self.channel.request(Capability.RESUME_NEXT, payload(entry), timeout=30),
        )
        for work in ready.work:
            key = entry.project, entry.session, request, work
            if key in self.active:
                continue
            if len(self.active) >= 16 or not self.capacity():
                break
            branch = ResumeBranch(
                project=entry.project, session=entry.session, request=request, work=work
            )
            self.active[key] = asyncio.create_task(
                self.channel.request(Capability.RESUME_BRANCH, payload(branch), timeout=None),
                name="confined recovery branch " + work,
            )

    async def close(self) -> None:
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()
