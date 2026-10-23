# SPDX-License-Identifier: MIT
"""The MCP proxy in an OpenShell sandbox of its own, run as docs/MCP-CONNECTORS.md says.

With ``[mcp_proxy]`` in the config, ``up``:

1. checks that the OpenShell providers exist, before it starts anything: the owner
   creates them, with the MCP servers' credentials. With ``provider_env_file``, it first
   registers ``crypto-research-mcp`` from the owner's file, when the provider is missing
   or ``--refresh-provider`` asks, by running access_provider.py: only that process,
   and the one OpenShell command it gives them to, reads the values, which OpenShell
   then keeps in its encrypted store;
2. compiles the sandbox's policy (``sandbox-science-mcp-proxy compile-policy``);
3. creates the sandbox from the proxy's image, with the providers, and waits until
   it is ready;
4. uploads the proxy's files, ``proxy.toml`` last, since the proxy starts once it is
   there;
5. binds the project to its policy in the proxy;
6. forwards a loopback address to the proxy, starting the forward again whenever it
   exits, as it does when the sandbox stops.

The run service calls the proxy there, with a token of its own; the proxy's one caller
holds the token's digest. On stopping, the launcher deletes the sandbox.

Each launch names its sandbox anew: the configured name, a dash and six random hex
digits, so two launchers, or one and a sandbox made by hand, never share a name. The
launcher records the name before it creates the sandbox, and the sandbox's ID once it is
ready; it deletes a sandbox only by the recorded name, and only if the ID is the
recorded one or none was recorded yet, which the random name makes safe. A create that
fails, leaving no sandbox of that name, forgets the record. A sandbox an earlier
launcher made and never deleted is deleted before the new one is made, and ``down``
deletes it too.

OpenShell commands run in a session of their own, so a Ctrl-C to ``up`` does not cut
one short, with the worker's environment: the one every part gets (supervise.py), the
worker's ``pass_env`` and ``[openshell] env``, since the worker runs the same CLI against
the same gateway. ``compile-policy`` and the forward get the same.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, cast

from . import access_provider, toml
from .config import LauncherConfig, LauncherError, McpProxyConfig
from .state import Layout, ensure_token, private_dir, write_private
from .supervise import Part, Supervisor, environment, passed

COMMAND = "sandbox-science-mcp-proxy"
PROXY_CALLER = "mcp-orchestrator"
OPERATOR_CALLER = "mcp-workspace"
"""The workspace's token: an operator caller, which reads and answers the project's
permission requests for its user (D76, owner decision 2026-10-21) and calls no tool."""
"""The run service's token for the proxy, by its file's name in tokens/."""
PROXY_PORT = 8080
REMOTE = "/sandbox/mcp-proxy"
RUN = "/opt/sandbox-science/bin/mcp-proxy-run"
PROXY = "/opt/sandbox-science/bin/sandbox-science-mcp-proxy"
DEAD_PHASES = {"Error", "Deleting", "Completed"}
UPLOADS = ("pool-ceiling.yaml", "policy.yaml", "proxy.toml")
"""In this order: the proxy starts once ``proxy.toml`` is there."""
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FRAME = re.compile(r"[\s\u2500-\u257f\u00d7]+")
"""Whitespace and the box drawing miette frames and wraps a report with."""
_MISSING = re.compile(r'(?:message|self): "sandbox not found"')
"""The gateway's NotFound for a sandbox name, as tonic shows a status: ``message:`` in
the OpenShell 0.1.3 labs, ``self:`` from tonic 0.14 (OpenShell 0.1.5). Other NotFound
answers, such as a missing service endpoint, gateway or file, do not match."""


class OpenShellError(LauncherError):
    """An OpenShell command failed; its message is the end of what it said."""


class SandboxMissingError(OpenShellError):
    """The gateway answered that no sandbox has the name: proof of absence, unlike any
    other failure, which says nothing about whether the sandbox exists."""


class OpenShell:
    """The OpenShell CLI, with the config's ``env``."""

    def __init__(self, config: LauncherConfig) -> None:
        self.cli = config.openshell.cli
        self.env = {**environment(), **tooling(config)}

    async def run(self, *args: str, timeout: float = 120.0) -> str:
        process = await asyncio.create_subprocess_exec(
            self.cli, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env,
            start_new_session=True,
        )  # fmt: skip
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            said = _ANSI.sub("", errors.decode("utf-8", errors="replace")).strip()
            # Classify the whole report: the status leads it, and the end is what is kept.
            missing = _MISSING.search(_FRAME.sub(" ", said)) is not None
            error = SandboxMissingError if missing else OpenShellError
            raise error(f"openshell {' '.join(args[:2])} failed: {said[-500:]}")
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


