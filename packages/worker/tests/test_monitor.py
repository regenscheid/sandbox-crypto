# SPDX-License-Identifier: MIT
"""A project's monitor figures (D103): counted by code from the policy middleware's and
the MCP proxy's records in the event store, newest first, within a window and a record
cap, and served to an operator client only."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import EnvironmentInfo
from sandbox_science.contracts.monitor import WorkerMonitor
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.event_store import EventStore
from sandbox_science.policy.approvals import RequestKey
from sandbox_science.testing import FakeClock
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.classifications import ClassificationDesk
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.monitor import MonitorDesk
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

POOL = "test-secret-monitor-pool-token-0000"
TOKENS = {name: f"test-secret-monitor-{name}-0000" for name in ("workspace", "harness")}
DIGEST = "sha256:" + "b" * 64
ARGS = "c" * 64
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MIDDLEWARE = "policy-middleware"
PROXY = "mcp-proxy"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def environment(env: str, project: str, sandbox: str, **extra: Any) -> EnvironmentInfo:
    fields: dict[str, Any] = {
        "environment_id": env, "project_id": project, "profile": "minimal", "image": "img",
        "sandbox": env, "sandbox_id": sandbox, "policy": DIGEST, "state": "ready",
        "created_at": NOW, "updated_at": NOW, **extra,
    }  # fmt: skip
    return EnvironmentInfo.model_validate(fields)


OWNED = [
    environment("env1", "p1", "sbx-1"),
    environment("job1", "p1", "sbx-j", kind="job", job_id="exe-1", state="stopped"),
    environment("env2", "p2", "sbx-2"),
]


def listed(projects: Iterable[str] | None) -> list[EnvironmentInfo]:
    wanted = None if projects is None else set(projects)
    return [info for info in OWNED if wanted is None or info.project_id in wanted]


class Clocked:
    """An event store whose records carry the times the test sets."""

    def __init__(self, path: Path) -> None:
        self.at = NOW - timedelta(hours=1)
        self.store = EventStore(path, timeout=5, clock=lambda: self.at)

    def add(self, kind: str, payload: dict[str, object], *, source: str = MIDDLEWARE) -> int:
        self.at += timedelta(seconds=1)
        return self.store.append(kind, source, payload).seq


def mcp_call(
    project: str = "p1",
    *,
    action: str = "allow",
    effect: str | None = "read",
    outcome: str = "admitted",
    reason: str | None = None,
    server: str = "papers",
    request: int | None = None,
) -> dict[str, object]:
    decision: dict[str, object] = {"action": action, "layer": "policy", "rule": "r"}
    if effect is not None:
        decision.update(effect=effect, max_grant="session")
    payload: dict[str, object] = {
        "project": project, "caller": "agent-runtime", "sandbox_id": "sbx-a", "session": "s-1",
        "run": "main-1", "server": server, "method": "tools/call", "tool": "search",
        "decision": decision, "reason_code": reason, "outcome": outcome, "sizes": {"body": 10},
    }  # fmt: skip
    if request is not None:
        payload["permission_request"] = request
    return payload


def escalation(key: RequestKey, *, timeout: int = 3600, **extra: object) -> dict[str, object]:
    return {
        "target": key.payload(),
        "escalation": {"timeout_seconds": timeout, "on_timeout": "deny"},
        "rule": "writes",
        **extra,
    }


def answer(seq: int, key: RequestKey, scope: str, **extra: object) -> dict[str, object]:
    return {"request_seq": seq, "scope": scope, "target": key.payload(), "expires": None, **extra}


def http(host: str, path: str = "/a", sandbox: str = "sbx-1") -> RequestKey:
    return RequestKey.http(sandbox, DIGEST, "GET", host, 443, path, "")


def mcp(project: str = "p1", tool: str = "send") -> RequestKey:
    return RequestKey.mcp(project, "agent-runtime", DIGEST, "papers", "tools/call", tool, ARGS)


def passed(host: str, sandbox: str = "sbx-1", **extra: object) -> dict[str, object]:
    return {
        "sandbox_id": sandbox, "request_id": "r", "rule": "reads", "reason": "ok",
        "request": {"method": "GET", "scheme": "https", "host": host, "port": 443,
                    "path": "/x", "query_bytes": 0},
        **extra,
    }  # fmt: skip


class Drifts:
    """The classification review's reading, as the monitor asks it."""

    def in_force(self, project: str) -> list[SimpleNamespace]:
        drift = SimpleNamespace(at=NOW - timedelta(hours=2), held=True)
        settled = SimpleNamespace(at=NOW - timedelta(hours=3), held=False)
        return [
            SimpleNamespace(server="papers", drift=drift),
            SimpleNamespace(server="web", drift=settled),
            SimpleNamespace(server="files", drift=None),
        ]


