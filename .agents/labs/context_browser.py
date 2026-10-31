# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Disposable authenticated browser fixture for context, Skills and MCP views.

No model, worker or external connections. All research data and login credentials
are visibly synthetic. An owned loopback stand-in for the control service's
availability check, and a synthetic reported listing, exercise the MCP services view
(the MCP proxy it used to run retired on 2026-10-26, D98). Starts on free loopback ports and removes its own
temporary state on SIGTERM/SIGINT. Serve a built workspace from this checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web
from resource_browser import seed_resources

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.contracts.classifications import ListedToolView, ReportedListing
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.capture import (
    ContextMessage,
    Origin,
    prompt_pins,
    request,
)
from sandbox_science.orchestrator.context.history.captured import CapturedContexts
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import Ownership, WorkRecords
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.memory import ProjectMemory
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_id_of
from sandbox_science.orchestrator.prompts import Section, assemble
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanOutput,
    PlanPhase,
    PlanScope,
    PlanStep,
    SessionPlans,
)
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.policy.store import PolicyStore
from sandbox_science.testing.mcp import SERVER_HOST
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.mcp_metadata import AvailabilityClient
from sandbox_science.workspace.mcp_services import ServiceConfig

TOKEN = "test-secret-disposable-context-browser-20261018"  # noqa: S105 -- synthetic fixture
PROJECT, SESSION = "context-demo", "research"
ROOT = "20261018T120000000000Z-root"
CHILD = "20261018T120001000000Z-child"
RESUMED = "20261018T130000000000Z-resumed"
LIVE = "20261018T140000000000Z-live"
CHECK_TOKEN = "test-secret-owned-browser-availability-check"  # noqa: S105 -- synthetic fixture


def proxy_policy(port: int) -> bytes:
    """A policy for one synthetic TLS peer, with distinct allowed/escalated/denied tools."""
    return f"""apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata: {{name: synthetic-browser-policy}}
defaults: {{action: deny}}
mcp:
  limits: {{max_argument_bytes: 16Ki}}
  servers:
    papers:
      url: https://{SERVER_HOST}:{port}/mcp
      revision: "2025-11-25"
      auth: {{mode: none}}
      tools:
        allow: [search]
        escalate: [crawl]
        deny: ["admin_*"]
""".encode()


class SyntheticListings:
    """The worker's reported listing of the synthetic service, as its client reads it."""

    async def mcp_listings(self, project_id: str) -> list[ReportedListing]:
        return [
            ReportedListing(
                server="papers",
                reported_at=datetime.now(UTC) - timedelta(minutes=3),
                session=SESSION,
                run=LIVE,
                listing="f" * 64,
                drifted=False,
                tools=[
                    ListedToolView(
                        name="search",
                        hint="read",
                        effect="read",
                        classified=False,
                        action="allow",
                    ),
                    ListedToolView(
                        name="crawl",
                        hint="read",
                        effect="read",
                        classified=False,
                        action="escalate",
                    ),
                    ListedToolView(
                        name="admin_reset",
                        hint="admin",
                        effect="admin",
                        classified=False,
                        action="deny",
                    ),
                ],
            )
        ]


