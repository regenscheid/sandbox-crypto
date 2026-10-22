# SPDX-License-Identifier: MIT
"""Command line: a local Sandbox Science, up and down (docs/QUICKSTART.md).

```
sandbox-science up [--config sandbox-science.toml] [--update-skills]
sandbox-science down [--config sandbox-science.toml] [--keep-sandboxes]
sandbox-science status [--config sandbox-science.toml]
sandbox-science skills update [--config sandbox-science.toml] [--pack NAME ...]
```

``up`` checks the config, makes what the parts need in the state directory, and starts
the worker, then grants the project policy, makes the projects and opens their
sessions, imports and pins their skill packs (skills.py), then starts the run service
and the workspace, each once the one before is ready. It prints the GUI's address and
how to log in, and runs until Ctrl-C, SIGTERM, SIGHUP or SIGQUIT (or ``down``), when it
stops the parts in reverse order, suspending the projects' research sandboxes before
the worker (sandboxes.py). ``down
--keep-sandboxes`` sends SIGUSR1 instead, which leaves them running. A part that stops
by itself stops the rest, and leaves the sandboxes running. ``status`` exits 0 while an
instance runs as it should, 3 when none runs, and 4 when something is wrong: a part
stopped or silent, something left behind by a launcher that was killed, or research
sandboxes running with the rest stopped. ``skills update`` imports the config's skill
packs again from their refs and moves the projects' pins to them, saying what changed,
whether an instance runs or not: runs started before it keep the skills they have.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import instance, mcp, orchestrator, parts, replacement, sandboxes, skills
from .config import LauncherConfig, LauncherError, load_config
from .instance import Record, RecordedPart
from .state import Layout, check_private, write_private
from .supervise import Part, Running, StopRequested, Supervisor, environment, passed, start_time

NOT_RUNNING = 3
DEGRADED = 4
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
"""Closing the terminal or the SSH session (SIGHUP) stops the parts too, in order."""
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
        ("replace-orchestrator", "replace the agent VM while keeping control and workspace alive"),
        ("skills", "fetch the skill packs again and move the projects' pins"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--config", type=Path, default=Path("sandbox-science.toml"))
        if name == "up":
            command.add_argument(
                "--refresh-provider",
                action="store_true",
                help="register [mcp_proxy] provider_env_file's provider again, though it exists",
            )
            command.add_argument(
                "--update-skills",
                action="store_true",
                help="fetch the skill packs again from their refs, and move the projects' pins",
            )
        if name == "skills":
            command.add_argument("action", choices=("update",))
            command.add_argument(
                "--pack", action="append", default=[], help="only this pack of [skills.packs]"
            )
        if name == "down":
            command.add_argument(
                "--keep-sandboxes",
                action="store_true",
                help="leave the research sandboxes running, rather than suspend them",
            )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        match args.command:
            case "up":
                return up(
                    config,
                    args.config.absolute(),
                    refresh=args.refresh_provider,
                    update_skills=args.update_skills,
                )
            case "down":
                return down(config, args.config.absolute(), keep=args.keep_sandboxes)
            case "replace-orchestrator":
                return replacement.request(config)
            case "skills":
                return update_skills(config, args.pack)
            case _:
                return status(config)
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def up(
    config: LauncherConfig, path: Path, *, refresh: bool = False, update_skills: bool = False
) -> int:
    parts.check(config)
    layout = Layout(config.state_dir)
    layout.make()
    held = instance.lock(layout)
    try:
        earlier = instance.read(layout)
        record = _recorder(config, path, layout)
        record([])  # this launcher's own record, before anything of the earlier one's goes
        for part in instance.stop_left_behind(earlier):
            print(f"stopped the {part.name} (process {part.pid}) an earlier launcher left running")
        parts.prepare(config)
        return asyncio.run(
            _up(config, path, layout, record, refresh=refresh, update_skills=update_skills)
        )
    finally:
        os.close(held)


def _recorder(
    config: LauncherConfig, path: Path, layout: Layout, purpose: str = "up"
) -> Callable[[list[Running]], None]:
    """Keep the instance's record as its parts start and stop."""
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    pid = os.getpid()
    started, boot = start_time(pid) or 0, instance.boot_id()

    def record(running: list[Running]) -> None:
        instance.write(
            layout,
            Record(
                pid=pid,
                started=started,
                boot=boot,
                started_at=started_at,
                config=str(path),
                gui=f"http://{config.workspace.listen}/",
                parts=tuple(
                    RecordedPart(
                        r.part.name,
                        r.process.pid,
                        start_time(r.process.pid) or 0,
                        r.part.argv[0],
                        r.part.address,
                    )
                    for r in running
                ),
                purpose=purpose,
                replacement=True,
            ),
        )

    return record


