# SPDX-License-Identifier: MIT
"""Ownership of the agent runtime's OpenShell sandbox, for whichever process holds it.

The owner records the sandbox's random name before it creates it, and the immutable ID
the gateway gives it before it waits for Ready, so a crash at any point leaves a record
naming what might exist. It deletes only by that record: the name must still report the
recorded ID, and the record goes only once the gateway confirms the sandbox gone. An
uncertain outcome (a failed create, a gateway that does not answer, a delete it cannot
confirm) keeps the record and refuses, since only the CLI's ``SandboxMissingError``
proves absence. On the host, ``orchestrator`` uses this with ``sandbox.json`` in the
launcher's state directory.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import secrets
import ssl
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast
from urllib.parse import urlsplit

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

from .config import LauncherError
from .mcp import DEAD_PHASES
from .state import private_dir, write_private

REMOTE = "/sandbox/runtime"
"""Where the runtime's settings and credentials go in its sandbox."""
RUNTIME = "sandbox_science.orchestrator.confinement.runtime"
_HEALTH_BYTES = 4096


class Owned(NamedTuple):
    """The recorded sandbox: its name, and its ID once the gateway gave one."""

    name: str
    id: str | None


class CreateRolledBackError(LauncherError):
    """The create returned, then the gateway answered that no sandbox has the name: it
    rolled the creation back, as OpenShell does with a sandbox that does not become ready
    within its bootstrap grace. The record stays; what clears it is the owner's choice."""

    def __init__(self, name: str, reason: str | None) -> None:
        self.name, self.reason = name, reason
        why = reason or "the gateway gave no reason"
        super().__init__(f"the gateway rolled back the creation of {name} ({why})")


def gateway_reason(document: Mapping[str, Any] | None) -> str | None:
    """What the gateway last said about a sandbox, from its ``sandbox get`` record: a
    ``reason`` or ``message``, at the top or under ``status``, else its last phase."""
    if document is None:
        return None
    status = document.get("status")
    places: list[Mapping[str, Any]] = [document]
    if isinstance(status, Mapping):
        places.append(cast(Mapping[str, Any], status))
    for place in places:
        for key in ("reason", "message"):
            said = place.get(key)
            if isinstance(said, str) and said.strip():
                return " ".join(said.split())[:200]
    phase = document.get("phase")
    return f"last seen {phase}"[:200] if isinstance(phase, str) and phase else None


class OwnershipStore(Protocol):
    """Where the owner keeps its record, replaced in one step on each write."""

    def read(self) -> Owned | None: ...

    def write(self, owned: Owned) -> None: ...

    def remove(self) -> None: ...


class FileOwnership:
    """The record as ``{"name": …, "id": …}`` in a mode-600 file of a private directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Owned | None:
        try:
            value = cast(dict[str, Any], json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        return Owned(str(value["name"]), cast(str | None, value["id"]))

    def write(self, owned: Owned) -> None:
        write_private(
            private_dir(self.path.parent) / self.path.name,
            json.dumps({"name": owned.name, "id": owned.id}) + "\n",
        )

    def remove(self) -> None:
        (private_dir(self.path.parent) / self.path.name).unlink()


class SandboxCli(Protocol):
    """The OpenShell CLI as the owner uses it (``mcp.OpenShell``)."""

    cli: str

    async def run(self, *args: str, timeout: float = ...) -> str:
        """The command's output; any failure raises."""
        ...

    async def sandbox(self, name: str) -> dict[str, Any] | None:
        """The sandbox's record; None only when the gateway answered that it has none."""
        ...


class Binding(Protocol):
    """What binds the runtime's sandbox to the project's policy revision in the policy
    middleware's store, which decides only bound sandboxes (``runtime_binding``)."""

    async def bind(self, sandbox_id: str) -> None:
        """Bind a Ready sandbox's ID; any failure raises."""
        ...

    async def unbind(self, sandbox_id: str) -> None:
        """Unbind a deleted sandbox's ID; never raises, since an ID is never reused and a
        binding left behind decides nothing."""
        ...