@asynccontextmanager
async def mcp_fixture(
    path: Path,
) -> AsyncIterator[tuple[AvailabilityClient, SyntheticListings, dict[str, str]]]:
    """Run only owned loopback listeners; no upstream DNS or external network is used."""
    async with AsyncExitStack() as stack:
        available = True
        methods: list[str] = []

        async def check(incoming: web.Request) -> web.Response:
            # The control service's check: one listing by the runtime's client, never a call.
            if incoming.headers.get("Authorization") != f"Bearer {CHECK_TOKEN}":
                raise web.HTTPNotFound()
            if (await incoming.json()).get("server") != "papers":
                raise web.HTTPNotFound()
            methods.append("tools/list")
            del methods[:-32]
            if not available:
                return web.json_response(
                    {"status": "unavailable", "tool_count": None, "digest": None,
                     "error_kind": "client_error"}
                )  # fmt: skip
            return web.json_response(
                {"status": "listed", "tool_count": 2, "digest": "f" * 64, "error_kind": None}
            )

        async def serve(app: web.Application) -> tuple[web.AppRunner, int]:
            runner = web.AppRunner(app)
            stack.push_async_callback(runner.cleanup)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            return runner, runner.addresses[0][1]

        check_app = web.Application(client_max_size=64 * 1024)
        check_app.router.add_post("/mcp-check", check)
        _, check_port = await serve(check_app)

        async def controls(_: web.Request) -> web.Response:
            return web.Response(
                content_type="text/html",
                text=f"""<!doctype html>
<title>Synthetic MCP availability controls</title><h1>Synthetic fixture only</h1>
<p>The service is {"available" if available else "unavailable"}. Changing it does not probe MCP.
Return to MCP services and explicitly check availability after each change.</p>
<form method="post" action="/availability/unavailable"><input type="hidden" name="token" value="{TOKEN}">
<button>Make synthetic service unavailable</button></form>
<form method="post" action="/availability/available"><input type="hidden" name="token" value="{TOKEN}">
<button>Restore synthetic service</button></form><p><a href="/stats">Read synthetic method counts</a></p>""",
            )

        async def toggle(incoming: web.Request) -> web.Response:
            nonlocal available
            if incoming.headers.get("Origin") != f"{incoming.scheme}://{incoming.host}":
                raise web.HTTPForbidden()
            if (await incoming.post()).get("token") != TOKEN:
                raise web.HTTPForbidden()
            available = incoming.match_info["state"] == "available"
            raise web.HTTPSeeOther("/")

        async def stats(_: web.Request) -> web.Response:
            return web.json_response(
                {
                    "synthetic": True,
                    "available": available,
                    "recent_methods": methods,
                    "attempted_tool_calls": 0,
                    "executed_tool_calls": 0,
                }
            )

        control_app = web.Application(client_max_size=1024)
        control_app.router.add_get("/", controls)
        control_app.router.add_get("/stats", stats)
        control_app.router.add_post("/availability/{state:available|unavailable}", toggle)
        _, control_port = await serve(control_app)
        yield (
            AvailabilityClient(f"http://127.0.0.1:{check_port}", CHECK_TOKEN),
            SyntheticListings(),
            {
                "mcp_controls": f"http://127.0.0.1:{control_port}/",
                "mcp_stats": f"http://127.0.0.1:{control_port}/stats",
            },
        )


def seed_capture(projects: ProjectStore, owner: Ownership, label: str) -> str:
    """Record explicitly synthetic adapter input through the normal public intent seam."""
    tools = [
        ToolSpec("synthetic_calculation", "Synthetic schema, never executed", {"type": "object"})
    ]
    system, pins = assemble(
        "main",
        [
            Section(
                "system", "synthetic-system-v1", "Synthetic browser fixture: no model is called."
            ),
            Section("skills", "synthetic-skills-v1", "Synthetic skill: retain exact evidence."),
            Section(
                "memory", "synthetic-memory-v1", "Synthetic notation: work over finite fields."
            ),
        ],
        tools,
    )
    with prompt_pins(pins):
        captured = request(
            [
                {"role": "system", "content": system},
                ContextMessage(
                    {"role": "user", "content": "Synthetic folded proof summary."},
                    Origin(kind="fold_region"),
                ),
                {
                    "role": "user",
                    "content": f"Synthetic {label}: inspect original \u03b1 evidence. "
                    + "x" * 40000
                    + " CAPTURE_END",
                },
            ],
            tools,
        )
    return ModelIntents(projects.control).begin(
        owner,
        kind="main" if owner.run == ROOT else "delegated",
        alias="synthetic-no-model",
        captured=captured,
    )


