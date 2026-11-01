# SPDX-License-Identifier: MIT
"""A single runtime connection, its passive probes and owned shutdown."""

from __future__ import annotations

import asyncio

from aiohttp import WSCloseCode, web

from sandbox_science.orchestrator.aio import owned

from .transport import CONTROL_PROTOCOL, Channel


class ControlLifecycle:
    """Keep admission ownership until admitted effects and the service have settled."""

    def __init__(self) -> None:
        self.occupied = False
        self.channel: Channel | None = None
        self.socket: web.WebSocketResponse | None = None
        self.handshaken = False
        self.draining = False
        self.settled = asyncio.Event()
        self.settled.set()

    def claim(self, socket: web.WebSocketResponse) -> None:
        self.occupied = True
        self.socket = socket
        self.settled.clear()

    def release(self) -> None:
        self.occupied = False
        self.socket = self.channel = None
        self.handshaken = False
        self.settled.set()

    def health(self) -> dict[str, bool | int]:
        connected = self.channel is not None and not self.channel.closed
        return {
            "ready": not self.draining,
            "process_alive": True,
            "service_ready": not self.draining,
            "draining": self.draining,
            "protocol": CONTROL_PROTOCOL,
            "runtime_connected": connected,
            "runtime_ready": connected and self.handshaken,
            "runtime_draining": self.occupied and not connected,
        }

    async def shutdown(self, app: web.Application) -> None:
        """Fence admission and close the socket, then join the original owned effects."""
        del app
        self.draining = True
        await owned(self._drain())

    async def _drain(self) -> None:
        async def close_socket() -> None:
            if self.socket is not None and self.socket.prepared:
                await self.socket.close(code=WSCloseCode.GOING_AWAY, message=b"control stopping")

        if self.channel is not None:
            await asyncio.gather(self.channel.close(), close_socket())
        else:
            await close_socket()
        # The handler also closes its RunService, repositories and worker client.
        await self.settled.wait()
