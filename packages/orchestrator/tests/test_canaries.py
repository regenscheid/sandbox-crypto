# SPDX-License-Identifier: MIT
"""The scheduled MCP canaries: the bounded listing, the runner with a fake source (statuses,
change events, pruning), the control store's table and its migration."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.mcp_proxy.callers import Caller, token_digest
from sandbox_science.orchestrator.connectors import ConnectorError, ProxyClient
from sandbox_science.orchestrator.control import ControlStore, migrations, store_path
from sandbox_science.orchestrator.mcp_canaries import (
    CHANGED,
    EVENT,
    CanaryResult,
    CanaryRunner,
    CanaryStore,
    Listing,
    ProxySource,
    list_tools,
    stamp,
)
from sandbox_science.testing import FakeClock, eventually
from sandbox_science.testing.mcp import FakeServer, Tls

from .control_versions import at_version
from .loop_harness import PROJECT
from .test_connectors import TOKEN, serve_proxy
from .test_connectors import tls as tls


class Pages:
    """A fake proxy client: answers ``tools/list`` from ``pages`` in turn, or raises."""

    def __init__(self, *answers: dict[str, Any] | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict[str, object], str]] = []

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((server, method, dict(params), session))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


TOOL_A = {"name": "search", "description": "Find papers", "inputSchema": {"type": "object"}}
TOOL_B = {"name": "read", "description": "Read a paper", "inputSchema": {"type": "object"}}


async def test_a_listing_pages_through_tools_list_only_and_digests_the_tools() -> None:
    client = Pages({"tools": [TOOL_A], "nextCursor": "2"}, {"tools": [TOOL_B]})
    found = await list_tools(client, "papers", session="mcp-canary")
    assert found.status == "listed"
    assert found.tool_count == 2
    assert found.error_kind is None
    assert found.digest is not None
    assert len(found.digest) == 64
    assert [call[1] for call in client.calls] == ["tools/list", "tools/list"]
    assert client.calls[1][2] == {"cursor": "2"}
    assert {call[3] for call in client.calls} == {"mcp-canary"}
    # The same tools in one page give the same digest; another tool list does not.
    same = await list_tools(Pages({"tools": [TOOL_A, TOOL_B]}), "papers", session="s")
    other = await list_tools(Pages({"tools": [TOOL_B, TOOL_A]}), "papers", session="s")
    assert same.digest == found.digest
    assert other.digest != found.digest


@pytest.mark.parametrize(
    ("answers", "status", "kind"),
    [
        ([{"tools": "none"}], "unavailable", "malformed_listing"),
        ([{"tools": [], "nextCursor": "a"}, {"tools": [], "nextCursor": "a"}], "unavailable",
         "cursor_loop"),
        ([{"tools": [], "nextCursor": 7}], "unavailable", "cursor_loop"),
        ([ConnectorError("policy_denied", "no")], "denied", "policy_denied"),
        ([ConnectorError("upstream_error", "down")], "unavailable", "upstream_error"),
        ([ConnectorError("Weird Kind!", "?")], "unavailable", "connector_error"),
        ([TimeoutError()], "timeout", "timeout"),
        ([ValueError("bad json")], "unavailable", "client_error"),
    ],
)  # fmt: skip
async def test_every_failure_is_a_status(
    answers: list[dict[str, Any] | BaseException], status: str, kind: str
) -> None:
    found = await list_tools(Pages(*answers), "papers", session="mcp-canary")
    assert (found.status, found.error_kind, found.tool_count, found.digest) == (
        status,
        kind,
        None,
        None,
    )


async def test_a_listing_is_bounded_by_pages_and_time() -> None:
    endless = Pages(*({"tools": [], "nextCursor": str(n)} for n in range(40)))
    found = await list_tools(endless, "papers", session="s", pages=4)
    assert (found.status, found.error_kind) == ("unavailable", "too_many_pages")
    assert len(endless.calls) == 4

    class Silent:
        async def request(self, *args: object, **kwargs: object) -> dict[str, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    found = await list_tools(Silent(), "papers", session="s", seconds=0.05)
    assert (found.status, found.error_kind) == ("timeout", "timeout")


class Source:
    """A fake listing source: each server's listings in turn, the last one repeated."""

    def __init__(self, clock: FakeClock, listings: dict[str, list[Listing]]) -> None:
        self.clock, self.listings = clock, listings
        self.asked: list[str] = []

    async def list(self, server: str) -> Listing:
        self.asked.append(server)
        self.clock.advance(0.25)  # each listing takes 250 ms of the fake clock
        queue = self.listings[server]
        return queue.pop(0) if len(queue) > 1 else queue[0]


LISTED = Listing("listed", tool_count=3, digest="a" * 64)
DOWN = Listing("unavailable", error_kind="upstream_error")