def seed_memory_and_plan(documents: DocumentStore) -> None:
    memory = ProjectMemory(documents)
    first = memory.change(
        PROJECT,
        {
            "action": "add",
            "text": "Synthetic convention: denote the finite field by $\\mathbb{F}_q$.",
            "operation_id": "synthetic-memory-add-one",
        },
        USER,
    )
    memory.change(
        PROJECT,
        {
            "action": "edit",
            "entry": first.entry_id,
            "expected": first.revision,
            "text": "Synthetic convention: denote a finite field by $\\mathbb{F}_q$, with q a prime power.",
            "operation_id": "synthetic-memory-edit-one",
        },
        USER,
    )
    second = memory.change(
        PROJECT,
        {
            "action": "add",
            "text": "Synthetic evidence rule: save the exact calculation before reporting.",
            "operation_id": "synthetic-memory-add-two",
        },
        USER,
    )
    agent = Author(
        kind="agent", session_id=SESSION, run_id=LIVE, role="delegated", alias="synthetic-no-model"
    )
    memory.propose(
        PROJECT,
        {
            "action": "change",
            "entry": first.entry_id,
            "revision": 2,
            "text": "Synthetic convention: write $\\mathbb{F}_q$ and state q explicitly in every derivation.",
            "reason": "Synthetic proposal for reviewing the exact before/after difference.",
        },
        agent,
        "synthetic-proposal-change",
    )
    memory.propose(
        PROJECT,
        {
            "action": "add",
            "text": "Synthetic workflow: compare the independent derivation with the saved reference.",
            "reason": "Synthetic addition; only explicit USER acceptance changes memory.",
        },
        agent,
        "synthetic-proposal-add",
    )
    memory.propose(
        PROJECT,
        {
            "action": "remove",
            "entry": second.entry_id,
            "revision": second.revision,
            "reason": "Synthetic removal proposal for decline testing; this is not advice to remove it.",
        },
        agent,
        "synthetic-proposal-remove",
    )
    plans = SessionPlans(documents)
    authority = PlanAuthority(
        scope=PlanScope(project_id=PROJECT, session_id=SESSION),
        role="user",
        user_id="synthetic-user",
    )
    plan = plans.publish(
        authority,
        "synthetic-plan",
        PlanBody(
            goal="Synthetic research: compare two finite-field calculations.",
            feasibility="Synthetic fixture only; no research execution or model is required.",
            outputs=(
                PlanOutput(
                    title="Synthetic derivation",
                    description="A saved argument with its assumptions.",
                    evidence_standard="Exact reference and an independent check.",
                ),
            ),
            phases=(
                PlanPhase(
                    phase_id="compare",
                    title="Compare calculations",
                    steps=(
                        PlanStep(
                            step_id="reference",
                            title="Read the reference",
                            description="Inspect the synthetic reference.",
                            evidence_standard="Keep the exact text.",
                        ),
                        PlanStep(
                            step_id="derive",
                            title="Derive independently",
                            description="Compare the second synthetic derivation.",
                            evidence_standard="State assumptions and differences.",
                        ),
                        PlanStep(
                            step_id="report",
                            title="Review the result",
                            description="Review the synthetic result.",
                            evidence_standard="No claim is based on a real computation.",
                        ),
                    ),
                ),
            ),
        ),
        "Synthetic browser plan",
    )
    plans.set_status(
        authority,
        "synthetic-plan-active",
        plan.ref,
        1,
        0,
        "active",
        "Synthetic workflow state only",
    )
    plans.progress(
        authority,
        "synthetic-step-done",
        plan.ref,
        "reference",
        0,
        "completed",
        "Synthetic source is retained; no real computation was run.",
    )
    plans.progress(
        authority,
        "synthetic-step-active",
        plan.ref,
        "derive",
        0,
        "in_progress",
        "Synthetic comparison for browser status filtering.",
    )


