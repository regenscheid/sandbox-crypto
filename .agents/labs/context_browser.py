# SPDX-License-Identifier: MIT
"""Disposable authenticated browser fixture for context, Skills and MCP views.

No model, worker or external services. All research data and login credentials are
visibly synthetic. Starts on a free loopback port and removes its own temporary
state on SIGTERM/SIGINT. Serve a built workspace from this checkout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiohttp import web

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_id_of
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.mcp_services import ServiceConfig

TOKEN = "test-secret-disposable-context-browser-20261018"  # noqa: S105 -- synthetic fixture
PROJECT, SESSION = "context-demo", "research"
ROOT = "20261018T120000000000Z-root"
CHILD = "20261018T120001000000Z-child"
RESUMED = "20261018T130000000000Z-resumed"


def seed(path: Path) -> dict[str, str]:
    with closing(ProjectStore(path)) as projects:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        session = projects.open_session(PROJECT, SESSION, dedicated=False)
        works = WorkRecords(projects.control)
        works.admit(PROJECT, SESSION, ROOT)
        works.admit(PROJECT, SESSION, CHILD, parent=ROOT)
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
                    delegation="dlg-fixture" if role == "delegated" else None,
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
    with closing(DocumentStore(store_path(path))) as documents:
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
    return {"document": document.document_id, "large_message": large.message_id}


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ss-context-browser-", dir="/var/tmp") as temp:
        path = Path(temp)
        info = seed(path)
        store = ProjectStore(path, readonly=True)
        app = create_app(
            store,
            Path(__file__).resolve().parents[2] / "web/workspace/dist",
            login_token=TOKEN,
            mcp_project=PROJECT,
            mcp_inventory=(ServiceConfig(name="papers", purpose="Synthetic literature service"),),
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        print(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}",
                    "login": f"http://127.0.0.1:{port}/login?token={TOKEN}",
                    "root": temp,
                    "project": PROJECT,
                    "session": SESSION,
                    "run": RESUMED,
                    **info,
                }
            ),
            flush=True,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await runner.cleanup()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
