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
        store,
        permissions,
        policies.get,
        worker.classification_receipts,
        FakeClock(NOW),
        listings=worker.listing_reports,
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
        "kind": "catalog", "name": SERVER, "revision": None, "pinned": "2026-10-26",
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
    body = {"project": "p1", "reason": "r", "listings": []}
    assert (await api.post(drift, body, who="harness"))[0] == 403
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
    [listing] = drift["drifts"]
    assert (listing["listing"], listing["source"]) == ("e" * 64, "middleware")
    assert listing["added"] == drift["added"]
    tools = {tool["name"]: tool for tool in shown["tools"]}
    assert tools[SCRAPE]["pin_state"] == "changed"
    new = tools["brand_new"]
    assert (new["mentioned"], new["effect"], new["pin_state"], new["escalates"]) == (
        False,
        "read",
        "added",
        False,
    )  # fmt: skip  (its read-only hint makes it a read, which the policy allows)
    accept = {"project": "p1", "reason": "reviewed", "listings": ["e" * 64]}
    status, accepted = await api.post(drift_path, accept)
    assert status == 201, accepted
    assert accepted == {"accepted_at": NOW.isoformat().replace("+00:00", "Z")}
    [(source, record)] = api.records("mcp-tools-accepted")
    assert source == "workspace"
    assert record == {
        "project": "p1", "server": SERVER, "reason": "reviewed", "actor": "user",
        "operation_id": "op-1", "pin": pin_digest(pins), "listing": "e" * 64,
        "listings": ["e" * 64],
    }  # fmt: skip
    _, [again] = await api.get("/v1/classifications?project=p1")
    assert again["drift"]["held"] is False


async def test_re_accepting_needs_a_drift_and_a_known_server(api: Api) -> None:
    body = {"project": "p1", "reason": "r", "listings": []}
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
                                             "reason": "r", "listings": []})  # fmt: skip
    assert status == 409
    api.events.append(
        "mcp-tools-drift", "policy-middleware",
        {"project": "p1", "server": SERVER, "pin": None, "added": ["x"], "removed": [],
         "changed": []},
    )  # fmt: skip
    body = {"operation_id": "drift-1", "project": "p1", "reason": "seen", "listings": []}
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


# Listings the agent runtime's direct client read and reported (D98).


def reported(extra: dict[str, str] | None = None, *, project: str = "p1", server: str = SERVER):
    """A report of the catalog's pinned listing of ``SERVER``, with ``extra`` tools added."""
    pins = catalog(SERVER).pins
    tools = [
        {"name": name, "digest": digest or "0" * 64, "hint": "read"}
        for name, digest in pins.items()
    ]
    tools += [
        {"name": name, "digest": digest, "hint": "write"} for name, digest in (extra or {}).items()
    ]
    listing = pin_digest({tool["name"]: tool["digest"] for tool in tools})
    return {
        "project_id": project, "server": server, "session": "s1",
        "run": "20261026T120000000000Z-main-1", "tools": tools, "listing": listing,
    }  # fmt: skip


async def report(api: Api, body: Mapping[str, object], who: str = "harness") -> Any:
    response = await api.client.post(
        "/v1/mcp-listings", data=json.dumps(body), headers=headers(who)
    )
    return response.status, await response.json()


async def test_a_reported_drift_is_recorded_once_as_the_middlewares_and_holds(api: Api) -> None:
    from sandbox_science.policy.detection import DRIFT_KINDS, DriftLedger

    # The pinned listing itself is no drift.
    status, found = await report(api, reported())
    assert (status, found) == (200, {"drifted": False, "record": None})
    body = reported({"brand_new": "1" * 64})
    status, found = await report(api, body)
    assert status == 200, found
    assert found["drifted"] is True
    assert isinstance(found["record"], int)
    [(source, drift)] = api.records("mcp-tools-drift")
    pin = pin_digest(catalog(SERVER).pins)
    assert source == "runtime"
    assert (drift["project"], drift["server"], drift["pin"], drift["listing"]) == (
        "p1", SERVER, pin, body["listing"],
    )  # fmt: skip
    assert (drift["added"], drift["hints"], drift["digests"]) == (
        ["brand_new"], {"brand_new": "write"}, {"brand_new": "1" * 64},
    )  # fmt: skip
    assert (drift["caller"], drift["session"], drift["finding"]) == (
        "agent-runtime", "s1", "tool_list_drift",
    )  # fmt: skip
    # The middleware's ledger reads it as its own: the server's non-read calls ask.
    ledger = DriftLedger()
    with StoreReader(api.events.path) as reader:
        ledger.load(list(reader.records(kinds=DRIFT_KINDS)))
    assert ledger.drifted(SERVER, pin, "p1") == found["record"]
    # The same listing again records nothing new.
    status, again = await report(api, body)
    assert (status, again) == (200, {"drifted": True, "record": None})
    assert len(api.records("mcp-tools-drift")) == 1
    # The review shows it, and the user's re-acceptance ends it as for the middleware's.
    status, shown = await api.get("/v1/classifications?project=p1")
    assert status == 200
    assert shown[0]["drift"] is not None