async def _up(
    config: LauncherConfig,
    path: Path,
    layout: Layout,
    record: Callable[[list[Running]], None],
    *,
    refresh: bool = False,
    update_skills: bool = False,
) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    replacing = asyncio.Event()
    asked = 0
    outcome = sandboxes.Outcome()
    hurry = asyncio.Event()
    """A second Ctrl-C: stop waiting, for the parts and for the suspension."""

    def ask(keep: bool) -> None:
        nonlocal asked
        asked += 1
        if asked == 1:
            outcome.tried, outcome.kept = not keep, keep
            print("stopping; Ctrl-C again to stop at once", flush=True)
        else:
            hurry.set()
        stop.set()

    for number in STOP_SIGNALS:
        loop.add_signal_handler(number, ask, False)
    loop.add_signal_handler(signal.SIGUSR1, ask, True)  # down --keep-sandboxes
    loop.add_signal_handler(signal.SIGUSR2, replacing.set)
    layout.stopped.unlink(missing_ok=True)
    supervisor = Supervisor(
        layout,
        stop,
        stop_timeout=config.stop_timeout,
        changed=lambda: record(supervisor.running),
        env=environment(),
    )
    status = 0
    try:
        await _start(
            config, layout, supervisor, outcome, hurry, refresh=refresh, update=update_skills
        )
        _announce(config, path, layout)
        reason = await replacement.watch(config, layout, supervisor, replacing)
        if reason is not None:
            print(f"error: {reason}", file=sys.stderr, flush=True)
            status = 1
    except StopRequested:
        pass
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        status = 1
    finally:
        await supervisor.stop(now=hurry.is_set)
        for leftover in (layout.login, layout.record):
            leftover.unlink(missing_ok=True)
        for number in (*STOP_SIGNALS, signal.SIGUSR1, signal.SIGUSR2):
            loop.remove_signal_handler(number)
    print("Sandbox Science stopped", flush=True)
    write_private(layout.stopped, outcome.dumps() + "\n")
    return max(status, outcome.report())


def _worker(config: LauncherConfig, layout: Layout) -> Part:
    return Part(
        "worker",
        parts.worker_argv(config, layout),
        layout.worker,
        address=config.worker.listen,
        env=passed(config.worker.pass_env),
        restart=bool(config.orchestrator and config.orchestrator.recover_infrastructure),
        required=not bool(config.orchestrator and config.orchestrator.recover_infrastructure),
    )


