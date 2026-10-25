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
        self.receipts: dict[str, tuple[tuple[object, ...], object]] = {}

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

    def _once(self, operation: str, request: tuple[object, ...], answer: object) -> object:
        """As the worker keeps receipts: a replay answers the first result, another
        request under the same operation ID is a conflict."""
        kept = self.receipts.get(operation)
        if kept is None:
            self.receipts[operation] = (request, answer)
            return answer
        if kept[0] != request:
            raise WorkerAPIError(ErrorKind.CONFLICT, "operation ID names another change", 409)
        return kept[1]

    async def accept_classification_draft(
        self, project: str, server: str, draft: str, yaml: str, reason: str, operation_id: str, /
    ) -> object:
        request = ("accept", project, server, draft, yaml, reason, operation_id)
        self.calls.append(request)
        self._check()
        answer = {"revision": 3, "accepted_at": "2026-10-25T10:00:00Z", "widenings": ["scrape"]}
        return self._once(operation_id, request[:-1], answer)

    async def accept_drift(
        self, project: str, server: str, reason: str, operation_id: str, /
    ) -> object:
        request = ("drift", project, server, reason, operation_id)
        self.calls.append(request)
        self._check()
        return self._once(operation_id, request[:-1], {"accepted_at": "2026-10-25T10:05:00Z"})


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


async def test_accepting_a_draft_passes_its_operation_id_to_the_worker(
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
    assert first == {"revision": 3, "accepted_at": "2026-10-25T10:00:00Z", "widenings": ["scrape"]}
    assert fake.calls[-1] == ("accept", "p1", "papers", DRAFT, body["yaml"], "Reviewed", "op-1")
    # The worker keeps the receipt: a retry reaches it again and gets the first answer.
    assert await post(body) == first
    # Another request under the same ID: the worker's conflict is the workspace's 409.
    refused = await post({**body, "reason": "Another"}, 409)
    assert refused["error"]["kind"] == "conflict"
    # Shapes: a reason is required and bounded; no unknown field; a hex draft ID; an ID.
    calls = len(fake.calls)
    for bad in (
        {**body, "operation_id": "op-2", "reason": ""},
        {**body, "operation_id": "op-2", "reason": "   "},
        {**body, "operation_id": "op-2", "reason": "x" * 501},
        {**body, "operation_id": "op-2", "author": "agent"},
        {**body, "operation_id": "-not an id"},
        {"yaml": body["yaml"], "reason": "Reviewed"},
    ):
        await post(bad, 400)
    await post({**body, "operation_id": "op-3"}, 400, path.replace(DRAFT, "NOT-HEX"))
    await post({**body, "operation_id": "op-3"}, 404, path.replace("papers", "elsewhere"))
    # Same origin only.
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        assert (await client.post(path, json=body, headers=headers)).status == 403
    assert len(fake.calls) == calls


async def test_drift_reacceptance_needs_a_reason_and_passes_its_operation_id(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    fake = FakeClassifications()
    client = await _client(aiohttp_client, state_dir, static_dir, fake)
    path = BASE + "/mcp-services/web/drift/accept"
    response = await client.post(
        path, json={"reason": "", "operation_id": "d-1"}, headers=origin(client)
    )
    await error_of(response, 400, "invalid_request")
    assert fake.calls == []
    body = {"reason": "Checked the added tool", "operation_id": "d-1"}
    first = await (await client.post(path, json=body, headers=origin(client))).json()
    assert first == {"accepted_at": "2026-10-25T10:05:00Z"}
    assert fake.calls[-1] == ("drift", "p1", "web", "Checked the added tool", "d-1")
    assert await (await client.post(path, json=body, headers=origin(client))).json() == first
    response = await client.post(
        path, json={**body, "reason": "Something else"}, headers=origin(client)
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