@pytest.mark.parametrize(
    ("body", "who", "status"),
    [
        (reported(project="p2"), "harness", 403),  # a project the caller is not bound to
        (reported(server="other-server"), "harness", 404),  # not a server of the project's
        ({**reported(), "listing": "f" * 64}, "harness", 400),  # not its tools' digest
        ({**reported(), "tools": [*reported()["tools"], reported()["tools"][0]]}, "harness", 400),
        ({**reported(), "extra": 1}, "harness", 400),
    ],
)
async def test_a_report_the_caller_is_not_bound_to_is_refused(
    api: Api, body: dict[str, Any], who: str, status: int
) -> None:
    found = await report(api, body, who)
    assert found[0] == status, found
    assert api.records("mcp-tools-drift") == []


def ledger_for(api: Api) -> Any:
    from sandbox_science.policy.detection import DRIFT_KINDS, DriftLedger

    ledger = DriftLedger()
    with StoreReader(api.events.path) as reader:
        ledger.load(list(reader.records(kinds=DRIFT_KINDS)))
    return ledger


async def test_replaying_an_accepted_listing_never_ends_a_newer_hold(api: Api) -> None:
    # The review's probe (2026-10-26): a runtime report may start or keep a hold, never end
    # one. A is accepted, the server serves B (held), and a replay of A is refused.
    pin = pin_digest(catalog(SERVER).pins)
    a = reported({"benign": "1" * 64})
    status, found = await report(api, a)
    assert status == 200
    assert found["drifted"]
    status, accepted = await api.post(
        f"/v1/classifications/{SERVER}/drift/accept",
        {"project": "p1", "reason": "fine", "listings": [a["listing"]]},
    )
    assert status == 201, accepted
    assert ledger_for(api).drifted(SERVER, pin, "p1") is None
    b = reported({"benign": "1" * 64, "evil_write": "2" * 64})
    status, found = await report(api, b)
    assert status == 200
    assert found["record"] is not None
    held = ledger_for(api).drifted(SERVER, pin, "p1")
    assert held == found["record"]
    status, refused = await report(api, a)
    assert (status, refused["error"]["kind"]) == (409, "conflict")
    assert ledger_for(api).drifted(SERVER, pin, "p1") == held
    # The user's acceptance names every listing it accepts: it ends the hold.
    status, _ = await api.post(
        f"/v1/classifications/{SERVER}/drift/accept",
        {"project": "p1", "reason": "seen", "listings": [b["listing"]]},
    )
    assert status == 201
    assert ledger_for(api).drifted(SERVER, pin, "p1") is None


async def test_every_report_taken_is_recorded_and_reports_are_rate_limited(api: Api) -> None:
    for _ in range(10):
        status, _ = await report(api, reported())
        assert status == 200
    status, refused = await report(api, reported())
    assert (status, refused["error"]["kind"]) == (409, "conflict")
    taken = api.records("mcp-tools-listing-reported")
    assert len(taken) == 10
    source, first = taken[0]
    assert (source, first["project"], first["server"], first["drifted"]) == (
        "runtime", "p1", SERVER, False,
    )  # fmt: skip
    # The rate is per caller: another client of the project still reports.
    status, _ = await report(api, reported(), who="workspace")
    assert status == 200
    # Checks' listings, which name no session or run (the canaries', the user's availability
    # check), have a budget of their own: they never spend the runs'.
    check = {**reported(), "session": None, "run": None}
    for _ in range(10):
        assert (await report(api, check))[0] == 200
    status, refused = await report(api, check)
    assert (status, refused["error"]["kind"]) == (409, "conflict")