async def delete_recorded(
    store: OwnershipStore,
    cli: SandboxCli,
    *,
    timeout: float,
    clock: Clock = SYSTEM_CLOCK,
    poll: float = 0.5,
    binding: Binding | None = None,
) -> bool:
    """Retain uncertain ownership; only confirmed original-identity deletion permits replacement.

    A cancellation waits for the deletion to finish, so the record never outlives or
    predeceases what the gateway did.
    """
    task = asyncio.create_task(_delete_recorded(store, cli, timeout, clock, poll, binding))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _delete_recorded(
    store: OwnershipStore,
    cli: SandboxCli,
    timeout: float,
    clock: Clock,
    poll: float,
    binding: Binding | None,
) -> bool:
    prior = store.read()
    if prior is None:
        return False
    name, identity = prior
    document = await cli.sandbox(name)
    if identity is None:
        observed = document.get("id") if document is not None else None
        if not isinstance(observed, str) or not observed:
            raise LauncherError(
                "orchestrator creation outcome is unknown; "
                "original name retained for reconciliation"
            )
        identity = observed
        store.write(Owned(name, identity))
    if document is not None:
        if document.get("id") != identity:
            raise LauncherError("orchestrator identity changed; replacement deletion refused")
        await cli.run("sandbox", "delete", name)
    deadline = clock.monotonic() + timeout
    while (document := await cli.sandbox(name)) is not None:
        if document.get("id") != identity:
            raise LauncherError(
                "orchestrator identity changed during deletion; ownership record retained"
            )
        if clock.monotonic() >= deadline:
            raise LauncherError(
                "orchestrator deletion was not confirmed; ownership record retained"
            )
        await clock.sleep(poll)
    if binding is not None:
        await binding.unbind(identity)
    store.remove()
    return True


async def create_owned(
    store: OwnershipStore,
    cli: SandboxCli,
    *,
    image: str,
    policy: Path,
    timeout: float,
    driver_config: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
    poll: float = 0.5,
    binding: Binding | None = None,
) -> Owned:
    """Create a fresh ``ss-orch-<10 hex>`` sandbox running ``sleep infinity``, and wait
    until it is Ready; ``driver_config`` is OpenShell's ``--driver-config-json``. Once
    it is Ready, ``binding`` binds its ID to the project's policy revision.

    Every failure leaves the record for ``delete_recorded`` to reconcile. A sandbox the
    gateway reports absent after the create returned raises ``CreateRolledBackError``.
    """
    name = "ss-orch-" + secrets.token_hex(5)
    if await cli.sandbox(name) is not None:
        raise LauncherError("orchestrator random sandbox name is already in use")
    store.write(Owned(name, None))
    placement = ("--driver-config-json", driver_config) if driver_config is not None else ()
    await cli.run(
        "sandbox", "create", "--name", name, "--from", image, "--policy", str(policy),
        *placement, "--no-auto-providers", "--detach", "--", "sleep", "infinity",
        timeout=timeout,
    )  # fmt: skip
    deadline = clock.monotonic() + timeout
    seen: dict[str, Any] | None = None
    while True:
        found = await cli.sandbox(name)
        if found is None:
            # The gateway's NotFound after a create that returned: it rolled it back.
            raise CreateRolledBackError(name, gateway_reason(seen))
        seen = found
        identity = found.get("id")
        if isinstance(identity, str) and identity:
            previous = store.read()
            if previous is not None and previous.id not in (None, identity):
                raise LauncherError("orchestrator identity changed during startup")
            if previous is None or previous.id is None:
                store.write(Owned(name, identity))
        phase = found.get("phase")
        if phase == "Ready" and isinstance(identity, str) and identity:
            if binding is not None:
                await binding.bind(identity)
            return Owned(name, identity)
        if phase in DEAD_PHASES or clock.monotonic() >= deadline:
            raise LauncherError("orchestrator sandbox did not become ready; ownership retained")
        await clock.sleep(poll)


def _shell(name: str) -> list[str]:
    return ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--"]


async def install(
    cli: SandboxCli,
    name: str,
    files: Iterable[tuple[Path, str]],
    *,
    private: Sequence[str] = (),
) -> None:
    """Upload each local file to ``REMOTE`` under its name, then make ``private`` mode 600."""
    await cli.run(*_shell(name), "mkdir", "-p", REMOTE)
    for source, remote in files:
        await cli.run(
            "sandbox", "upload", "--no-git-ignore", name, str(source), REMOTE + "/" + remote
        )
    if private:
        await cli.run(*_shell(name), "chmod", "600", *(REMOTE + "/" + file for file in private))


