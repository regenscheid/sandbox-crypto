# SPDX-License-Identifier: MIT
"""The runtime sandbox's ownership record, against a fake OpenShell CLI."""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from sandbox_science.launcher import runtime_owner
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.mcp import OpenShell, OpenShellError, SandboxMissingError
from sandbox_science.launcher.runtime_owner import FileOwnership, Owned
from sandbox_science.testing.mcp import SERVER_HOST, make_tls

GATEWAY_DOWN = 'openshell sandbox get failed: status: Unavailable, self: "sandbox not found"'
"""A gateway failure whose text mentions "not found" without being the NotFound status."""


class FakeCli(OpenShell):
    """The real ``sandbox()`` over a fake ``run``: sandboxes by name, each an ID and phase."""

    def __init__(self, store: FileOwnership) -> None:
        self.cli, self.env = "openshell", {}
        self.store = store
        self.sandboxes: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.created: list[Owned | None] = []
        """The record as it was when each create ran."""
        self.phases: Iterator[str] = iter(())
        """Each ``get`` after a create takes the next phase, keeping the last."""
        self.identities: Iterator[str] = iter(())
        """Likewise for the ID, as a gateway that changed it would report."""
        self.create_fails = False
        self.makes = True
        self.gateway_down = False
        self.deletes = True

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        self.calls.append(args)
        match args[:2]:
            case ("sandbox", "get"):
                name = args[2]
                if self.gateway_down:
                    raise OpenShellError(GATEWAY_DOWN)
                if name not in self.sandboxes:
                    raise SandboxMissingError("openshell sandbox get failed: NotFound")
                found = self.sandboxes[name]
                found["phase"] = next(self.phases, found["phase"])
                found["id"] = next(self.identities, found["id"])
                return json.dumps(found)
            case ("sandbox", "create"):
                self.created.append(self.store.read())
                name = args[args.index("--name") + 1]
                if self.makes:
                    self.sandboxes[name] = {"id": "id-first", "phase": "Provisioning"}
                if self.create_fails:
                    raise OpenShellError("openshell sandbox create failed: timed out")
            case ("sandbox", "delete"):
                if self.deletes:
                    del self.sandboxes[args[2]]
            case _:
                pass
        return ""


@pytest.fixture
def store(tmp_path: Path) -> FileOwnership:
    return FileOwnership(tmp_path / "state" / "orchestrator" / "sandbox.json")


@pytest.fixture
def cli(store: FileOwnership) -> FakeCli:
    return FakeCli(store)


async def create(store: FileOwnership, cli: FakeCli, **options: Any) -> Owned:
    return await runtime_owner.create_owned(
        store, cli, image="registry.test/orchestrator@sha256:" + "0" * 64,
        policy=Path("policy.yaml"), timeout=5, poll=0, **options,
    )  # fmt: skip


async def delete(store: FileOwnership, cli: FakeCli, timeout: float = 5, **options: Any) -> bool:
    return await runtime_owner.delete_recorded(store, cli, timeout=timeout, poll=0, **options)


class Bindings:
    """What binds the sandbox's ID to the project's revision, recording its calls."""

    def __init__(self, store: FileOwnership, *, fails: bool = False) -> None:
        self.store, self.fails = store, fails
        self.calls: list[tuple[str, str, Owned | None]] = []
        """Each call, with the ownership record as it stood."""

    async def bind(self, sandbox_id: str) -> None:
        self.calls.append(("bind", sandbox_id, self.store.read()))
        if self.fails:
            raise LauncherError("the worker refused")

    async def unbind(self, sandbox_id: str) -> None:
        self.calls.append(("unbind", sandbox_id, self.store.read()))


def test_the_file_record_is_todays_sandbox_json(store: FileOwnership) -> None:
    assert store.read() is None
    store.write(Owned("ss-orch-0123456789", None))
    assert store.path.read_text() == '{"name": "ss-orch-0123456789", "id": null}\n'
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    store.write(Owned("ss-orch-0123456789", "id-first"))
    assert store.read() == ("ss-orch-0123456789", "id-first")
    store.remove()
    assert store.read() is None


