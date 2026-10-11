# SPDX-License-Identifier: MIT
"""Caller sessions and the tool names a server advertised."""

import pytest

from sandbox_science.mcp_proxy.callers import Caller
from sandbox_science.mcp_proxy.sessions import (
    MAX_TOOLS,
    AdvertisedTools,
    Sessions,
    SessionsFullError,
)

ORCHESTRATOR = Caller("orchestrator", "orchestrator", "crypto")
WORKER = Caller("worker", "worker", "crypto", container="ss-1")


def test_a_one_page_listing_sets_the_names() -> None:
    tools = AdvertisedTools()
    assert tools.names is None
    tools.page(None, ["a", "b"], None)
    assert tools.names == {"a", "b"}


def test_a_listing_counts_once_its_last_page_has_arrived() -> None:
    tools = AdvertisedTools()
    tools.page(None, ["old"], None)
    tools.page(None, ["a"], "c1")
    tools.page("c1", ["b"], "c2")
    assert tools.names == {"old"}
    tools.page("c2", ["c"], None)
    assert tools.names == {"a", "b", "c"}


def test_a_page_whose_earlier_pages_were_not_seen_changes_nothing() -> None:
    tools = AdvertisedTools()
    tools.page(None, ["a"], None)
    tools.page("unknown-cursor", ["b"], None)
    assert tools.names == {"a"}


def test_partial_listings_are_bounded() -> None:
    tools = AdvertisedTools()
    for n in range(40):
        tools.page(None, [f"t{n}"], f"cursor-{n}")
    tools.page("cursor-0", ["x"], None)  # the oldest partial listing was dropped
    assert tools.names is None
    tools.page("cursor-39", ["x"], None)
    assert tools.names == {"t39", "x"}


def test_a_listing_with_too_many_tools_fails() -> None:
    with pytest.raises(ValueError, match="more than"):
        AdvertisedTools().page(None, [f"t{n}" for n in range(MAX_TOOLS + 1)], None)


def test_invalidate_forgets_the_listing() -> None:
    tools = AdvertisedTools()
    tools.page(None, ["a"], "c1")
    tools.page(None, ["a"], None)
    tools.invalidate()
    assert tools.names is None
    tools.page("c1", ["b"], None)
    assert tools.names is None


def test_a_session_is_found_only_by_its_caller_and_server() -> None:
    sessions = Sessions(limit=4, idle=60, clock=lambda: 0.0)
    session = sessions.add(
        ORCHESTRATOR, "papers", "https://mcp.example.test/mcp", "2025-11-25", None
    )
    assert sessions.get(session.id, ORCHESTRATOR, "papers") is session
    assert sessions.get(session.id, WORKER, "papers") is None
    assert sessions.get(session.id, ORCHESTRATOR, "other") is None
    assert sessions.get("guess", ORCHESTRATOR, "papers") is None
    assert session.id != session.handle
    assert len(session.id) >= 43
    sessions.remove(session)
    assert sessions.get(session.id, ORCHESTRATOR, "papers") is None


def test_request_ids_are_new_for_each_request() -> None:
    sessions = Sessions(limit=4, idle=60)
    session = sessions.add(ORCHESTRATOR, "papers", "https://x.test/mcp", "2025-11-25", "up-1")
    assert [session.request_id() for _ in range(3)] == [1, 2, 3]


URL = "https://x.test/mcp"
OTHER = Caller("other", "orchestrator", "other-project")


def test_idle_sessions_end_and_free_their_place() -> None:
    now = [0.0]
    sessions = Sessions(limit=2, idle=60, clock=lambda: now[0])
    first = sessions.add(ORCHESTRATOR, "papers", URL, "2025-11-25", None)
    sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    assert not sessions.has_room(OTHER)
    with pytest.raises(SessionsFullError):
        sessions.add(OTHER, "papers", URL, "2025-11-25", None)
    now[0] = 30.0
    assert sessions.get(first.id, ORCHESTRATOR, "papers") is first  # use keeps it alive
    now[0] = 61.0
    assert sessions.has_room(OTHER)  # the worker's session ended
    assert len(sessions) == 1
    now[0] = 200.0
    assert sessions.get(first.id, ORCHESTRATOR, "papers") is None
    assert len(sessions) == 0


def test_a_callers_new_session_ends_its_least_recently_used_one() -> None:
    now = [0.0]
    sessions = Sessions(limit=10, per_caller=2, idle=600, clock=lambda: now[0])
    first = sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    now[0] = 1.0
    second = sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    now[0] = 2.0
    assert sessions.get(first.id, WORKER, "papers") is first  # now the second is older
    other = sessions.add(ORCHESTRATOR, "papers", URL, "2025-11-25", None)
    third = sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    assert sessions.get(second.id, WORKER, "papers") is None
    assert sessions.get(first.id, WORKER, "papers") is first
    assert sessions.get(third.id, WORKER, "papers") is third
    assert sessions.get(other.id, ORCHESTRATOR, "papers") is other


def test_a_full_proxy_takes_a_callers_place_from_its_own_sessions() -> None:
    sessions = Sessions(limit=2, per_caller=5, idle=600)
    worker = sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    sessions.add(ORCHESTRATOR, "papers", URL, "2025-11-25", None)
    # The worker holds a session, so it has room: its new session ends its old one.
    assert sessions.has_room(WORKER)
    sessions.add(WORKER, "papers", URL, "2025-11-25", None)
    assert sessions.get(worker.id, WORKER, "papers") is None
    assert len(sessions) == 2
    with pytest.raises(SessionsFullError):
        sessions.add(OTHER, "papers", URL, "2025-11-25", None)


def test_names_too_long_to_call_are_not_kept() -> None:
    tools = AdvertisedTools()
    tools.page(None, ["a", "b" * 257], None)
    assert tools.names == {"a"}