def seed(path: Path) -> dict[str, str]:
    with closing(ProjectStore(path)) as projects:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        session = projects.open_session(PROJECT, SESSION, dedicated=False)
        works = WorkRecords(projects.control)
        root_owner = works.admit(PROJECT, SESSION, ROOT)
        works.admit(PROJECT, SESSION, CHILD, parent=ROOT)
        root_capture = seed_capture(projects, root_owner, "saved root request")
        delegations = DelegationRecords(projects.control)
        saved_delegation = new_id("dlg")
        delegations.start(
            PROJECT,
            SESSION,
            Delegation(
                delegation_id=saved_delegation,
                parent=ROOT,
                run=CHILD,
                kind="task",
                brief={"task": "Synthetic independent finite-field derivation"},
                started_at=datetime(2026, 10, 18, 12, 0, 1, tzinfo=UTC),
            ),
        )
        delegations.end(
            PROJECT,
            SESSION,
            saved_delegation,
            Ending(
                "returned",
                "answered",
                report="Synthetic returned report: the recorded derivation is ready to inspect.",
            ),
        )
        archive = Archive(projects.control)
        for index in range(70):
            archive.append(
                PROJECT, SESSION, ROOT, Entry("assistant", "assistant", f"Synthetic note {index}")
            )
        archive.append(
            PROJECT,
            SESSION,
            ROOT,
            Entry("assistant", "assistant", "CONTINUITY_NEEDLE: original proof retained."),
        )
        archive.append(
            PROJECT,
            SESSION,
            CHILD,
            Entry("assistant", "assistant", "CHILD_ONLY: separate conversation."),
        )
        large = archive.append(
            PROJECT, SESSION, ROOT, Entry("tool", "tool_result", "x" * 1100000 + "LARGE_END")
        )
        works.interrupt(PROJECT, SESSION, ROOT)
        request = works.request(PROJECT, SESSION)
        if request is None:
            raise RuntimeError("Synthetic resume request was not created")
        state = RunState(projects.control)
        state.acquire(PROJECT, SESSION, RESUMED, "fixture")
        _, epoch = works.claim(PROJECT, SESSION, request, lease_run=RESUMED)
        works.admit(PROJECT, SESSION, RESUMED, work=ROOT, predecessor=ROOT, epoch=epoch)
        archive.append(
            PROJECT,
            SESSION,
            RESUMED,
            Entry("assistant", "assistant", "CONTINUITY_NEEDLE: continued proof."),
        )
        for name, hour, role in (
            (ROOT, 12, "main"),
            (CHILD, 12, "delegated"),
            (RESUMED, 13, "main"),
        ):
            started = datetime(2026, 10, 18, hour, 0, 1 if name == CHILD else 0, tzinfo=UTC)
            projects.save_run(
                RunRecord(
                    run_id=run_id_of(name) or "invalid",
                    context=ExecutionContext(
                        project_id=PROJECT,
                        session_id=SESSION,
                        environment_id=session.environment_id,
                    ),
                    role=role,
                    parent=ROOT if role == "delegated" else None,
                    delegation=saved_delegation if role == "delegated" else None,
                    task="Synthetic context browser acceptance",
                    started_at=started,
                    ended_at=started + timedelta(minutes=5),
                    stopped="answered",
                    final_text="Synthetic result: $x^2=1$.",
                    messages=[
                        {
                            "role": "user",
                            "content": "Synthetic browser fixture; no model was called.",
                        }
                    ],
                    tools=[],
                    usage=[
                        ModelUsage(
                            model="fixture",
                            input_tokens=n,
                            output_tokens=100,
                            cache_read_input_tokens=n // 2,
                        )
                        for n in (12000, 17000, 9000, 14000)
                    ],
                    summaries={"connectors": {"servers": {"papers": 3}}},
                )
            )
            state.append(PROJECT, SESSION, name, "assistant", {"text": "Synthetic saved answer."})
            state.append(PROJECT, SESSION, name, "stopped", {"stopped": "answered"})
        state.release(PROJECT, SESSION, "fixture")
        # A deliberately unsaved attempt shows the live context inspector path.
        # This remains a synthetic journal entry, never a model dispatch.
        owner = works.admit(PROJECT, SESSION, LIVE, parent=RESUMED)
        live_capture = seed_capture(projects, owner, "live unsaved request")
        delegations.start(
            PROJECT,
            SESSION,
            Delegation(
                delegation_id=new_id("dlg"),
                parent=RESUMED,
                run=LIVE,
                kind="task",
                brief={"task": "Synthetic live proof comparison; no model is running"},
                started_at=datetime(2026, 10, 18, 14, tzinfo=UTC),
            ),
        )
        state.append(PROJECT, SESSION, LIVE, "started", {"role": "delegated"})
        archive.append(
            PROJECT,
            SESSION,
            LIVE,
            Entry("assistant", "assistant", "Synthetic live answer, no model called."),
            model_usage=AnsweredUsage(
                ordinal=1,
                observed_at=datetime.now(UTC),
                usage=ModelUsage(
                    model="fixture",
                    input_tokens=12000,
                    output_tokens=100,
                    cache_read_input_tokens=6000,
                    cache_creation_input_tokens=1000,
                ),
            ),
        )
        fold = archive.append(
            PROJECT, SESSION, LIVE, Entry("user", "fold", "Synthetic folded context.")
        )
        state.append(
            PROJECT,
            SESSION,
            LIVE,
            "fold",
            {"state": "saved", "fold": fold.message_id, "span": [1, 3], "freed": 1200},
        )
        pending = ModelIntents(projects.control).begin(owner, kind="fold", alias="fixture")
        resource_session = seed_resources(projects, PROJECT)
    with closing(DocumentStore(store_path(path))) as documents:
        seed_memory_and_plan(documents)
        document = documents.create(
            PROJECT,
            name="findings",
            title="Synthetic findings",
            text="# Findings\n\nA synthetic formula $x^2=1$.\n",
            author=USER,
        )
        files = (
            PackFile(
                "outline/SKILL.md",
                b"---\nname: outline\ndescription: Build an annotated outline.\n---\n# Outline\nSynthetic skill for browser acceptance.\n",
            ),
        )
        pack = snapshot(
            Fetched(
                "archive",
                "synthetic.tar",
                "",
                files,
                (),
                archive_sha256=hashlib.sha256(files[0].content).hexdigest(),
            )
        )
        skills = SkillStore.of(documents)
        skills.add("demo", pack, USER)
        skills.pin(PROJECT, "demo", USER)
    return {
        "resource_session": resource_session,
        "document": document.document_id,
        "large_message": large.message_id,
        "live_run": LIVE,
        "pending_auxiliary": pending,
        "root_capture": root_capture,
        "live_capture": live_capture,
    }