async def test_a_newer_drift_never_hides_an_older_one_from_review_or_acceptance(api: Api) -> None:
    # The review's second probe (2026-10-26): a real drift B adds evil_write; a runtime's
    # newer report C adds harmless. The review shows both, and an acceptance that does not
    # name exactly what it showed is refused.
    pin = pin_digest(catalog(SERVER).pins)
    b = reported({"evil_write": "2" * 64})
    assert (await report(api, b))[0] == 200
    c = reported({"harmless": "3" * 64})
    assert (await report(api, c))[0] == 200
    status, shown = await api.get("/v1/classifications?project=p1")
    assert status == 200
    drift = shown[0]["drift"]
    listings = drift["drifts"]
    assert [item["listing"] for item in listings] == [c["listing"], b["listing"]]
    assert [[tool["name"] for tool in item["added"]] for item in listings] == [
        ["harmless"],
        ["evil_write"],
    ]
    assert {item["source"] for item in listings} == {"runtime"}
    path = f"/v1/classifications/{SERVER}/drift/accept"
    status, refused = await api.post(
        path, {"project": "p1", "reason": "looks fine", "listings": [c["listing"]]}
    )
    assert (status, refused["error"]["kind"]) == (409, "conflict")
    assert "review again" in refused["error"]["message"]
    assert ledger_for(api).drifted(SERVER, pin, "p1") is not None
    both = [c["listing"], b["listing"]]
    status, _ = await api.post(path, {"project": "p1", "reason": "both seen", "listings": both})
    assert status == 201
    assert ledger_for(api).drifted(SERVER, pin, "p1") is None


async def test_the_latest_reported_listing_is_kept_for_the_services_view(api: Api) -> None:
    """The workspace's MCP services view reads the latest listing the runtime reported of
    each server (D98): every tool the server listed, its class in force and what a call of
    it by name is decided as; operator-only, for its own projects; none before a report."""
    status, found = await api.get("/v1/mcp-listings?project=p1")
    assert (status, found) == (200, [])
    await report(api, reported())
    newer = reported({"brand_new": "1" * 64})
    await report(api, newer)
    status, found = await api.get("/v1/mcp-listings?project=p1")
    assert status == 200, found
    (listing,) = found
    assert (listing["server"], listing["listing"], listing["drifted"]) == (
        SERVER, newer["listing"], True,
    )  # fmt: skip
    assert (listing["session"], listing["run"]) == ("s1", newer["run"])
    assert listing["reported_at"] == "2026-10-25T12:00:00Z"
    tools = {tool["name"]: tool for tool in listing["tools"]}
    assert len(tools) == len(newer["tools"])  # the server's own listing, denied tools too
    # A read without arguments, which an argument can make browsing: told as browse.
    assert tools[SCRAPE] == {
        "name": SCRAPE, "hint": "read", "effect": "browse", "classified": True, "action": "allow",
    }  # fmt: skip
    assert (tools[FEEDBACK]["effect"], tools[FEEDBACK]["action"]) == ("write", "escalate")
    assert tools["portal_toggle_servers"]["action"] == "deny"
    # A tool the classification does not name is decided by its hint, here a write.
    assert tools["brand_new"] == {
        "name": "brand_new", "hint": "write", "effect": "write", "classified": False,
        "action": "escalate",
    }  # fmt: skip
    # Only the operator client, for its own projects.
    assert (await api.get("/v1/mcp-listings?project=p1", who="harness"))[0] == 403
    assert (await api.get("/v1/mcp-listings?project=p2"))[0] == 403
    assert (await api.get("/v1/mcp-listings"))[0] == 400


async def test_the_worker_client_reads_the_reported_listings(api: Api) -> None:
    await report(api, reported())
    async with WorkerClient(
        str(api.client.make_url("/")).rstrip("/"), TOKENS["workspace"]
    ) as worker:
        (listing,) = await worker.mcp_listings("p1")
    assert listing.server == SERVER
    assert not listing.drifted
    assert {tool.action for tool in listing.tools} >= {"allow", "escalate"}


def test_reported_listings_survive_a_restart(tmp_path: Path) -> None:
    """The latest listing of each project's server is kept in the worker's store, with the
    time it was taken and whether it drifted, so the services view survives a restart."""
    from sandbox_science.contracts.classifications import ListingReport

    root = tmp_path / "middleware"
    store = PolicyStore(root)
    policies: dict[str, LoadedPolicy] = {"p1": store.add_policy(EXAMPLE.read_bytes())}
    events = EventStore(root / "events.db", timeout=5)
    permissions = Permissions(root / "events.db")

    def desk(worker: Store, now: datetime) -> ClassificationDesk:
        return ClassificationDesk(
            store, permissions, policies.get, worker.classification_receipts, FakeClock(now),
            listings=worker.listing_reports,
        )  # fmt: skip

    try:
        worker = Store(tmp_path / "worker.sqlite3")
        try:
            before = desk(worker, NOW)
            before.report_listing(ListingReport.model_validate(reported()), "harness")
            drifted = ListingReport.model_validate(reported({"brand_new": "1" * 64}))
            before.report_listing(drifted, "harness")
        finally:
            worker.close()
        worker = Store(tmp_path / "worker.sqlite3")
        try:
            (kept,) = desk(worker, datetime(2026, 10, 26, tzinfo=UTC)).listings("p1")
        finally:
            worker.close()
    finally:
        permissions.close()
        events.close()
    assert (kept.server, kept.listing, kept.drifted) == (SERVER, drifted.listing, True)
    assert kept.reported_at == NOW
    assert (kept.session, kept.run) == ("s1", drifted.run)
    assert "brand_new" in {tool.name for tool in kept.tools}


