# SPDX-License-Identifier: MIT
"""The runtime manager's serve loop against the fake OpenShell CLI and a fake control
(KRB-25): bootstrap only on request, ownership kept through uncertainty, reconnect at
start and on a new control instance, and bounded, opt-in recovery."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.runtime_manager import RuntimeManager, RuntimeManagerSettings
from sandbox_science.launcher.runtime_manager.control import ControlClient
from sandbox_science.launcher.runtime_manager.records import ManagerRequest, Result
from sandbox_science.testing import eventually

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

    def manager(self, **changes: Any) -> RuntimeManager:
        return RuntimeManager(
            self.settings(**changes), poll=0.02, health_interval=0.05, delays=(0.02,) * 6
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
        await eventually(lambda: settled(site.recovery()), timeout=60)
    assert site.record() == record
    assert site.count("openshell sandbox create") == 1
    error = site.recovery()["error"]
    assert ("Unavailable" if trouble == "get-gateway-not-found" else "not confirmed") in error


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