def append_later(path: Path, pending: str) -> None:
    """Emit one later observation for a visible-page polling browser check."""
    with closing(ProjectStore(path)) as projects:
        owner = WorkRecords(projects.control).owner(PROJECT, SESSION, LIVE)
        if owner is None:
            raise RuntimeError("Synthetic live attempt disappeared")
        Archive(projects.control).append(
            PROJECT,
            SESSION,
            LIVE,
            Entry("assistant", "assistant", "Synthetic later answer, no model called."),
            model_usage=AnsweredUsage(
                ordinal=2,
                observed_at=datetime.now(UTC),
                usage=ModelUsage(
                    model="fixture",
                    input_tokens=9000,
                    output_tokens=80,
                    cache_read_input_tokens=4500,
                ),
            ),
        )
        ModelIntents(projects.control).observe(
            owner,
            pending,
            ModelUsage(
                model="fixture",
                input_tokens=250,
                output_tokens=20,
                cache_read_input_tokens=125,
                cost=0.0125,
                cost_source="gateway",
            ),
        )


def selftest() -> None:
    """Check synthetic records and policy offline, without starting any listener."""
    with tempfile.TemporaryDirectory(prefix="ss-browser-offline-", dir="/var/tmp") as temp:
        path = Path(temp)
        info = seed(path)
        loaded = PolicyStore(path / "policy-check").add_policy(proxy_policy(31415))
        assert loaded.policy.mcp.servers["papers"].tools.allow == ("search",)
        with closing(DocumentStore(store_path(path))) as documents:
            memory = ProjectMemory(documents)
            assert len(memory.memory(PROJECT).entries) == 2
            assert len(memory.proposals(PROJECT)) == 3
            assert len(memory.history(PROJECT, "mem-1").versions) == 2
            contexts = CapturedContexts(documents.control)
            assert any(
                call.captured and call.identity == info["live_capture"]
                for call in contexts.calls(PROJECT, SESSION, LIVE).calls
            )
            assert len(contexts.parts(PROJECT, SESSION, LIVE, info["live_capture"]).parts) == 6
    print("PASS synthetic browser seed/policy; no listeners, model or worker calls", flush=True)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ss-context-browser-", dir="/var/tmp") as temp:
        path = Path(temp)
        info = seed(path)
        async with mcp_fixture(path) as (checks, listings, mcp_info):
            store = ProjectStore(path, readonly=True)
            app = create_app(
                store,
                Path(__file__).resolve().parents[2] / "web/workspace/dist",
                login_token=TOKEN,
                mcp_project=PROJECT,
                mcp_inventory=(
                    ServiceConfig(name="papers", purpose="Synthetic literature service"),
                ),
                mcp_checks=checks,
                mcp_listings=listings,
            )
            runner = web.AppRunner(app)
            update = None
            try:
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                port = runner.addresses[0][1]
                print(
                    json.dumps(
                        {
                            "synthetic": True,
                            "url": f"http://127.0.0.1:{port}",
                            "login": f"http://127.0.0.1:{port}/login?token={TOKEN}",
                            "root": temp,
                            "project": PROJECT,
                            "session": SESSION,
                            "run": RESUMED,
                            "live_update_after_seconds": 20,
                            "mcp_check_sequence": "Open MCP; check; make peer unavailable; check; restore peer; check. Stats must show zero tool calls.",
                            **mcp_info,
                            **info,
                        }
                    ),
                    flush=True,
                )
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stop.set)

                async def later() -> None:
                    await asyncio.sleep(20)
                    await asyncio.to_thread(append_later, path, info["pending_auxiliary"])

                update = asyncio.create_task(later())
                await stop.wait()
            finally:
                if update is not None:
                    update.cancel()
                    await asyncio.gather(update, return_exceptions=True)
                await runner.cleanup()
                store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="validate owned records offline")
    if parser.parse_args().selftest:
        selftest()
    else:
        asyncio.run(main())