def settings(config: LauncherConfig) -> McpProxyConfig:
    if config.mcp_proxy is None:
        raise LauncherError("the config has no [mcp_proxy]")
    return config.mcp_proxy


def sources(config: LauncherConfig) -> dict[str, Path]:
    """The policy, targets and pool ceiling the proxy's sandbox is made from."""
    mcp, environments = settings(config), config.openshell.environments
    return {
        "policy": mcp.policy or config.worker.policy,
        "targets": mcp.targets or environments.targets,
        "ceiling": mcp.ceiling or environments.ceiling,
    }


def prepare(config: LauncherConfig, layout: Layout) -> None:
    """The run service's token for the proxy, and the files the proxy's sandbox gets."""
    upload = private_dir(private_dir(layout.mcp) / "upload")
    digest = ensure_token(layout.token(PROXY_CALLER))
    found = sources(config)
    write_private(upload / "policy.yaml", found["policy"].read_text(encoding="utf-8"))
    write_private(upload / "pool-ceiling.yaml", found["ceiling"].read_text(encoding="utf-8"))
    proxy = {
        "listen": f"127.0.0.1:{PROXY_PORT}",
        "state_dir": "state",
        "ceiling": "pool-ceiling.yaml",
        "credentials": settings(config).credentials,
        "callers": [
            {
                "name": "orchestrator",
                "token_sha256": digest,
                "kind": "orchestrator",
                "project": config.mcp_project(),
            },
        ],
    }
    if settings(config).approvals:
        # The workspace's own caller, which answers permission requests and calls no tool;
        # without approvals the proxy gets the callers an older image knows.
        cast(list[dict[str, str]], proxy["callers"]).append(
            {
                "name": "workspace",
                "token_sha256": ensure_token(layout.token(OPERATOR_CALLER)),
                "kind": "operator",
                "project": config.mcp_project(),
            }
        )
    write_private(upload / "proxy.toml", toml.dumps(proxy))


def harness_options(config: LauncherConfig, layout: Layout) -> list[str]:
    """How the run service reaches the proxy, for the project whose runs use it."""
    if config.mcp_proxy is None:
        return []
    proxy = config.mcp_proxy
    servers = [option for server in proxy.servers for option in ("--mcp-server", server)]
    about = [o for name, text in proxy.about.items() for o in ("--mcp-about", f"{name}={text}")]
    index = [option for name in proxy.index or () for option in ("--mcp-index", name)]
    return [
        "--mcp-proxy", f"http://{proxy.listen}",
        "--mcp-token-file", str(layout.token(PROXY_CALLER)),
        "--mcp-project", config.mcp_project(), *servers, *about, *index,
    ]  # fmt: skip


async def register_provider(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, *, refresh: bool
) -> None:
    """Register the Access provider from the owner's file when it is missing, or always
    with ``refresh``, in a process of its own: the launcher never opens the file. The
    process gets the parts' usual environment and ``[openshell] env`` alone, and the
    OpenShell CLI as an absolute path, found as running it would find it."""
    mcp = settings(config)
    if mcp.provider_env_file is None or mcp.provider_profile is None:
        if refresh:
            raise LauncherError("--refresh-provider needs [mcp_proxy] provider_env_file")
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
        layout.mcp,
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
    names = await _providers(config)
    for provider in settings(config).providers:
        if provider not in names:
            raise LauncherError(
                f"the OpenShell provider {provider} does not exist: create it with the MCP "
                "servers' credentials, as docs/MCP-CONNECTORS.md says, then run up again"
            )


