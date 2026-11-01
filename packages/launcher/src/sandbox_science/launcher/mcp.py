# SPDX-License-Identifier: MIT
"""The OpenShell CLI as the launcher runs it, and the MCP servers' OpenShell providers.

With ``[mcp_direct]`` (D98), ``up`` first checks that the OpenShell providers exist, before
it starts anything: the owner creates them, with the MCP servers' credentials, and they are
attached to the agent runtime's sandbox (orchestrator.py). With ``provider_env_file``, it
registers ``crypto-research-mcp`` from the owner's file, when the provider is missing or
``--refresh-provider`` asks, by running access_provider.py: only that process, and the one
OpenShell command it gives them to, reads the values, which OpenShell then keeps in its
encrypted store.

The MCP proxy, which ran in a sandbox of its own until it retired (2026-10-26), is no part
of a launch. A proxy sandbox an earlier launcher recorded (``mcp-proxy/sandbox.json`` in
the state directory) and never deleted is deleted by ``up`` and ``down``, only by its
recorded name, and only if its ID is the recorded one or none was recorded.

OpenShell commands run in a session of their own, so a Ctrl-C to ``up`` does not cut
one short, with the worker's environment: the one every part gets (supervise.py), the
worker's ``pass_env`` and ``[openshell] env``, since the worker runs the same CLI against
the same gateway.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from typing import Any, cast

from . import access_provider
from .config import LauncherConfig, LauncherError, McpDirectConfig
from .state import Layout, private_dir
from .supervise import Supervisor, environment, passed

DEAD_PHASES = {"Error", "Deleting", "Completed"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FRAME = re.compile(r"[\s\u2500-\u257f\u25b6\u00d7]+")
"""Whitespace and the box drawing, arrows and crosses miette frames and wraps a report
with."""
_MISSING = re.compile(r'(?:message|self): "sandbox not found"')
"""The gateway's NotFound for a sandbox name, as tonic shows a status: ``message:`` in
the OpenShell 0.1.3 labs, ``self:`` from tonic 0.14 (OpenShell 0.1.5). Other NotFound
answers, such as a missing service endpoint, gateway or file, do not match."""


class OpenShellError(LauncherError):
    """An OpenShell command failed; its message is the end of what it said."""


class SandboxMissingError(OpenShellError):
    """The gateway answered that no sandbox has the name: proof of absence, unlike any
    other failure, which says nothing about whether the sandbox exists."""


NO_ERROR_TEXT = "no error text from openshell"


def last_line(said: str, limit: int = 400) -> str:
    """The last line of what a failed command wrote, without miette's frame, at most
    ``limit`` characters; ``NO_ERROR_TEXT`` when it wrote nothing."""
    lines = [" ".join(_FRAME.sub(" ", line).split()) for line in said.splitlines()]
    found = next((line for line in reversed(lines) if line), "")
    return found[-limit:] or NO_ERROR_TEXT


class OpenShell:
    """The OpenShell CLI, with the config's ``env``."""

    def __init__(self, config: LauncherConfig) -> None:
        self.cli = config.openshell.cli
        self.env = {**environment(), **tooling(config)}

    @classmethod
    def at(cls, cli: str, env: Mapping[str, str]) -> OpenShell:
        """The CLI without a launcher config, as the runtime manager runs it."""
        shell = cls.__new__(cls)
        shell.cli, shell.env = cli, {**environment(), **env}
        return shell

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        process = await asyncio.create_subprocess_exec(
            self.cli, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env,
            start_new_session=True,
        )  # fmt: skip
        command = " ".join(args[:2])
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException as exc:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if isinstance(exc, TimeoutError):
                raise OpenShellError(
                    f"openshell {command} gave no answer within {timeout:g} s"
                ) from None
            raise
        if process.returncode != 0:
            said = _ANSI.sub("", errors.decode("utf-8", errors="replace")).strip()
            # Classify the whole report: the status leads it; its last line is what is kept.
            missing = _MISSING.search(_FRAME.sub(" ", said)) is not None
            error = SandboxMissingError if missing else OpenShellError
            raise error(
                f"openshell {command} failed (exit {process.returncode}): {last_line(said)}"
            )
        return output.decode("utf-8", errors="replace")

    async def json(self, *args: str) -> object:
        return json.loads(await self.run(*args, "-o", "json"))

    async def sandbox(self, name: str) -> dict[str, Any] | None:
        """The sandbox's record, or None when there is none."""
        try:
            found = await self.json("sandbox", "get", name)
        except SandboxMissingError:
            return None
        if not isinstance(found, dict):
            # Only the gateway's NotFound proves absence; an answer of another shape does not.
            raise OpenShellError("openshell sandbox get returned no sandbox record")
        return cast(dict[str, Any], found)