async def test_create_records_the_name_before_create_and_the_id_before_ready(
    store: FileOwnership, cli: FakeCli
) -> None:
    cli.phases = iter(("Provisioning", "Ready"))
    owned = await create(store, cli)
    assert owned.name.startswith("ss-orch-")
    assert len(owned.name) == len("ss-orch-") + 10
    assert cli.created == [Owned(owned.name, None)]
    assert owned == store.read() == Owned(owned.name, "id-first")
    (created,) = [call for call in cli.calls if call[:2] == ("sandbox", "create")]
    assert "--driver-config-json" not in created
    assert created[-4:] == ("--detach", "--", "sleep", "infinity")


class RollingCli(FakeCli):
    """A gateway that answers one ``get`` with its message, then has rolled it back."""

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        said = await super().run(*args, timeout=timeout)
        if args[:2] == ("sandbox", "get") and args[2] in self.sandboxes:
            del self.sandboxes[args[2]]
            return json.dumps(json.loads(said) | {"message": "bootstrap grace exceeded"})
        return said


@pytest.mark.parametrize("seen", [True, False])
async def test_a_sandbox_gone_after_create_returned_is_a_rollback(
    store: FileOwnership, cli: FakeCli, seen: bool
) -> None:
    if seen:
        cli = RollingCli(store)
    else:
        cli.makes = False  # the create returned, but the gateway has nothing
    with pytest.raises(runtime_owner.CreateRolledBackError) as raised:
        await create(store, cli)
    record = store.read()
    assert record is not None
    assert raised.value.name == record.name
    if seen:
        assert raised.value.reason == "bootstrap grace exceeded"
        assert record.id == "id-first"
    else:
        assert raised.value.reason is None
        assert "the gateway gave no reason" in str(raised.value)
        assert record.id is None


async def test_the_id_is_recorded_even_when_the_sandbox_then_dies(
    store: FileOwnership, cli: FakeCli
) -> None:
    cli.phases = iter(("Error",))
    with pytest.raises(LauncherError, match="did not become ready; ownership retained"):
        await create(store, cli)
    record = store.read()
    assert record is not None
    assert record.id == "id-first"


async def test_an_id_change_during_startup_aborts_and_keeps_the_first(
    store: FileOwnership, cli: FakeCli
) -> None:
    cli.identities = iter(("id-first", "id-other"))
    with pytest.raises(LauncherError, match="identity changed during startup"):
        await create(store, cli)
    record = store.read()
    assert record is not None
    assert record.id == "id-first"


async def test_a_ready_sandbox_is_bound_by_its_id_and_unbound_once_gone(
    store: FileOwnership, cli: FakeCli
) -> None:
    binding = Bindings(store)
    cli.phases = iter(("Provisioning", "Ready"))
    owned = await create(store, cli, binding=binding)
    # Bound by the gateway's ID, once Ready, with the record already naming the ID.
    assert binding.calls == [("bind", "id-first", owned)]
    assert await delete(store, cli, binding=binding)
    assert binding.calls[1:] == [("unbind", "id-first", owned)]
    assert store.read() is None


async def test_a_sandbox_that_is_not_ready_is_not_bound(store: FileOwnership, cli: FakeCli) -> None:
    binding = Bindings(store)
    cli.phases = iter(("Error",))
    with pytest.raises(LauncherError, match="did not become ready"):
        await create(store, cli, binding=binding)
    assert binding.calls == []


async def test_a_binding_that_fails_keeps_the_record_for_the_owner(
    store: FileOwnership, cli: FakeCli
) -> None:
    cli.phases = iter(("Ready",))
    with pytest.raises(LauncherError, match="the worker refused"):
        await create(store, cli, binding=Bindings(store, fails=True))
    record = store.read()
    assert record is not None
    assert record.id == "id-first"
    # The record deletes what exists, and nothing is unbound that was never bound.
    unbinding = Bindings(store)
    assert await delete(store, cli, binding=unbinding)
    assert [call[:2] for call in unbinding.calls] == [("unbind", "id-first")]