def desk(path: Path, *, cap: int = 100_000, drifts: object = None) -> MonitorDesk:
    return MonitorDesk(
        Permissions(path),
        listed,
        declared_tier=3,
        measured_tier=lambda environment_id: 2 if environment_id == "env1" else None,
        classifications=cast(ClassificationDesk | None, drifts),
        clock=FakeClock(NOW),
        max_scanned=cap,
    )


def row(found: WorkerMonitor, effect: str, server: str | None = None, host: str | None = None):
    assert found.records is not None
    (match,) = [
        item
        for item in found.records.calls
        if (item.effect, item.server, item.host) == (effect, server, host)
    ]
    return (match.allowed, match.escalated, match.denied, match.held)


def test_mcp_calls_are_counted_by_class_and_server_with_each_outcome(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    events.add("mcp-call", mcp_call())  # a read, allowed
    events.add("mcp-call", mcp_call(action="alert"))  # allowed with an alert
    events.add("mcp-call", mcp_call(action="deny", outcome="refused", reason="policy_denied"))
    seq = events.add("escalation", escalation(mcp(), effect="write", server="papers"))
    for _ in range(3):  # the run service's retries, while the request waits
        refused = mcp_call(
            action="escalate", effect="write", outcome="refused",
            reason=f"approval_required_r{seq}", request=seq,
        )  # fmt: skip
        events.add("mcp-call", refused)
    events.add("mcp-call", mcp_call(action="escalate", effect="write"))  # approved: went through
    events.add(
        "mcp-call",
        mcp_call(action="escalate", effect="write", outcome="refused", reason="approval_denied"),
    )
    events.add("mcp-call", mcp_call(outcome="refused", reason="session_held_r12"))
    drift = escalation(mcp(tool="post"), effect="write", rule="detect.tool-list-drift")
    events.add("escalation", drift)
    events.add("mcp-call", {**mcp_call(effect=None), "method": "tools/list", "tool": None})
    # The proxy's record names the project in its attribution.
    proxied = mcp_call(project="other", outcome="forwarded", server="papers")
    proxied["attribution"] = {"project": "p1", "caller": "run-service"}
    del proxied["project"]
    events.add("mcp-call", proxied, source=PROXY)
    events.add("mcp-call", mcp_call(project="p2"))  # another project's
    events.add("escalation", escalation(mcp("p2"), effect="write"))
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    assert found.records.sources == ["policy-middleware", "mcp-proxy"]
    assert row(found, "read", "papers") == (3, 0, 1, 1)
    assert row(found, "write", "papers") == (1, 1, 1, 1)
    assert row(found, "none", "papers") == (1, 0, 0, 0)
    assert found.records.truncated is False


def test_research_codes_requests_are_counted_by_host_for_the_sandboxs_project(
    tmp_path: Path,
) -> None:
    events = Clocked(tmp_path / "events.db")
    events.add("escalation", escalation(http("data.org"), caller_kind="code"))
    events.add("escalation", escalation(http("data.org", "/b")))
    action = http("data.org", "/confirm/abcdef0123456789abcdef")
    events.add("escalation", escalation(action, rule="detect.action-url"))
    events.add("approved", passed("data.org", approval_seq=9, request_seq=1))
    events.add("alert", passed("data.org"))
    cache = http("cache.local", "/pypi/f/x.whl")
    events.add("escalation", escalation(cache, package={"ecosystem": "pypi", "name": "x"}))
    events.add("escalation", escalation(http("data.org", sandbox="sbx-2")))  # p2's sandbox
    events.add("alert", passed("data.org", sandbox="sbx-unknown"))  # nobody's
    events.add("alert", {**passed("papers.org"), "server": "papers"})  # an MCP call's
    found = desk(events.store.path).figures("p1", "24h")
    assert row(found, "network", host="data.org") == (2, 2, 0, 1)
    assert row(found, "packages", host="cache.local") == (0, 1, 0, 0)
    assert found.records is not None
    assert {item.host for item in found.records.calls} == {"data.org", "cache.local"}


def test_approvals_are_counted_by_kind_and_how_they_stand(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    asked = events.add("escalation", escalation(mcp(), effect="write", server="papers"))
    events.at += timedelta(seconds=59)
    events.add("approval", answer(asked, mcp(), "call"))
    refused = events.add("escalation", escalation(http("a.org")))
    events.at += timedelta(seconds=19)
    events.add("denial", answer(refused, http("a.org"), "request"))
    events.add("denial", answer(refused, http("a.org"), "request"))  # a second
    events.add("escalation", escalation(http("b.org"), timeout=60))
    files = RequestKey.packages("sbx-1", DIGEST, "op-1", ["/conda/c/x.conda"])
    withdrawn = events.add("escalation", escalation(files))
    events.add("denial", answer(withdrawn, files, "files", withdrawn=True))
    stamp = "2026-10-25T11:00:00.000000+00:00"
    held = RequestKey.hold("p1", "sbx-a", "s-1", "same-url", stamp)
    events.add("escalation", escalation(held, timeout=30 * 86400, rule="detect.same-url"))
    events.add("escalation", escalation(mcp("p2")))  # another project's
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    counts = {
        item.kind: (
            item.waiting,
            item.approved,
            item.denied,
            item.expired,
            item.median_wait_seconds,
        )
        for item in found.records.approvals
    }
    assert counts == {
        "mcp": (0, 1, 0, 0, 60.0),
        "network": (0, 0, 1, 1, 20.0),  # b.org's lapsed unanswered
        "packages": (0, 0, 0, 1, None),
        "hold": (1, 0, 0, 0, None),
    }


def test_open_holds_and_drifted_servers_are_listed(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    stamp = "2026-10-20T11:00:00.000000+00:00"
    agent = RequestKey.hold("p1", "sbx-a", "s-1", "same-url", stamp)
    code = RequestKey.hold("", "sbx-1", "", "same-url", stamp)
    rate = RequestKey.hold("p1", "sbx-a", "s-2", "host-rate", stamp)
    cleared = RequestKey.hold("p1", "sbx-a", "s-3", "host-rate", stamp)
    for key in (agent, code, rate, cleared, RequestKey.hold("p2", "sbx-b", "s-9", "x", stamp)):
        events.add("escalation", escalation(key, timeout=30 * 86400, rule="detect.r"))
    events.add("approval", answer(4, cleared, "session"))
    found = desk(events.store.path, drifts=Drifts()).figures("p1", "24h")
    assert found.records is not None
    holds = {item.rule: (item.count, item.sessions) for item in found.records.holds}
    assert holds == {"detect.host-rate": (1, ["s-2"]), "detect.same-url": (2, ["s-1"])}
    assert [(item.server, item.at) for item in found.records.drift] == [
        ("papers", NOW - timedelta(hours=2))
    ]


def test_the_window_bounds_what_is_counted(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    events.at = NOW - timedelta(days=8)  # older than either window: never counted
    events.add("mcp-call", mcp_call())
    events.at = NOW - timedelta(days=3)
    events.add("mcp-call", mcp_call())
    events.add("escalation", escalation(mcp(), effect="write"))
    events.at = NOW - timedelta(hours=2)
    events.add("mcp-call", mcp_call())
    day = desk(events.store.path).figures("p1", "24h")
    week = desk(events.store.path).figures("p1", "7d")
    assert row(day, "read", "papers") == (1, 0, 0, 0)
    assert day.records is not None
    assert day.records.scanned == 1
    assert row(week, "read", "papers") == (2, 0, 0, 0)
    assert (week.window, week.since) == ("7d", NOW - timedelta(days=7))
    assert week.records is not None
    assert {item.kind: item.expired for item in week.records.approvals}["mcp"] == 1


def test_the_record_cap_bounds_the_scan_and_says_so(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    for _ in range(5):
        events.add("mcp-call", mcp_call())
    for _ in range(3):
        events.add("monitor", {"rule": "egress"})  # another kind, counted by the cap
    capped = desk(events.store.path, cap=5).figures("p1", "24h")
    assert capped.records is not None
    assert (capped.records.scanned, capped.records.truncated) == (2, True)
    assert row(capped, "read", "papers") == (2, 0, 0, 0)
    whole = desk(events.store.path, cap=8).figures("p1", "24h")
    assert whole.records is not None
    assert (whole.records.scanned, whole.records.truncated) == (5, False)
    # A cap that reaches past the window's start cut nothing.
    old = Clocked(tmp_path / "old.db")
    old.at = NOW - timedelta(days=2)
    for _ in range(3):
        old.add("mcp-call", mcp_call())
    old.at = NOW - timedelta(hours=1)
    old.add("mcp-call", mcp_call())
    bounded = desk(old.store.path, cap=2).figures("p1", "24h")
    assert bounded.records is not None
    assert (bounded.records.scanned, bounded.records.truncated) == (1, False)


def test_environments_carry_their_tiers_and_no_store_is_said(tmp_path: Path) -> None:
    found = desk(tmp_path / "missing.db").figures("p1", "24h")
    assert found.records is not None
    assert found.records.scanned == 0
    tiers = [
        (item.environment_id, item.kind, item.state, item.declared_tier, item.tier, item.job_id)
        for item in found.environments
    ]
    assert tiers == [
        ("env1", "session", "ready", 3, 2, None),
        ("job1", "job", "stopped", 3, None, "exe-1"),
    ]
    none = MonitorDesk(
        None, listed, declared_tier=None, measured_tier=lambda _: None, clock=FakeClock(NOW)
    ).figures("p1", "7d")
    assert none.records is None
    assert none.unavailable == "the worker has no policy middleware event store"


def headers(name: str | None = None) -> dict[str, str]:
    token = POOL if name is None else TOKENS[name]
    return {"Authorization": f"Bearer {token}", PROTOCOL_HEADER: str(PROTOCOL_VERSION)}


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], Clocked]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    events = Clocked(tmp_path / "events.db")
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=listed),
        ),
    )  # fmt: skip
    clients = [
        ClientSettings(
            name=name, token_sha256=token_digest(token), projects=("p1",),
            operator=name == "workspace",
        )
        for name, token in TOKENS.items()
    ]  # fmt: skip
    figures = desk(events.store.path)
    client = await aiohttp_client(
        create_app(manager, POOL, environments, clients, monitor_desk=figures)
    )
    try:
        yield client, events
    finally:
        assert figures.permissions is not None
        figures.permissions.close()
        events.store.close()
        store.close()


