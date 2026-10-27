# SPDX-License-Identifier: MIT
"""The runtime manager's serve loop against the fake OpenShell CLI and a fake control
(KRB-25): bootstrap only on request, ownership kept through uncertainty, reconnect at
start and on a new control instance, and bounded, opt-in recovery."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from sandbox_science.contracts.classifications import RuntimeClassifications
from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION_OUTPUT
from sandbox_science.launcher import openshell_version, runtime_owner
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.runtime_manager import (
    RuntimeManager,
    RuntimeManagerSettings,
    request,
)
from sandbox_science.launcher.runtime_manager.control import ControlClient
from sandbox_science.launcher.runtime_manager.fence import Reader
from sandbox_science.launcher.runtime_manager.manager import request_deadline
from sandbox_science.launcher.runtime_manager.records import ManagerRequest, Result, Status
from sandbox_science.launcher.state import private_dir
from sandbox_science.policy.store import Acceptance, PolicyStore
from sandbox_science.testing import eventually
from sandbox_science.worker.classifications import runtime_classifications

from .conftest import openshell_stand_in
from .manager_support import (
    FakeControl,
    LocalTls,
    live_runtimes,
    localhost_tls,
    settings_document,
)

pytestmark = pytest.mark.slow

RUNTIME = "sandbox_science.orchestrator.confinement.runtime"
SERVE = (sys.executable, "-m", "sandbox_science.launcher.runtime_manager")


class Site:
    """A fake control and fake CLI, and managers serving from one state directory."""

    def __init__(self, tmp_path: Path, control: FakeControl, url: str, tls: LocalTls) -> None:
        self.tmp_path, self.control, self.url, self.tls = tmp_path, control, url, tls
        self.state = tmp_path / "openshell.json"
        self.journal = tmp_path / "journal"
        self.cli = openshell_stand_in(
            tmp_path / "bin" / "openshell", FAKE_OPENSHELL=self.state, FAKE_JOURNAL=self.journal
        )
        self.document = settings_document(
            tmp_path, manager_url=url, ca=tls.certificate, cli=self.cli
        )
        self.root = Path(self.document["state_dir"])

    def settings(self, **changes: Any) -> RuntimeManagerSettings:
        return RuntimeManagerSettings.model_validate(self.document | changes)

    def manager(
        self,
        binding: runtime_owner.Binding | None = None,
        classifications: Reader | None = None,
        **changes: Any,
    ) -> RuntimeManager:
        return RuntimeManager(
            self.settings(**changes),
            binding=binding,
            classifications=classifications,
            poll=0.02,
            health_interval=0.05,
            delays=(0.02,) * 6,
            gateway_backoff=(0.05, 0.2),
            gateway_interval=0.5,
        )

    def fake(self) -> dict[str, Any]:
        return json.loads(self.state.read_text()) if self.state.exists() else {"sandboxes": {}}

    def set_fake(self, **changes: Any) -> None:
        temporary = self.state.with_name(".openshell.json.test")
        temporary.write_text(json.dumps(self.fake() | {"providers": []} | changes))
        temporary.replace(self.state)

    def steps(self) -> list[str]:
        return self.journal.read_text().splitlines() if self.journal.exists() else []

    def count(self, prefix: str) -> int:
        return sum(step.startswith(prefix) for step in self.steps())

    def record(self) -> dict[str, Any] | None:
        path = self.root / "sandbox.json"
        return json.loads(path.read_text()) if path.exists() else None

    def recovery(self) -> dict[str, Any]:
        return json.loads((self.root / "infrastructure-recovery.json").read_text())

    def status(self) -> dict[str, Any]:
        return json.loads((self.root / "status.json").read_text())

    def uploaded(self, name: str, file: str) -> str:
        return (self.tmp_path / "uploads" / name / file).read_text()

    async def ask(self, manager: RuntimeManager, action: str) -> str | None:
        """An operator request, as ``request`` hands it over; the receipt's error."""
        selected = ManagerRequest(
            nonce="replacement-" + secrets.token_hex(16),
            instance=manager.instance,
            action=action,  # pyright: ignore[reportArgumentType]
            delete=action == "stop",
        )
        (self.root / "request.json").write_text(selected.model_dump_json(exclude_none=True))
        manager.requested.set()

        def answered() -> Result | None:
            try:
                found = Result.model_validate_json((self.root / "result.json").read_bytes())
            except FileNotFoundError:
                return None
            return found if found.nonce == selected.nonce else None

        return (await eventually(answered, timeout=60)).error

    async def ticks(self, count: int = 5) -> None:
        """Let the manager look at control ``count`` more times."""
        seen = self.control.health_calls
        await eventually(lambda: self.control.health_calls >= seen + count, timeout=30)


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    tls = localhost_tls(tmp_path / "tls")
    control = FakeControl(tmp_path)
    async with control.serving(tls) as url:
        yield Site(tmp_path, control, url, tls)
    for pid in live_runtimes(tmp_path):
        os.kill(pid, signal.SIGKILL)


@asynccontextmanager
async def serving(manager: RuntimeManager) -> AsyncIterator[RuntimeManager]:
    task = asyncio.create_task(manager.run())
    try:
        yield manager
    finally:
        manager.stopping.set()
        await asyncio.wait_for(task, 30)


async def bootstrapped(site: Site) -> dict[str, Any]:
    """A sandbox made by one manager that has since stopped; its record."""
    async with serving(site.manager()) as manager:
        assert await site.ask(manager, "bootstrap") is None
    record = site.record()
    assert record is not None
    return record


