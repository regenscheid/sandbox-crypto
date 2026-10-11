# SPDX-License-Identifier: MIT
"""Caller sessions: each is bound to one caller and one server, and has its own upstream session.

A caller's ``initialize`` creates a session at the proxy, whose id is the
proxy's own, and an upstream session that the proxy opens itself. The id is
honoured only for the caller that created the session, so a session id that
leaks to another sandbox or project is useless there.

Each caller may hold a few sessions; a new one ends the caller's least
recently used session, so that one caller cannot take every place.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from sandbox_science.policy.operations import MAX_MCP_NAME_LENGTH

from .callers import Caller
from .jsonrpc import RequestId

MAX_TOOLS: Final = 10_000
"""Tool names one listing may hold; a longer listing fails."""
_MAX_PARTIAL_LISTINGS: Final = 16


class SessionsFullError(Exception):
    """No session can be added: the proxy holds its limit of active sessions."""


@dataclass(eq=False)
class AdvertisedTools:
    """Tool names from the server's latest complete ``tools/list``, over all its pages.

    The proxy forwards ``tools/call`` only for a name in this set, compared
    exactly, so that a server that folds case or normalizes names cannot be
    reached through a spelling the policy decided differently (docs/POLICY.md,
    "MCP calls"). Listings the caller makes count as well as the proxy's own; a
    listing replaces the set only once its last page has arrived.
    """

    names: frozenset[str] | None = None
    _partial: OrderedDict[str, frozenset[str]] = field(
        default_factory=OrderedDict[str, frozenset[str]]
    )

    def page(self, cursor: str | None, names: Iterable[str], next_cursor: str | None) -> None:
        """Take one page of a listing: the cursor it was asked with, its names and the next."""
        # The policy refuses longer names, so they could never be called.
        page = frozenset(name for name in names if len(name) <= MAX_MCP_NAME_LENGTH)
        if cursor is None:
            collected = page
        else:
            earlier = self._partial.pop(cursor, None)
            if earlier is None:
                return  # without the earlier pages, this page cannot complete a listing
            collected = earlier | page
        if len(collected) > MAX_TOOLS:
            raise ValueError(f"the server lists more than {MAX_TOOLS} tools")
        if next_cursor is None:
            self.names = collected
            return
        self._partial[next_cursor] = collected
        while len(self._partial) > _MAX_PARTIAL_LISTINGS:
            self._partial.popitem(last=False)

    def invalidate(self) -> None:
        """Forget the listing, after the server said its tools changed."""
        self.names = None
        self._partial.clear()


@dataclass(eq=False)
class Session:
    """One caller's session with one server."""

    id: str
    handle: str
    """A short label for records; unlike ``id``, it grants nothing."""
    caller: Caller
    server: str
    url: str
    version: str
    upstream_id: str | None
    last_used: float
    tools: AdvertisedTools = field(default_factory=AdvertisedTools)
    listing: asyncio.Lock = field(default_factory=asyncio.Lock)
    in_flight: dict[RequestId, int] = field(default_factory=dict[RequestId, int])
    _next_request: int = 1

    def request_id(self) -> int:
        """A new id for a request the proxy sends upstream in this session."""
        value = self._next_request
        self._next_request += 1
        return value


class Sessions:
    """Active sessions; a session unused for ``idle`` seconds ends.

    At most ``limit`` sessions in all, and ``per_caller`` for each caller.
    """

    def __init__(
        self,
        *,
        limit: int,
        idle: float,
        per_caller: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._per_caller = per_caller
        self._idle = idle
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def has_room(self, caller: Caller) -> bool:
        """Whether ``add`` would succeed for this caller, after ending idle sessions."""
        self._sweep()
        return len(self._sessions) < self._limit or any(
            session.caller == caller for session in self._sessions.values()
        )

    def add(
        self, caller: Caller, server: str, url: str, version: str, upstream_id: str | None
    ) -> Session:
        """Start a session, ending the caller's least recently used one when it must.

        Raises SessionsFullError when the proxy is full and the caller holds no session.
        """
        self._sweep()
        own = sorted(
            (session for session in self._sessions.values() if session.caller == caller),
            key=lambda session: session.last_used,
        )
        while own and (len(own) >= self._per_caller or len(self._sessions) >= self._limit):
            del self._sessions[own.pop(0).id]
        if len(self._sessions) >= self._limit:
            raise SessionsFullError(f"{self._limit} sessions are active")
        session = Session(
            id=secrets.token_urlsafe(32),
            handle=f"mcps-{secrets.token_hex(6)}",
            caller=caller,
            server=server,
            url=url,
            version=version,
            upstream_id=upstream_id,
            last_used=self._clock(),
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str, caller: Caller, server: str) -> Session | None:
        """The caller's session with this id and server, or None (also once it has ended)."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_used > self._idle:
            del self._sessions[session_id]
            return None
        if session.caller != caller or session.server != server:
            return None
        session.last_used = now
        return session

    def remove(self, session: Session) -> None:
        """End a session."""
        self._sessions.pop(session.id, None)

    def _sweep(self) -> None:
        cutoff = self._clock() - self._idle
        for session_id in [
            key for key, value in self._sessions.items() if value.last_used < cutoff
        ]:
            del self._sessions[session_id]
