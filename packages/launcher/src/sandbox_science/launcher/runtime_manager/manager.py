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
  attempts (``InfrastructureRecovery``); a pending replacement at start, an unknown or
  changed ID, or a foreign name blocks every automatic action until an operator
  request succeeds (KRB-16, KRB-17). A pending reattach is retried: it deletes and
  creates nothing, and control revokes the previous token at each issue.
- An operator request that created a sandbox but could not attach the runtime leaves
  the attach to the serve loop, under the same journal.
- The gateway is waited for, never a reason to exit: while ``openshell sandbox get``
  gets no answer from it, no automatic action starts and none of the ten attempts is
  used; it is asked again after 5 s, doubling to at most 60 s, without limit.
- ``status.json``'s note says what needs attention now, with its kind and since when,
  and is cleared when that ends (control or the gateway answers again, an action
  succeeds after a failed one). A create the gateway rolled back is marked on the
  record; ``request bootstrap`` clears such a record once the gateway confirms the
  name absent, as ``forget`` would.
- SIGTERM ends the exec session and exits; nothing is deleted (KRB-15).
- One ``serve`` per state directory: it holds ``serve.lock`` while it runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import secrets
import signal
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NoReturn

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep
from sandbox_science.contracts.logs import json_line, log_format, relayed_line
from sandbox_science.launcher import runtime_owner
from sandbox_science.launcher.config import LauncherError, error_text
from sandbox_science.launcher.infrastructure import InfrastructureRecovery, RecoveryState
from sandbox_science.launcher.mcp import DEAD_PHASES, OpenShell, SandboxMissingError
from sandbox_science.launcher.runtime_binding import WorkerBinding
from sandbox_science.launcher.runtime_owner import CreateRolledBackError, Owned
from sandbox_science.launcher.sandboxes import WorkerApi
from sandbox_science.launcher.state import private_dir, write_private
from sandbox_science.policy.openshell import compile_orchestrator

from .control import ControlClient, ControlError, Issued, OccupiedError, SupersededError
from .records import (
    CredentialsRecord,
    ManagerOwnership,
    ManagerRequest,
    NoteKind,
    Placement,
    Result,
    Status,
    read_bounded,
)
from .settings import RuntimeManagerSettings

_CLAIMED = "request.claimed.json"
"""``request.json`` once the serving process has taken it."""
NAME = re.compile(r"ss-orch-[0-9a-f]{10}")
"""The only sandbox names the manager acts on (KRB-3)."""
READY_LINE = "confined agent runtime ready"
_LINE_BYTES = 64 * 1024
"""The longest runtime output line relayed whole; a longer one is cut there."""
_CLI_CALL = 120.0
"""``OpenShell.run``'s own bound on one CLI call, which no setting changes."""
_CONTROL_CALL = 10.0
"""``ControlClient``'s bound on one call to control."""
_GATEWAY_CALL = 30.0
"""The bound on one look at the gateway (``RuntimeManager._gateway``)."""
_PROBE = "ss-orch-gateway-probe"
"""The name the gateway is asked about when none is recorded: one ``NAME`` never
matches, so its answer is only ever a sign of life."""
_PRIORITY: tuple[NoteKind, ...] = (
    "blocked",
    "gateway_unreachable",
    "control_unreachable",
    "action_failed",
)
"""Which note ``status.json`` shows when several conditions hold at once."""


def request_deadline(settings: RuntimeManagerSettings) -> float:
    """How long an operator's request may take at most: a replace, the longest one,
    with each step at its bound, and a first reconcile before it, with a margin."""
    stop, start = settings.stop_timeout, settings.start_timeout
    stop_exec = stop + 1  # terminate, then the relay's last second
    delete = 3 * _CLI_CALL + stop  # look up, delete, then poll until confirmed
    drain = stop + 2  # the drain wait and its last check
    create = 3 * _CLI_CALL + 2 * start  # look up, create, then poll until Ready
    attach = 3 * _CONTROL_CALL + stop + 6 * _CLI_CALL + start  # credentials, upload, exec
    replace = stop_exec + delete + drain + create + attach
    reconcile = stop_exec + 2 * _CLI_CALL + attach  # an automatic attempt under way
    return replace + reconcile + _GATEWAY_CALL + 30