async def test_driver_config_reaches_create(store: FileOwnership, cli: FakeCli) -> None:
    cli.phases = iter(("Ready",))
    placement = '{"kubernetes":{"pod":{"runtime_class_name":"kata"}}}'
    await create(store, cli, driver_config=placement)
    (created,) = [call for call in cli.calls if call[:2] == ("sandbox", "create")]
    assert created[created.index("--driver-config-json") + 1] == placement


@pytest.mark.parametrize("makes", [True, False])
async def test_an_uncertain_create_keeps_the_name_and_refuses_to_forget_it(
    store: FileOwnership, cli: FakeCli, makes: bool
) -> None:
    cli.create_fails, cli.makes = True, makes
    with pytest.raises(OpenShellError, match="create failed"):
        await create(store, cli)
    record = store.read()
    assert record is not None
    assert record.id is None
    if makes:
        # The sandbox appeared after all: its ID is adopted, then it is deleted.
        assert await delete(store, cli)
        assert store.read() is None
    else:
        with pytest.raises(LauncherError, match="creation outcome is unknown"):
            await delete(store, cli)
        assert store.read() == record


async def test_a_gateway_failure_before_create_records_nothing(
    store: FileOwnership, cli: FakeCli
) -> None:
    cli.gateway_down = True
    with pytest.raises(OpenShellError):
        await create(store, cli)
    assert store.read() is None
    assert not [call for call in cli.calls if call[:2] == ("sandbox", "create")]


async def test_delete_confirms_absence_before_forgetting(
    store: FileOwnership, cli: FakeCli
) -> None:
    assert not await delete(store, cli)
    assert cli.calls == []
    store.write(Owned("ss-orch-0123456789", "id-first"))
    cli.sandboxes["ss-orch-0123456789"] = {"id": "id-first", "phase": "Ready"}
    assert await delete(store, cli)
    assert ("sandbox", "delete", "ss-orch-0123456789") in cli.calls
    assert store.read() is None


async def test_another_id_under_the_recorded_name_is_never_deleted(
    store: FileOwnership, cli: FakeCli
) -> None:
    store.write(Owned("ss-orch-0123456789", "id-first"))
    cli.sandboxes["ss-orch-0123456789"] = {"id": "id-other", "phase": "Ready"}
    with pytest.raises(LauncherError, match="identity changed; replacement deletion refused"):
        await delete(store, cli)
    assert not [call for call in cli.calls if call[:2] == ("sandbox", "delete")]
    assert store.read() == ("ss-orch-0123456789", "id-first")


async def test_an_unconfirmed_delete_keeps_the_record(store: FileOwnership, cli: FakeCli) -> None:
    store.write(Owned("ss-orch-0123456789", "id-first"))
    cli.sandboxes["ss-orch-0123456789"] = {"id": "id-first", "phase": "Ready"}
    cli.deletes = False
    with pytest.raises(LauncherError, match="deletion was not confirmed"):
        await delete(store, cli, timeout=0.05)
    assert store.read() == ("ss-orch-0123456789", "id-first")


async def test_only_the_gateways_missing_sandbox_is_absence(
    store: FileOwnership, cli: FakeCli
) -> None:
    store.write(Owned("ss-orch-0123456789", "id-first"))
    cli.gateway_down = True
    with pytest.raises(OpenShellError, match="Unavailable"):
        await delete(store, cli)
    assert store.read() == ("ss-orch-0123456789", "id-first")
    cli.gateway_down = False
    assert await delete(store, cli)  # SandboxMissingError: confirmed gone
    assert store.read() is None


async def test_a_cancelled_delete_still_settles_the_record(
    store: FileOwnership, cli: FakeCli
) -> None:
    store.write(Owned("ss-orch-0123456789", "id-first"))
    cli.sandboxes["ss-orch-0123456789"] = {"id": "id-first", "phase": "Ready"}
    task = asyncio.create_task(delete(store, cli))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.read() is None


