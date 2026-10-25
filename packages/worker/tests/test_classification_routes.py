# SPDX-License-Identifier: MIT
"""The classification review's worker routes (D96, CL-1 to CL-6): the classification in
force with pin states and the middleware's drift, drafts beside it, and the user's
acceptances, operator-only, over the policy store and the middleware's event store."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.event_store import EventStore, StoreReader
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.policy import LoadedPolicy
from sandbox_science.policy.classification import catalog
from sandbox_science.policy.detection import pin_digest
from sandbox_science.policy.store import PolicyStore
from sandbox_science.testing import FakeClock
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.classifications import ClassificationDesk
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permissions import Permissions, PermissionStoreError
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

POOL = "test-secret-classifications-pool-token-0000"
TOKENS = {name: f"test-secret-classifications-{name}-0000" for name in ("workspace", "harness")}
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SERVER = "crypto-research-mcp"
SCRAPE = "firecrawl-mcp_firecrawl_scrape"
FEEDBACK = "firecrawl-mcp_firecrawl_feedback"
EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "policy.yaml"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def headers(name: str | None = None) -> dict[str, str]:
    token = POOL if name is None else TOKENS[name]
    return {"Authorization": f"Bearer {token}", PROTOCOL_HEADER: str(PROTOCOL_VERSION)}


def classification(*tools: tuple[str, str, str | None]) -> str:
    """A classification file of ``(name, class, sha256)`` entries."""
    lines = [
        "apiVersion: sandbox-science/v0alpha1",
        "kind: ToolClassification",
        f"server: {SERVER}",
        "tools:",
    ]
    for name, effect, digest in tools:
        lines += [f"  {name}:", f"    class: {effect}", '    reason: "Reviewed."']
        if digest is not None:
            lines.append(f"    sha256: {digest}")
    return "\n".join(lines) + "\n"


class Api:
    def __init__(
        self, client: TestClient[Any, Any], store: PolicyStore, events: EventStore, desk: Any
    ) -> None:
        self.client, self.store, self.events, self.desk = client, store, events, desk
        self.operations = 0

    async def get(self, path: str, who: str | None = "workspace") -> Any:
        response = await self.client.get(path, headers=headers(who))
        return response.status, await response.json()

    async def post(
        self, path: str, body: Mapping[str, object], who: str | None = "workspace"
    ) -> Any:
        self.operations += 1
        body = {"operation_id": f"op-{self.operations}", **body}
        response = await self.client.post(path, data=json.dumps(body), headers=headers(who))
        return response.status, await response.json()

    def records(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        with StoreReader(self.events.path) as reader:
            return [(r.source, r.data()) for r in reader.records() if r.kind == kind]

    def draft(self, text: str) -> str:
        return self.store.add_classification_draft(
            "p1", SERVER, text.encode(), created_at=NOW, model="main"
        ).draft


@pytest_asyncio.fixture
async def api(aiohttp_client: ClientFactory, tmp_path: Path) -> AsyncIterator[Api]:
    worker = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        worker, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    root = tmp_path / "middleware"
    store = PolicyStore(root)
    loaded = store.add_policy(EXAMPLE.read_bytes())
    policies: dict[str, LoadedPolicy] = {"p1": loaded}
    events = EventStore(root / "events.db", timeout=5)
    permissions = Permissions(root / "events.db")
    desk = ClassificationDesk(
        store, permissions, policies.get, worker.classification_receipts, FakeClock(NOW)
    )
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=lambda _projects: []),
        ),
    )  # fmt: skip
    clients = [
        ClientSettings(
            name=name, token_sha256=token_digest(token), projects=("p1",),
            operator=name == "workspace",
        )
        for name, token in TOKENS.items()
    ]  # fmt: skip
    client = await aiohttp_client(
        create_app(manager, POOL, environments, clients, classification_desk=desk)
    )
    try:
        yield Api(client, store, events, desk)
    finally:
        permissions.close()
        events.close()
        worker.close()


async def test_the_catalogs_classification_is_in_force_tool_by_tool(api: Api) -> None:
    status, found = await api.get("/v1/classifications?project=p1")
    assert status == 200, found
    [shown] = found
    assert shown["server"] == SERVER
    assert shown["source"] == {
        "kind": "catalog", "name": SERVER, "revision": None, "pinned": "2026-10-24",
        "accepted_at": None, "accepted_by": None, "reason": None,
    }  # fmt: skip
    assert shown["digest"] == catalog(SERVER).digest
    assert shown["drift"] is None
    tools = {tool["name"]: tool for tool in shown["tools"]}
    assert set(tools) == set(catalog(SERVER).tools)
    scrape = tools[SCRAPE]
    assert (scrape["effect"], scrape["pin_state"], scrape["mentioned"]) == ("read", "current", True)
    assert scrape["escalates"] is False
    assert {"argument": "actions", "present": True, "equals": None, "effect": "browse"} in (
        scrape["rules"]
    )
    assert {"argument": "profile.saveChanges", "present": None, "equals": "true",
            "effect": "write"} in scrape["rules"]  # fmt: skip
    assert scrape["significant"] == {"url": "url"}
    assert tools[FEEDBACK]["escalates"] is True  # a write asks under the example policy
    status, refused = await api.get("/v1/classifications?project=p1&x=1")
    assert (status, refused["error"]["kind"]) == (400, "invalid_request")


async def test_only_an_operator_client_reads_or_accepts_for_its_own_projects(api: Api) -> None:
    status, _ = await api.get("/v1/classifications?project=p1", who="harness")
    assert status == 403
    status, _ = await api.get("/v1/classifications?project=p2")
    assert status == 403
    status, found = await api.get("/v1/classifications?project=p2", who=None)
    assert (status, found) == (200, [])  # the pool's token; p2 has no policy
    status, _ = await api.get(f"/v1/classifications/{SERVER}/drafts?project=p1", who="harness")
    assert status == 403
    draft = api.draft(classification((FEEDBACK, "admin", None)))
    body = {"project": "p1", "yaml": classification((FEEDBACK, "admin", None)), "reason": "r"}
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    assert (await api.post(path, body, who="harness"))[0] == 403
    assert (await api.post(path, {**body, "project": "p2"}))[0] == 403
    drift = f"/v1/classifications/{SERVER}/drift/accept"
    assert (await api.post(drift, {"project": "p1", "reason": "r"}, who="harness"))[0] == 403
    assert api.store.classification("p1", SERVER) is None
    assert api.records("mcp-tools-accepted") == []


async def test_a_drift_shows_its_tools_and_is_re_accepted_with_a_reason(api: Api) -> None:
    pins = catalog(SERVER).pins
    api.events.append(
        "mcp-tools-drift",
        "policy-middleware",
        {
            "project": "p1", "server": SERVER, "pin": pin_digest(pins),
            "finding": "tool_list_drift", "added": ["brand_new"], "removed": [],
            "changed": [SCRAPE], "digests": {"brand_new": "c" * 64, SCRAPE: "d" * 64},
            "hints": {"brand_new": "read"}, "listing": "e" * 64,
        },
    )  # fmt: skip
    drift_path = f"/v1/classifications/{SERVER}/drift/accept"
    _, [shown] = await api.get("/v1/classifications?project=p1")
    drift = shown["drift"]
    assert drift["held"] is True
    assert drift["added"] == [{"name": "brand_new", "sha256": "c" * 64}]
    assert drift["changed"] == [{"name": SCRAPE, "sha256": "d" * 64}]
    tools = {tool["name"]: tool for tool in shown["tools"]}
    assert tools[SCRAPE]["pin_state"] == "changed"
    new = tools["brand_new"]
    assert (new["mentioned"], new["effect"], new["pin_state"], new["escalates"]) == (
        False,
        "read",
        "added",
        False,
    )  # fmt: skip  (its read-only hint makes it a read, which the policy allows)
    status, accepted = await api.post(drift_path, {"project": "p1", "reason": "reviewed"})
    assert status == 201, accepted
    assert accepted == {"accepted_at": NOW.isoformat().replace("+00:00", "Z")}
    [(source, record)] = api.records("mcp-tools-accepted")
    assert source == "workspace"
    assert record == {
        "project": "p1", "server": SERVER, "reason": "reviewed", "actor": "user",
        "operation_id": "op-1", "pin": pin_digest(pins), "listing": "e" * 64,
    }  # fmt: skip
    _, [again] = await api.get("/v1/classifications?project=p1")
    assert again["drift"]["held"] is False


async def test_re_accepting_needs_a_drift_and_a_known_server(api: Api) -> None:
    body = {"project": "p1", "reason": "r"}
    status, _ = await api.post(f"/v1/classifications/{SERVER}/drift/accept", body)
    assert status == 409
    status, _ = await api.post("/v1/classifications/other/drift/accept", body)
    assert status == 404
    status, _ = await api.post(f"/v1/classifications/{SERVER}/drift/accept", {"project": "p1"})
    assert status == 400


async def test_drafts_are_listed_beside_the_classification_in_force(api: Api) -> None:
    pin = catalog(SERVER).tools[FEEDBACK].sha256
    draft = api.draft(classification((FEEDBACK, "read", pin), (SCRAPE, "write", None)))
    status, found = await api.get(f"/v1/classifications/{SERVER}/drafts?project=p1")
    assert status == 200, found
    [shown] = found
    assert (shown["draft"], shown["model"], shown["server"]) == (draft, "main", SERVER)
    assert shown["yaml"].startswith("apiVersion")
    tools = {tool["name"]: tool for tool in shown["tools"]}
    feedback, scrape = tools[FEEDBACK], tools[SCRAPE]
    assert (feedback["current_effect"], feedback["widening"], feedback["pin_state"]) == (
        "write", True, "current",
    )  # fmt: skip
    assert (scrape["current_effect"], scrape["widening"], scrape["pin_state"]) == (
        "read", False, "unpinned",
    )  # fmt: skip
    status, _ = await api.get("/v1/classifications/other/drafts?project=p1")
    assert status == 404


async def test_accepting_an_edited_draft_puts_it_in_force_with_its_record(api: Api) -> None:
    pin = catalog(SERVER).tools[FEEDBACK].sha256
    draft = api.draft(classification((FEEDBACK, "read", pin)))
    edited = classification((FEEDBACK, "admin", pin), (SCRAPE, "read", None))
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    status, accepted = await api.post(path, {"project": "p1", "yaml": edited, "reason": "ok"})
    assert status == 201, accepted
    revision = accepted["revision"]
    assert api.store.classification_binding("p1", SERVER) == revision
    record = api.store.acceptance("p1", SERVER, revision)
    assert record is not None
    assert (record.accepted_by, record.reason, record.draft) == ("user", "ok", draft)
    # Every catalog tool the revision leaves out could become a read by its hints.
    assert "firecrawl-mcp_firecrawl_monitor_create" in accepted["widenings"]
    assert FEEDBACK not in accepted["widenings"]
    [(source, event)] = api.records("mcp-classification-accepted")
    assert source == "workspace"
    assert (event["project"], event["server"], event["revision"], event["draft"]) == (
        "p1", SERVER, revision, draft,
    )  # fmt: skip
    assert (event["reason"], event["actor"], event["replaces"]) == ("ok", "user", None)
    assert event["widenings"] == accepted["widenings"]
    assert event["diff"]["changed"] == [FEEDBACK, SCRAPE]  # scrape loses its pin and rules
    _, [shown] = await api.get("/v1/classifications?project=p1")
    assert shown["source"]["kind"] == "project"
    assert (shown["source"]["revision"], shown["source"]["accepted_by"]) == (revision, "user")
    assert {tool["name"] for tool in shown["tools"]} == {FEEDBACK, SCRAPE}


@pytest.mark.parametrize(
    ("edited", "why"),
    [
        (classification(("not_the_servers", "read", None)), "not known to have"),
        (classification((FEEDBACK, "read", "e" * 64)), "pins no listing gave"),
        (classification((FEEDBACK, "sometimes", None)), "class"),
        ("apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\nserver: other\n"
         "tools: {}\n", "not 'crypto-research-mcp'"),
        ("tools: !!map {}\n", "tags"),
    ],
)  # fmt: skip
async def test_an_edit_is_checked_as_the_loader_checks_a_file(
    api: Api, edited: str, why: str
) -> None:
    draft = api.draft(classification((FEEDBACK, "read", None)))
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    status, refused = await api.post(path, {"project": "p1", "yaml": edited, "reason": "r"})
    assert status == 400, refused
    assert why in refused["error"]["message"]
    assert api.store.classification("p1", SERVER) is None
    assert api.records("mcp-classification-accepted") == []


async def test_an_unknown_draft_is_not_found_and_an_unrecorded_acceptance_is_undone(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"project": "p1", "yaml": classification((FEEDBACK, "admin", None)), "reason": "r"}
    missing = f"/v1/classifications/{SERVER}/drafts/{'0' * 64}/accept"
    assert (await api.post(missing, body))[0] == 404
    assert (await api.post(f"/v1/classifications/{SERVER}/drafts/zz/accept", body))[0] == 400
    draft = api.draft(classification((FEEDBACK, "admin", None)))

    def fail(kind: str, payload: object) -> None:
        raise PermissionStoreError("test: the event store is gone")

    monkeypatch.setattr(api.desk.permissions, "record_acceptance", fail)
    status, _ = await api.post(f"/v1/classifications/{SERVER}/drafts/{draft}/accept", body)
    assert status == 503
    assert api.store.classification("p1", SERVER) is None  # the catalog's again


async def test_a_retried_acceptance_gets_its_first_answer_and_a_reused_id_conflicts(
    api: Api,
) -> None:
    draft = api.draft(classification((FEEDBACK, "admin", None)))
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    body = {
        "operation_id": "accept-1", "project": "p1", "reason": "r",
        "yaml": classification((FEEDBACK, "admin", None)),
    }  # fmt: skip
    status, first = await api.post(path, body)
    assert status == 201, first
    api.desk.clock.advance(60)
    status, again = await api.post(path, body)
    assert (status, again) == (201, first)  # unchanged, accepted_at included
    assert len(api.records("mcp-classification-accepted")) == 1
    other = {**body, "yaml": classification((FEEDBACK, "write", None))}
    status, refused = await api.post(path, other)
    assert (status, refused["error"]["kind"]) == (409, "conflict")
    assert api.store.classification_binding("p1", SERVER) == first["revision"]
    # Another project's operation IDs are its own; the other route's reuse conflicts.
    drift = f"/v1/classifications/{SERVER}/drift/accept"
    status, refused = await api.post(drift, {"operation_id": "accept-1", "project": "p1",
                                             "reason": "r"})  # fmt: skip
    assert status == 409
    api.events.append(
        "mcp-tools-drift", "policy-middleware",
        {"project": "p1", "server": SERVER, "pin": None, "added": ["x"], "removed": [],
         "changed": []},
    )  # fmt: skip
    body = {"operation_id": "drift-1", "project": "p1", "reason": "seen"}
    status, first = await api.post(drift, body)
    assert status == 201, first
    api.desk.clock.advance(60)
    assert await api.post(drift, body) == (201, first)
    assert len(api.records("mcp-tools-accepted")) == 1
    status, _ = await api.post(drift, {**body, "reason": "other words"})
    assert status == 409


async def test_an_acceptance_without_an_operation_id_is_invalid(api: Api) -> None:
    response = await api.client.post(
        f"/v1/classifications/{SERVER}/drift/accept",
        data=json.dumps({"project": "p1", "reason": "r"}),
        headers=headers("workspace"),
    )
    assert response.status == 400
    bad = await api.client.post(
        f"/v1/classifications/{SERVER}/drift/accept",
        data=json.dumps({"project": "p1", "reason": "r", "operation_id": "-bad id"}),
        headers=headers("workspace"),
    )
    assert bad.status == 400


async def test_an_accepted_revision_exports_as_the_bytes_stored(api: Api) -> None:
    text = classification((FEEDBACK, "admin", None))
    draft = api.draft(text)
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    _, accepted = await api.post(path, {"project": "p1", "yaml": text, "reason": "ok"})
    revision = accepted["revision"]
    export = f"/v1/classifications/{SERVER}/revisions/{revision}/export?project=p1"
    response = await api.client.get(export, headers=headers("workspace"))
    assert response.status == 200
    data = await response.read()
    assert data == text.encode()
    assert hashlib.sha256(data).hexdigest() == revision
    assert response.content_type == "application/yaml"
    assert response.headers["X-Classification-Server"] == SERVER
    assert response.headers["X-Classification-Revision"] == revision
    assert response.headers["X-Classification-Accepted-At"] == accepted["accepted_at"].replace(
        "Z", "+00:00"
    )
    # The worker client reads it back and checks its digest.
    client = WorkerClient(str(api.client.make_url("")).rstrip("/"), TOKENS["workspace"])
    try:
        exported = await client.export_classification("p1", SERVER, revision)
    finally:
        await client.close()
    assert (exported.yaml, exported.revision, exported.server) == (data, revision, SERVER)
    assert exported.accepted_at == NOW


async def test_only_the_projects_own_revisions_export(api: Api) -> None:
    text = classification((FEEDBACK, "admin", None))
    draft = api.draft(text)
    path = f"/v1/classifications/{SERVER}/drafts/{draft}/accept"
    _, accepted = await api.post(path, {"project": "p1", "yaml": text, "reason": "ok"})
    revision = accepted["revision"]
    base = f"/v1/classifications/{SERVER}/revisions"
    assert (await api.client.get(f"{base}/{'0' * 64}/export?project=p1",
                                 headers=headers("workspace"))).status == 404  # fmt: skip
    # Another project has no such revision, whatever the token.
    assert (await api.client.get(f"{base}/{revision}/export?project=p2",
                                 headers=headers(None))).status == 404  # fmt: skip
    assert (await api.client.get(f"{base}/{revision}/export?project=p2",
                                 headers=headers("workspace"))).status == 403  # fmt: skip
    assert (await api.client.get(f"{base}/{revision}/export?project=p1",
                                 headers=headers("harness"))).status == 403  # fmt: skip
    assert (await api.client.get(f"{base}/nothex/export?project=p1",
                                 headers=headers("workspace"))).status == 400  # fmt: skip


async def test_a_listed_tool_without_a_hint_classified_read_is_a_widening(api: Api) -> None:
    pins = catalog(SERVER).pins
    api.events.append(
        "mcp-tools-drift", "policy-middleware",
        {"project": "p1", "server": SERVER, "pin": pin_digest(pins), "added": ["brand_new"],
         "removed": [], "changed": [], "digests": {"brand_new": "c" * 64}, "hints": {},
         "listing": "e" * 64},
    )  # fmt: skip
    api.draft(classification(("brand_new", "read", "c" * 64)))
    _, [shown] = await api.get(f"/v1/classifications/{SERVER}/drafts?project=p1")
    tools = {tool["name"]: tool for tool in shown["tools"]}
    new = tools["brand_new"]
    assert (new["widening"], new["current_effect"]) == (True, None)
    assert new["widening_reasons"] == ["read passes without asking, where unclassified is escalate"]


def test_the_projects_policy_is_the_one_bound_to_it(tmp_path: Path) -> None:
    from sandbox_science.worker.classifications import project_policy

    root = tmp_path / "middleware"
    store = PolicyStore(root)
    bound = store.add_policy(EXAMPLE.read_bytes())
    granted = store.add_policy(EXAMPLE.read_bytes() + b"# another revision\n")
    environments = cast(
        Environments,
        SimpleNamespace(
            settings=SimpleNamespace(policy_store=root),
            store=store,
            records=SimpleNamespace(latest_grant=lambda _project: granted.digest),
        ),
    )
    unbound = project_policy(environments, "p1")
    assert unbound is not None
    assert unbound.digest == granted.digest  # no binding: the last grant
    PolicyStore(root, "projects").bind("p1", bound.digest)
    found = project_policy(environments, "p1")
    assert found is not None
    assert found.digest == bound.digest
