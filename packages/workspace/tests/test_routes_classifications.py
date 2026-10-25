# SPDX-License-Identifier: MIT
"""The classification review routes pass the worker's records through, keep receipts and
answer the worker's refusals as the workspace's errors (CL-1 to CL-6)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.workspace.classifications import from_worker
from sandbox_science.workspace.mcp_services import ServiceConfig

from .route_support import WORKER_SECRET, error_of, origin, workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
DIGEST = "sha256:" + "d" * 64
PIN = "e" * 64
DRAFT = "ab12" * 16
BASE = "/api/v0/projects/p1"


def _tool(name: str, effect: str, pin_state: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "effect": effect,
        "reason": "Fetches one page.",
        "rules": [{"argument": "actions", "present": True, "equals": None, "effect": "browse"}],
        "significant": {"url": "url"},
        "sha256": PIN,
        "pin_state": pin_state,
        "mentioned": True,
        "escalates": effect != "read",
        **extra,
    }


def _in_force(server: str) -> dict[str, object]:
    return {
        "server": server,
        "source": {
            "kind": "catalog",
            "name": server,
            "revision": None,
            "pinned": "2026-10-24",
            "accepted_at": None,
            "accepted_by": None,
            "reason": None,
        },
        "digest": DIGEST,
        "tools": [
            _tool("scrape", "read", "current"),
            _tool("crawl", "read", "changed"),
            _tool("monitor", "unclassified", "added", mentioned=False, escalates=True),
        ],
        "drift": {
            "at": "2026-10-25T09:00:00Z",
            "pin": DIGEST,
            "added": [{"name": "monitor", "sha256": PIN}],
            "removed": ["old"],
            "changed": [{"name": "crawl", "sha256": PIN}],
            "held": True,
        },
    }


class FakeClassifications:
    """The worker client's classification methods, answering from fixed records."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.refusal: WorkerAPIError | None = None
        self.records: list[Any] = [_in_force("papers"), _in_force("web"), _in_force("elsewhere")]

    def _check(self) -> None:
        if self.refusal is not None:
            raise self.refusal

    async def classifications(self, project: str, /) -> object:
        self.calls.append(("classifications", project))
        self._check()
        return self.records

    async def classification_drafts(self, project: str, server: str, /) -> object:
        self.calls.append(("drafts", project, server))
        self._check()
        return [
            {
                "draft": DRAFT,
                "server": server,
                "created_at": "2026-10-25T08:00:00Z",
                "model": "test-model",
                "yaml": "server: papers\n",
                "tools": [
                    {
                        **_tool("scrape", "write", "current"),
                        "current_effect": "read",
                        "widening": False,
                    }
                ],
            }
        ]

    async def accept_classification_draft(
        self, project: str, server: str, draft: str, yaml: str, reason: str, /
    ) -> object:
        self.calls.append(("accept", project, server, draft, yaml, reason))
        self._check()
        return {"revision": 3, "accepted_at": "2026-10-25T10:00:00Z", "widenings": ["scrape"]}

    async def accept_drift(self, project: str, server: str, reason: str, /) -> object:
        self.calls.append(("drift", project, server, reason))
        self._check()
        return {"accepted_at": "2026-10-25T10:05:00Z"}


async def _client(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, fake: Any
) -> TestClient[Any, Any]:
    return await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(
            ServiceConfig(name="papers", purpose="Papers"),
            ServiceConfig(name="web", purpose="Web pages"),
        ),
        mcp_classifications=fake,
    )