def say(text: str) -> None:
    """One line of the manager's own log: as written, or, with ``SS_LOG_FORMAT=json``, a JSON
    record of the ``runtime-manager`` service."""
    print(json_line("runtime-manager", text) if log_format() == "json" else text, flush=True)


NOTE_EVENT = "runtime_manager_note"
"""The log event of a condition that needs attention (``status.json``'s note), which the
rendered Loki alerts read (docs/KUBERNETES-BUNDLE.md, "Alert rules")."""
NOTE_REPEAT = 300.0
"""Seconds between a held condition's log records: logged when it starts, then again every
five minutes while it holds, so an alert can tell one that lasts from one that passed."""


def say_note(kind: NoteKind, text: str, since: datetime) -> None:
    """A note's record: ``event: runtime_manager_note`` with its ``kind`` and ``since`` in
    JSON, or a plain line."""
    if log_format() == "json":
        line = json_line(
            "runtime-manager",
            text,
            level="warning",
            event=NOTE_EVENT,
            kind=kind,
            since=since.isoformat(),
        )
    else:
        line = f"note {kind} since {since.isoformat()}: {text}"
    print(line, flush=True)


class BlockedError(LauncherError):
    """An automatic action found what only an operator may settle."""


@dataclass(frozen=True)
class _Note:
    kind: NoteKind
    text: str
    since: datetime


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
        binding: runtime_owner.Binding | None = None,
        clock: Clock = SYSTEM_CLOCK,
        poll: float = 1.0,
        health_interval: float = 5.0,
        steady: float = 60.0,
        delays: tuple[float, ...] = (2, 4, 8, 16, 32, 60),
        gateway_backoff: tuple[float, float] = (5.0, 60.0),
        gateway_interval: float = 60.0,
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
        self.binding = binding or self._worker_binding(settings)
        self.clock, self.poll = clock, poll
        self.health_interval, self.steady = health_interval, steady
        self.gateway_backoff, self.gateway_interval = gateway_backoff, gateway_interval
        self.ownership = ManagerOwnership(self.root / "sandbox.json")
        self.instance = "instance-" + secrets.token_hex(16)
        self.stopping = asyncio.Event()
        self.requested = asyncio.Event()
        self.recovery = InfrastructureRecovery(
            self.root / "infrastructure-recovery.json",
            enabled=True,
            alive=self.alive,
            replace=self._automatic,
            stopping=self.stopping,
            clock=clock,
            steady=steady,
            delays=delays,
            retryable=True,
        )
        self.process: asyncio.subprocess.Process | None = None
        self.attached: str | None = None
        """The control instance the running runtime was attached under."""
        self._notes: dict[str, _Note] = {}
        """What needs attention now, by its source; ``status.json`` shows one."""
        self._note_logged: dict[str, float] = {}
        """When each source's note was last logged (monotonic), for ``NOTE_REPEAT``."""
        self._output: asyncio.Task[None] | None = None
        self._exited = False
        self._control_up = False
        self._gateway_up = False
        self._gateway_due = 0.0
        self._gateway_delay: float | None = None
        self._wanted = False
        self._blocking: str | None = None
        self._next_health = 0.0
        self._alive_since: float | None = None
        self._shown = ""
        self._created = False
        """An operator request created a sandbox, so its attach is the serve loop's."""

    def alive(self) -> bool | None:
        """Whether the exec session runs (True), has ended (False) or is not held (None)."""
        return None if self.process is None else self.process.returncode is None

    def _noted(self, source: str, kind: NoteKind, text: str) -> None:
        """Note a condition from ``source``; one that goes on keeps its since time."""
        found = self._notes.get(source)
        held = found is not None and found.kind == kind
        since = found.since if held and found is not None else self.clock.now()
        self._notes[source] = _Note(kind, text[:512], since)
        now = self.clock.monotonic()
        logged = self._note_logged.get(source)
        if not held or logged is None or now - logged >= NOTE_REPEAT:
            self._note_logged[source] = now
            say_note(kind, text[:512], since)

    def _cleared(self, *sources: str) -> None:
        for source in sources:
            self._notes.pop(source, None)
            self._note_logged.pop(source, None)

    def _attachable(self) -> bool:
        """A sandbox is recorded that an automatic action may attach: not one whose
        create the gateway rolled back, which waits for the operator's bootstrap."""
        return self.ownership.read() is not None and self.ownership.rolled_back() is None

    async def run(self) -> None:
        """Serve until ``stopping`` is set, then end the exec session; never delete."""
        self._wanted = self._attachable()
        (self.root / _CLAIMED).unlink(missing_ok=True)  # a dead process's request
        self._status("starting")  # names this process before the first reconcile
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
            say("runtime manager stopped; the runtime sandbox is kept")

    async def _serve(self) -> None:
        while True:
            if self.requested.is_set() or (self.root / "request.json").exists():
                self.requested.clear()
                await self._answer()
            await self._observe()
            gateway = await self._gateway()
            state = self.recovery.state
            if (
                self._wanted
                and self._control_up
                and gateway
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
                self._wanted = self.alive() is not True and self._attachable()
            self._steady()
            self._status()
            await wait_or_sleep(self.clock, self.requested, self.poll)

    async def _observe(self) -> None:
        """Notice the exec's end and control's restarts, and decide what is wanted."""
        if self.process is not None and self.process.returncode is not None:
            say(f"runtime exec ended with status {self.process.returncode}")
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
            self._noted("control", "control_unreachable", error_text(exc))
            return
        self._control_up = True
        self._cleared("control")
        if self.attached is not None and health.control_instance != self.attached:
            say("control restarted; reconnecting the runtime to its sandbox")
            await self._stop_exec()
            self.attached, self._exited = None, False
            self._wanted = self._attachable()
        elif self._exited:
            self._exited = False
            if self.settings.recover_infrastructure:
                self._wanted = True
            else:
                self._noted(
                    "exited",
                    "blocked",
                    "the runtime exited under the same control; recover_infrastructure is "
                    "off (request replace, or a control restart, starts it again)",
                )

    async def _gateway(self) -> bool:
        """Whether the OpenShell gateway answers, asked when due: every
        ``gateway_interval`` while it does, else after a backoff from
        ``gateway_backoff``'s first value doubling to its second, without limit. Any
        answer counts, the gateway's NotFound too; a failure is noted, never fatal."""
        now = self.clock.monotonic()
        if now < self._gateway_due:
            return self._gateway_up
        owned = self.ownership.read()
        name = owned.name if owned is not None and NAME.fullmatch(owned.name) else _PROBE
        try:
            await self.cli.run("sandbox", "get", name, "-o", "json", timeout=_GATEWAY_CALL)
        except SandboxMissingError:
            pass
        except (LauncherError, OSError, ValueError) as exc:
            first, cap = self.gateway_backoff
            delay = first if self._gateway_delay is None else min(2 * self._gateway_delay, cap)
            if self._gateway_up or self._gateway_delay is None:
                say(f"the OpenShell gateway does not answer; waiting for it ({error_text(exc)})")
            self._gateway_up, self._gateway_delay = False, delay
            self._gateway_due = self.clock.monotonic() + delay
            self._noted(
                "gateway", "gateway_unreachable", "the OpenShell gateway is unreachable: "
                + error_text(exc),
            )  # fmt: skip
            return False
        if not self._gateway_up and self._gateway_delay is not None:
            say("the OpenShell gateway answers again")
        self._gateway_up, self._gateway_delay = True, None
        self._gateway_due = self.clock.monotonic() + self.gateway_interval
        self._cleared("gateway")
        return True

    def _recheck_gateway(self) -> None:
        """Ask the gateway again before the next automatic action: a failure may have
        been the gateway's."""
        self._gateway_due = 0.0

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

    async def _automatic(self) -> None:
        """``_recover`` under the journal, its outcome noted: a failure until an action
        succeeds."""
        try:
            await self._recover()
        except (LauncherError, OSError, ValueError) as exc:
            self._noted("action", "action_failed", "automatic action failed: " + error_text(exc))
            self._recheck_gateway()
            raise
        if self.alive():
            self._cleared("action")

    async def _recover(self) -> None:
        """One automatic action, under the recovery journal: attach, or with the opt-in
        replace a sandbox confirmed gone or dead."""
        owned = self.ownership.read()
        if owned is None:
            return
        if not NAME.fullmatch(owned.name):
            self._block("the recorded sandbox name is not one the runtime manager makes")
        if self.ownership.rolled_back() is not None:
            self._block("the gateway rolled back the runtime sandbox's creation; request bootstrap")
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
        self.recovery.irreversible()  # a deletion and a creation are never replayed
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
        self.attached, self._exited = instance, False
        self._cleared("exited", "action")
        say(f"runtime attached in {owned.name} ({issued.generation})")

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
        lines = _Lines(stream)
        while True:
            text = await lines.next()
            if text is None:
                await process.wait()
                raise LauncherError(
                    f"the runtime exec ended before it was ready (status {process.returncode})"
                )
            print(relayed_line("runtime", text, prefix="runtime: "), flush=True)
            if READY_LINE in text:
                break
        self._output = asyncio.create_task(_relay(lines))
        while not (await asyncio.to_thread(self.control.health)).runtime_ready:
            if process.returncode is not None:
                raise LauncherError("the runtime exec ended before control saw it ready")
            await self.clock.sleep(0.1)

    async def _stop_exec(self) -> None:
        """End the exec session; OpenShell then ends the runtime (KRB-11).

        The handle is kept until the process has ended, so a stop interrupted by
        SIGTERM leaves it for ``run``'s own stop to end."""
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.settings.stop_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self.process is process:
            self.process = None
        output, self._output = self._output, None
        if output is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(output), 1)
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)

    @staticmethod
    def _worker_binding(settings: RuntimeManagerSettings) -> runtime_owner.Binding | None:
        link = settings.worker
        if link is None:
            return None
        api = WorkerApi(link.url, link.token_file, link.ca_file)
        return WorkerBinding(api, link.project, say)

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
                binding=self.binding,
            )
        except CreateRolledBackError as exc:
            self.ownership.mark_rolled_back(exc.reason, self.clock.now())
            say(f"{exc}; recorded as create_rolled_back")
            raise LauncherError(f"{exc}; request bootstrap to create another") from None
        finally:
            self.ownership.creating = None
        self._created = True
        await self._attach(owned)

    async def _delete(self) -> bool:
        owned = self.ownership.read()
        if owned is None:
            return False
        _ours(owned)
        deleted = await runtime_owner.delete_recorded(
            self.ownership,
            self.cli,
            timeout=self.settings.stop_timeout,
            clock=self.clock,
            binding=self.binding,
        )
        self.attached = None
        return deleted

    async def _replace(self, driver_config: str) -> None:
        """Stop, confirmed identity-checked deletion, drain, create, attach (KRB-14)."""
        await self._stop_exec()
        if not await self._delete():
            raise LauncherError("no runtime sandbox is recorded; request bootstrap")
        try:
            await runtime_owner.wait_unoccupied(
                self.settings.manager_url + "/health",
                timeout=self.settings.stop_timeout,
                ca_file=self.settings.control_ca_file,
                clock=self.clock,
            )
        except OSError as exc:
            # Control is unreachable, so no runtime holds its connection, and it revokes
            # at its start; Attach's issue fences any survivor (KRB-7). Creating now
            # keeps a record, which the serve loop attaches once control answers.
            say(f"control did not answer the drain check ({exc}); creating")
        except LauncherError as exc:
            raise LauncherError(f"{exc}; the old sandbox is deleted: request bootstrap") from None
        await self._create_and_attach(driver_config)

    async def _bootstrap(self) -> None:
        if self.ownership.read() is not None:
            if self.ownership.rolled_back() is None:
                raise LauncherError(
                    "a runtime sandbox is already recorded; bootstrap needs none (request replace)"
                )
            await self._clear_rolled_back()
        self.recovery.save(RecoveryState())
        (self.root / "credentials.json").unlink(missing_ok=True)
        await self._stop_exec()
        await self._create_and_attach(self.settings.driver_config())

    async def _clear_rolled_back(self) -> None:
        """Clear a record whose create the gateway rolled back, as ``forget`` clears one
        whose create never happened: only once the gateway answers that the name is
        absent, the proof ``delete_recorded`` accepts for a deletion too."""
        owned, rolled = self.ownership.read(), self.ownership.rolled_back()
        if owned is None or rolled is None:
            return
        _ours(owned)
        if await self.cli.sandbox(owned.name) is not None:
            raise LauncherError(
                "the gateway has a sandbox of the recorded name again; bootstrap refused "
                "(request stop --delete deletes it)"
            )
        self.ownership.remove()
        (self.root / "credentials.json").unlink(missing_ok=True)
        why = rolled.reason or "the gateway gave no reason"
        say(
            f"cleared {owned.name}: the gateway rolled back its creation at "
            f"{rolled.at.isoformat()} ({why}) and answers that it has no sandbox of that name"
        )

    async def _forget(self) -> None:
        """Clear a record whose create never happened: no ID was ever observed, and the
        gateway answers that the name is absent. Observed absence without an identity
        is no proof of deletion, so this is the operator's decision, never automatic."""
        owned = self.ownership.read()
        if owned is None:
            raise LauncherError("no runtime sandbox is recorded; nothing to forget")
        if owned.id is not None:
            raise LauncherError(
                "the recorded sandbox has a gateway ID; forget refused (request stop --delete)"
            )
        if await self.cli.sandbox(owned.name) is not None:
            raise LauncherError(
                "the gateway has a sandbox of the recorded name; forget refused "
                "(request stop --delete adopts and deletes it)"
            )
        self.ownership.remove()
        (self.root / "credentials.json").unlink(missing_ok=True)
        say(f"forgot {owned.name}: the gateway has no sandbox of that name")

    async def _teardown(self) -> None:
        await self._stop_exec()
        if not await self._delete():
            say("no runtime sandbox is recorded; nothing to delete")
        (self.root / "credentials.json").unlink(missing_ok=True)

    async def _answer(self) -> None:
        """Carry out the operator's request and write its receipt (``operations.request``)."""
        # Claimed by a rename, so a newer request written meanwhile is the next one.
        path = self.root / _CLAIMED
        try:
            (self.root / "request.json").rename(path)
        except FileNotFoundError:
            return
        try:
            data = read_bounded(path)
            if data is None:
                return
            request = ManagerRequest.model_validate_json(data)
        except (LauncherError, ValueError) as exc:
            say(f"ignored a malformed operator request: {str(exc)[:200]}")
            path.unlink(missing_ok=True)
            return
        error: str | None = None
        self._created = False
        try:
            if not request.owned_by(self.instance):
                raise LauncherError("the request names another runtime manager process")
            say(f"operator request: {request.action}")
            if request.action == "bootstrap":
                await self._bootstrap()
            elif request.action == "replace":
                await self._replace(self.settings.driver_config())
            elif request.action == "forget":
                await self._forget()
            else:
                await self._teardown()
            self.recovery.save(RecoveryState())
            self._wanted = False
            self._cleared("action", "exited")
        except (LauncherError, OSError, ValueError) as exc:
            error = error_text(exc)
            if self._created and self.ownership.read() is not None:
                # The operator asked for a runtime and a sandbox of theirs is recorded:
                # attaching it is the serve loop's, journaled and bounded.
                self.recovery.save(RecoveryState())
                self._wanted = True
                error = (
                    error[:400] + "; the new sandbox is kept and the manager keeps trying "
                    "to attach the runtime"
                )
            say(f"operator {request.action} failed: {error}")
            self._noted("action", "action_failed", f"operator {request.action} failed: {error}")
        finally:
            self._created = False
            self._recheck_gateway()  # the request used the gateway, or found it silent
        receipt = Result(nonce=request.nonce, error=error)
        write_private(self.root / "result.json", receipt.model_dump_json() + "\n")
        path.unlink(missing_ok=True)

    def _note(self) -> _Note | None:
        """The note ``status.json`` shows: the journal's block and a rolled-back create
        as they stand now, else what was noted, by ``_PRIORITY``."""
        state = self.recovery.state
        if state.blocked:
            self._noted(
                "journal", "blocked", "automatic action blocked: "
                + (state.error or "attempts exhausted"),
            )  # fmt: skip
        else:
            self._cleared("journal")
        owned, rolled = self.ownership.read(), self.ownership.rolled_back()
        if owned is not None and rolled is not None:
            why = rolled.reason or "the gateway gave no reason"
            self._notes["rolled_back"] = _Note(
                "action_failed",
                f"the gateway rolled back the creation of {owned.name} ({why}); "
                "request bootstrap creates another",
                rolled.at,
            )
        else:
            self._cleared("rolled_back")
        found = sorted(self._notes.values(), key=lambda note: _PRIORITY.index(note.kind))
        return found[0] if found else None

    def _status(self, runtime: Literal["starting"] | None = None) -> None:
        owned = self.ownership.read()
        note = self._note()
        status = Status(
            instance=self.instance,
            pid=os.getpid(),
            runtime=runtime or ("running" if self.alive() else "stopped"),
            sandbox=owned.name if owned is not None else None,
            control_instance=self.attached,
            note=note.text[:512] if note is not None else None,
            note_kind=note.kind if note is not None else None,
            note_since=note.since if note is not None else None,
        )
        shown = status.model_dump_json()
        if shown != self._shown:
            write_private(self.root / "status.json", shown + "\n")
            self._shown = shown


