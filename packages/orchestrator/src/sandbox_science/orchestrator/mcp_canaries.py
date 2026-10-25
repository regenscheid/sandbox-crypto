# SPDX-License-Identifier: MIT
"""Scheduled MCP canaries, run from the trusted side (D103, docs/MCP-CONNECTORS.md).

A canary is one bounded ``tools/list`` per configured MCP server, made every
``interval`` through the same path as the workspace's explicit availability check: the
MCP proxy's ordinary, policy-enforcing endpoint, with the proxy caller's token. It
initializes and lists, and never calls a tool; it never retries within a run (a run is
one listing per server, at most ``MAX_PAGES`` pages within ``LIST_SECONDS``, each request
within ``REQUEST_SECONDS``).

Each result is a row of the control store's ``mcp_canaries`` (status, tool count and
listing digest when listed, elapsed milliseconds, error kind otherwise), and one log
record, ``event: mcp_canary``. When a server's status moves from ``listed`` to anything
else, or back, a second record, ``event: mcp_canary_changed``, says so; a server's first
result that is not ``listed`` counts as a change too, from no earlier result, so a server
down from the start is noticed. The runner prunes each server's rows to
``RETENTION_DAYS`` and ``RETENTION_ROWS``.

The direct path (D98 prototype) has no trusted-side listing: the policy middleware
reads listings only as they pass from a server to a sandbox. A deployment without the
proxy runs no canaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast

import aiohttp

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep

from .connectors import ConnectorError, ProxyClient, check_proxy
from .control import ControlError, ControlStore, Tx

_log = logging.getLogger(__name__)

ListingStatus = Literal["listed", "unavailable", "denied", "timeout"]
LIST_SECONDS: Final = 15.0
"""The bound on one server's whole listing, every page included."""
REQUEST_SECONDS: Final = 10.0
"""The bound on one request to the proxy."""
MAX_PAGES: Final = 32
"""Pages one listing may take; a longer one is ``unavailable``."""
INTERVAL: Final = 900.0
"""Seconds between canary runs by default: 15 minutes."""
RETENTION_DAYS: Final = 30
RETENTION_ROWS: Final = 10_000
"""Rows kept per server, besides the age bound."""
SESSION: Final = "mcp-canary"
"""The session name the canary's requests carry to the proxy."""
EVENT: Final = "mcp_canary"
CHANGED: Final = "mcp_canary_changed"
_KIND: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ListingClient(Protocol):
    """What a listing needs of a client: ``ProxyClient.request``."""

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Listing:
    """One bounded listing's outcome. ``tool_count`` and ``digest`` are set exactly when
    it is ``listed``; ``error_kind`` exactly when it is not."""

    status: ListingStatus
    tool_count: int | None = None
    digest: str | None = None
    error_kind: str | None = None