def running(site: Site) -> Callable[[], bool]:
    return lambda: bool(live_runtimes(site.tmp_path)) and site.status()["runtime"] == "running"


async def test_bootstrap_only_on_request_records_before_create_and_attaches(site: Site) -> None:
    site.set_fake(record=str(site.root / "sandbox.json"))
    async with serving(site.manager()) as manager:
        await site.ticks()
        assert site.count("openshell sandbox create") == 0  # an empty PVC creates nothing
        assert site.record() is None
        assert site.status()["runtime"] == "stopped"
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        name = record["name"]
        assert record["id"] == site.fake()["sandboxes"][name]["id"]
        assert record["image"] == site.document["image"]
        assert record["driver_config"] == site.settings().driver_config()
        sandbox = site.fake()["sandboxes"][name]
        assert sandbox["driver_config"] == record["driver_config"]
        assert "kata" in sandbox["driver_config"]
        assert "sandbox-science-state-service" in sandbox["policy"]
        (seen,) = [step for step in site.steps() if "record at create" in step]
        assert json.loads(seen.split("record at create ", 1)[1])["id"] is None
        issued = site.control.latest()
        assert site.uploaded(name, "runtime.token").strip() == issued.token
        runtime = json.loads(site.uploaded(name, "runtime.json"))
        assert runtime["generation"] == issued.generation
        assert runtime["control_ca_file"] == "/sandbox/runtime/control-ca.crt"
        assert site.uploaded(name, "control-ca.crt") == site.tls.certificate.read_text()
        assert site.uploaded(name, "model.key") == "test-secret-model"
        chmod = f"openshell exec {name} chmod 600 /sandbox/runtime/runtime.token"
        assert any(step.startswith(chmod) for step in site.steps())
        assert any(RUNTIME in step for step in site.steps())
        await eventually(running(site))
        assert site.status()["control_instance"] == site.control.instance
        assert "token" not in (site.root / "credentials.json").read_text()
        assert issued.token is not None
        assert issued.token not in "".join(path.read_text() for path in site.root.glob("*.json"))
        error = await site.ask(manager, "bootstrap")
        assert error is not None
        assert "already recorded" in error
    assert site.count("openshell sandbox create") == 1


class Bindings:
    """What binds the runtime's sandbox in the policy store, recording its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def bind(self, sandbox_id: str) -> None:
        self.calls.append(("bind", sandbox_id))

    async def unbind(self, sandbox_id: str) -> None:
        self.calls.append(("unbind", sandbox_id))


async def test_the_runtime_sandbox_is_bound_when_created_and_unbound_when_deleted(
    site: Site,
) -> None:
    bindings = Bindings()
    async with serving(site.manager(bindings)) as manager:
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        assert bindings.calls == [("bind", record["id"])]
        assert await site.ask(manager, "replace") is None
        replaced = site.record()
        assert replaced is not None
        assert replaced["id"] != record["id"]
        assert bindings.calls == [
            ("bind", record["id"]),
            ("unbind", record["id"]),
            ("bind", replaced["id"]),
        ]
        assert await site.ask(manager, "stop") is None
    assert site.record() is None
    assert bindings.calls[3:] == [("unbind", replaced["id"])]


def direct_files(site: Site) -> dict[str, Any]:
    """The direct MCP path's settings, with the site's files as the site generator puts
    them in the manager's Secret (the example policy)."""
    root = Path(__file__).parents[3]
    ceiling = site.tmp_path / "ceiling.yaml"
    ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
    token = site.tmp_path / "secret" / "manager-worker.token"
    token.write_text("test-secret-manager-worker")
    return {
        "worker": {
            "url": "https://worker.example.test:8100",
            "ca_file": str(site.tls.certificate),
            "token_file": str(token),
            "project": "crypto",
        },
        "mcp": {
            "project_policy_file": str(root / "examples/policy.yaml"),
            "ceiling_file": str(ceiling),
            "targets_file": str(root / "examples/openshell/targets.yaml"),
            "servers": {SERVER: {"credentials": {"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"}}},
            "providers": [SERVER],
        },
    }


SERVER = "crypto-research-mcp"
READ_TOOL = "attack-catalog-mcp_search_attacks"
NEW_TOOL = "attack-catalog-mcp_brand_new"


class StoreClassifications:
    """The worker's ``GET /v1/runtime-classifications`` over a local policy store, as the
    manager's worker link reads it, recording each read."""

    def __init__(self, root: Path) -> None:
        self.store = PolicyStore(root)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, project: str, servers: Sequence[str]) -> RuntimeClassifications:
        self.calls.append((project, tuple(servers)))
        return runtime_classifications(self.store, project, servers)

    def accept(self, tools: dict[str, str]) -> str:
        entries = "".join(
            f'  {name}:\n    class: {effect}\n    reason: "Reviewed."\n'
            for name, effect in tools.items()
        )
        data = (
            "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\n"
            f"server: {SERVER}\ntools:\n{entries}"
        ).encode()
        found = self.store.accept_classification(
            "crypto", SERVER, data, Acceptance("user", datetime.now(UTC), "reviewed")
        )
        assert found.revision is not None
        return found.revision