async def start(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, commands: Path
) -> None:
    """Make the proxy's sandbox and start the forward to it."""
    mcp, openshell, timeout = settings(config), OpenShell(config), config.start_timeout
    found = sources(config)
    compiled = layout.mcp / "openshell-policy.yaml"
    await supervisor.run(
        "the MCP proxy's policy",
        [str(commands / COMMAND), "compile-policy",
         "--policy", str(found["policy"]), "--ceiling", str(found["ceiling"]),
         "--targets", str(found["targets"]), "--out", str(compiled),
         # Tools the policy escalates by name pass the sandbox's fence, so that the
         # user's approval in the workspace can let them through the proxy.
         *(["--admit-escalated"] if mcp.approvals else [])],
        layout.mcp,
        timeout,
        tooling(config),
    )  # fmt: skip
    made = recorded(layout)
    if made is not None and await delete_recorded(config, layout):
        print(f"deleted the sandbox {made[0]} an earlier launcher made", flush=True)
    sandbox = f"{mcp.sandbox}-{secrets.token_hex(3)}"
    if await openshell.sandbox(sandbox) is not None:
        raise LauncherError(f"a sandbox named {sandbox} exists already: run up again")
    _record(layout, sandbox, None)
    print(f"making the MCP proxy's sandbox {sandbox}", flush=True)

    def delete_on_stop() -> None:
        supervisor.on_stop(
            f"delete the sandbox {sandbox}", lambda: _deleted(config, layout, sandbox)
        )

    try:
        await openshell.run(
            "sandbox", "create", "--name", sandbox, "--from", mcp.image,
            "--policy", str(compiled),
            *[option for provider in mcp.providers for option in ("--provider", provider)],
            "--no-auto-providers",
            "--detach", "--", RUN, timeout=timeout,
        )  # fmt: skip
    except LauncherError:
        if await openshell.sandbox(sandbox) is None:
            (layout.mcp / "sandbox.json").unlink(missing_ok=True)  # nothing was made
        else:
            delete_on_stop()
        raise
    delete_on_stop()
    _record(layout, sandbox, await _ready(openshell, sandbox, timeout))
    shell = ["sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell", "--"]
    await openshell.run(*shell, "mkdir", "-p", REMOTE)
    for name in UPLOADS:
        upload = str(layout.mcp / "upload" / name)
        await openshell.run("sandbox", "upload", "--no-git-ignore", sandbox, upload, REMOTE + "/")
    await openshell.run(
        *shell, PROXY, "bind", "--state-dir", f"{REMOTE}/state",
        "--project", config.mcp_project(), "--policy", f"{REMOTE}/policy.yaml",
    )  # fmt: skip
    forward = Part(
        "mcp-forward",
        [openshell.cli, "forward", "service", "--target-port", str(PROXY_PORT),
         "--local", mcp.listen, sandbox],
        layout.mcp,
        address=mcp.listen,
        env=tooling(config),
        restart=True,
    )  # fmt: skip
    print(f"forwarding {mcp.listen} to the MCP proxy", flush=True)
    await supervisor.start(forward, timeout)


async def _ready(openshell: OpenShell, name: str, timeout: float) -> str:
    """The sandbox's ID once it is ready."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        found = await openshell.sandbox(name) or {}
        phase, identity = found.get("phase"), found.get("id")
        if phase == "Ready" and isinstance(identity, str) and identity:
            return identity
        if phase in DEAD_PHASES:
            raise LauncherError(f"the sandbox {name} is {phase}, not ready")
        if asyncio.get_running_loop().time() > deadline:
            raise LauncherError(f"the sandbox {name} was not ready within {timeout:g} s")
        await asyncio.sleep(1)


def _record(layout: Layout, name: str, identity: str | None) -> None:
    write_private(layout.mcp / "sandbox.json", json.dumps({"name": name, "id": identity}) + "\n")


def recorded(layout: Layout) -> tuple[str, str | None] | None:
    """The sandbox this launcher made and has not deleted yet: its name and ID."""
    try:
        raw = cast(dict[str, Any], json.loads((layout.mcp / "sandbox.json").read_text()))
        identity = raw["id"]
        return str(raw["name"]), None if identity is None else str(identity)
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def delete_recorded(config: LauncherConfig, layout: Layout) -> bool:
    """Delete the sandbox the record names, if it is still the one this launcher made."""
    found = recorded(layout)
    if found is None:
        return False
    return await _deleted(config, layout, found[0])


async def _deleted(config: LauncherConfig, layout: Layout, name: str) -> bool:
    """Delete the sandbox ``name`` if the record names it: by its recorded ID, or, before
    the ID was known, by its name alone, which no one else's sandbox has (``start``)."""
    found = recorded(layout)
    if found is None or found[0] != name:
        return False
    openshell = OpenShell(config)
    existing = await openshell.sandbox(name)
    deleted = False
    if existing is not None and found[1] in (None, existing.get("id")):
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
