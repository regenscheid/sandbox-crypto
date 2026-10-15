# SPDX-License-Identifier: MIT
"""Command line: a local Sandbox Science, up and down (docs/QUICKSTART.md).

```
sandbox-science up [--config sandbox-science.toml]
sandbox-science down [--config sandbox-science.toml]
sandbox-science status [--config sandbox-science.toml]
```

``up`` checks the config, makes what the parts need in the state directory, and starts
the worker, then grants the project policy, makes the projects and opens their
sessions, then starts the run service and the workspace, each once the one before is
ready. It prints the GUI's address and how to log in, and runs until Ctrl-C or SIGTERM
(or ``down``), when it stops the parts in reverse order. A part that stops by itself
stops the rest. ``status`` exits 0 while an instance runs and 3 when none does.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import socket
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import instance, parts
from .config import LauncherConfig, LauncherError, load_config
from .instance import Record, RecordedPart
from .state import Layout
from .supervise import Part, StopRequested, Supervisor

NOT_RUNNING = 3
LOGIN = re.compile(r"/login\?token=")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbox-science", description="A local Sandbox Science, from one config file."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("up", "start everything, and stop it all on Ctrl-C"),
        ("down", "stop the instance running from the config's state directory"),
        ("status", "show what runs"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--config", type=Path, default=Path("sandbox-science.toml"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        match args.command:
            case "up":
                return up(config, args.config.absolute())
            case "down":
                return down(config)
            case _:
                return status(config)
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def up(config: LauncherConfig, path: Path) -> int:
    parts.check(config)
    layout = Layout(config.state_dir)
    layout.make()
    held = instance.lock(layout)
    try:
        for part in instance.stop_left_behind(layout, locked=True):
            print(f"stopped the {part.name} (process {part.pid}) an earlier launcher left running")
        parts.prepare(config)
        return asyncio.run(_up(config, path, layout))
    finally:
        os.close(held)


async def _up(config: LauncherConfig, path: Path, layout: Layout) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    asked = 0

    def ask() -> None:
        nonlocal asked
        asked += 1
        if asked == 1:
            print("stopping; Ctrl-C again to stop at once", flush=True)
        stop.set()

    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, ask)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def record() -> None:
        instance.write(
            layout,
            Record(
                pid=os.getpid(),
                started_at=started_at,
                config=str(path),
                gui=f"http://{config.workspace.listen}/",
                parts=tuple(
                    RecordedPart(r.part.name, r.process.pid, r.part.argv[0], r.part.address)
                    for r in supervisor.running
                ),
            ),
        )

    supervisor = Supervisor(layout, stop, stop_timeout=config.stop_timeout, changed=record)
    record()
    status = 0
    try:
        await _start(config, layout, supervisor)
        _announce(config, path, layout)
        reason = await supervisor.watch()
        if reason is not None:
            print(f"error: {reason}", file=sys.stderr, flush=True)
            status = 1
    except StopRequested:
        pass
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        status = 1
    finally:
        await supervisor.stop(now=lambda: asked > 1)
        for leftover in (layout.login, layout.record):
            leftover.unlink(missing_ok=True)
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)
    print("Sandbox Science stopped", flush=True)
    return status


async def _start(config: LauncherConfig, layout: Layout, supervisor: Supervisor) -> None:
    timeout = config.start_timeout
    print(f"starting the worker on {config.worker.listen}", flush=True)
    worker = Part(
        "worker", parts.worker_argv(config, layout), layout.worker, address=config.worker.listen
    )
    await supervisor.start(worker, timeout)
    granted = await supervisor.run(
        "the policy grant", parts.policy_argv(config, layout), layout.worker, timeout
    )
    digest = re.search(r"sha256:[0-9a-f]{64}", granted)
    if digest is None:
        raise LauncherError(f"the policy grant named no policy; see {layout.log('launcher')}")
    for argv in parts.project_argvs(config, layout, digest.group()):
        await supervisor.run(" ".join(argv[1:3]), argv, layout.control, timeout)
    print("starting the run service", flush=True)
    harness = Part(
        "run-service",
        parts.harness_argv(config, layout),
        layout.control,
        ready_line="serving the sessions of",
    )
    await supervisor.start(harness, timeout)
    print(f"starting the workspace on {config.workspace.listen}", flush=True)
    layout.login.unlink(missing_ok=True)
    workspace = Part(
        "workspace",
        parts.workspace_argv(config, layout),
        layout.control,
        ready_line="/login?token=",
        address=config.workspace.listen,
        private=(LOGIN, layout.login),
    )
    await supervisor.start(workspace, timeout)


def _announce(config: LauncherConfig, path: Path, layout: Layout) -> None:
    projects = "; ".join(
        f"{project.id} (sessions {', '.join(project.sessions)})" for project in config.projects
    )
    print(
        "\nSandbox Science is running.\n"
        f"  GUI:      http://{config.workspace.listen}/\n"
        f"  Log in:   open the address in {layout.login}, for example with\n"
        f'            xdg-open "$(cat {layout.login})"\n'
        "            It works until Sandbox Science stops.\n"
        f"  Projects: {projects}\n"
        f"  Logs:     {layout.logs}\n"
        f"  Stop:     Ctrl-C, or sandbox-science down --config {path}\n",
        flush=True,
    )


def down(config: LauncherConfig) -> int:
    layout = Layout(config.state_dir)
    if not instance.running(layout):
        stopped = instance.stop_left_behind(layout)
        for part in stopped:
            print(f"stopped the {part.name} (process {part.pid}) an earlier launcher left running")
        if not stopped:
            print("Sandbox Science is not running")
        return 0
    found = instance.read(layout)
    if found is None:
        raise LauncherError(
            f"an instance holds {layout.lock} but has no record yet: try again, or press "
            "Ctrl-C where it runs"
        )
    os.kill(found.pid, signal.SIGTERM)
    patience = config.stop_timeout * (len(found.parts) + 1) + 30
    deadline = time.monotonic() + patience
    while instance.running(layout):
        if time.monotonic() > deadline:
            raise LauncherError(
                f"Sandbox Science (process {found.pid}) still runs after {patience:g} s; "
                f"its parts' logs are in {layout.logs}"
            )
        time.sleep(0.2)
    print("Sandbox Science stopped")
    return 0


def status(config: LauncherConfig) -> int:
    layout = Layout(config.state_dir)
    if not instance.running(layout):
        print("Sandbox Science is not running")
        for part in instance.left_behind(layout):
            print(
                f"  but the {part.name} an earlier launcher started still runs (process "
                f"{part.pid}): sandbox-science down stops it"
            )
        return NOT_RUNNING
    found = instance.read(layout)
    if found is None:
        print(f"Sandbox Science is starting, from {layout.root}")
        return 0
    print(f"Sandbox Science is running (process {found.pid}, since {found.started_at})")
    print(f"  config: {found.config}")
    if found.gui is not None and layout.login.exists():
        print(f"  GUI:    {found.gui}, logging in with the address in {layout.login}")
    for part in found.parts:
        state = "running" if instance.alive(part) else "stopped"
        reach = ""
        if part.address is not None:
            reach = f", {part.address} " + ("answers" if _answers(part.address) else "is silent")
        print(f"  {part.name}: process {part.pid}, {state}{reach}")
    return 0


def _answers(address: str) -> bool:
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False