async def test_the_route_serves_an_operator_client_its_projects_figures(
    api: tuple[TestClient[Any, Any], Clocked],
) -> None:
    client, events = api
    events.add("mcp-call", mcp_call())
    answered = await client.get("/v1/monitor?project=p1&window=7d", headers=headers("workspace"))
    assert answered.status == 200, await answered.text()
    found = WorkerMonitor.model_validate(await answered.json())
    assert (found.project_id, found.window, found.at) == ("p1", "7d", NOW)
    assert row(found, "read", "papers") == (1, 0, 0, 0)


@pytest.mark.parametrize(
    ("query", "who", "status"),
    [
        ("project=p1&window=24h", "harness", 403),  # the run service's token counts nothing
        ("project=p2&window=24h", "workspace", 403),  # not this client's project
        ("project=p1&window=30d", "workspace", 400),
        ("project=p1", "workspace", 400),
        ("project=p1&window=24h&extra=1", "workspace", 400),
        ("project=..&window=24h", "workspace", 400),
    ],
)
async def test_the_route_refuses_other_clients_projects_and_windows(
    api: tuple[TestClient[Any, Any], Clocked], query: str, who: str, status: int
) -> None:
    client, _ = api
    answered = await client.get(f"/v1/monitor?{query}", headers=headers(who))
    assert answered.status == status, await answered.text()