def _canonical(tool: object) -> bytes:
    return json.dumps(tool, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


async def list_tools(
    client: ListingClient,
    server: str,
    *,
    session: str,
    seconds: float = LIST_SECONDS,
    pages: int = MAX_PAGES,
) -> Listing:
    """List ``server``'s tools through ``client``: ``tools/list`` only, page by page, within
    ``seconds`` and ``pages``. The digest is SHA-256 over each tool's canonical JSON, one
    per line, in the order listed. Every failure is a status, never an exception: a
    policy refusal is ``denied``, the bound is ``timeout``, anything else ``unavailable``."""
    digest = hashlib.sha256()
    count = 0
    try:
        async with asyncio.timeout(seconds):
            cursor: str | None = None
            seen: set[str] = set()
            for _ in range(pages):
                result = await client.request(
                    server,
                    "tools/list",
                    {} if cursor is None else {"cursor": cursor},
                    session=session,
                )
                tools = result.get("tools")
                if not isinstance(tools, list):
                    return Listing("unavailable", error_kind="malformed_listing")
                for tool in cast("list[object]", tools):
                    digest.update(_canonical(tool) + b"\n")
                count += len(cast("list[object]", tools))
                following = result.get("nextCursor")
                if following is None:
                    return Listing("listed", tool_count=count, digest=digest.hexdigest())
                if not isinstance(following, str) or following in seen:
                    return Listing("unavailable", error_kind="cursor_loop")
                seen.add(following)
                cursor = following
            return Listing("unavailable", error_kind="too_many_pages")
    except TimeoutError:
        return Listing("timeout", error_kind="timeout")
    except ConnectorError as error:
        kind = error.kind if _KIND.fullmatch(error.kind) else "connector_error"
        return Listing(
            "denied" if error.kind == "policy_denied" else "unavailable", error_kind=kind
        )
    except (aiohttp.ClientError, ValueError):
        return Listing("unavailable", error_kind="client_error")


class ListingSource(Protocol):
    """Where the runner's listings come from: the proxy, or a test's fake."""

    async def list(self, server: str) -> Listing: ...


class ProxySource:
    """Listings through the MCP proxy, a fresh MCP session per listing, as the workspace's
    explicit check makes them."""

    def __init__(self, url: str, token: str) -> None:
        check_proxy(url, token)
        self.url, self._token = url.rstrip("/"), token

    async def list(self, server: str) -> Listing:
        client = ProxyClient(self.url, self._token, timeout=REQUEST_SECONDS)
        try:
            return await list_tools(client, server, session=SESSION)
        finally:
            await client.close()


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """One row of ``mcp_canaries``."""

    server: str
    at: datetime
    status: ListingStatus
    tool_count: int | None
    digest: str | None
    elapsed_ms: int
    error_kind: str | None


def stamp(moment: datetime) -> str:
    """``moment`` as the table keeps it: UTC, to the millisecond, ``Z`` at the end."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _parsed(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


_COLUMNS: Final = "server, at, status, tool_count, digest, elapsed_ms, error_kind"


def _row(row: tuple[Any, ...]) -> CanaryResult:
    server, at, status, tool_count, digest, elapsed_ms, error_kind = row
    return CanaryResult(server, _parsed(at), status, tool_count, digest, elapsed_ms, error_kind)


def _latest(tx: Tx, server: str) -> CanaryResult | None:
    row = tx.sql.execute(
        f"SELECT {_COLUMNS} FROM mcp_canaries WHERE server = ? ORDER BY canary_id DESC LIMIT 1",  # noqa: S608 - fixed columns
        (server,),
    ).fetchone()
    return None if row is None else _row(row)


class HasControl(Protocol):
    """A repository holder whose control store the canaries share: a ``ProjectStore`` or a
    ``DocumentStore``."""

    @property
    def control(self) -> ControlStore: ...


class CanaryStore:
    """The canaries' rows in the control store."""

    def __init__(
        self,
        control: ControlStore,
        *,
        retention: timedelta = timedelta(days=RETENTION_DAYS),
        rows: int = RETENTION_ROWS,
    ) -> None:
        self.control, self.retention, self.rows = control, retention, rows

    @classmethod
    def of(cls, holder: HasControl) -> CanaryStore:
        """The canaries of ``holder``'s control store, read-only when it is."""
        return cls(holder.control)

    def record(self, result: CanaryResult) -> CanaryResult | None:
        """Add ``result`` and prune its server's rows, in one change; the server's result
        before it, if there was one."""
        with self.control.write() as tx:
            previous = _latest(tx, result.server)
            tx.sql.execute(
                f"INSERT INTO mcp_canaries ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: S608 - fixed columns
                (
                    result.server,
                    stamp(result.at),
                    result.status,
                    result.tool_count,
                    result.digest,
                    result.elapsed_ms,
                    result.error_kind,
                ),
            )
            tx.sql.execute(
                "DELETE FROM mcp_canaries WHERE server = ? AND at < ?",
                (result.server, stamp(result.at - self.retention)),
            )
            tx.sql.execute(
                "DELETE FROM mcp_canaries WHERE server = ? AND canary_id <= (SELECT canary_id "
                "FROM mcp_canaries WHERE server = ? ORDER BY canary_id DESC LIMIT 1 OFFSET ?)",
                (result.server, result.server, self.rows),
            )
        return previous

    def latest(self, servers: Iterable[str]) -> dict[str, CanaryResult]:
        """Each server's newest result, for those that have one."""
        if self.control.missing:
            return {}
        with self.control.read() as tx:
            found = {server: _latest(tx, server) for server in dict.fromkeys(servers)}
        return {server: result for server, result in found.items() if result is not None}

    def history(self, server: str) -> list[CanaryResult]:
        """A server's kept results, oldest first (for tests and inspection)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM mcp_canaries WHERE server = ? ORDER BY canary_id",  # noqa: S608 - fixed columns
                (server,),
            ).fetchall()
        return [_row(row) for row in rows]


def changed(previous: CanaryResult | None, result: CanaryResult) -> bool:
    """Whether ``result`` moves its server into or out of ``listed``. With no earlier
    result, only a first result that is not ``listed`` is a change."""
    was = True if previous is None else previous.status == "listed"
    return was != (result.status == "listed")


class CanaryRunner:
    """Runs the canaries every ``interval`` seconds until asked to stop."""

    def __init__(
        self,
        store: CanaryStore,
        source: ListingSource,
        servers: Sequence[str],
        *,
        interval: float = INTERVAL,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not servers:
            raise ValueError("canaries need at least one server")
        if not interval > 0:
            raise ValueError("the canary interval is positive")
        self.store, self.source, self.servers = store, source, tuple(dict.fromkeys(servers))
        self.interval, self.clock = interval, clock

    async def run_once(self) -> list[CanaryResult]:
        """One listing per server, one after another; each result recorded and logged."""
        done: list[CanaryResult] = []
        for server in self.servers:
            started = self.clock.monotonic()
            try:
                listing = await self.source.list(server)
            except Exception:
                # A source fails into a status; anything else is a fault worth its trace.
                _log.exception("the canary of %s failed", server)
                listing = Listing("unavailable", error_kind="internal_error")
            elapsed = max(0, round((self.clock.monotonic() - started) * 1000))
            result = CanaryResult(
                server=server,
                at=self.clock.now(),
                status=listing.status,
                tool_count=listing.tool_count,
                digest=listing.digest,
                elapsed_ms=min(elapsed, 86_400_000),
                error_kind=listing.error_kind,
            )
            try:
                previous = await asyncio.to_thread(self.store.record, result)
            except ControlError as error:
                # Unrecorded, the change from the last result is unknown: only the result
                # itself is logged.
                _log.error("could not record the canary of %s: %s", server, error)
                _logged(result)
            else:
                _logged(result, previous, compare=True)
            done.append(result)
        return done

    async def run(self, stop: asyncio.Event) -> None:
        """Run at once, then every ``interval``, until ``stop`` is set."""
        while not stop.is_set():
            await self.run_once()
            if await wait_or_sleep(self.clock, stop, self.interval):
                return


def _logged(
    result: CanaryResult, previous: CanaryResult | None = None, *, compare: bool = False
) -> None:
    listed = result.status == "listed"
    _log.log(
        logging.INFO if listed else logging.WARNING,
        "canary %s: %s in %d ms",
        result.server,
        result.status,
        result.elapsed_ms,
        extra={
            "event": EVENT,
            "server": result.server,
            "status": result.status,
            "tool_count": result.tool_count,
            "elapsed_ms": result.elapsed_ms,
            "error_kind": result.error_kind,
        },
    )
    if compare and changed(previous, result):
        before = None if previous is None else previous.status
        _log.log(
            logging.INFO if listed else logging.WARNING,
            "canary %s changed from %s to %s",
            result.server,
            before or "no earlier result",
            result.status,
            extra={
                "event": CHANGED,
                "server": result.server,
                "status": result.status,
                "previous": before,
            },
        )