async def _start(
    config: LauncherConfig,
    layout: Layout,
    supervisor: Supervisor,
    outcome: sandboxes.Outcome,
    hurry: asyncio.Event,
    *,
    refresh: bool = False,
    update: bool = False,
) -> None:
    timeout = config.start_timeout
    if config.mcp_proxy is not None:
        await mcp.register_provider(config, layout, supervisor, refresh=refresh)
        await mcp.check_providers(config)
    elif refresh:
        raise LauncherError("--refresh-provider needs [mcp_proxy] provider_env_file")
    print(f"starting the worker on {config.worker.listen}", flush=True)
    worker_env = passed(config.worker.pass_env)
    await supervisor.start(_worker(config, layout), timeout)
    # Taken just before the worker stops, once the run service and the workspace have.
    api = sandboxes.WorkerApi(parts.worker_url(config), layout.token("launcher-worker"))
    listing = asyncio.ensure_future(sandboxes.keep_listing(api, layout))

    async def research_sandboxes() -> None:
        listing.cancel()
        await sandboxes.at_stop(api, layout, outcome, hurry)

    supervisor.on_stop("look after the research sandboxes", research_sandboxes)
    granted = await supervisor.run(
        "the policy grant", parts.policy_argv(config, layout), layout.worker, timeout, worker_env
    )
    digest = re.search(r"sha256:[0-9a-f]{64}", granted)
    if digest is None:
        raise LauncherError(f"the policy grant named no policy; see {layout.log('launcher')}")
    harness_env = passed(config.run_service.pass_env)
    existing = await _existing(config, layout, supervisor, timeout, harness_env)
    for argv in parts.project_argvs(config, layout, digest.group(), existing):
        await supervisor.run(" ".join(argv[1:3]), argv, layout.control, timeout, harness_env)
    lines, _ = await skills.keep(config, layout, supervisor, update=update, models=False)
    for line in lines:  # a pack that failed is said here, and the rest starts without it
        print(line, flush=True)
    if config.mcp_proxy is not None:
        await mcp.start(config, layout, supervisor, parts.commands(config))
    await orchestrator.start(config, layout, supervisor)
    print(f"starting the workspace on {config.workspace.listen}", flush=True)
    layout.login.unlink(missing_ok=True)
    workspace = Part(
        "workspace",
        parts.workspace_argv(config, layout),
        layout.control,
        ready_line="/login?token=",
        address=config.workspace.listen,
        private=(LOGIN, layout.login),
        env=passed(config.workspace.pass_env),
        restart=bool(config.orchestrator and config.orchestrator.recover_infrastructure),
        required=not bool(config.orchestrator and config.orchestrator.recover_infrastructure),
    )
    await supervisor.start(workspace, timeout)


async def _existing(
    config: LauncherConfig,
    layout: Layout,
    supervisor: Supervisor,
    timeout: float,
    env: Mapping[str, str],
) -> dict[str, str]:
    """The config's projects the control store has already, with their profiles: ``project
    show`` answers a project's record, and fails for one that is not there."""
    found: dict[str, str] = {}
    for project in config.projects:
        argv = parts.project_show_argv(config, layout, project.id)
        status, text = await supervisor.outcome("project show", argv, layout.control, timeout, env)
        if status != 0:
            continue
        try:
            found[project.id] = str(json.loads(text)["profile"])
        except (ValueError, KeyError, TypeError):
            raise LauncherError(
                f"project show {project.id} did not answer the project's record; see "
                f"{layout.log('launcher')}"
            ) from None
    return found


def _announce(config: LauncherConfig, path: Path, layout: Layout) -> None:
    projects = "; ".join(
        f"{project.id} (sessions "
        + ", ".join(
            session.id + (" in an environment of its own" if session.dedicated else "")
            for session in project.sessions
        )
        + ")"
        for project in config.projects
    )
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    print(
        "\nSandbox Science is running.\n"
        f"  GUI:      http://{config.workspace.listen}/\n"
        f"  Log in:   open the address in {layout.login}, for example with\n"
        f'            {opener} "$(cat {layout.login})"\n'
        "            It works until Sandbox Science stops.\n"
        f"  Projects: {projects}\n"
        f"  Logs:     {layout.logs}\n"
        f"  Stop:     Ctrl-C, or sandbox-science down --config {path}\n",
        flush=True,
    )


def update_skills(config: LauncherConfig, packs: Sequence[str]) -> int:
    """Import the config's packs, or ``packs``, again, and move the projects' pins."""
    layout = Layout(config.state_dir)
    check_private(layout.root)  # before anything in it is read
    if not config.skills.packs:
        raise LauncherError("the config names no [skills.packs]")
    if not layout.control.is_dir():
        raise LauncherError("sandbox-science up makes the projects first")
    models = (config.skills.embedding_model, config.skills.metadata_model)
    if any(model is not None for model in models) and (problems := parts.key_problems(config)):
        raise LauncherError("cannot call the import's models: " + "; ".join(problems))
    supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=config.stop_timeout)
    lines, failed = asyncio.run(skills.keep(config, layout, supervisor, update=True, only=packs))
    for line in lines:
        print(line)
    return 1 if failed else 0