class _Lines:
    """The exec's output as lines, read in bounded chunks until its end. A line longer
    than ``_LINE_BYTES`` is cut there, with a notice, and its rest dropped: the pipe is
    always drained, since a runtime blocked on a full pipe would still look alive."""

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.cutting = False
        self.ended = False

    async def next(self) -> str | None:
        """The next line, without its end; None once the output has ended."""
        while True:
            end = self.buffer.find(b"\n", 0, _LINE_BYTES + 1)
            if end >= 0:
                line = bytes(self.buffer[:end])
                del self.buffer[: end + 1]
                if self.cutting:
                    self.cutting = False
                    continue
                return _text(line)
            if len(self.buffer) > _LINE_BYTES:
                line = bytes(self.buffer[:_LINE_BYTES])
                del self.buffer[:_LINE_BYTES]
                if self.cutting:
                    continue
                self.cutting = True
                return _text(line) + f" [line cut at {_LINE_BYTES} bytes]"
            if self.ended:
                line, cut = bytes(self.buffer), self.cutting
                self.buffer.clear()
                self.cutting = False
                if line and not cut:
                    return _text(line)
                return None
            chunk = await self.stream.read(_LINE_BYTES)
            if chunk:
                self.buffer += chunk
            else:
                self.ended = True


def _text(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").rstrip()


async def _relay(lines: _Lines) -> None:
    """Pass the runtime's output on to the manager's own, for the Pod's log, until the
    output ends."""
    while (text := await lines.next()) is not None:
        print(relayed_line("runtime", text, prefix="runtime: "), flush=True)


def _hold_serve_lock(root: Path) -> int:
    """Hold ``serve.lock`` for this process's life: two managers sharing one state
    directory would each act on the same record and journal."""
    descriptor = os.open(
        root / "serve.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise LauncherError(
            f"another runtime manager is serving from {root} (it holds serve.lock)"
        ) from None
    return descriptor


async def serve(settings: RuntimeManagerSettings) -> None:
    """Serve with SIGTERM and SIGINT stopping (never deleting) and SIGUSR2 waking the
    manager for an operator request. A second ``serve`` on the same state directory is
    refused."""
    lock = _hold_serve_lock(private_dir(settings.state_dir))
    try:
        await _serve_locked(settings)
    finally:
        os.close(lock)


async def _serve_locked(settings: RuntimeManagerSettings) -> None:
    manager = RuntimeManager(settings)
    loop = asyncio.get_running_loop()
    handlers = {
        signal.SIGTERM: manager.stopping.set,
        signal.SIGINT: manager.stopping.set,
        signal.SIGUSR2: manager.requested.set,
    }
    for number, handler in handlers.items():
        loop.add_signal_handler(number, handler)
    say(f"runtime manager serving ({manager.instance})")
    try:
        await manager.run()
    finally:
        for number in handlers:
            loop.remove_signal_handler(number)