def test_kept_listings_are_one_per_server_and_bounded(tmp_path: Path) -> None:
    from sandbox_science.contracts.classifications import ListingReport
    from sandbox_science.worker.listing_reports import ListingReports

    worker = Store(tmp_path / "worker.sqlite3")
    try:
        kept = ListingReports(worker.listing_reports.db, keep=2)

        def put(project: str, server: str, minute: int, drifted: bool = False) -> None:
            body = {**reported(project=project, server=server), "session": None, "run": None}
            at = datetime(2026, 10, 25, 12, minute, tzinfo=UTC)
            kept.put(ListingReport.model_validate(body), drifted, at)

        put("p1", SERVER, 0)
        put("p1", SERVER, 1, drifted=True)  # the newest replaces the last
        assert [(k.report.server, k.drifted) for k in kept.of("p1")] == [(SERVER, True)]
        put("p1", "other-server", 2)
        put("p2", SERVER, 3)  # past the bound: the least recently reported is forgotten
        assert [k.report.server for k in kept.of("p1")] == ["other-server"]
        assert [k.report.server for k in kept.of("p2")] == [SERVER]
        with pytest.raises(ValueError, match="aware"):
            kept.put(ListingReport.model_validate(reported()), False, datetime(2026, 10, 25))
    finally:
        worker.close()


# Classes on direct listings the host trusts (DA-7).


async def tool_classes(api: Api, body: Mapping[str, object], who: str = "harness") -> Any:
    response = await api.client.post(
        "/v1/mcp-tool-classes", data=json.dumps(body), headers=headers(who)
    )
    return response.status, await response.json()


def pins_of(*names: str, digest: str | None = None) -> list[dict[str, str]]:
    pins = catalog(SERVER).pins
    return [{"name": name, "digest": digest or pins.get(name, "0" * 64)} for name in names]


async def test_a_tool_has_a_class_only_with_its_pin_in_the_classification_in_force(
    api: Api,
) -> None:
    body = {
        "project_id": "p1",
        "server": SERVER,
        "tools": [
            *pins_of(SCRAPE, FEEDBACK, "firecrawl-mcp_firecrawl_map", "portal_toggle_servers"),
            *pins_of("brand_new"),
            *pins_of("firecrawl-mcp_firecrawl_parse", digest="1" * 64),
        ],
    }
    status, found = await tool_classes(api, body)
    assert status == 200, found
    assert found["classification"] == catalog(SERVER).digest
    assert {tool["name"]: (tool["state"], tool["effect"], tool["escalates"])
            for tool in found["tools"]} == {
        SCRAPE: ("classified", "browse", False),  # an argument can make it browse
        FEEDBACK: ("classified", "write", True),
        "firecrawl-mcp_firecrawl_map": ("classified", "read", False),
        "portal_toggle_servers": ("denied", None, False),
        "brand_new": ("unclassified", None, False),
        "firecrawl-mcp_firecrawl_parse": ("changed", None, False),
    }  # fmt: skip
    # A project revision in force replaces the catalog's, a tool without a pin has no class.
    mapped = "firecrawl-mcp_firecrawl_map"
    text = classification((mapped, "write", catalog(SERVER).pins[mapped]), (SCRAPE, "read", None))
    from sandbox_science.policy.store import Acceptance

    api.store.accept_classification("p1", SERVER, text.encode(), Acceptance("user", NOW, "test"))
    status, found = await tool_classes(api, body)
    assert status == 200, found
    states = {tool["name"]: (tool["state"], tool["effect"]) for tool in found["tools"]}
    assert states["firecrawl-mcp_firecrawl_map"] == ("classified", "write")
    assert states[SCRAPE] == ("unpinned", None)
    assert states[FEEDBACK] == ("unclassified", None)  # the revision stands for the server


@pytest.mark.parametrize(
    ("body", "who", "status"),
    [
        ({"project_id": "p2", "server": SERVER, "tools": []}, "harness", 403),
        ({"project_id": "p1", "server": "other-server", "tools": []}, "harness", 404),
        ({"project_id": "p1", "server": SERVER, "tools": pins_of(SCRAPE, SCRAPE)}, "harness", 400),
        ({"project_id": "p1", "server": SERVER, "tools": [], "extra": 1}, "harness", 400),
    ],
)
async def test_tool_classes_are_a_clients_own_projects_servers_only(
    api: Api, body: dict[str, Any], who: str, status: int
) -> None:
    found = await tool_classes(api, body, who)
    assert found[0] == status, found