def down(config: LauncherConfig, path: Path, *, keep: bool) -> int:
    layout = Layout(config.state_dir)
    check_private(layout.root)  # before anything in it is read
    held = instance.try_lock(layout) if layout.root.is_dir() else None
    if held is not None:
        try:
            return _down_stopped(config, path, layout, keep=keep)
        finally:
            os.close(held)
    if not instance.running(layout):
        print("Sandbox Science is not running")
        return 0
    found = instance.read(layout)
    if found is None or not instance.launcher_alive(found):
        raise LauncherError(
            f"an instance holds {layout.lock}, but its record does not name it: try again, "
            "or press Ctrl-C where it runs"
        )
    if found.purpose == "down":
        print(
            f"a sandbox-science down (process {found.pid}) is suspending the research "
            "sandboxes: waiting for it",
            flush=True,
        )
    else:
        layout.stopped.unlink(missing_ok=True)
        os.kill(found.pid, signal.SIGUSR1 if keep else signal.SIGTERM)
    patience = config.stop_timeout * (len(found.parts) + 1) + sandboxes.SUSPEND_TIMEOUT + 30
    deadline = time.monotonic() + patience
    while instance.running(layout):
        if time.monotonic() > deadline:
            raise LauncherError(
                f"Sandbox Science (process {found.pid}) still runs after {patience:g} s; "
                f"its parts' logs are in {layout.logs}"
            )
        time.sleep(0.2)
    print("Sandbox Science stopped")
    outcome = sandboxes.Outcome.read(layout.stopped)
    if outcome is None:
        print("what happened to the research sandboxes is not known: sandbox-science status says")
        return 0 if keep else 1
    return outcome.report()


def _down_stopped(config: LauncherConfig, path: Path, layout: Layout, *, keep: bool) -> int:
    """``down`` with the lock held, so no ``up`` runs: clean up after one that was killed,
    and suspend the research sandboxes that still run, starting the worker for it."""
    stopped = instance.stop_left_behind(instance.read(layout))
    for part in stopped:
        print(f"stopped the {part.name} (process {part.pid}) an earlier launcher left running")
    layout.record.unlink(missing_ok=True)
    if orchestrator.recorded(layout) is not None:
        asyncio.run(orchestrator.delete_recorded(config, layout))
    made = mcp.recorded(layout)
    if made is not None and asyncio.run(mcp.delete_recorded(config, layout)):
        print(f"deleted the sandbox {made[0]} an earlier launcher made")
    if not stopped and made is None:
        print("Sandbox Science is not running")
    known = sandboxes.remembered(layout)
    if keep or known is None:
        return 0  # kept on request, or no worker of this state directory ever listed any
    if sandboxes.Outcome.read(layout.stopped) is None:
        # The last stop said nothing of them (a launcher killed outright): the list may
        # be half a minute old, so the worker lists them again.
        print("starting the worker to list and suspend the research sandboxes", flush=True)
    else:
        try:
            now = asyncio.run(sandboxes.checked(config, known[1]))
        except LauncherError:
            now = known[1]  # OpenShell could not say: they may run
        running = [item for item in now if item.state not in ("suspended", "gone")]
        if not running:
            return 0
        print(
            "starting the worker to suspend the research sandboxes " + sandboxes.describe(running),
            flush=True,
        )
    parts.prepare(config)
    return asyncio.run(_suspend_with_worker(config, path, layout))