def test_answer_alerts_and_blocks_are_counted_by_server(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    alert: dict[str, object] = {
        "project": "p1",
        "server": "papers",
        "request_id": "r",
        "action": "alert",
    }
    events.add("mcp-answer-alert", alert)
    events.add("mcp-answer-alert", alert)
    events.add("mcp-answer-alert", {**alert, "action": "block"})
    events.add("mcp-answer-alert", {**alert, "server": "web", "action": "block"})
    events.add("mcp-answer-alert", {**alert, "project": "p2"})
    events.add("mcp-answer-alert", {**alert, "action": "uninspected", "reason": "capacity"})
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    assert [
        (row.server, row.alerts, row.blocks, row.uninspected) for row in found.records.inspection
    ] == [("papers", 2, 1, 1), ("web", 0, 1, 0)]
    assert found.records.inspection_enabled is None  # the middleware never said
    events.add(
        "mcp-inspection-dropped",
        {
            "answers": 7,
            "counts": [
                {"project": "p1", "sandbox_id": "sbx-a", "answers": 5},
                {"project": "p2", "sandbox_id": "sbx-b", "answers": 2},
            ],
        },
    )
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    assert found.records.inspection_dropped == 5
    events.add("mcp-inspection-state", {"enabled": True})
    events.add("mcp-inspection-state", {"enabled": False})  # the latest start's word
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    assert found.records.inspection_enabled is False


def test_the_inspection_state_is_found_behind_later_records_of_other_kinds(
    tmp_path: Path,
) -> None:
    events = Clocked(tmp_path / "events.db")
    events.add("mcp-inspection-state", {"enabled": True})
    alert: dict[str, object] = {"project": "p1", "server": "papers", "action": "alert"}
    for _ in range(3):
        events.add("mcp-answer-alert", alert)
    events.add("mcp-inspection-dropped", {"answers": 1, "counts": []})
    found = desk(events.store.path).figures("p1", "24h")
    assert found.records is not None
    assert found.records.inspection_enabled is True
    # The search is bounded like the scan: past the cap the state is unknown, not guessed.
    found = desk(events.store.path, cap=4).figures("p1", "24h")
    assert found.records is not None
    assert found.records.inspection_enabled is None