def fence(site: Site, name: str) -> tuple[set[str], set[str]]:
    """The tools a created sandbox's fence allows by name, and those its runtime is told."""
    policy = yaml.safe_load(site.fake()["sandboxes"][name]["policy"])
    (endpoint,) = policy["network_policies"][f"mcp-{SERVER}"]["endpoints"]
    allowed = {
        tool
        for rule in endpoint["rules"]
        if rule["allow"]["method"] == "tools/call"
        for tool in rule["allow"]["tool"]["any"]
    }
    runtime = json.loads(site.uploaded(name, "runtime.json"))
    return allowed, set(runtime["mcp"]["servers"][SERVER]["tools"]["allow"])


async def test_the_manager_compiles_the_fence_from_the_projects_bound_revision(
    site: Site,
) -> None:
    site.set_fake(providers=[SERVER])
    reader = StoreClassifications(site.tmp_path / "policy-store")
    reader.accept({READ_TOOL: "browse", NEW_TOOL: "read"})
    async with serving(site.manager(Bindings(), reader, **direct_files(site))) as manager:
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        allowed, told = fence(site, record["name"])
        assert NEW_TOOL in allowed  # widened by the revision
        assert READ_TOOL not in allowed  # narrowed: browse is denied
        assert told == allowed  # the runtime is offered what its fence admits
    assert reader.calls == [("crypto", (SERVER,))]


@pytest.mark.parametrize("binding", [None, "not json", '{"revision": "' + "d" * 64 + '"}'])
async def test_the_manager_falls_back_to_the_checked_in_file_without_a_readable_binding(
    site: Site, binding: str | None
) -> None:
    site.set_fake(providers=[SERVER])
    reader = StoreClassifications(site.tmp_path / "policy-store")
    if binding is not None:
        reader.accept({NEW_TOOL: "read"})
        current = site.tmp_path / "policy-store/classifications/crypto" / SERVER
        (current / "current.json").write_text(binding)
    async with serving(site.manager(Bindings(), reader, **direct_files(site))) as manager:
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        allowed, told = fence(site, record["name"])
        assert READ_TOOL in allowed  # the checked-in classification
        assert NEW_TOOL not in allowed
        assert told == allowed


async def test_the_manager_falls_back_when_the_worker_does_not_answer(site: Site) -> None:
    site.set_fake(providers=[SERVER])

    async def unanswered(project: str, servers: Sequence[str]) -> RuntimeClassifications:
        raise LauncherError("the worker did not answer")

    async with serving(site.manager(Bindings(), unanswered, **direct_files(site))) as manager:
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        allowed, _ = fence(site, record["name"])
        assert READ_TOOL in allowed
        assert NEW_TOOL not in allowed


async def test_the_manager_recompiles_the_fence_at_replace_after_an_acceptance(
    site: Site,
) -> None:
    site.set_fake(providers=[SERVER])
    reader = StoreClassifications(site.tmp_path / "policy-store")
    async with serving(site.manager(Bindings(), reader, **direct_files(site))) as manager:
        assert await site.ask(manager, "bootstrap") is None
        first = site.record()
        assert first is not None
        before, _ = fence(site, first["name"])
        assert NEW_TOOL not in before
        # Accepted while the runtime runs: the middleware decides by it from the next call,
        # and the fence follows at the next creation of the sandbox.
        reader.accept({READ_TOOL: "read", NEW_TOOL: "read"})
        assert await site.ask(manager, "replace") is None
        second = site.record()
        assert second is not None
        assert second["name"] != first["name"]
        after, told = fence(site, second["name"])
        assert NEW_TOOL in after
        assert told == after
    assert len(reader.calls) == 2


async def test_the_direct_path_attaches_its_provider_and_merges_its_egress(site: Site) -> None:
    site.set_fake(providers=["crypto-research-mcp"])
    bindings = Bindings()
    reader = StoreClassifications(site.tmp_path / "policy-store")
    async with serving(site.manager(bindings, reader, **direct_files(site))) as manager:
        assert await site.ask(manager, "bootstrap") is None
        record = site.record()
        assert record is not None
        sandbox = site.fake()["sandboxes"][record["name"]]
        assert sandbox["providers"] == ["crypto-research-mcp"]
        assert sandbox["labels"] == {"sandbox-science.io/project": "crypto"}
        policy = yaml.safe_load(sandbox["policy"])
        assert "sandbox-science-state-service" in policy["network_policies"]
        assert "mcp-crypto-research-mcp" in policy["network_policies"]
        (middleware,) = policy["network_middlewares"].values()
        assert middleware["config"]["mcp"]["project"] == "crypto"
        runtime = json.loads(site.uploaded(record["name"], "runtime.json"))
        assert runtime["mcp"]["servers"]["crypto-research-mcp"]["tools"]["allow"]
        assert bindings.calls == [("bind", record["id"])]


def test_the_direct_path_needs_the_worker_link_that_binds_the_sandbox(site: Site) -> None:
    direct = direct_files(site)
    with pytest.raises(ValidationError, match="worker link"):
        site.settings(mcp=direct["mcp"])


