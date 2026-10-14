# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S607, PT017
# pyright: reportPrivateUsage=false
"""Root-owned Lab29 MAIN notices; fixed stages, no service lifecycle.

Run only from the reviewed signed clean source. Root creates/restarts
services and supplies safe gateway Stopped evidence. Stage order: init, before,
flows, root manual worker restart, restarted, reopen, idle-resume, inspect. Start
idle-resume with idle disabled; after idle-owner-ready root enables idle=3 and
restarts its worker. After idle-complete root writes the bounded safe gateway
projection; the same live coroutine performs only the fixed harmless resume cell.
This is a production-components host
adapter using _keep, not the complete CLI entrypoint. Scripted models are local
host fixtures; exhaustive CLI lifecycle evidence remains in focused source tests.
Never read old private provider registration output or export credential material.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import threading
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from sandbox_science.contracts import ExecutionContext, KernelBirthPin, KernelLineageObservation
from sandbox_science.orchestrator.cli import _keep
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.tool import DocumentTool
from sandbox_science.orchestrator.kernel_delivery import KernelDeliveryController
from sandbox_science.orchestrator.kernel_notices import KernelNoticeError
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectRecord, ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

SOURCE = Path(__file__).resolve().parents[2]  # Correct only after root adopts in .agents/labs.
LAB = Path.home() / ".local/state/openshell-lab"
CONTROL = LAB / "orch/kernel-main"
PROJECT = "lab29"
URL = "http://127.0.0.1:8100"
MARKER = "kernel-main.created"
SESSIONS = ("a", "b", "held", "late", "reopen", "empty")
STAGES = ("init", "before", "flows", "restarted", "reopen", "idle-resume", "inspect")
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
CODE = (
    *tuple(
        "packages/orchestrator/src/sandbox_science/orchestrator/" + name
        for name in (
            "kernel_delivery.py",
            "kernel_notice_bindings.py",
            "kernel_notice_receipts.py",
            "kernel_notices.py",
            "projects.py",
            "loop.py",
            "cli.py",
            "tools.py",
            "model.py",
            "worker_client.py",
            "documents/store.py",
            "documents/tool.py",
        )
    ),
    "packages/contracts/src/sandbox_science/contracts/kernel_lineage.py",
)


def directory(relative: str = "") -> int:
    held = os.open(LAB, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert os.fstat(held).st_uid == os.getuid()
        for name in relative.split("/") if relative else ():
            assert name not in {"", ".", ".."}
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held)
            os.close(held)
            held = child
            assert os.fstat(held).st_uid == os.getuid()
        return held
    except BaseException:
        os.close(held)
        raise


def bounded(relative: str, limit: int = 128 * 1024) -> bytes:
    parent, _, name = relative.rpartition("/")
    root = directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            before = os.fstat(fd)
            assert stat.S_ISREG(before.st_mode)
            assert before.st_uid == os.getuid()
            assert before.st_nlink == 1
            assert 0 <= before.st_size <= limit
            raw = os.pread(fd, limit + 1, 0)
            after = os.fstat(fd)
            assert len(raw) == before.st_size
            assert len(raw) <= limit
            assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(root)


def write_new(relative: str, value: object) -> None:
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    assert len(raw) <= 128 * 1024
    parent, _, name = relative.rpartition("/")
    root = directory(parent)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(root)
    finally:
        os.close(root)


def settings() -> dict[str, Any]:
    value = tomllib.loads(bounded("worker/worker.toml", 65536).decode("utf-8"))
    assert value["listen"] == "127.0.0.1:8100"
    assert any(client["projects"] == [PROJECT] for client in value["clients"])
    assert value["openshell"].get("daemon", True) is True
    assert value["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    return value  # Private local use only; never serialize.


def inputs() -> dict[str, object]:
    values = {}
    for name in CODE:
        fd = os.open(SOURCE / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            assert stat.S_ISREG(before.st_mode)
            assert before.st_uid == os.getuid()
            assert 0 <= before.st_size <= 1024 * 1024
            raw = os.pread(fd, 1024 * 1024 + 1, 0)
            after = os.fstat(fd)
            assert len(raw) == before.st_size
            assert (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            values[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        finally:
            os.close(fd)
    return values


def project_record() -> ProjectRecord:
    store = ProjectStore(CONTROL, readonly=True)
    try:
        return store.get("lab29")
    finally:
        store.close()


def guard(head: str) -> dict[str, Any]:
    assert re.fullmatch("[0-9a-f]{40}", head)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == head
    )
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE)
    settings()
    value = json.loads(bounded(MARKER, 16384))
    assert value["version"] == 1
    assert value["project"] == PROJECT
    assert value["source_head"] == head
    assert value["purpose"] == "disposable explicit MAIN kernel-notice acceptance"
    assert value["helper_sha256"] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert value["inputs"] == inputs()
    assert re.fullmatch("[0-9a-f]{32}", value["run_id"])
    held = directory("orch/kernel-main")
    try:
        assert stat.S_IMODE(os.fstat(held).st_mode) == 0o700
    finally:
        os.close(held)
    project = project_record()
    parent = directory("orch/kernel-main")
    try:
        fd = os.open(
            store_path(CONTROL).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode)
            assert info.st_uid == os.getuid()
            assert info.st_nlink == 1
            assert 0 < info.st_size <= 64 * 1024 * 1024
            assert os.pread(fd, 16, 0) == b"SQLite format 3\0"
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    assert project.default_environment_id == value["environment_id"]
    assert project.profile == "minimal"
    assert project.policy == value["policy"]
    assert set(project.sessions) == set(SESSIONS)
    assert all(
        s.environment_id == value["environment_id"] and not s.dedicated
        for s in project.sessions.values()
    )
    return value


def init(head: str) -> None:
    assert re.fullmatch("[0-9a-f]{40}", head)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == head
    )
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE)
    settings()
    root = directory("orch")
    try:
        os.mkdir("kernel-main", 0o700, dir_fd=root)
    finally:
        os.close(root)
    root = directory()
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir("evidence", 0o700, dir_fd=root)
    finally:
        os.close(root)
    policy = bounded("policy.digest", 128).decode("ascii").strip()
    assert re.fullmatch("sha256:[0-9a-f]{64}", policy)
    store = ProjectStore(CONTROL)
    project, created = store.create(PROJECT, "minimal", policy)
    assert created
    for session in SESSIONS:
        store.open_session(PROJECT, session, dedicated=False)
    documents = DocumentStore(store_path(CONTROL), producer="lab29-fixture")
    try:
        documents.initialize()
    finally:
        documents.close()
    write_new(
        MARKER,
        {
            "version": 1,
            "project": PROJECT,
            "environment_id": project.default_environment_id,
            "policy": policy,
            "purpose": "disposable explicit MAIN kernel-notice acceptance",
            "source_head": head,
            "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "run_id": uuid.uuid4().hex,
            "inputs": inputs(),
        },
    )
    guard(head)


def evidence(name: str) -> dict[str, Any]:
    assert re.fullmatch("[a-z][a-z0-9-]{0,63}", name)
    return json.loads(bounded("evidence/kernel-main-" + name + ".json"))


def save(head: str, name: str, value: object, token: str = "") -> dict[str, object]:
    marker = guard(head)
    data = {
        "source_head": head,
        "run_id": marker["run_id"],
        "helper_sha256": marker["helper_sha256"],
        "recorded_at": datetime.now(UTC).isoformat(),
        "activity": "owned synthetic acceptance",
        "result": value,
    }
    raw = json.dumps(data, ensure_ascii=True)
    assert not token or (token not in raw and "Bearer " + token not in raw)
    write_new("evidence/kernel-main-" + name + ".json", data)
    print("PASS " + name, flush=True)
    relative = "evidence/kernel-main-" + name + ".json"
    saved = bounded(relative)
    return {"path": relative, "bytes": len(saved), "sha256": hashlib.sha256(saved).hexdigest()}


def gateway_proof(
    value: object, marker: dict[str, Any], environment: dict[str, Any], not_before: datetime
) -> dict[str, object]:
    """Accept only root's bounded public projection, never raw output or its hash."""
    assert type(value) is dict
    required = {"run_id", "source_head", "helper_sha256", "name", "id", "phase", "observed_at"}
    assert required <= value.keys() <= required | {"owner", "raw_bytes"}
    for key in required:
        assert type(value[key]) is str
        assert 0 < len(value[key]) <= 256
    assert (value["run_id"], value["source_head"], value["helper_sha256"]) == (
        marker["run_id"],
        marker["source_head"],
        marker["helper_sha256"],
    )
    assert (value["name"], value["id"], value["phase"]) == (
        environment["sandbox"],
        environment["sandbox_id"],
        "Stopped",
    )
    observed = datetime.fromisoformat(value["observed_at"])
    assert observed.tzinfo is not None
    assert not_before <= observed <= datetime.now(UTC)
    if "owner" in value:
        assert type(value["owner"]) is str
        assert 0 < len(value["owner"]) <= 128
    if "raw_bytes" in value:
        assert type(value["raw_bytes"]) is int
        assert 0 <= value["raw_bytes"] <= 1024 * 1024
    return value.copy()


