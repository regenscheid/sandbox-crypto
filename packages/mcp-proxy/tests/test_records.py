# SPDX-License-Identifier: MIT
"""Records reach the event store, chained, and hold no argument values."""

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Protocol

import pytest
import pytest_asyncio

from sandbox_science.event_store import EventStore, StoreReader, verify_store
from sandbox_science.mcp_proxy.records import SOURCE, StoreSink

pytestmark = pytest.mark.asyncio


class Answer(Protocol):
    status: int
    body: Any


class Harness(Protocol):
    async def initialize(
        self, caller: str = "orchestrator", version: str = "2025-11-25"
    ) -> str: ...
    async def tool(
        self, session: str, name: str, arguments: Mapping[str, object] | None = None
    ) -> Answer: ...


@pytest.fixture
def events_path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest_asyncio.fixture
async def sink(events_path: Path) -> AsyncIterator[StoreSink]:
    """Overrides conftest's in-memory sink: the harness records to a real event store."""
    with EventStore(events_path) as store:
        yield StoreSink(store)


async def test_calls_and_findings_are_chained_in_the_event_store(
    harness: Harness, events_path: Path
) -> None:
    session = await harness.initialize()
    assert "result" in (await harness.tool(session, "search", {"query": "secret-ish"})).body
    assert "error" in (await harness.tool(session, "crawl", {"url": "secret-ish"})).body
    verification = verify_store(events_path)
    assert verification.ok, verification.problems
    with StoreReader(events_path) as reader:
        records = list(reader.records())
    assert {record.source for record in records} == {SOURCE}
    assert [record.kind for record in records] == [
        "mcp-call",  # initialize
        "mcp-call",  # notifications/initialized
        "mcp-call",  # search
        "escalation",
        "mcp-call",  # crawl
    ]
    assert all("secret-ish" not in record.payload for record in records)
    escalation = records[3].data()
    assert escalation["tool"] == "crawl"
    assert escalation["rule"] == "mcp.servers.papers.tools.escalate[crawl]"