async def test_sigterm_keeps_the_sandbox_and_reconnects_at_start_and_new_control_only(
    site: Site,
) -> None:
    record = await bootstrapped(site)
    assert site.count("openshell sandbox delete") == 0
    assert site.record() == record
    assert live_runtimes(site.tmp_path) == []  # the exec session ended with the manager
    asked = len(site.control.asked)
    async with serving(site.manager()):
        await eventually(running(site))  # reconnect at start, without a request
        assert len(site.control.asked) == asked + 1
        assert site.count("openshell sandbox create") == 1
        # A runtime exit under the same control instance: no reattach without the opt-in.
        (pid,) = live_runtimes(site.tmp_path)
        os.kill(pid, signal.SIGKILL)
        await eventually(lambda: site.status()["runtime"] == "stopped")
        await site.ticks(10)
        assert len(site.control.asked) == asked + 1
        assert "recover_infrastructure is off" in site.status()["note"]
        site.control.restart()
        await eventually(running(site))
        assert len(site.control.asked) == asked + 2
        assert site.status()["control_instance"] == site.control.instance
        # A control restart while the runtime runs: the manager reconnects again.
        site.control.restart()
        await eventually(lambda: len(site.control.asked) == asked + 3)
        await eventually(running(site))
    assert site.count("openshell sandbox create") == 1
    assert site.count("openshell sandbox delete") == 0
    assert site.record() == record


async def test_a_runtime_exit_is_reattached_with_recover_infrastructure(site: Site) -> None:
    record = await bootstrapped(site)
    async with serving(site.manager(recover_infrastructure=True)):
        await eventually(running(site))
        asked = len(site.control.asked)
        (pid,) = live_runtimes(site.tmp_path)
        os.kill(pid, signal.SIGKILL)
        await eventually(lambda: len(site.control.asked) == asked + 1)
        await eventually(running(site))
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1


@pytest.mark.parametrize("started", [False, True])
async def test_a_journaled_request_replays_unless_an_exec_used_it(
    site: Site, started: bool
) -> None:
    record = await bootstrapped(site)
    client = ControlClient(
        site.url,
        ca_file=site.tls.certificate,
        token_file=Path(site.document["manager_token_file"]),
    )
    crashed = secrets.token_hex(16)
    issued = await asyncio.to_thread(client.issue, crashed)
    journal = {"request": crashed, "started": started}
    (site.root / "credentials.json").write_text(json.dumps(journal))
    async with serving(site.manager()):
        await eventually(running(site))
    uploaded = site.uploaded(record["name"], "runtime.token").strip()
    if started:
        assert site.control.asked[-1] != crashed
        assert uploaded != issued.token
    else:
        assert site.control.asked[-1] == crashed  # the crashed request, replayed
        assert uploaded == issued.token


async def test_a_superseded_journaled_request_is_replaced_by_a_new_one(site: Site) -> None:
    record = await bootstrapped(site)
    old = site.control.asked[-1]
    site.control.restart()  # the latest issue is superseded
    (site.root / "credentials.json").write_text(json.dumps({"request": old}))
    async with serving(site.manager()):
        await eventually(running(site))
    assert site.control.asked[-2:] == [old, site.control.latest().request]
    assert site.control.latest().request != old
    assert site.uploaded(record["name"], "runtime.token").strip() == site.control.latest().token


@pytest.mark.parametrize("mismatch", ["name", "id", "unknown"])
async def test_a_record_the_gateway_does_not_confirm_blocks_automatic_action(
    site: Site, mismatch: str
) -> None:
    record = await bootstrapped(site)
    if mismatch == "name":
        sandboxes = site.fake()["sandboxes"]
        sandboxes["research-1"] = sandboxes.pop(record["name"])
        site.set_fake(sandboxes=sandboxes)
        record["name"] = "research-1"
    elif mismatch == "id":
        sandboxes = site.fake()["sandboxes"]
        sandboxes[record["name"]]["id"] = "id-of-another-sandbox"
        site.set_fake(sandboxes=sandboxes)
    else:
        record["id"] = None
    (site.root / "sandbox.json").write_text(json.dumps(record))
    asked = len(site.control.asked)
    async with serving(site.manager(recover_infrastructure=True)):
        await eventually(lambda: site.recovery()["blocked"])
        await site.ticks()
        assert "blocked" in site.status()["note"]
    assert len(site.control.asked) == asked
    assert site.count("openshell sandbox delete") == 0
    assert site.count("openshell sandbox create") == 1
    assert site.record() == record


async def test_a_pending_journal_blocks_at_startup_until_an_explicit_replace(site: Site) -> None:
    record = await bootstrapped(site)
    (site.root / "infrastructure-recovery.json").write_text(
        json.dumps({"attempts": 1, "pending": True})
    )
    asked = len(site.control.asked)
    async with serving(site.manager(recover_infrastructure=True)) as manager:
        await site.ticks()
        assert site.recovery()["blocked"]
        assert len(site.control.asked) == asked
        assert live_runtimes(site.tmp_path) == []
        assert await site.ask(manager, "replace") is None
        await eventually(running(site))
        assert site.recovery() == {
            "attempts": 0,
            "retry_after": 0.0,
            "pending": False,
            "retryable": False,
            "blocked": False,
            "error": None,
        }
    replaced = site.record()
    assert replaced is not None
    assert replaced["name"] != record["name"]
    assert record["name"] not in site.fake()["sandboxes"]
    assert site.steps().index(f"openshell sandbox delete {record['name']}") < site.steps().index(
        f"openshell sandbox create {replaced['name']} {site.document['image']} sleep infinity"
    )


async def test_automatic_attempts_stop_after_ten(site: Site) -> None:
    await bootstrapped(site)
    site.set_fake(behaviour="exec-fails")
    async with serving(site.manager()):
        await eventually(lambda: site.recovery()["blocked"], timeout=60)
        await site.ticks()
    assert site.recovery()["attempts"] == 10
    assert sum(RUNTIME in step for step in site.steps()) == 1 + 10
    assert site.count("openshell sandbox create") == 1