@pytest.fixture
def control(tmp_path: Path) -> Iterator[ControlStore]:
    store = ControlStore(store_path(tmp_path))
    store.initialize()
    yield store
    store.close()


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == event]


async def test_the_runner_records_each_server_and_logs_changes(
    control: ControlStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.orchestrator.mcp_canaries")
    clock = FakeClock()
    source = Source(
        clock,
        {
            "papers": [LISTED, DOWN, Listing("timeout", error_kind="timeout"), LISTED],
            "archive": [DOWN],
        },
    )
    store = CanaryStore(control)
    runner = CanaryRunner(store, source, ["papers", "archive", "papers"], clock=clock)
    assert runner.servers == ("papers", "archive")
    for _ in range(4):
        await runner.run_once()
    history = store.history("papers")
    assert [row.status for row in history] == ["listed", "unavailable", "timeout", "listed"]
    assert history[0].tool_count == 3
    assert history[0].digest == "a" * 64
    assert history[1].error_kind == "upstream_error"
    assert history[1].digest is None
    assert {row.elapsed_ms for row in history} == {250}
    latest = store.latest(["papers", "archive", "absent"])
    assert set(latest) == {"papers", "archive"}
    assert latest["archive"].status == "unavailable"
    # One mcp_canary record per result; a change record only into or out of listed, and
    # for a server whose first result is not listed.
    assert len(_events(caplog, EVENT)) == 8
    changes = [
        tuple(getattr(record, name) for name in ("server", "previous", "status"))
        for record in _events(caplog, CHANGED)
    ]
    assert changes == [
        ("archive", None, "unavailable"),
        ("papers", "listed", "unavailable"),
        ("papers", "timeout", "listed"),
    ]
    failing = _events(caplog, CHANGED)[1]
    assert failing.levelno == logging.WARNING


async def test_the_runner_runs_every_interval_until_stopped(control: ControlStore) -> None:
    clock = FakeClock()
    source = Source(clock, {"papers": [LISTED]})
    runner = CanaryRunner(CanaryStore(control), source, ["papers"], interval=900, clock=clock)
    stop = asyncio.Event()
    task = asyncio.ensure_future(runner.run(stop))
    await eventually(lambda: clock.sleepers == 1, describe="the first run")
    assert source.asked == ["papers"]
    clock.advance(899)
    await asyncio.sleep(0)
    assert source.asked == ["papers"]
    clock.advance(1)
    await eventually(lambda: len(source.asked) == 2, describe="the second run")
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_failing_source_is_an_unavailable_result(
    control: ControlStore, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        async def list(self, server: str) -> Listing:
            raise RuntimeError("a bug")

    store = CanaryStore(control)
    (result,) = await CanaryRunner(store, Broken(), ["papers"], clock=FakeClock()).run_once()
    assert (result.status, result.error_kind) == ("unavailable", "internal_error")
    assert store.latest(["papers"])["papers"].status == "unavailable"


def test_pruning_keeps_thirty_days_and_a_row_bound_for_every_server(
    control: ControlStore,
) -> None:
    clock = FakeClock()
    store = CanaryStore(control, rows=5)

    def result(server: str) -> CanaryResult:
        return CanaryResult(server, clock.now(), "listed", 1, "b" * 64, 10, None)

    store.record(result("gone"))  # a server no longer configured: never recorded again
    for _ in range(8):
        store.record(result("papers"))
        store.record(result("other"))
        clock.advance(60)
    assert store.prune(clock.now()) == 6
    assert len(store.history("papers")) == 5
    assert len(store.history("other")) == 5
    assert len(store.history("gone")) == 1
    clock.advance(timedelta(days=30).total_seconds())
    store.record(result("papers"))
    # Older than 30 days are gone, whichever server they were of.
    store.prune(clock.now())
    assert len(store.history("papers")) == 1
    assert store.history("other") == []
    assert store.history("gone") == []


async def test_a_run_prunes_every_server(control: ControlStore) -> None:
    clock = FakeClock()
    store = CanaryStore(control, rows=2)
    store.record(CanaryResult("gone", clock.now(), "listed", 1, "b" * 64, 1, None))
    for _ in range(3):
        store.record(CanaryResult("gone", clock.now(), "listed", 1, "b" * 64, 1, None))
    runner = CanaryRunner(store, Source(clock, {"papers": [LISTED]}), ["papers"], clock=clock)
    for _ in range(4):
        await runner.run_once()
    assert len(store.history("papers")) == 2
    assert len(store.history("gone")) == 2


def test_the_table_refuses_inconsistent_rows(control: ControlStore) -> None:
    at = stamp(FakeClock().now())
    bad = [
        ("papers", at, "listed", None, "c" * 64, 1, None),  # listed without a count
        ("papers", at, "listed", 1, "c" * 64, 1, "oops"),  # listed with an error
        ("papers", at, "unavailable", None, None, 1, None),  # a failure without its kind
        ("papers", at, "broken", None, None, 1, "x"),
        ("papers", "2026-10-25 12:00:00", "timeout", None, None, 1, "timeout"),
        ("papers", at, "timeout", None, None, -1, "timeout"),
        ("papers", at, "listed", 1, "C" * 64, 1, None),
        ("papers", at, "timeout", None, None, 1, "Bad Kind"),
    ]
    for row in bad:
        with pytest.raises(sqlite3.IntegrityError), control.write() as tx:
            tx.sql.execute(
                "INSERT INTO mcp_canaries (server, at, status, tool_count, digest, elapsed_ms, "
                "error_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )


def test_a_store_from_before_canaries_gains_the_table(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with at_version(migrations.VERSION - 1):
        older = ControlStore(path)
        older.initialize()
        older.close()
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION - 1,)
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'mcp_canaries'"
        ).fetchone() == (0,)
    finally:
        db.close()
    control = ControlStore(path)
    try:
        store = CanaryStore(control)
        assert store.latest(["papers"]) == {}
        clock = FakeClock()
        store.record(CanaryResult("papers", clock.now(), "denied", None, None, 5, "policy_denied"))
        with control.read() as tx:
            assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        found = store.latest(["papers"])["papers"]
        assert (found.status, found.at) == ("denied", clock.now())
    finally:
        control.close()


def test_a_read_only_store_before_any_writer_has_no_canaries(tmp_path: Path) -> None:
    reader = ControlStore(store_path(tmp_path), readonly=True)
    try:
        assert CanaryStore(reader).latest(["papers"]) == {}
    finally:
        reader.close()


def test_the_canaries_command_refuses_a_short_interval_and_an_open_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandbox_science.orchestrator.cli import main

    token = tmp_path / "token"
    token.write_text("test-secret-token-0123456789\n")
    token.chmod(0o600)
    base = [
        "mcp-canaries", "--state-dir", str(tmp_path / "state"),
        "--mcp-proxy", "http://127.0.0.1:18080", "--mcp-token-file", str(token),
        "--server", "papers",
    ]  # fmt: skip
    assert main([*base, "--interval", "5"]) == 2
    assert "at least 60" in capsys.readouterr().err
    token.chmod(0o644)
    assert main(base) == 1
    assert "open to other users" in capsys.readouterr().err
    token.chmod(0o600)
    assert main([*base[:3], "--mcp-proxy", "http://198.51.100.7:80", *base[5:]]) == 1
    assert "loopback" in capsys.readouterr().err


def test_the_ready_line_is_the_one_the_launcher_waits_for() -> None:
    from sandbox_science.launcher.parts import CANARIES_READY
    from sandbox_science.orchestrator.cli_canaries import READY

    assert READY == CANARIES_READY
    assert json.dumps(READY)  # one line of plain text


def _initializes(fake: FakeServer) -> int:
    """The upstream sessions the proxy started at the fake server."""
    return sum(
        1
        for seen in fake.seen
        if isinstance(seen.message, dict)
        and cast("dict[str, object]", seen.message).get("method") == "initialize"
    )


async def test_the_canaries_hold_no_proxy_session_and_take_no_run_service_place(
    tls: Tls, tmp_path: Path
) -> None:
    """Against the real proxy: the canaries' own caller, one client per server across runs,
    and each listing's session ended, so after many runs the canary caller holds at most
    one session per server (none, once each listing has ended), and the run service's
    session is never evicted (the proxy allows 4 per caller here)."""
    canary_token = "test-secret-canary-caller-token"
    canary = Caller("canary", "orchestrator", PROJECT)
    async with serve_proxy(
        tls, tmp_path / "proxy", callers=[(token_digest(canary_token), canary)]
    ) as proxied:
        assert proxied.sessions is not None
        run_service = ProxyClient(proxied.url, TOKEN)
        await run_service.request("papers", "tools/list", {}, session="s-1")
        started = _initializes(proxied.fake)
        source = ProxySource(proxied.url, canary_token)
        clock = FakeClock()
        control = ControlStore(store_path(tmp_path))
        try:
            runner = CanaryRunner(CanaryStore(control), source, ["papers"], clock=clock)
            for _ in range(6):
                (result,) = await runner.run_once()
                assert result.status == "listed"
                assert result.tool_count is not None
                assert result.tool_count > 0
                # The run service's one session, and none of the canary's: each ended.
                assert len(proxied.sessions) == 1
            # The run service's session is the one it started: no new initialize.
            await run_service.request("papers", "tools/list", {}, session="s-1")
            assert _initializes(proxied.fake) == started + 6
        finally:
            await source.close()
            await run_service.close()
            control.close()
