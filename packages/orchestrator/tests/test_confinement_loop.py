# SPDX-License-Identifier: MIT
"""Production loop/capability boundary with a local worker and scripted HTTP gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import (
    CallbackGateway,
    CallbackModel,
    RuntimeModels,
)
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.context.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import records, serving

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("memory_pause", [False, True])
async def test_vm_loop_commits_host_state_without_a_local_harness_fallback(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_pause: bool
) -> None:
    async def pending_before_provider() -> None:
        store = ProjectStore(harness.state)
        try:
            with store.control.read() as tx:
                assert (
                    tx.sql.execute(
                        "SELECT count(*) FROM research_model_calls WHERE usage IS NULL"
                    ).fetchone()[0]
                    == 1
                )
        finally:
            store.close()

    async with serving(
        harness,
        Reply(calls=[call("shell", script="printf confined")], before=pending_before_provider),
        Reply("Done."),
        serve=False,
    ) as service:
        gateway_models = ModelGateway(harness.gateway.url, API_KEY)
        finishing, release = asyncio.Event(), asyncio.Event()
        order: list[str] = []
        guarded_runs: set[int] = set()
        original_sent = Transcript.sent.fget
        assert original_sent is not None

        def guarded_sent(run: Transcript):
            if id(run) in guarded_runs:
                guarded_runs.remove(id(run))
                raise WorkingMemoryLimitError("synthetic host working-state ceiling")
            return original_sent(run)

        class MemoryGuard(TurnHook):
            name = "memory_pause_fixture"

            @asynccontextmanager
            async def lease(self) -> AsyncIterator[None]:
                try:
                    yield
                finally:
                    order.append("lease_exit")

            async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
                transcript = cast(Transcript, run)
                guarded_runs.add(id(transcript))
                _ = transcript.sent  # actual host property failure crosses the typed channel

            async def finishing(self, run: RunView, stopped: str) -> str | None:
                assert stopped == "memory_limit"
                order.append("finishing")
                finishing.set()
                await release.wait()
                order.append("drained")
                return None

        if memory_pause:
            monkeypatch.setattr(Transcript, "sent", property(guarded_sent))

        class ProfileGuard(TurnHook):
            name = "profile_context_fixture"

            async def notices(self, point, run):
                if point == "start":
                    assert service.harness.gateway is not None
                    model = service.harness.gateway.model("main", role="fold")
                    assert model.requested_profile.reasoning_effort == "high"
                return ()

        forged = False
        requested_profiles: list[object] = []

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            nonlocal forged
            if kind is Capability.MODEL_INTENT and isinstance(value, dict):
                requested_profiles.append(value.get("profile"))
            if (
                kind is Capability.LOG_APPEND
                and isinstance(value, dict)
                and value.get("kind") == "tool_result"
            ):
                # A compromised runtime cannot replace trusted execution evidence.
                value = {
                    **value,
                    "message": {"chat": {"role": "tool", "content": "FORGED"}},
                    "outcome": {"text": "FORGED", "record": {"stdout": "FORGED"}},
                }
                forged = True
            try:
                return await host.capability(kind, value)
            except WorkingMemoryLimitError:
                order.append("host_memory_limit")
                raise

        async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
            if kind is Capability.RUN:
                return await runtime.run(value)
            if kind is Capability.CANCEL:
                return await runtime.cancel(value)
            if kind is Capability.MODEL:
                return await vm_models.complete(value)
            if kind is Capability.MODEL_INFO:
                return await vm_models.info(value)
            raise TransportError("denied", "fixture refused capability")

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        vm_models = RuntimeModels(gateway_models, frozenset({"main"}), vm_channel)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, gateway_models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        service.harness.main = Role(
            CallbackModel(
                host_channel,
                "main",
                role="main",
                profile=ModelProfile(alias="main", reasoning_effort="max"),
            ),
            "main",
        )
        service.harness.gateway = CallbackGateway(host_channel, reasoning_effort={"fold": "high"})

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            assert service.harness.gateway is not None
            service.harness.gateway.reasoning_effort["fold"] = "low"
            options["hooks"] = (*options["hooks"], ProfileGuard())
            if memory_pause:
                options["hooks"] = (*options["hooks"], MemoryGuard())
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-runtime", "Print confined", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            if memory_pause:
                await asyncio.wait_for(finishing.wait(), 5)
                assert order == ["host_memory_limit", "finishing"]
                assert not task.done()
                assert service.runs.lease(PROJECT, SESSION) is not None
                assert runtime.active
                assert host.attempts
                release.set()
            await task
            (record,) = records(harness)
            assert record.model_profiles["main"].reasoning_effort == "max"
            assert requested_profiles
            assert all(
                profile == {"alias": "main", "api": "chat", "reasoning_effort": "max"}
                for profile in requested_profiles
            )
            assert record.final_text == (None if memory_pause else "Done.")
            assert record.failure is None
            assert record.stopped == ("memory_limit" if memory_pause else "answered")
            if memory_pause:
                assert order == ["host_memory_limit", "finishing", "drained", "lease_exit"]
                assert "synthetic host working-state ceiling" in str(record.summaries["memory"])
            assert forged
            assert "FORGED" not in str(record.tools)
            execution = record.tools[0]["execution"]
            assert isinstance(execution, dict)
            assert execution["state"] == "succeeded"
            result = execution["result"]
            assert isinstance(result, dict)
            stdout = result["stdout"]
            assert isinstance(stdout, dict)
            assert stdout["text"] == "confined"
            assert record.tools[0]["replayed"] is False
            assert not list(runtime.scratch.iterdir())
            assert not runtime.active
            assert not host.attempts
            with service.stores.projects.control.read() as tx:
                assert (
                    tx.sql.execute(
                        "SELECT count(*) FROM research_model_calls WHERE usage IS NULL"
                    ).fetchone()[0]
                    == 0
                )
                assert tx.sql.execute(
                    "SELECT count(*) FROM research_model_calls WHERE kind='main'"
                ).fetchone()[0] == (1 if memory_pause else 2)
                # Canonical archive rows own the full native model replies. The host's
                # paged working history must retain only scoped archive references.
                rows = tx.sql.execute(
                    "SELECT item FROM run_collections WHERE project=? AND session=? "
                    "AND collection='messages'",
                    (PROJECT, SESSION),
                ).fetchall()
                assert any('"archive_message"' in item for (item,) in rows)
                assert not any('"assistant-cache-v1"' in item for (item,) in rows)
                assert (
                    tx.sql.execute(
                        "SELECT count(*) FROM run_collections c LEFT JOIN archive_messages a "
                        "ON a.project=c.project AND a.message_id="
                        "json_extract(c.item,'$.archive_message') "
                        "WHERE c.project=? AND c.session=? AND c.collection='messages' "
                        "AND json_extract(c.item,'$.archive_message') IS NOT NULL "
                        "AND a.message_id IS NULL",
                        (PROJECT, SESSION),
                    ).fetchone()[0]
                    == 0
                )
        finally:
            release.set()
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await gateway_models.close()


def test_loop_metadata_never_iterates_historical_delivery_membership() -> None:
    from sandbox_science.orchestrator.confinement.runtime_loop import state
    from sandbox_science.orchestrator.loop import Transcript

    class Historical(set[str]):
        def __iter__(self):
            raise AssertionError("historical identities must stay behind the capability")

    transcript = Transcript(messages=[{"role": "system", "content": "bounded"}])
    transcript.delivered["jobs"] = Historical()
    transcript.offered["jobs"] = Historical()
    wire = state(transcript)
    assert wire.delivered == wire.offered == {}
    assert len(wire.model_dump_json()) < 4096