async def _suspend_with_worker(config: LauncherConfig, path: Path, layout: Layout) -> int:
    """Start the worker alone, suspend the research sandboxes through it, and stop it.

    Its record says it is ``down``'s, so another ``down`` waits for it rather than
    signal it, and ``status`` says so. A stop signal (Ctrl-C, SIGTERM, SIGHUP, SIGQUIT
    or SIGUSR1) stops waiting for the suspension, and stops the worker in order.
    """
    loop = asyncio.get_running_loop()
    hurry = asyncio.Event()

    def ask() -> None:
        if not hurry.is_set():
            print("stopping; no longer waiting for the suspension", flush=True)
        hurry.set()

    for number in (*STOP_SIGNALS, signal.SIGUSR1):
        loop.add_signal_handler(number, ask)
    record = _recorder(config, path, layout, purpose="down")
    supervisor = Supervisor(
        layout,
        hurry,
        stop_timeout=config.stop_timeout,
        changed=lambda: record(supervisor.running),
        env=environment(),
    )
    outcome = sandboxes.Outcome(tried=True)
    try:
        await supervisor.start(_worker(config, layout), config.start_timeout)
        api = sandboxes.WorkerApi(parts.worker_url(config), layout.token("launcher-worker"))
        await sandboxes.at_stop(api, layout, outcome, hurry)
    except StopRequested:
        outcome.reason = "stopped before the worker was ready"
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        outcome.reason = str(exc)
    finally:
        await supervisor.stop()
        layout.record.unlink(missing_ok=True)
        for number in (*STOP_SIGNALS, signal.SIGUSR1):
            loop.remove_signal_handler(number)
    write_private(layout.stopped, outcome.dumps() + "\n")
    return outcome.report()


def status(config: LauncherConfig) -> int:
    layout = Layout(config.state_dir)
    check_private(layout.root)  # before anything in it is read
    found = instance.read(layout)
    if not instance.running(layout):
        print("Sandbox Science is not running")
        left = instance.left_behind(found)
        made = mcp.recorded(layout)
        agent = orchestrator.recorded(layout)
        if agent is not None:
            print(
                f"  but the orchestrator sandbox {agent[0]} may be left: "
                "sandbox-science down deletes it"
            )
        if made is not None:
            print(
                f"  but the sandbox {made[0]} it made may be left: sandbox-science down deletes it"
            )
        for part in left:
            print(
                f"  but the {part.name} an earlier launcher started still runs (process "
                f"{part.pid}, or what it started): sandbox-science down stops it"
            )
        shown, research = sandboxes.lines(config, layout, None)
        for line in shown:
            print(line)
        return (
            DEGRADED if left or made is not None or agent is not None or research else NOT_RUNNING
        )
    if found is None:
        print(f"Sandbox Science is starting, from {layout.root}")
        return 0
    if found.purpose == "down":
        print(
            f"Sandbox Science is stopping: sandbox-science down (process {found.pid}) runs "
            "the worker alone to suspend the research sandboxes"
        )
        return DEGRADED
    print(f"Sandbox Science is running (process {found.pid}, since {found.started_at})")
    print(f"  config: {found.config}")
    if found.gui is not None and layout.login.exists():
        print(f"  GUI:    {found.gui}, logging in with the address in {layout.login}")
    healthy = True
    for part in found.parts:
        running = instance.alive(part, found)
        answers = part.address is None or _answers(part.address)
        healthy = healthy and running and answers
        reach = ""
        if part.address is not None:
            reach = f", {part.address} " + ("answers" if answers else "is silent")
        print(f"  {part.name}: process {part.pid}, {'running' if running else 'stopped'}{reach}")
    made = mcp.recorded(layout)
    if made is not None:
        print(f"  the MCP proxy's sandbox: {made[0]}")
    agent = orchestrator.recorded(layout)
    if agent is not None:
        print(f"  the orchestrator's sandbox: {agent[0]}")
    api = sandboxes.WorkerApi(parts.worker_url(config), layout.token("launcher-worker"))
    try:
        listed = asyncio.run(api.sandboxes())
    except LauncherError:
        listed = None  # the worker does not answer: ask OpenShell
    for line in sandboxes.lines(config, layout, listed)[0]:
        print(line)
    return 0 if healthy else DEGRADED


def _answers(address: str) -> bool:
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False