async def test_a_gone_sandbox_is_replaced_only_with_the_opt_in(site: Site) -> None:
    record = await bootstrapped(site)
    site.set_fake(sandboxes={})
    async with serving(site.manager()):
        await eventually(lambda: site.recovery()["blocked"])
    assert "recover_infrastructure" in site.recovery()["error"]
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1
    (site.root / "infrastructure-recovery.json").unlink()
    async with serving(site.manager(recover_infrastructure=True)):
        await eventually(running(site))
    replaced = site.record()
    assert replaced is not None
    assert replaced["name"] != record["name"]
    assert replaced["driver_config"] == record["driver_config"]  # recovery reuses it
    assert site.count("openshell sandbox create") == 2


def settled(recovery: dict[str, Any]) -> bool:
    """At least two attempts made, the last one finished."""
    return recovery["attempts"] >= 2 and not recovery["pending"]


@pytest.mark.parametrize("trouble", ["get-gateway-not-found", "delete-ignored"])
async def test_no_replacement_without_confirmed_absence(site: Site, trouble: str) -> None:
    record = await bootstrapped(site)
    sandboxes = site.fake()["sandboxes"]
    sandboxes[record["name"]]["phase"] = "Error"
    site.set_fake(sandboxes=sandboxes, behaviour=trouble)
    async with serving(site.manager(recover_infrastructure=True, stop_timeout=0.2)):
        if trouble == "get-gateway-not-found":
            # A gateway that does not answer is waited for: no attempt is made or used.
            await eventually(lambda: site.status()["note_kind"] == "gateway_unreachable")
            await site.ticks()
            assert "Unavailable" in site.status()["note"]
            assert site.recovery()["attempts"] == 0
        else:
            await eventually(lambda: settled(site.recovery()), timeout=60)
            assert "not confirmed" in site.recovery()["error"]
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1


async def test_uncertain_create_and_delete_keep_the_record(site: Site) -> None:
    site.set_fake(behaviour="create-pending")  # the sandbox is made, but create fails
    async with serving(site.manager()) as manager:
        error = await site.ask(manager, "bootstrap")
        assert error is not None
        assert "timed out" in error
        record = site.record()
        assert record is not None
        assert record["id"] is None
        site.set_fake(behaviour="delete-ignored")
        error = await site.ask(manager, "stop")
        assert error is not None
        assert "not confirmed" in error
        adopted = site.record()
        assert adopted is not None
        assert adopted["id"] == site.fake()["sandboxes"][record["name"]]["id"]
        site.set_fake(behaviour="")
        assert await site.ask(manager, "stop") is None
        assert site.record() is None
        assert await site.ask(manager, "bootstrap") is None
        await eventually(running(site))
    site.set_fake(behaviour="create-fails")  # nothing is made: the outcome stays unknown
    (site.root / "sandbox.json").unlink()
    async with serving(site.manager()) as manager:
        assert await site.ask(manager, "bootstrap") is not None
        record = site.record()
        assert record is not None
        error = await site.ask(manager, "replace")
        assert error is not None
        assert "creation outcome is unknown" in error
        assert site.record() == record


async def test_the_runtime_output_is_drained_past_an_oversized_line(
    site: Site, capsys: pytest.CaptureFixture[str]
) -> None:
    site.set_fake(behaviour="exec-floods")
    seen: list[str] = []

    def shown() -> str:
        seen.append(capsys.readouterr().out)
        return "".join(seen)

    async with serving(site.manager()) as manager:
        assert await site.ask(manager, "bootstrap") is None
        # The exec wrote far more than a pipe holds after its long line: it never blocked.
        await eventually(lambda: (site.tmp_path / "flooded").exists())
        await eventually(lambda: "runtime: flood done" in shown())
        await eventually(running(site))
    lines = shown().splitlines()
    (cut,) = [line for line in lines if "line cut at" in line]
    assert len(cut) < 64 * 1024 + 64
    assert "runtime: line 1999 " + "y" * 200 in lines