async def test_install_uploads_then_makes_credentials_private(cli: FakeCli) -> None:
    files = ((Path("token"), "runtime.token"), (Path("settings"), "runtime.json"))
    await runtime_owner.install(cli, "ss-orch-0123456789", files, private=("runtime.token",))
    shell = ("sandbox", "exec", "-n", "ss-orch-0123456789", "--no-tty", "--no-login-shell", "--")
    assert cli.calls == [
        (*shell, "mkdir", "-p", "/sandbox/runtime"),
        ("sandbox", "upload", "--no-git-ignore", "ss-orch-0123456789", "token",
         "/sandbox/runtime/runtime.token"),
        ("sandbox", "upload", "--no-git-ignore", "ss-orch-0123456789", "settings",
         "/sandbox/runtime/runtime.json"),
        (*shell, "chmod", "600", "/sandbox/runtime/runtime.token"),
    ]  # fmt: skip
    assert runtime_owner.exec_argv("openshell", "ss-orch-0123456789", "python3")[-5:] == [
        "python3",
        "-m",
        "sandbox_science.orchestrator.confinement.runtime",
        "--settings",
        "/sandbox/runtime/runtime.json",
    ]


def test_agent_settings_name_a_control_ca_only_when_one_is_uploaded() -> None:
    common: dict[str, Any] = {
        "control_url": "https://control.test:8443",
        "model_url": "https://models.test",
        "models": ("main",),
        "apis": {},
        "reasoning_effort": {},
        "embedding": None,
        "thinking_replay": None,
        "generation": "runtime-" + "0" * 24,
    }
    plain = runtime_owner.agent_settings(**common)
    assert plain["control_url"] == "https://control.test:8443/control"
    assert "control_ca_file" not in plain
    verified = runtime_owner.agent_settings(**common, control_ca=True)
    assert verified["control_ca_file"] == "/sandbox/runtime/control-ca.crt"


@asynccontextmanager
async def control(tmp_path: Path, health: Mapping[str, object], *, tls: bool) -> AsyncIterator[str]:
    async def answer(request: web.Request) -> web.Response:
        return web.json_response(dict(health))

    app = web.Application()
    app.router.add_get("/health", answer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner, "127.0.0.1", 0, ssl_context=make_tls(tmp_path).server if tls else None
    )
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"{'https' if tls else 'http'}://127.0.0.1:{port}/health"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("connected", "draining", "occupied"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
async def test_occupancy_over_verified_tls(
    tmp_path: Path, connected: bool, draining: bool, occupied: bool
) -> None:
    health = {"ready": True, "runtime_connected": connected, "runtime_draining": draining}
    async with control(tmp_path, health, tls=True) as url:
        found = await asyncio.to_thread(
            runtime_owner.occupied, url, ca_file=tmp_path / "ca.pem", hostname=SERVER_HOST
        )
        assert found is occupied


async def test_occupancy_refuses_an_unverified_control(tmp_path: Path) -> None:
    health = {"ready": True, "runtime_connected": False}
    async with control(tmp_path, health, tls=True) as url:
        # The system's CAs do not know the test CA; the test CA's name is not the address.
        with pytest.raises(OSError, match="CERTIFICATE_VERIFY_FAILED"):
            await asyncio.to_thread(runtime_owner.occupied, url, hostname=SERVER_HOST)
        with pytest.raises(OSError, match="CERTIFICATE_VERIFY_FAILED"):
            await asyncio.to_thread(runtime_owner.occupied, url, ca_file=tmp_path / "ca.pem")
    with pytest.raises(LauncherError, match="requires an HTTPS URL"):
        runtime_owner.occupied("http://127.0.0.1:1/health", ca_file=tmp_path / "ca.pem")


async def test_waiting_for_a_draining_runtime_is_bounded(tmp_path: Path) -> None:
    health = {"ready": True, "runtime_connected": False, "runtime_draining": True}
    async with control(tmp_path, health, tls=False) as url:
        with pytest.raises(LauncherError, match="did not settle"):
            await runtime_owner.wait_unoccupied(url, timeout=0.05, poll=0.01)