def worker_snapshot(environment: str) -> dict[str, object]:
    """Read only bounded nonsecret worker projections; never daemon auth fields."""
    root = directory("worker/state")
    try:
        fd = os.open("worker.sqlite3", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    finally:
        os.close(root)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert 0 < info.st_size <= 64 * 1024 * 1024
        with contextlib.closing(
            sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro", uri=True)
        ) as db:
            db.execute("BEGIN")
            executions = db.execute(
                "SELECT substr(CAST(execution_id AS BLOB),1,129),substr(CAST(session_id AS BLOB),1,129),CASE WHEN typeof(terminal)='integer' AND terminal IN(0,1) THEN terminal END FROM executions WHERE environment_id=? ORDER BY rowid LIMIT 33",
                (environment,),
            ).fetchall()
            assert len(executions) <= 32
            assert all(type(row[2]) is int for row in executions)
            endpoint = db.execute(
                "SELECT substr(CAST(incarnation AS BLOB),1,29),substr(CAST(sandbox AS BLOB),1,129),substr(CAST(sandbox_id AS BLOB),1,129) FROM executor_endpoints WHERE environment_id=?",
                (environment,),
            ).fetchone()

            def text(row):
                return [v.decode("ascii") if isinstance(v, bytes) else v for v in row]

            birth_rows = db.execute(
                "SELECT substr(CAST(record AS BLOB),1,2049) FROM kernel_start_revisions WHERE environment_id=? ORDER BY session_id,language LIMIT 11",
                (environment,),
            ).fetchall()
            assert len(birth_rows) <= 10
            births = []
            for (raw,) in birth_rows:
                assert isinstance(raw, bytes)
                assert len(raw) <= 2048
                birth = KernelBirthPin.model_validate_json(raw, strict=True)
                assert birth.project_id == PROJECT
                assert birth.environment_id == environment
                assert birth.session_id in SESSIONS
                births.append(birth.model_dump(mode="json"))
            generations = db.execute(
                "SELECT substr(CAST(session_id AS BLOB),1,129),substr(CAST(language AS BLOB),1,7),substr(CAST(incarnation AS BLOB),1,29),CASE WHEN typeof(base)='integer' THEN base END,CASE WHEN typeof(last)='integer' THEN last END FROM kernels WHERE environment_id=? ORDER BY session_id,language LIMIT 11",
                (environment,),
            ).fetchall()
            assert len(generations) <= 10
            for row in generations:
                assert type(row[3]) is int
                assert type(row[4]) is int
                assert 0 <= row[3] <= row[4] <= 2**62
            return {
                "executions": [text(row) for row in executions],
                "endpoint": text(endpoint) if endpoint else None,
                "births": births,
                "generations": [text(row) for row in generations],
            }
    finally:
        os.close(fd)


class Lab:
    def __init__(self, head: str, token: str, http: aiohttp.ClientSession):
        self.head, self.token, self.http = head, token, http
        self.marker = guard(head)
        self.projects = ProjectStore(CONTROL)

    def context(self, session: str = "a") -> ExecutionContext:
        assert session in SESSIONS
        project = project_record()
        item = project.sessions[session]
        assert item.environment_id == self.marker["environment_id"]
        return ExecutionContext(
            project_id=PROJECT,
            session_id=session,
            environment_id=item.environment_id,
            policy_revision=project.policy,
        )

    async def api(self, method: str, path: str, body: object = None) -> tuple[int, dict[str, Any]]:
        guard(self.head)
        allowed = {
            "/v1/environments",
            "/v1/executions",
            "/v1/provisioning",
            "/v1/kernels/lineage",
            "/v1/kernels/observe",
            "/v1/sessions/close",
            "/v1/environments/" + self.marker["environment_id"],
        }
        assert method in {"GET", "POST"}
        assert path in allowed or re.fullmatch(
            "/v1/(?:executions/exe|provisioning/prv)-[0-9a-f]{24}", path
        )
        if method == "POST" and path != "/v1/environments":
            assert isinstance(body, dict)
            assert isinstance(body.get("context"), dict)
            context = ExecutionContext.model_validate(body["context"], strict=True)
            assert context == self.context(context.session_id)
        if method == "POST" and path == "/v1/environments":
            assert isinstance(body, dict)
            assert body == {
                "project_id": PROJECT,
                "environment_id": self.marker["environment_id"],
                "profile": "minimal",
                "policy": self.marker["policy"],
            }
        async with self.http.request(
            method, URL + path, json=body, allow_redirects=False
        ) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                assert len(raw) + len(chunk) <= 1024 * 1024
                raw.extend(chunk)
            return response.status, json.loads(raw)

    async def success(self, method: str, path: str, body: object = None) -> dict[str, Any]:
        status, value = await self.api(method, path, body)
        assert status in {200, 201}, "fixed Lab29 API refusal"
        return value

    async def cell(self, name: str, session: str, code: str) -> dict[str, Any]:
        assert name in {
            "initial-a",
            "initial-held",
            "initial-late",
            "initial-reopen",
            "b-import",
            "worker-global",
            "resume",
        }
        value = await self.success(
            "POST",
            "/v1/executions",
            {
                "kind": "cell",
                "context": self.context(session).model_dump(mode="json"),
                "language": "python",
                "operation_id": "lab29-" + self.marker["run_id"][:12] + "-" + name,
                "timeout_seconds": 120,
                "code": code,
            },
        )
        async with asyncio.timeout(150):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.success("GET", "/v1/executions/" + value["execution_id"])
        assert value["state"] == "succeeded"
        assert value["location"]["backend"] == "openshell"
        assert value["location"]["isolation_tier"] == 3
        return value

    async def lineage(self, worker: WorkerClient, session: str) -> KernelLineageObservation:
        value = await worker.observe_kernel_lineage(self.context(session))
        assert (value.project_id, value.environment_id, value.session_id) == (
            PROJECT,
            self.marker["environment_id"],
            session,
        )
        return value

    async def before(self) -> None:
        assert settings()["openshell"]["environments"].get("suspend_after") is None
        environment = await self.success(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": self.marker["environment_id"],
                "profile": "minimal",
                "policy": self.marker["policy"],
            },
        )
        initial = {}
        async with WorkerClient(URL, self.token) as worker:
            for session in ("a", "held", "late", "reopen"):
                initial[session] = await self.cell(
                    "initial-" + session,
                    session,
                    "import json,pathlib,importlib.util\nlab29_value=1729\nprint(json.dumps({'value':lab29_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'networkx_present':importlib.util.find_spec('networkx') is not None}))",
                )
                assert (
                    json.loads(initial[session]["result"]["stdout"]["text"])["networkx_present"]
                    is False
                )
                prior = await self.lineage(worker, session)
                assert len(prior.kernels) == 1
                assert prior.kernels[0].birth is not None
                assert prior.kernels[0].birth.start_revision == 0
            operation = await self.success(
                "POST",
                "/v1/provisioning",
                {
                    "operation_id": "lab29-install-" + self.marker["run_id"][:12],
                    "context": self.context("b").model_dump(mode="json"),
                    "purpose": "Owned MAIN kernel-notice package revision acceptance",
                    "conda": ["networkx"],
                },
            )
            async with asyncio.timeout(900):
                while operation["state"] != "finished":
                    assert operation["state"] != "awaiting_permission"
                    await asyncio.sleep(0.2)
                    operation = await self.success(
                        "GET", "/v1/provisioning/" + operation["provision_id"]
                    )
            assert operation["outcome"] == "completed"
            imported = await self.cell(
                "b-import", "b", "import networkx\nprint(networkx.__version__)"
            )
            observations = {
                s: (await self.lineage(worker, s)).model_dump(mode="json") for s in SESSIONS
            }
            for session in ("a", "held", "late", "reopen"):
                entry = observations[session]["kernels"][0]
                assert entry["lineage_status"] == "proved"
                assert entry["kernel"]["revision_status"] == "predates"
                assert entry["birth"]["sandbox_id"] == environment["sandbox_id"]
            assert observations["b"]["kernels"][0]["kernel"]["revision_status"] == "current"
            assert observations["empty"]["kernels"] == []
        save(
            self.head,
            "before",
            {
                "environment": environment,
                "initial": initial,
                "provision": operation,
                "import": imported,
                "lineage": observations,
                "worker": worker_snapshot(self.marker["environment_id"]),
            },
            self.token,
        )

    async def run_case(
        self, session: str, case: str, mode: str, saving: str, *, expect_notice: bool
    ) -> dict[str, object]:
        assert session in SESSIONS
        assert mode in {"answer", "first-failure", "late-failure", "cancel-model"}
        assert saving in {
            "ack",
            "fail",
            "cancel-admitted",
            "defer",
            "none",
            "live-reopen",
            "live-idle-resume",
        }
        run_id = "lab29-" + case + "-" + self.marker["run_id"][:12]
        context = self.context(session)
        source = WorkerClient(URL, self.token)
        owner = KernelDeliveryController(self.projects, context, run_id, source)
        documents = DocumentTool(
            DocumentStore(store_path(CONTROL), producer="lab29-host"),
            context,
            run_id=run_id,
            alias="scripted",
        )
        proofs: list[dict[str, object]] = []
        events: list[str] = []
        attempted_inputs: list[dict[str, object]] = []
        before = worker_snapshot(context.environment_id)
        model_started = asyncio.Event()
        started = datetime.now(UTC)
        earlier_runs = {e.run_id for e in self.projects.runs(PROJECT, session)}

        lab = self

        class Model(ScriptedModel):
            async def complete(self, messages, tools):
                # Capture the actual boundary before any synthetic failure/cancel.
                # ScriptedModel.requests intentionally records successful returns only.
                assert len(attempted_inputs) < 2
                actual = [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in tools
                ]
                expected = [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in toolbox.specs
                ]
                assert actual == expected
                assert messages[0] == {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                }
                prefix = [
                    m
                    for m in messages
                    if m.get("role") == "user"
                    and str(m.get("content", "")).startswith(
                        "Historical kernel package observations"
                    )
                ]
                assert len(prefix) == int(expect_notice)
                if expect_notice:
                    assert owner._injected is not None
                    assert owner.claim is not None
                    assert prefix == [{"role": "user", "content": owner._injected.text()}]
                    assert [pin.sequence for pin in owner.claim.pins] == [1]
                    assert owner.inbox is not None
                    assert owner.actor is not None
                    state = owner.inbox.status(owner.actor)
                    assert (state.pending, state.acknowledged_through) == (1, 0)
                else:
                    assert owner._injected is None
                    assert owner.claim is None
                item = save(
                    lab.head,
                    "input-" + case + "-" + str(len(attempted_inputs) + 1),
                    {
                        "run_id": run_id,
                        "attempt": len(attempted_inputs) + 1,
                        "messages": messages,
                        "tools": actual,
                        "system_exact": True,
                        "offered_specs_exact": True,
                        "notice_inputs": len(prefix),
                        "notice_sequences": [pin.sequence for pin in owner.claim.pins]
                        if owner.claim
                        else [],
                    },
                    lab.token,
                )
                attempted_inputs.append(item)
                model_started.set()
                if mode == "cancel-model":
                    await asyncio.Event().wait()
                if mode == "first-failure" or (
                    mode == "late-failure" and len(attempted_inputs) == 2
                ):
                    raise RuntimeError("synthetic model failure")
                return await super().complete(messages, tools)

        turns = [ModelTurn("Synthetic MAIN retained historical observations; no research result.")]
        if mode == "late-failure":
            turns = [ModelTurn(None, (ToolCall("control-read", "document_list", "{}"),))]
        model = Model(turns)
        transcript = None
        try:
            documents.store.initialize()
            source_proof = await source.observe_kernel_lineage(context)
            borrowed_transport = source._session
            assert borrowed_transport is not None
            await owner.initialize()
            assert owner._ready
            assert isinstance(owner.client, WorkerClient)
            assert owner.client is not source
            original = owner.client.observe_kernel_lineage

            async def observed(ctx):
                value = await original(ctx)
                assert len(proofs) < 16
                proofs.append(
                    {
                        "received_at": datetime.now(UTC).isoformat(),
                        "context": ctx.model_dump(mode="json"),
                        "lineage": value.model_dump(mode="json"),
                    }
                )
                return value

            owner.client.observe_kernel_lineage = observed
            toolbox = Toolbox(source, context, documents=documents, kernel_notices=owner)
            assert toolbox.specs == Toolbox(source, context, documents=documents).specs
            call = asyncio.create_task(
                run_task(
                    model,
                    toolbox,
                    "Owned Lab29 historical kernel-notice acceptance",
                    run_id=run_id,
                    max_turns=2,
                )
            )
            if mode == "cancel-model":
                try:
                    await asyncio.wait_for(model_started.wait(), 12)
                    call.cancel()
                    call.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await call
                    assert call.cancelled()
                    assert {e.run_id for e in self.projects.runs(PROJECT, session)} == earlier_runs
                    assert worker_snapshot(context.environment_id) == before
                    assert len(attempted_inputs) == 1
                    assert owner.delivered is None
                    assert owner.inbox is not None
                    assert owner.actor is not None
                    pending = owner.inbox.status(owner.actor)
                    assert (pending.pending, pending.acknowledged_through) == (1, 0)
                    return {
                        "run_id": run_id,
                        "cancelled_before_save": True,
                        "attempted_inputs": attempted_inputs,
                        "successful_model_requests": len(model.requests),
                        "delivery": None,
                        "pending": pending.model_dump(mode="json"),
                        "proofs": proofs,
                        "events": events,
                        "worker_rows_unchanged": True,
                    }
                finally:
                    if not call.done():
                        call.cancel()
                        await asyncio.gather(call, return_exceptions=True)
            transcript = await call
            assert all(request[0]["content"] == SYSTEM_PROMPT for request in model.requests)
            assert transcript.kernel_notice_delivery is owner.delivered
            if mode == "first-failure":
                assert transcript.failure == "model"
                assert transcript.kernel_notice_delivery is None
            elif mode == "late-failure":
                assert transcript.failure == "model"
                assert transcript.kernel_notice_delivery is not None
            else:
                assert transcript.stopped == "answered"
            assert len(attempted_inputs) == (2 if mode == "late-failure" else 1)
            if expect_notice and mode != "first-failure":
                assert transcript.kernel_notice_delivery is not None
                assert [pin.sequence for pin in transcript.kernel_notice_delivery.claim.pins] == [1]
            else:
                assert transcript.kernel_notice_delivery is None
            record = RunRecord(
                run_id=run_id,
                context=context,
                task="Owned Lab29 scripted host acceptance",
                started_at=started,
                ended_at=datetime.now(UTC),
                stopped=transcript.stopped,
                failure=transcript.failure,
                final_text=transcript.final_text,
                messages=transcript.messages,
                tools=transcript.tool_records,
                prompt_provenance=transcript.prompt_provenance,
                kernel_notice_delivery=transcript.kernel_notice_delivery,
            )
            assert all(
                sum("Historical kernel package observations" in str(m["content"]) for m in r) <= 1
                for r in model.requests
            )

            def status_view():
                if owner.inbox is None or owner.actor is None:
                    return None
                try:
                    return owner.inbox.status(owner.actor).model_dump(mode="json")
                except KernelNoticeError:
                    return {"observation": "held; current binding not admitted"}

            status = status_view()
            if expect_notice:
                assert status
                assert (status["pending"], status["acknowledged_through"]) == (1, 0)
            elif status:
                assert status["pending"] == 0
                assert status["acknowledged_through"] == (1 if case == "repeat" else 0)
            await source.close()
            assert borrowed_transport.closed
            events.append("borrowed-research-close")
            documents.store.close()
            saved: list[RunRecord] = []
            entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
            saved_at: datetime | None = None

            def on_saved(value):
                nonlocal saved_at
                saved.append(value)
                saved_at = datetime.now(UTC)
                events.append("record-fsynced")
                if saving == "cancel-admitted":
                    entered.set()
                    assert release.wait(8)

            def keep():
                code = _keep(
                    argparse.Namespace(state_dir=CONTROL, transcript=None),
                    record.model_dump(mode="json"),
                    1 if record.failure else 0,
                    saved=on_saved,
                    may_ack=lambda: not cancelled.is_set(),
                )
                assert saved
                assert code == (1 if record.failure else 0)
                events.append("keep-return")
                return code

            if saving == "fail":

                def refused():
                    raise OSError("synthetic save pre-write refusal")

                try:
                    await owner.save(refused)
                except OSError:
                    pass
                else:
                    raise AssertionError("expected synthetic save failure")
                assert not saved
            elif saving == "cancel-admitted":
                task = asyncio.create_task(owner.save(keep, cancelled=cancelled))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel()
                    task.cancel()
                    await asyncio.sleep(0.02)
                    assert not task.done()
                    assert saved
                finally:
                    release.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                assert task.cancelled()
                assert cancelled.is_set()
            elif saving in {"ack", "defer", "live-reopen", "live-idle-resume"}:
                await owner.save(keep)
            transition = None
            if saving == "live-reopen":
                assert saved
                assert record.kernel_notice_delivery is not None
                assert saved_at is not None
                old = project_record().sessions[session]
                self.projects.close_session(PROJECT, session)
                current = self.projects.open_session(PROJECT, session, dedicated=False)
                assert current.environment_id == context.environment_id
                assert current.kernel_notice_lifetime != old.kernel_notice_lifetime
                events.append("control-lifetime-reopened")
                transition = {
                    "previous_lifetime": old.kernel_notice_lifetime,
                    "current_lifetime": current.kernel_notice_lifetime,
                    "actual_environment_id": current.environment_id,
                    "worker_session_close": False,
                }
            elif saving == "live-idle-resume":
                assert saved
                assert record.kernel_notice_delivery is not None
                assert saved_at is not None
                transition = await self.idle_transition(owner, record, saved_at)
            acknowledged = False
            if saving == "ack" and record.kernel_notice_delivery is not None:
                events.append("ack-start")
                acknowledged = await owner.acknowledge_saved()
                assert acknowledged
                events.append("ack-return")
                assert saved_at is not None
                assert datetime.fromisoformat(str(proofs[-1]["received_at"])) >= saved_at
            if saving in {"live-reopen", "live-idle-resume"}:
                events.append("late-ack-start")
                acknowledged = await owner.acknowledge_saved()
                assert not acknowledged
                assert not owner._acked
                events.append("late-ack-refused")
                if saving == "live-idle-resume":
                    assert owner._replaced
                    assert saved_at is not None
                    assert datetime.fromisoformat(str(proofs[-1]["received_at"])) >= saved_at
            if saved:
                entry = next(e for e in self.projects.runs(PROJECT, session) if e.run_id == run_id)
                assert (
                    self.projects.run(PROJECT, session, entry.name, max_bytes=128 * 1024) == record
                )
            worker_after = worker_snapshot(context.environment_id)
            if saving != "live-idle-resume":
                assert worker_after == before
            after = status_view()
            if expect_notice:
                assert after
                assert after["pending"] == int(not acknowledged)
                assert after["acknowledged_through"] == int(acknowledged)
            elif after:
                assert after["pending"] == 0
            result = {
                "run_id": run_id,
                "case": case,
                "mode": mode,
                "save_mode": saving,
                "record_candidate": record.model_dump(mode="json"),
                "durable_saved": bool(saved),
                "saved_at": saved_at.isoformat() if saved_at else None,
                "borrowed_transport_closed": borrowed_transport.closed,
                "source_preflight": source_proof.model_dump(mode="json"),
                "attempted_inputs": attempted_inputs,
                "successful_model_requests": len(model.requests),
                "proofs": proofs,
                "events": events,
                "before": status,
                "after": after,
                "acknowledged": acknowledged,
                "worker_rows_unchanged": worker_after == before,
                "transition": transition,
            }
            if (
                saving in {"fail", "defer", "cancel-admitted"}
                and record.kernel_notice_delivery is not None
            ):
                assert not owner._acked
                assert after
                assert after["pending"] > 0
            return result
        finally:
            try:
                documents.store.close()
            finally:
                passive_transport = (
                    owner.client._session if isinstance(owner.client, WorkerClient) else None
                )
                borrowed_open = source._session is not None and not source._session.closed
                try:
                    await owner.close()
                    if passive_transport is not None:
                        assert passive_transport.closed
                    if borrowed_open:
                        assert source._session is not None
                        assert not source._session.closed
                        events.append("controller-close-kept-borrowed-open")
                    events.append("controller-passive-close")
                finally:
                    await source.close()

    async def flows(self) -> None:
        evidence("before")
        cases = (
            ("a", "first-failure", "first-failure", "ack", True),
            ("a", "cancel-model", "cancel-model", "none", True),
            ("a", "save-failure", "answer", "fail", True),
            ("a", "admitted-save-cancel", "answer", "cancel-admitted", True),
            ("a", "success", "answer", "ack", True),
            ("a", "repeat", "answer", "ack", False),
            ("b", "current", "answer", "ack", False),
            ("empty", "empty", "answer", "ack", False),
            ("late", "post-delivery-failure", "late-failure", "ack", True),
            ("held", "saved-unacked", "answer", "defer", True),
        )
        for session, name, mode, saving, expected in cases:
            result = await self.run_case(session, name, mode, saving, expect_notice=expected)
            save(self.head, "case-" + name, result, self.token)
        save(
            self.head,
            "flows",
            {
                "completed": True,
                "cases": len(cases),
                "activity": "production-components host adapter (_keep), not full CLI",
            },
            self.token,
        )

    async def restarted(self) -> None:
        prior = evidence("before")["result"]
        evidence("flows")
        async with WorkerClient(URL, self.token) as worker:
            current = await self.lineage(worker, "held")
        assert (
            current.kernels[0].birth
            == KernelLineageObservation.model_validate_json(
                json.dumps(prior["lineage"]["held"]), strict=True
            )
            .kernels[0]
            .birth
        )
        value = await self.cell(
            "worker-global",
            "a",
            "import json,pathlib\nprint(json.dumps({'value':lab29_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        initial = json.loads(prior["initial"]["a"]["result"]["stdout"]["text"])
        assert json.loads(value["result"]["stdout"]["text"]) == {
            "value": 1729,
            "boot": initial["boot"],
        }
        save(
            self.head,
            "restarted",
            {"lineage": current.model_dump(mode="json"), "global": value},
            self.token,
        )

    async def reopen(self) -> None:
        evidence("flows")
        result = await self.run_case(
            "reopen", "lifetime-reopen", "answer", "live-reopen", expect_notice=True
        )
        assert result["durable_saved"]
        assert not result["acknowledged"]
        assert result["worker_rows_unchanged"]
        save(self.head, "reopen", result, self.token)

    async def idle_transition(
        self, owner: KernelDeliveryController, record: RunRecord, saved_at: datetime
    ) -> dict[str, object]:
        # Root launches this stage before enabling idle=3/restarting its worker.
        # The live owner renews historical ownership only; these separate passive
        # probes do not make its receipt current or authorize research.
        assert settings()["openshell"]["environments"].get("suspend_after") is None
        save(
            self.head,
            "idle-owner-ready",
            {
                "run_id": record.run_id,
                "record_fsynced_at": saved_at.isoformat(),
                "delivery": record.kernel_notice_delivery.model_dump(mode="json"),
                "next_root_action": "Enable reviewed idle=3 and gracefully restart the owned worker; preserve this process.",
            },
            self.token,
        )
        async with asyncio.timeout(180):
            while settings()["openshell"]["environments"].get("suspend_after") != 3:
                await asyncio.sleep(0.2)
        samples: list[dict[str, object]] = []
        full: list[dict[str, object]] = []
        sample_count = 0
        last_sample = None
        async with WorkerClient(URL, self.token) as worker:
            async with asyncio.timeout(90):
                while True:
                    assert sample_count < 200
                    sample: dict[str, object] = {"received_at": datetime.now(UTC).isoformat()}
                    try:
                        value = await self.lineage(worker, "held")
                        sample.update(
                            state="observed",
                            observed_at=value.observed_at.isoformat(),
                            current_revision=value.current_revision,
                            kernels=[
                                [k.kernel.generation, k.kernel.revision_status, k.lineage_status]
                                for k in value.kernels
                            ],
                        )
                        if len(full) < 3:
                            full.append(value.model_dump(mode="json"))
                    except WorkerAPIError as error:
                        assert error.kind == "unavailable"
                        sample["state"] = "unavailable"
                    sample_count += 1
                    last_sample = sample
                    if len(samples) < 64:
                        samples.append(sample)
                    try:
                        status, env = await self.api(
                            "GET", "/v1/environments/" + self.marker["environment_id"]
                        )
                        assert status in {200, 503}
                    except (aiohttp.ClientError, TimeoutError):
                        status, env = 503, {}
                    if status == 200 and env["state"] == "stopped":
                        break
                    await asyncio.sleep(0.2)
            before = worker_snapshot(self.marker["environment_id"])
            for _ in range(30):
                try:
                    await worker.observe_kernel_lineage(self.context("held"))
                except WorkerAPIError as error:
                    assert error.kind == "unavailable"
                else:
                    raise AssertionError("stopped passive proof falsely succeeded")
                await asyncio.sleep(0.2)
            assert worker_snapshot(self.marker["environment_id"]) == before
        save(
            self.head,
            "idle-complete",
            {
                "environment": env,
                "compact_samples": samples,
                "sample_count": sample_count,
                "omitted_compact_samples": max(0, sample_count - len(samples)),
                "last_sample": last_sample,
                "full_samples": full,
                "no_wake_reads": 30,
                "worker": before,
                "live_owner_run": owner.run_id,
                "record_fsynced_at": saved_at.isoformat(),
            },
            self.token,
        )
        # Only the exact root-owned safe projection admits this one fixed resume
        # cell. No arbitrary command, path, policy, environment or VM selector.
        async with asyncio.timeout(180):
            while True:
                try:
                    proof = json.loads(bounded("evidence/kernel-main-gateway-stopped.json", 65536))
                    break
                except FileNotFoundError:
                    await asyncio.sleep(0.2)
        old = evidence("before")["result"]
        proof = gateway_proof(
            proof,
            self.marker,
            old["environment"],
            datetime.fromisoformat(evidence("idle-complete")["recorded_at"]),
        )
        assert (
            await self.success("GET", "/v1/environments/" + self.marker["environment_id"])
        ) == env
        assert worker_snapshot(self.marker["environment_id"]) == before
        result = await self.cell(
            "resume",
            "held",
            "import json,pathlib\nprint(json.dumps({'old_global':'lab29_value' in globals(),'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        assert not json.loads(result["result"]["stdout"]["text"])["old_global"]
        assert result["kernel_state_lost"] is True
        async with WorkerClient(URL, self.token) as worker:
            lineage = await self.lineage(worker, "held")
        prior = KernelLineageObservation.model_validate_json(
            json.dumps(old["lineage"]["held"]), strict=True
        )
        assert lineage.kernels[0].birth is not None
        assert prior.kernels[0].birth is not None
        assert lineage.kernels[0].birth.incarnation != prior.kernels[0].birth.incarnation
        assert lineage.kernels[0].kernel.revision_status == "current"
        return {
            "idle_complete": "evidence/kernel-main-idle-complete.json",
            "gateway_projection": proof,
            "execution": result,
            "lineage": lineage.model_dump(mode="json"),
            "observed_original_binding_replaced": True,
        }

    async def idle_resume(self) -> None:
        evidence("restarted")
        evidence("reopen")
        result = await self.run_case(
            "held", "idle-live-owner", "answer", "live-idle-resume", expect_notice=True
        )
        assert result["durable_saved"]
        assert not result["acknowledged"]
        assert result["after"]["pending"] == 1
        assert result["after"]["acknowledged_through"] == 0
        save(self.head, "idle-resume", result, self.token)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    if args.stage == "init":
        init(args.head)
        print("Lab29 source/control marker created; no RPC")
        return
    marker = guard(args.head)
    if args.stage == "inspect":
        save(
            args.head,
            "inspect",
            {
                "control": project_record().model_dump(mode="json"),
                "worker": worker_snapshot(marker["environment_id"]),
            },
        )
        return
    token = bounded("worker/client-a.token", 4096).decode("utf-8").strip()
    assert 8 <= len(token) <= 4096
    assert not any(c in token for c in "\0\r\n")
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=180)
    ) as http:
        await getattr(Lab(args.head, token, http), args.stage.replace("-", "_"))()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print(
            "Lab29 stage failed; preserve owned state and bounded public evidence for root review.",
            flush=True,
        )
        raise SystemExit(1) from None