def test_a_second_serve_on_one_state_directory_is_refused(tmp_path: Path) -> None:
    tls = localhost_tls(tmp_path / "tls")
    cli = tmp_path / "openshell"
    cli.write_text(f"#!/bin/sh\necho {OPENSHELL_VERSION_OUTPUT}\n")
    cli.chmod(0o755)
    document = settings_document(
        tmp_path, manager_url="https://localhost:9", ca=tls.certificate, cli=cli
    )
    settings = tmp_path / "runtime-manager.json"
    settings.write_text(json.dumps(document))
    root = private_dir(Path(document["state_dir"]))
    held = os.open(root / "serve.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # as a serving manager holds it
        second = subprocess.run(
            [*SERVE, "--settings", str(settings), "serve"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        os.close(held)
    assert second.returncode == 1
    assert "another runtime manager is serving" in second.stderr
    assert not (root / "status.json").exists()


async def test_an_operator_create_whose_attach_failed_is_attached_by_the_serve_loop(
    site: Site,
) -> None:
    site.set_fake(behaviour="exec-fails")
    async with serving(site.manager()) as manager:
        error = await site.ask(manager, "bootstrap")
        assert error is not None
        assert "keeps trying to attach" in error
        site.set_fake(behaviour="")
        record = site.record()
        assert record is not None
        await eventually(running(site))
        assert site.record() == record
    assert site.count("openshell sandbox create") == 1


async def test_a_replace_while_control_is_down_attaches_once_control_answers(site: Site) -> None:
    record = await bootstrapped(site)
    async with serving(site.manager()) as manager:
        await eventually(running(site))
        await site.control.down()
        error = await site.ask(manager, "replace")
        assert error is not None
        assert "keeps trying to attach" in error
        replaced = site.record()
        assert replaced is not None
        assert replaced["name"] != record["name"]
        await site.control.up(site.tls)
        await eventually(running(site))
    assert site.record() == replaced
    assert site.count("openshell sandbox create") == 2


async def test_forget_clears_only_an_id_less_record_the_gateway_proves_absent(
    site: Site,
) -> None:
    site.set_fake(behaviour="create-fails")
    async with serving(site.manager()) as manager:
        assert await site.ask(manager, "bootstrap") is not None
        record = site.record()
        assert record is not None
        assert record["id"] is None
        site.set_fake(behaviour="get-fails")  # a gateway error is not absence
        assert await site.ask(manager, "forget") is not None
        assert site.record() == record
        site.set_fake(behaviour="")
        assert await site.ask(manager, "forget") is None
        assert site.record() is None
        assert await site.ask(manager, "bootstrap") is None
        await eventually(running(site))
        error = await site.ask(manager, "forget")  # the record has the gateway's ID
        assert error is not None
        assert "gateway ID" in error
        assert await site.ask(manager, "stop") is None
        site.set_fake(behaviour="create-pending")  # made, though create failed
        assert await site.ask(manager, "bootstrap") is not None
        pending = site.record()
        assert pending is not None
        assert pending["id"] is None
        error = await site.ask(manager, "forget")
        assert error is not None
        assert "the gateway has a sandbox" in error
        assert site.record() == pending
    assert site.count("openshell sandbox delete") == 1


async def test_sigterm_during_a_stop_still_ends_the_exec(site: Site) -> None:
    await bootstrapped(site)
    site.set_fake(behaviour="exec-ignores-term")
    manager = site.manager()
    task = asyncio.create_task(manager.run())
    try:
        await eventually(running(site))
        asking = asyncio.create_task(site.ask(manager, "stop"))
        await eventually(lambda: any("ignores SIGTERM" in step for step in site.steps()))
        manager.stopping.set()  # SIGTERM while the teardown waits for the exec
        await asyncio.wait_for(task, 30)
        asking.cancel()
        await asyncio.gather(asking, return_exceptions=True)
    finally:
        manager.stopping.set()
        await asyncio.wait_for(task, 30)
    assert live_runtimes(site.tmp_path) == []


async def test_a_request_written_while_another_is_answered_is_kept(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serving(site.manager()) as manager:
        newer = ManagerRequest(
            nonce="replacement-" + secrets.token_hex(16),
            instance=manager.instance,
            action="stop",
            delete=True,
        )
        real = Path.unlink
        written: list[bool] = []

        def unlink(path: Path, missing_ok: bool = False) -> None:
            # A newer request arrives just as the manager clears the one it answered.
            if (
                path.parent == site.root
                and path.name.startswith("request")
                and not written
                and (site.root / "result.json").exists()
            ):
                written.append(True)
                (site.root / "request.json").write_text(newer.model_dump_json())
            real(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert await site.ask(manager, "stop") is None  # nothing recorded, nothing to do
        assert written

        def answered() -> bool:
            found = Result.model_validate_json((site.root / "result.json").read_bytes())
            return found.nonce == newer.nonce

        await eventually(answered)


async def test_a_starting_manager_names_itself_before_its_first_reconcile(site: Site) -> None:
    await bootstrapped(site)
    site.control.occupied = True  # the first attach waits for the old runtime to drain
    async with serving(site.manager(stop_timeout=60)) as manager:
        await eventually(lambda: site.status()["instance"] == manager.instance, timeout=10)
        assert site.status()["runtime"] == "starting"
        site.control.occupied = None
        await eventually(running(site))


async def test_an_interrupted_reattach_is_retried_at_the_next_start(site: Site) -> None:
    record = await bootstrapped(site)
    site.control.occupied = True
    async with serving(site.manager(stop_timeout=60)):
        await eventually(lambda: site.recovery()["pending"])
        assert site.recovery()["retryable"]
    assert site.recovery()["pending"]  # SIGTERM during the attach
    site.control.occupied = None
    async with serving(site.manager()):
        await eventually(running(site))
        assert not site.recovery()["blocked"]
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1


def test_a_request_waits_as_long_as_the_managers_bounds_allow(tmp_path: Path) -> None:
    document = settings_document(
        tmp_path, manager_url="https://localhost:9", ca=tmp_path / "ca.pem"
    )
    settings = RuntimeManagerSettings.model_validate(
        document | {"start_timeout": 0.01, "stop_timeout": 0.01}
    )
    assert request_deadline(settings) > 2 * 0.01 + 2 * 0.01 + 10 + 2
    root = private_dir(settings.state_dir)
    slow = (
        "import json, pathlib, signal, sys, time\n"
        "asked = []\n"
        "signal.signal(signal.SIGUSR2, lambda *_: asked.append(1))\n"
        "print('ready', flush=True)\n"
        "while not asked: time.sleep(0.02)\n"
        "time.sleep(12)  # longer than the old fixed deadline, within the bounds\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "nonce = json.loads((root / 'request.json').read_text())['nonce']\n"
        "(root / 'result.json').write_text(json.dumps({'nonce': nonce}))\n"
        "time.sleep(30)\n"
    )
    manager = subprocess.Popen(
        [sys.executable, "-c", slow, str(root)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert manager.stdout is not None
        assert manager.stdout.readline() == "ready\n"
        status = Status(instance="instance-" + "a" * 32, pid=manager.pid, runtime="running")
        (root / "status.json").write_text(status.model_dump_json())
        assert request(settings, "replace", poll=0.05) == 0
    finally:
        manager.kill()
        manager.wait()


def test_the_manager_says_its_lines_as_json_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandbox_science.launcher.runtime_manager.manager import say

    monkeypatch.delenv("SS_LOG_FORMAT", raising=False)
    say("runtime attached")
    assert capsys.readouterr().out == "runtime attached\n"
    monkeypatch.setenv("SS_LOG_FORMAT", "json")
    say("runtime attached with token=test-secret-1")
    record = json.loads(capsys.readouterr().out)
    assert (record["service"], record["level"]) == ("runtime-manager", "info")
    assert "test-secret-1" not in record["msg"]


def test_a_note_is_a_structured_record_in_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    from sandbox_science.launcher.runtime_manager.manager import NOTE_EVENT, say_note

    since = datetime(2026, 10, 25, 12, tzinfo=UTC)
    monkeypatch.setenv("SS_LOG_FORMAT", "json")
    say_note("gateway_unreachable", "the OpenShell gateway is unreachable", since)
    record = json.loads(capsys.readouterr().out)
    assert (record["service"], record["level"], record["event"]) == (
        "runtime-manager",
        "warning",
        NOTE_EVENT,
    )
    assert (record["kind"], record["since"]) == ("gateway_unreachable", since.isoformat())


def test_the_manager_starts_its_logging_when_it_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sandbox_science.launcher.runtime_manager import operations

    set_up: list[str] = []
    monkeypatch.setattr(operations, "configure_logging", set_up.append)
    monkeypatch.setattr(
        operations, "load", lambda path: SimpleNamespace(openshell=SimpleNamespace(cli="x"))
    )
    monkeypatch.setattr(openshell_version, "require", lambda cli: None)

    async def serve(settings: object) -> None:
        return None

    monkeypatch.setattr(operations, "serve", serve)
    assert operations.main(["--settings", str(tmp_path / "x.json"), "serve"]) == 0
    assert set_up == ["runtime-manager"]


def test_the_manager_refuses_another_release_of_the_openshell_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandbox_science.launcher.runtime_manager import operations

    cli = tmp_path / "openshell"
    cli.write_text("#!/bin/sh\necho openshell 0.1.3\n")
    cli.chmod(0o755)
    monkeypatch.setattr(
        operations, "load", lambda path: SimpleNamespace(openshell=SimpleNamespace(cli=str(cli)))
    )

    async def serve(settings: object) -> None:
        raise AssertionError("served with the wrong OpenShell")

    monkeypatch.setattr(operations, "serve", serve)
    assert operations.main(["--settings", str(tmp_path / "x.json"), "serve"]) == 1
    assert "needs OpenShell" in capsys.readouterr().err


def noted(site: Site, kind: str | None) -> Callable[[], bool]:
    return lambda: site.status()["note_kind"] == kind


async def test_a_gateway_that_refuses_is_waited_for_and_the_manager_resumes(
    site: Site, capsys: pytest.CaptureFixture[str]
) -> None:
    record = await bootstrapped(site)
    asked = len(site.control.asked)
    site.set_fake(behaviour="gateway-refuses")
    async with serving(site.manager(recover_infrastructure=True)):
        await eventually(noted(site, "gateway_unreachable"))
        first = site.status()
        assert "Connection refused" in first["note"]
        assert first["note"].startswith("the OpenShell gateway is unreachable: openshell sandbox")
        assert first["runtime"] == "stopped"
        await site.ticks(20)  # several more looks at the gateway, each refused
        assert site.status()["note_since"] == first["note_since"]  # since it began
        # Logged once when it began, not at every look: again only after NOTE_REPEAT
        # (the alert rules read it, docs/KUBERNETES-BUNDLE.md#alert-rules).
        assert capsys.readouterr().out.count("note gateway_unreachable since ") == 1
        assert site.recovery()["attempts"] == 0  # waiting uses none of the ten
        assert len(site.control.asked) == asked
        site.set_fake(behaviour="")
        await eventually(running(site))
        assert site.status()["note"] is None
        assert site.status()["note_kind"] is None
        assert site.status()["note_since"] is None
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1


def test_serve_stays_up_while_the_gateway_refuses_at_start(tmp_path: Path) -> None:
    """The process, as Kubernetes runs it: no exit, so no crash loop."""
    tls = localhost_tls(tmp_path / "tls")
    cli = openshell_stand_in(
        tmp_path / "bin" / "openshell",
        FAKE_OPENSHELL=tmp_path / "openshell.json",
        FAKE_JOURNAL=tmp_path / "journal",
    )
    (tmp_path / "openshell.json").write_text(
        json.dumps({"providers": [], "sandboxes": {}, "behaviour": "gateway-refuses"})
    )
    document = settings_document(
        tmp_path, manager_url="https://localhost:9", ca=tls.certificate, cli=cli
    )
    settings = tmp_path / "runtime-manager.json"
    settings.write_text(json.dumps(document))
    root = Path(document["state_dir"])
    manager = subprocess.Popen(
        [*SERVE, "--settings", str(settings), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:

        def waiting() -> bool:
            assert manager.poll() is None, "the manager exited"
            try:
                found = json.loads((root / "status.json").read_text())
            except (FileNotFoundError, ValueError):
                return False
            return found["note_kind"] == "gateway_unreachable"

        asyncio.run(eventually(waiting, timeout=60))
        status = json.loads((root / "status.json").read_text())
        assert "Connection refused" in status["note"]
        assert status["note_since"] is not None
        manager.send_signal(signal.SIGTERM)
        output, _ = manager.communicate(timeout=60)
    finally:
        if manager.poll() is None:
            manager.kill()
            manager.wait()
    assert manager.returncode == 0
    assert "the OpenShell gateway does not answer; waiting for it" in output


def test_an_unusable_state_directory_is_still_fatal(tmp_path: Path) -> None:
    tls = localhost_tls(tmp_path / "tls")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    document = settings_document(
        tmp_path, manager_url="https://localhost:9", ca=tls.certificate
    ) | {"state_dir": str(locked / "state")}
    settings = tmp_path / "runtime-manager.json"
    settings.write_text(json.dumps(document))
    try:
        found = subprocess.run(
            [*SERVE, "--settings", str(settings), "serve"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=os.environ | {"PATH": f"{openshell_dir(tmp_path)}:{os.environ['PATH']}"},
        )
    finally:
        locked.chmod(0o700)
    assert found.returncode == 1
    assert found.stderr.startswith("error: ")
    assert "Permission denied" in found.stderr
    assert "Traceback" not in found.stderr


def openshell_dir(tmp_path: Path) -> Path:
    """A directory with an ``openshell`` of the pinned release, for the version check."""
    directory = tmp_path / "pinned"
    directory.mkdir(exist_ok=True)
    cli = directory / "openshell"
    cli.write_text(f"#!/bin/sh\necho {OPENSHELL_VERSION_OUTPUT}\n")
    cli.chmod(0o755)
    return directory


async def test_a_note_clears_when_control_answers_again(site: Site) -> None:
    await bootstrapped(site)
    async with serving(site.manager()):
        await eventually(running(site))
        assert site.status()["note"] is None
        await site.control.down()
        await eventually(noted(site, "control_unreachable"))
        assert "control's manager listener is unreachable" in site.status()["note"]
        await site.control.up(site.tls)
        await eventually(noted(site, None))
        assert site.status()["note"] is None
        await eventually(running(site))


@pytest.mark.parametrize(
    ("behaviour", "said"),
    [("create-fails", "error: create failed"), ("create-silent", "no error text from openshell")],
)
async def test_a_failed_replace_names_what_failed(site: Site, behaviour: str, said: str) -> None:
    await bootstrapped(site)
    async with serving(site.manager()) as manager:
        await eventually(running(site))
        site.set_fake(behaviour=behaviour)
        error = await site.ask(manager, "replace")
        assert error == f"openshell sandbox create failed (exit 1): {said}"
        await eventually(noted(site, "action_failed"))
        assert site.status()["note"] == f"operator replace failed: {error}"
        site.set_fake(behaviour="")
        assert await site.ask(manager, "forget") is None  # an action that succeeds
        await eventually(noted(site, None))


async def test_a_rolled_back_create_is_said_and_cleared_by_the_next_bootstrap(
    site: Site,
) -> None:
    site.set_fake(behaviour="create-rolls-back")
    async with serving(site.manager(recover_infrastructure=True)) as manager:
        error = await site.ask(manager, "bootstrap")
        assert error is not None
        assert "the gateway rolled back the creation of ss-orch-" in error
        assert "did not become ready within the bootstrap grace" in error
        assert "request bootstrap" in error
        record = site.record()
        assert record is not None
        assert record["outcome"] == "create_rolled_back"
        assert record["reason"] == "sandbox did not become ready within the bootstrap grace"
        assert record["id"] is not None  # seen once before the gateway rolled it back
        await eventually(noted(site, "action_failed"))
        assert "rolled back" in site.status()["note"]
    # A restart reconnects a recorded sandbox, but never a rolled-back one.
    async with serving(site.manager(recover_infrastructure=True)) as manager:
        await site.ticks(10)
        assert site.recovery()["attempts"] == 0
        assert site.record() == record
        assert site.status()["note_kind"] == "action_failed"
        assert "request bootstrap creates another" in site.status()["note"]
        site.set_fake(behaviour="")
        assert await site.ask(manager, "bootstrap") is None  # no forget needed first
        await eventually(running(site))
        replaced = site.record()
        assert replaced is not None
        assert replaced["name"] != record["name"]
        assert "outcome" not in replaced
        assert site.status()["note"] is None
    assert site.count("openshell sandbox create") == 2


async def test_a_rolled_back_record_is_cleared_only_on_confirmed_absence(site: Site) -> None:
    site.set_fake(behaviour="create-rolls-back")
    async with serving(site.manager()) as manager:
        assert await site.ask(manager, "bootstrap") is not None
        record = site.record()
        assert record is not None
        site.set_fake(behaviour="get-fails")  # a gateway error is not absence
        error = await site.ask(manager, "bootstrap")
        assert error is not None
        assert "gateway is not reachable" in error
        assert site.record() == record
    assert site.count("openshell sandbox create") == 1