def tooling(config: LauncherConfig) -> dict[str, str]:
    """What the launcher's OpenShell commands get besides every part's environment."""
    return {**passed(config.worker.pass_env), **config.openshell.env}


def provided(config: LauncherConfig) -> McpDirectConfig | None:
    """The MCP configuration whose providers the gateway must have, attached to the agent
    runtime's sandbox (D98)."""
    return config.mcp_direct


async def register_provider(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, *, refresh: bool
) -> None:
    """Register the Access provider from the owner's file when it is missing, or always
    with ``refresh``, in a process of its own: the launcher never opens the file. The
    process gets the parts' usual environment and ``[openshell] env`` alone, and the
    OpenShell CLI as an absolute path, found as running it would find it."""
    mcp = provided(config)
    if mcp is None or mcp.provider_env_file is None or mcp.provider_profile is None:
        if refresh:
            raise LauncherError("--refresh-provider needs [mcp_direct] provider_env_file")
        return
    provider = access_provider.PROVIDER
    if not refresh and provider in await _providers(config):
        return
    env = config.openshell.env
    cli = _located(config.openshell.cli, {**environment(), **env}.get("PATH"))
    if cli is None:
        raise LauncherError(f"no OpenShell CLI {config.openshell.cli} to register {provider} with")
    print(f"registering the OpenShell provider {provider} from {mcp.provider_env_file}", flush=True)
    said = await supervisor.run(
        f"registering the provider {provider}",
        [sys.executable, "-I", "-m", access_provider.__name__, str(mcp.provider_env_file),
         "--profile", str(mcp.provider_profile), "--provider", provider,
         "--openshell", cli],
        private_dir(layout.mcp),
        config.start_timeout,
        env,
    )  # fmt: skip
    print(said.strip(), flush=True)


def _located(cli: str, path: str | None) -> str | None:
    """A path made absolute, or a name found on ``path``, as running it would find it."""
    if os.sep in cli:
        return os.path.abspath(cli)
    found = shutil.which(cli, path=path)
    return None if found is None else os.path.abspath(found)


async def _providers(config: LauncherConfig) -> set[str]:
    return access_provider.provider_names(await OpenShell(config).json("provider", "list"))


async def check_providers(config: LauncherConfig) -> None:
    mcp = provided(config)
    if mcp is None or not mcp.providers:
        return
    names = await _providers(config)
    for provider in mcp.providers:
        if provider not in names:
            raise LauncherError(
                f"the OpenShell provider {provider} does not exist: create it with the MCP "
                "servers' credentials, as docs/MCP-CONNECTORS.md says, then run up again"
            )


def recorded(layout: Layout) -> tuple[str, str | None] | None:
    """The MCP proxy's sandbox an earlier launcher made and did not delete: its name and
    ID."""
    try:
        raw = cast(dict[str, Any], json.loads((layout.mcp / "sandbox.json").read_text()))
        identity = raw["id"]
        return str(raw["name"]), None if identity is None else str(identity)
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def delete_recorded(config: LauncherConfig, layout: Layout) -> bool:
    """Delete the MCP proxy's sandbox the record names, if it is still the one an earlier
    launcher made."""
    found = recorded(layout)
    if found is None:
        return False
    return await _deleted(config, layout, found[0])


async def _deleted(config: LauncherConfig, layout: Layout, name: str) -> bool:
    """Delete the sandbox ``name`` if the record names it: by its recorded ID, or, before
    the ID was known, by its name alone, which no one else's sandbox had: each launch named
    its proxy sandbox anew, with six random hex digits."""
    found = recorded(layout)
    if found is None or found[0] != name:
        return False
    openshell = OpenShell(config)
    existing = await openshell.sandbox(name)
    deleted = False
    if existing is not None and found[1] not in (None, existing.get("id")):
        # Ours is gone, since the gateway's names are unique; the name is another's now.
        print(
            f"left the sandbox {name} alone: it is {existing.get('id')}, not the retired MCP"
            f" proxy's {found[1]}; forgetting the record",
            flush=True,
        )
    elif existing is not None:
        await openshell.run("sandbox", "delete", name)
        for _ in range(120):
            if await openshell.sandbox(name) is None:
                break
            await asyncio.sleep(1)
        else:
            raise LauncherError(f"the sandbox {name} is still there two minutes after its delete")
        deleted = True
    (layout.mcp / "sandbox.json").unlink(missing_ok=True)
    return deleted