async def test_in_force_views_pass_the_worker_records_through(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    fake = FakeClassifications()
    client = await _client(aiohttp_client, state_dir, static_dir, fake)
    response = await client.get(BASE + "/mcp-services/papers/classification")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == {**_in_force("papers"), "project": "p1"}
    listing = await (await client.get(BASE + "/classifications")).json()
    # Only the configured services, by name; a server the launcher does not configure is not
    # shown even when the worker classifies it.
    assert listing["project"] == "p1"
    assert [item["server"] for item in listing["classifications"]] == ["papers", "web"]
    drafts = await (await client.get(BASE + "/mcp-services/web/classification/drafts")).json()
    assert drafts["server"] == "web"
    assert drafts["drafts"][0]["draft"] == DRAFT
    assert drafts["drafts"][0]["tools"][0]["current_effect"] == "read"
    assert ("drafts", "p1", "web") in fake.calls
    # Unknown or unconfigured service, unknown project, a query: refused before the worker.
    calls = len(fake.calls)
    await error_of(
        await client.get(BASE + "/mcp-services/elsewhere/classification"), 404, "not_found"
    )
    await error_of(
        await client.get(BASE + "/mcp-services/nope/classification/drafts"), 404, "not_found"
    )
    await error_of(
        await client.get("/api/v0/projects/no-such/mcp-services/papers/classification"),
        404,
        "not_found",
    )
    await error_of(
        await client.get(BASE + "/mcp-services/papers/classification?x=1"), 400, "invalid_request"
    )
    assert len(fake.calls) == calls
    # A configured service the worker does not classify has none in force.
    fake.records = [_in_force("web")]
    await error_of(await client.get(BASE + "/mcp-services/papers/classification"), 404, "not_found")
    # Another project configures no services here: an empty list, no worker call.
    other = await (await client.get("/api/v0/projects/p2/classifications")).json()
    assert other == {"project": "p2", "classifications": []}


async def test_accepting_a_draft_keeps_a_receipt_and_sends_once(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    fake = FakeClassifications()
    client = await _client(aiohttp_client, state_dir, static_dir, fake)
    path = BASE + f"/mcp-services/papers/classification/drafts/{DRAFT}/accept"
    body = {"yaml": "server: papers\ntools: {}\n", "reason": "Reviewed", "operation_id": "op-1"}

    async def post(value: dict[str, Any], status: int = 200, url: str = path) -> Any:
        response = await client.post(url, json=value, headers=origin(client))
        assert response.status == status, await response.text()
        return await response.json()

    first = await post(body)
    assert first == {
        "revision": 3,
        "accepted_at": "2026-10-25T10:00:00Z",
        "widenings": ["scrape"],
        "replayed": False,
    }
    assert fake.calls[-1] == ("accept", "p1", "papers", DRAFT, body["yaml"], "Reviewed")
    again = await post(body)
    assert again == {**first, "replayed": True}
    assert [call for call in fake.calls if call[0] == "accept"] == [fake.calls[-1]]
    # The same operation ID for another request is a conflict.
    await post({**body, "reason": "Another"}, 409)
    # Shapes: a reason is required and bounded; no unknown field; a hex draft ID.
    for bad in (
        {**body, "operation_id": "op-2", "reason": ""},
        {**body, "operation_id": "op-2", "reason": "   "},
        {**body, "operation_id": "op-2", "reason": "x" * 501},
        {**body, "operation_id": "op-2", "author": "agent"},
        {"yaml": body["yaml"], "reason": "Reviewed"},
    ):
        await post(bad, 400)
    await post({**body, "operation_id": "op-3"}, 400, path.replace(DRAFT, "NOT-HEX"))
    await post({**body, "operation_id": "op-3"}, 404, path.replace("papers", "elsewhere"))
    # Same origin only.
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        assert (await client.post(path, json=body, headers=headers)).status == 403


async def test_drift_reacceptance_needs_a_reason_and_keeps_a_receipt(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    fake = FakeClassifications()
    client = await _client(aiohttp_client, state_dir, static_dir, fake)
    path = BASE + "/mcp-services/web/drift/accept"
    response = await client.post(
        path, json={"reason": "", "operation_id": "d-1"}, headers=origin(client)
    )
    await error_of(response, 400, "invalid_request")
    body = {"reason": "Checked the added tool", "operation_id": "d-1"}
    first = await (await client.post(path, json=body, headers=origin(client))).json()
    assert first == {"accepted_at": "2026-10-25T10:05:00Z", "replayed": False}
    again = await (await client.post(path, json=body, headers=origin(client))).json()
    assert again["replayed"] is True
    assert [call for call in fake.calls if call[0] == "drift"] == [
        ("drift", "p1", "web", "Checked the added tool")
    ]
    # A draft acceptance's receipt never answers a re-acceptance under another kind.
    draft = BASE + f"/mcp-services/web/classification/drafts/{DRAFT}/accept"
    response = await client.post(
        draft,
        json={"yaml": "x: 1\n", "reason": body["reason"], "operation_id": "d-1"},
        headers=origin(client),
    )
    await error_of(response, 409, "conflict")


async def test_worker_refusals_answer_as_workspace_errors(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    fake = FakeClassifications()
    client = await _client(aiohttp_client, state_dir, static_dir, fake)
    path = BASE + f"/mcp-services/papers/classification/drafts/{DRAFT}/accept"
    body = {"yaml": "bad: [\n", "reason": "Reviewed", "operation_id": "r-1"}
    fake.refusal = WorkerAPIError(ErrorKind.INVALID_REQUEST, "line 1: not a classification", 400)
    error = await error_of(
        await client.post(path, json=body, headers=origin(client)), 400, "invalid_request"
    )
    assert error["message"] == "line 1: not a classification"
    fake.refusal = WorkerAPIError(ErrorKind.POLICY_DENIED, "only an admin accepts", 403)
    await error_of(
        await client.post(path, json={**body, "operation_id": "r-2"}, headers=origin(client)),
        403,
        "policy_denied",
    )
    # A failed write keeps no receipt: once the worker takes it, the same ID goes through.
    fake.refusal = None
    accepted = await client.post(path, json={**body, "operation_id": "r-2"}, headers=origin(client))
    assert (await accepted.json())["replayed"] is False
    fake.refusal = WorkerAPIError(ErrorKind.INTERNAL, WORKER_SECRET, 500)
    response = await client.get(BASE + "/mcp-services/papers/classification")
    await error_of(response, 503, "unavailable")
    assert WORKER_SECRET not in await response.text()
    fake.refusal = WorkerAPIError(ErrorKind.NOT_FOUND, "no such draft", 404)
    await error_of(
        await client.get(BASE + "/mcp-services/papers/classification/drafts"), 404, "not_found"
    )
    # A worker answer of the wrong shape is unavailable, not passed on.
    fake.refusal = None
    fake.records = [{"tools": []}]
    await error_of(await client.get(BASE + "/classifications"), 503, "unavailable")


async def test_without_the_worker_routes_classifications_are_unavailable(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await _client(aiohttp_client, state_dir, static_dir, None)
    await error_of(await client.get(BASE + "/classifications"), 503, "unavailable")
    await error_of(
        await client.post(
            BASE + "/mcp-services/papers/drift/accept",
            json={"reason": "r", "operation_id": "n-1"},
            headers=origin(client),
        ),
        503,
        "unavailable",
    )


def test_a_worker_client_is_a_source_only_with_every_route() -> None:
    class Partial:
        async def classifications(self, project: str) -> object:
            return []

    fake = FakeClassifications()
    assert from_worker(fake) is fake  # pyright: ignore[reportArgumentType]
    assert from_worker(Partial()) is None  # pyright: ignore[reportArgumentType]
    assert from_worker(None) is None