def exec_argv(cli: str, name: str, python: str) -> list[str]:
    """The command that runs the confined runtime in the sandbox, held as an exec session."""
    return [cli, *_shell(name), python, "-m", RUNTIME, "--settings", REMOTE + "/runtime.json"]


def agent_settings(
    *,
    control_url: str,
    model_url: str,
    models: Sequence[str],
    apis: Mapping[str, str],
    reasoning_effort: Mapping[str, str],
    embedding: str | None,
    thinking_replay: Mapping[str, object] | None,
    generation: str,
    control_ca: bool = False,
    mcp: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """The runtime's ``runtime.json``, for credentials ``install`` puts in ``REMOTE``.

    ``control_url`` is control's base URL as the sandbox reaches it; ``control_ca`` says
    a ``control-ca.crt`` is uploaded beside the token to verify it; ``mcp`` configures the
    runtime's direct MCP client (D98).
    """
    settings: dict[str, Any] = {"control_url": control_url + "/control"}
    if control_ca:
        settings["control_ca_file"] = REMOTE + "/control-ca.crt"
    if mcp is not None:
        settings["mcp"] = dict(mcp)
    return settings | {
        "token_file": REMOTE + "/runtime.token",
        "model_url": model_url,
        "model_key_file": REMOTE + "/model.key",
        "models": tuple(models),
        "apis": dict(apis),
        "reasoning_effort": dict(reasoning_effort),
        "embedding": embedding,
        "thinking_replay": None if thinking_replay is None else dict(thinking_replay),
        "generation": generation,
    }


class _Named(http.client.HTTPSConnection):
    """HTTPS that checks the certificate against ``server_name``, not the host it dials."""

    def __init__(
        self, host: str, port: int | None, *, tls: ssl.SSLContext, server_name: str, timeout: float
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=tls)
        self.tls, self.server_name = tls, server_name

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self.tls.wrap_socket(self.sock, server_hostname=self.server_name)


def occupied(
    url: str, *, ca_file: Path | None = None, hostname: str | None = None, timeout: float = 2
) -> bool:
    """Whether control's ``/health`` at ``url`` says a runtime is connected or draining.

    An HTTPS ``url`` is verified against ``ca_file`` (else the system's CAs) and
    ``hostname`` (else the URL's host). Anything but a well-formed answer raises.
    """
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.scheme not in ("http", "https"):
        raise LauncherError("control readiness needs an HTTP(S) URL")
    if parsed.scheme == "http":
        if ca_file is not None or hostname is not None:
            raise LauncherError("a control CA or hostname requires an HTTPS URL")
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    else:
        tls = ssl.create_default_context()
        if ca_file is not None:
            tls.load_verify_locations(cafile=ca_file)
        connection = _Named(
            parsed.hostname,
            parsed.port,
            tls=tls,
            server_name=hostname or parsed.hostname,
            timeout=timeout,
        )
    try:
        connection.request(
            "GET", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        )
        response = connection.getresponse()
        if response.status != 200:
            raise LauncherError(f"control readiness answered HTTP {response.status}")
        body = response.read(_HEALTH_BYTES + 1)
    except http.client.HTTPException as exc:
        raise LauncherError(f"control readiness was not HTTP: {exc}") from None
    finally:
        connection.close()
    if len(body) > _HEALTH_BYTES:
        raise LauncherError("control readiness exceeded its byte bound")
    value: object = json.loads(body)
    if (
        not isinstance(value, dict)
        or (health := cast(dict[str, object], value)).get("ready") is not True
        or not isinstance(connected := health.get("runtime_connected"), bool)
        or not isinstance(draining := health.get("runtime_draining", False), bool)
    ):
        raise LauncherError("control readiness is incompatible with runtime replacement")
    return connected or draining


async def wait_unoccupied(
    url: str,
    *,
    timeout: float,
    ca_file: Path | None = None,
    hostname: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
    poll: float = 0.1,
) -> None:
    """Wait until control has no runtime connected or draining, refusing after ``timeout``."""
    deadline = clock.monotonic() + timeout
    while await asyncio.to_thread(occupied, url, ca_file=ca_file, hostname=hostname):
        if clock.monotonic() >= deadline:
            raise LauncherError(
                "old runtime control connection did not settle; replacement refused"
            )
        await clock.sleep(poll)
