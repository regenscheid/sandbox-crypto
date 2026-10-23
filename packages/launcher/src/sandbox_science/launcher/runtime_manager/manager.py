# SPDX-License-Identifier: MIT
"""``sandbox-science-runtime-manager serve``: the one owner of the agent runtime's sandbox.

The sandbox's main process is ``sleep infinity``; the manager holds one ``openshell
sandbox exec`` session running the runtime in it (KRB-10). It never creates a sandbox
on its own: the first one comes from the operator's ``request bootstrap`` (KRB-12).

- **Reconnect** (Attach, KRB-13) is automatic at start, for a recorded sandbox that
  the gateway reports under the recorded ID and Ready, and after control restarts,
  seen as a new ``control_instance`` in its health. Each attach journals a fresh
  request ID before asking control for credentials, uploads them with the settings,
  and runs the exec; ready is the runtime's ready line plus ``runtime_ready``.
- A runtime exit under the same control is reattached only with
  ``recover_infrastructure``. A recorded sandbox that is confirmed gone or dead is
  replaced only with it too, after a confirmed identity-checked deletion (KRB-14).
- Every automatic action is journaled pending before any effect, with at most ten
  attempts (``InfrastructureRecovery``); a pending journal at start, an unknown or
  changed ID, or a foreign name blocks every automatic action until an operator
  request succeeds (KRB-16, KRB-17).
- SIGTERM ends the exec session and exits; nothing is deleted (KRB-15).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import signal
import tempfile
from pathlib import Path
from typing import NoReturn

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep
from sandbox_science.launcher import runtime_owner
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.infrastructure import InfrastructureRecovery, RecoveryState
from sandbox_science.launcher.mcp import DEAD_PHASES, OpenShell
from sandbox_science.launcher.runtime_owner import Owned
from sandbox_science.launcher.state import private_dir, write_private
from sandbox_science.policy.openshell import compile_orchestrator

from .control import ControlClient, ControlError, Issued, OccupiedError, SupersededError
from .records import (
    CredentialsRecord,
    ManagerOwnership,
    ManagerRequest,
    Placement,
    Result,
    Status,
    read_bounded,
)
from .settings import RuntimeManagerSettings

NAME = re.compile(r"ss-orch-[0-9a-f]{10}")
"""The only sandbox names the manager acts on (KRB-3)."""
READY_LINE = "confined agent runtime ready"
_LINE_BYTES = 64 * 1024


class BlockedError(LauncherError):
    """An automatic action found what only an operator may settle."""


def _ours(owned: Owned) -> None:
    if not NAME.fullmatch(owned.name):
        raise LauncherError("the recorded sandbox name is not one the runtime manager makes")


class RuntimeManager:
    """One serving manager: its record, its exec session and its recovery journal."""

    def __init__(
        self,
        settings: RuntimeManagerSettings,
        *,
        cli: OpenShell | None = None,
        control: ControlClient | None = None,
        clock: Clock = SYSTEM_CLOCK,
        poll: float = 1.0,
        health_interval: float = 5.0,
        steady: float = 60.0,
        delays: tuple[float, ...] = (2, 4, 8, 16, 32, 60),
    ) -> None:
        self.settings = settings
        self.root = private_dir(settings.state_dir)
        self.models = settings.models()
        self.cli = cli or OpenShell.at(settings.openshell.cli, settings.openshell.env)
        self.control = control or ControlClient(
            settings.manager_url,
            ca_file=settings.control_ca_file,
            token_file=settings.manager_token_file,
        )
        self.clock, self.poll = clock, poll
        self.health_interval, self.steady = health_interval, steady
        self.ownership = ManagerOwnership(self.root / "sandbox.json")
        self.instance = "instance-" + secrets.token_hex(16)
        self.stopping = asyncio.Event()
        self.requested = asyncio.Event()
        self.recovery = InfrastructureRecovery(
            self.root / "infrastructure-recovery.json",
            enabled=True,
            alive=self.alive,
            replace=self._recover,
            stopping=self.stopping,
            clock=clock,
            steady=steady,
            delays=delays,
        )
        self.process: asyncio.subprocess.Process | None = None
        self.attached: str | None = None
        """The control instance the running runtime was attached under."""
        self.note: str | None = None
        self._output: asyncio.Task[None] | None = None
        self._exited = False
        self._control_up = False
        self._wanted = False
        self._blocking: str | None = None
        self._next_health = 0.0
        self._alive_since: float | None = None
        self._shown = ""

    def alive(self) -> bool | None:
        """Whether the exec session runs (True), has ended (False) or is not held (None)."""
        return None if self.process is None else self.process.returncode is None

    async def run(self) -> None:
        """Serve until ``stopping`` is set, then end the exec session; never delete."""
        self._wanted = self.ownership.read() is not None
        serving = asyncio.create_task(self._serve())
        stopped = asyncio.create_task(self.stopping.wait())
        try:
            done, _ = await asyncio.wait((serving, stopped), return_when=asyncio.FIRST_COMPLETED)
            if serving in done:
                await serving
        finally:
            for task in (serving, stopped):
                task.cancel()
            await asyncio.gather(serving, stopped, return_exceptions=True)
            await self._stop_exec()
            self._status()
            print("runtime manager stopped; the runtime sandbox is kept", flush=True)

    async def _serve(self) -> None:
        while True:
            if self.requested.is_set() or (self.root / "request.json").exists():
                self.requested.clear()
                await self._answer()
            await self._observe()
            state = self.recovery.state
            if (
                self._wanted
                and self._control_up
                and not state.blocked
                and self.clock.now().timestamp() >= state.retry_after
            ):
                self._blocking = None
                await self.recovery.replace()
                if self._blocking is not None:
                    self.recovery.save(
                        self.recovery.state.model_copy(
                            update={"blocked": True, "error": self._blocking}
                        )
                    )
                self._wanted = self.alive() is not True and self.ownership.read() is not None
            self._steady()
            self._status()
            await wait_or_sleep(self.clock, self.requested, self.poll)

    async def _observe(self) -> None:
        """Notice the exec's end and control's restarts, and decide what is wanted."""
        if self.process is not None and self.process.returncode is not None:
            print(f"runtime exec ended with status {self.process.returncode}", flush=True)
            await self._stop_exec()
            self._exited = True
        now = self.clock.monotonic()
        if now < self._next_health and not (self._exited or self._wanted):
            return
        self._next_health = now + self.health_interval
        try:
            health = await asyncio.to_thread(self.control.health)
        except LauncherError as exc:
            self._control_up = False
            self.note = str(exc)[:512]
            return
        self._control_up = True
        if self.attached is not None and health.control_instance != self.attached:
            print("control restarted; reconnecting the runtime to its sandbox", flush=True)
            await self._stop_exec()
            self.attached, self._exited = None, False
            self._wanted = self.ownership.read() is not None
        elif self._exited:
            self._exited = False
            if self.settings.recover_infrastructure:
                self._wanted = True
            else:
                self.note = (
                    "the runtime exited under the same control; recover_infrastructure is off"
                )

    def _steady(self) -> None:
        if self.alive() is not True:
            self._alive_since = None
            return
        now = self.clock.monotonic()
        if self._alive_since is None:
            self._alive_since = now
        state = self.recovery.state
        if state.attempts and not state.blocked and now - self._alive_since >= self.steady:
            self.recovery.save(RecoveryState())

    def _block(self, reason: str) -> NoReturn:
        self._blocking = reason
        raise BlockedError(reason)

    async def _recover(self) -> None:
        """One automatic action, under the recovery journal: attach, or with the opt-in
        replace a sandbox confirmed gone or dead."""
        owned = self.ownership.read()
        if owned is None:
            return
        if not NAME.fullmatch(owned.name):
            self._block("the recorded sandbox name is not one the runtime manager makes")
        if owned.id is None:
            self._block("the runtime sandbox's creation outcome is unknown; request replace")
        found = await self.cli.sandbox(owned.name)
        if found is not None and found.get("id") != owned.id:
            self._block("the runtime sandbox's identity changed; automatic action refused")
        phase = found.get("phase") if found is not None else None
        if phase == "Ready":
            await self._attach(owned)
            return
        if found is not None and phase not in DEAD_PHASES:
            raise LauncherError(f"the runtime sandbox is {phase}, not Ready")
        if not self.settings.recover_infrastructure:
            self._block(
                "the runtime sandbox is gone or dead; replacing it needs "
                "recover_infrastructure or request replace"
            )
        placement = self.ownership.placement()
        recorded = placement.driver_config if placement is not None else None
        await self._replace(recorded or self.settings.driver_config())

    async def _attach(self, owned: Owned) -> None:
        """Start the runtime in the recorded sandbox with fresh credentials (KRB-13)."""
        await self._stop_exec()
        instance = (await asyncio.to_thread(self.control.health)).control_instance
        if instance is None:
            raise ControlError("control's health names no control_instance")
        issued = await self._credentials()
        settings = self.root / "runtime.json"
        write_private(settings, self.settings.runtime_json(self.models, issued.generation))
        with tempfile.TemporaryDirectory(prefix="runtime-manager-") as scratch:
            token = Path(scratch) / "runtime.token"
            write_private(token, issued.token + "\n")
            await runtime_owner.install(
                self.cli,
                owned.name,
                (
                    (token, "runtime.token"),
                    (self.settings.model_key_file, "model.key"),
                    (self.settings.control_ca_file, "control-ca.crt"),
                    (settings, "runtime.json"),
                ),
                private=("runtime.token", "model.key"),
            )
        record = self._credentials_record()
        if record is not None:
            self._journal(record.model_copy(update={"started": True}))
        await self._exec(owned.name)
        self.attached, self._exited, self.note = instance, False, None
        print(f"runtime attached in {owned.name} ({issued.generation})", flush=True)

    def _credentials_record(self) -> CredentialsRecord | None:
        data = read_bounded(self.root / "credentials.json")
        return None if data is None else CredentialsRecord.model_validate_json(data)

    def _journal(self, record: CredentialsRecord) -> None:
        write_private(self.root / "credentials.json", record.model_dump_json() + "\n")

    def _new_request(self) -> CredentialsRecord:
        record = CredentialsRecord(request=secrets.token_hex(16))
        self._journal(record)
        return record

    async def _credentials(self) -> Issued:
        """Control's values for a journaled request: a request whose values no exec used
        is replayed; on 409 the same request is retried once the old runtime drained."""
        record = self._credentials_record()
        if record is None or record.started:
            record = self._new_request()
        deadline = self.clock.monotonic() + self.settings.stop_timeout
        renewed = False
        while True:
            try:
                return await asyncio.to_thread(self.control.issue, record.request)
            except SupersededError:
                if renewed:
                    raise
                renewed, record = True, self._new_request()
            except OccupiedError:
                # The issue revoked the old runtime's token; it ends at its next frame.
                await runtime_owner.wait_unoccupied(
                    self.settings.manager_url + "/health",
                    timeout=max(0.0, deadline - self.clock.monotonic()),
                    ca_file=self.settings.control_ca_file,
                    clock=self.clock,
                )

    async def _exec(self, name: str) -> None:
        argv = runtime_owner.exec_argv(self.cli.cli, name, self.settings.python)
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.cli.env, start_new_session=True,
            limit=_LINE_BYTES,
        )  # fmt: skip
        self.process = process
        try:
            await asyncio.wait_for(self._ready(process), self.settings.start_timeout)
        except TimeoutError:
            await self._stop_exec()
            raise LauncherError("the runtime did not become ready in time") from None
        except BaseException:
            await self._stop_exec()
            raise

    async def _ready(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stdout
        if stream is None:
            raise LauncherError("the runtime exec has no output to watch")
        while True:
            line = await stream.readline()
            if not line:
                await process.wait()
                raise LauncherError(
                    f"the runtime exec ended before it was ready (status {process.returncode})"
                )
            text = line.decode("utf-8", errors="replace").rstrip()
            print("runtime: " + text, flush=True)
            if READY_LINE in text:
                break
        self._output = asyncio.create_task(_relay(stream))
        while not (await asyncio.to_thread(self.control.health)).runtime_ready:
            if process.returncode is not None:
                raise LauncherError("the runtime exec ended before control saw it ready")
            await self.clock.sleep(0.1)

    async def _stop_exec(self) -> None:
        """End the exec session; OpenShell then ends the runtime (KRB-11)."""
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.settings.stop_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        output, self._output = self._output, None
        if output is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(output), 1)
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)

    async def _create_and_attach(self, driver_config: str) -> None:
        policy = self.root / "policy.yaml"
        write_private(policy, compile_orchestrator(self.settings.policy).to_yaml())
        self.ownership.creating = Placement.of(
            self.settings.image, self.settings.revision, driver_config
        )
        try:
            owned = await runtime_owner.create_owned(
                self.ownership,
                self.cli,
                image=self.settings.image,
                policy=policy,
                timeout=self.settings.start_timeout,
                driver_config=driver_config,
                clock=self.clock,
            )
        finally:
            self.ownership.creating = None
        await self._attach(owned)

    async def _delete(self) -> bool:
        owned = self.ownership.read()
        if owned is None:
            return False
        _ours(owned)
        deleted = await runtime_owner.delete_recorded(
            self.ownership, self.cli, timeout=self.settings.stop_timeout, clock=self.clock
        )
        self.attached = None
        return deleted

    async def _replace(self, driver_config: str) -> None:
        """Stop, confirmed identity-checked deletion, drain, create, attach (KRB-14)."""
        await self._stop_exec()
        if not await self._delete():
            raise LauncherError("no runtime sandbox is recorded; request bootstrap")
        await runtime_owner.wait_unoccupied(
            self.settings.manager_url + "/health",
            timeout=self.settings.stop_timeout,
            ca_file=self.settings.control_ca_file,
            clock=self.clock,
        )
        await self._create_and_attach(driver_config)

    async def _bootstrap(self) -> None:
        if self.ownership.read() is not None:
            raise LauncherError(
                "a runtime sandbox is already recorded; bootstrap needs none (request replace)"
            )
        self.recovery.save(RecoveryState())
        (self.root / "credentials.json").unlink(missing_ok=True)
        await self._stop_exec()
        await self._create_and_attach(self.settings.driver_config())

    async def _teardown(self) -> None:
        await self._stop_exec()
        if not await self._delete():
            print("no runtime sandbox is recorded; nothing to delete", flush=True)
        (self.root / "credentials.json").unlink(missing_ok=True)

    async def _answer(self) -> None:
        """Carry out the operator's request and write its receipt (``operations.request``)."""
        path = self.root / "request.json"
        try:
            data = read_bounded(path)
            if data is None:
                return
            request = ManagerRequest.model_validate_json(data)
        except (LauncherError, ValueError) as exc:
            print(f"ignored a malformed operator request: {str(exc)[:200]}", flush=True)
            path.unlink(missing_ok=True)
            return
        error: str | None = None
        try:
            if not request.owned_by(self.instance):
                raise LauncherError("the request names another runtime manager process")
            print(f"operator request: {request.action}", flush=True)
            if request.action == "bootstrap":
                await self._bootstrap()
            elif request.action == "replace":
                await self._replace(self.settings.driver_config())
            else:
                await self._teardown()
            self.recovery.save(RecoveryState())
            self._wanted = False
        except (LauncherError, OSError, ValueError) as exc:
            error = str(exc)[:512]
            print(f"operator {request.action} failed: {error}", flush=True)
        receipt = Result(nonce=request.nonce, error=error)
        write_private(self.root / "result.json", receipt.model_dump_json() + "\n")
        with contextlib.suppress(FileNotFoundError, LauncherError, ValueError):
            current = read_bounded(path)
            if current is not None and ManagerRequest.model_validate_json(current).nonce == (
                request.nonce
            ):
                path.unlink()

    def _status(self) -> None:
        owned = self.ownership.read()
        state = self.recovery.state
        note = self.note
        if state.blocked:
            note = "automatic action blocked: " + (state.error or "attempts exhausted")
        status = Status(
            instance=self.instance,
            pid=os.getpid(),
            runtime="running" if self.alive() else "stopped",
            sandbox=owned.name if owned is not None else None,
            control_instance=self.attached,
            note=note[:512] if note is not None else None,
        )
        shown = status.model_dump_json()
        if shown != self._shown:
            write_private(self.root / "status.json", shown + "\n")
            self._shown = shown


async def _relay(stream: asyncio.StreamReader) -> None:
    """Pass the runtime's output on to the manager's own, for the Pod's log."""
    with contextlib.suppress(ValueError):
        while line := await stream.readline():
            print("runtime: " + line.decode("utf-8", errors="replace").rstrip(), flush=True)


async def serve(settings: RuntimeManagerSettings) -> None:
    """Serve with SIGTERM and SIGINT stopping (never deleting) and SIGUSR2 waking the
    manager for an operator request."""
    manager = RuntimeManager(settings)
    loop = asyncio.get_running_loop()
    handlers = {
        signal.SIGTERM: manager.stopping.set,
        signal.SIGINT: manager.stopping.set,
        signal.SIGUSR2: manager.requested.set,
    }
    for number, handler in handlers.items():
        loop.add_signal_handler(number, handler)
    print(f"runtime manager serving ({manager.instance})", flush=True)
    try:
        await manager.run()
    finally:
        for number in handlers:
            loop.remove_signal_handler(number)
