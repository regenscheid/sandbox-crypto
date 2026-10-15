# SPDX-License-Identifier: MIT
"""The research sandboxes the worker made for the launcher's projects.

The launcher asks the worker about them with a client token of its own, which the
worker limits to the config's projects. It never sees or stops another project's
sandbox, or one the worker did not make.

``down``, and Ctrl-C or SIGTERM to ``up``, suspend them before the worker stops, through
the worker's own suspension (``POST /v1/environments/{id}/suspend``, the same steps as
``suspend_after``). A suspended sandbox keeps its files and installed tools, and the
next use starts it again. One in use, such as one running a background job, keeps
running, and the launcher says so.

The launcher keeps the last list it got in ``sandboxes.json``. It refreshes the list
while it runs, so that ``status`` can name the sandboxes when the worker is down, and
ask OpenShell whether each one runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .config import LauncherConfig, LauncherError
from .mcp import OpenShell
from .state import Layout, write_private

SUSPEND_TIMEOUT = 180.0
"""Seconds one sandbox's suspension may take before it counts as failed."""
LIST_TIMEOUT = 30.0
REFRESH = 30.0
"""Seconds between the lists ``up`` keeps while it runs."""
SHOWN = {"ready": "running", "stopped": "suspended", "creating": "being created"}
PHASES = {"Ready": "running", "Stopped": "suspended"}


@dataclass(frozen=True)
class Sandbox:
    environment: str
    project: str
    sandbox: str
    state: str
    """As the worker or OpenShell last said: running, suspended, being created or gone."""


class WorkerApi:
    """The worker's environment routes, with the launcher's own client token."""

    def __init__(self, url: str, token_file: Path) -> None:
        self.url = url
        self.token_file = token_file
        # The worker is on loopback: never through a proxy the environment names.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, timeout: float) -> dict[str, Any]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request(  # noqa: S310 - the worker's own http URL
                self.url + path,
                method=method,
                data=b"" if method == "POST" else None,
                headers={"Authorization": f"Bearer {token}"},
            )
            with self._opener.open(request, timeout=timeout) as response:
                return cast(dict[str, Any], json.loads(response.read()))
        except urllib.error.HTTPError as error:
            try:
                said = json.loads(error.read())["error"]["message"]
            except (ValueError, KeyError, TypeError):
                said = f"status {error.code}"
            raise LauncherError(f"the worker refused {method} {path}: {said}") from None
        except (OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            raise LauncherError(f"the worker did not answer {method} {path}: {reason}") from None

    async def sandboxes(self) -> list[Sandbox]:
        listed = await asyncio.to_thread(self._call, "GET", "/v1/environments", LIST_TIMEOUT)
        return [_sandbox(item) for item in cast(list[dict[str, Any]], listed["environments"])]

    async def suspend(self, environment: str) -> tuple[bool, Sandbox]:
        """Whether the environment's sandbox is stopped now, and what the worker says of it."""
        path = f"/v1/environments/{environment}/suspend"
        result = await asyncio.to_thread(self._call, "POST", path, SUSPEND_TIMEOUT)
        return result["suspended"] is True, _sandbox(cast(dict[str, Any], result["environment"]))


def _sandbox(info: dict[str, Any]) -> Sandbox:
    state = str(info["state"])
    return Sandbox(
        environment=str(info["environment_id"]),
        project=str(info["project_id"]),
        sandbox=str(info["sandbox"]),
        state=SHOWN.get(state, state),
    )


def remember(layout: Layout, sandboxes: list[Sandbox]) -> None:
    listed = {
        "listed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sandboxes": [asdict(sandbox) for sandbox in sandboxes],
    }
    write_private(layout.sandboxes, json.dumps(listed, indent=2) + "\n")


def remembered(layout: Layout) -> tuple[str, list[Sandbox]] | None:
    """When the launcher last listed the sandboxes, and what it found; None if it never did."""
    try:
        raw = cast(dict[str, Any], json.loads(layout.sandboxes.read_text(encoding="utf-8")))
        found = [Sandbox(**item) for item in cast(list[dict[str, str]], raw["sandboxes"])]
        return str(raw["listed_at"]), found
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def keep_listing(api: WorkerApi, layout: Layout) -> None:
    """Refresh the remembered list until cancelled; a list that fails waits for the next."""
    while True:
        with contextlib.suppress(LauncherError):
            remember(layout, await api.sandboxes())
        await asyncio.sleep(REFRESH)


async def suspend_all(api: WorkerApi, layout: Layout) -> tuple[list[Sandbox], list[Sandbox]]:
    """Suspend every sandbox that runs; the ones suspended, and the ones that may still run."""
    listed = await api.sandboxes()
    running = [sandbox for sandbox in listed if sandbox.state != "suspended"]

    async def one(sandbox: Sandbox) -> tuple[bool, Sandbox]:
        try:
            return await api.suspend(sandbox.environment)
        except LauncherError:
            return False, sandbox

    outcomes = await asyncio.gather(*(one(sandbox) for sandbox in running))
    suspended = [replace(found, state="suspended") for done, found in outcomes if done]
    still = [found for done, found in outcomes if not done]
    after = {sandbox.environment: sandbox for sandbox in [*listed, *suspended, *still]}
    remember(layout, list(after.values()))
    return suspended, still


async def checked(config: LauncherConfig, known: list[Sandbox]) -> list[Sandbox]:
    """The known sandboxes, each as OpenShell says it is now; LauncherError if it cannot say."""
    openshell = OpenShell(config)

    async def one(sandbox: Sandbox) -> Sandbox:
        found = await openshell.sandbox(sandbox.sandbox)
        if found is None:
            return replace(sandbox, state="gone")
        phase = str(found.get("phase"))
        return replace(sandbox, state=PHASES.get(phase, phase.lower()))

    return list(await asyncio.gather(*(one(sandbox) for sandbox in known)))


def describe(sandboxes: list[Sandbox]) -> str:
    return ", ".join(f"{sandbox.sandbox} ({sandbox.environment})" for sandbox in sandboxes)


@dataclass
class Outcome:
    """What stopping did to the research sandboxes, for ``up`` and ``down`` to say."""

    tried: bool = False
    """An explicit stop, which suspends them."""
    kept: bool = False
    """``down --keep-sandboxes``."""
    suspended: list[Sandbox] = field(default_factory=list[Sandbox])
    running: list[Sandbox] | None = field(default_factory=list[Sandbox])
    """Those that may still run; None when the worker could not say."""

    def dumps(self) -> str:
        running = None if self.running is None else [asdict(item) for item in self.running]
        return json.dumps(
            {
                "tried": self.tried,
                "kept": self.kept,
                "suspended": [asdict(item) for item in self.suspended],
                "running": running,
            }
        )

    @staticmethod
    def read(path: Path) -> Outcome | None:
        try:
            raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            running = raw["running"]
            return Outcome(
                tried=bool(raw["tried"]),
                kept=bool(raw["kept"]),
                suspended=[Sandbox(**item) for item in raw["suspended"]],
                running=None if running is None else [Sandbox(**item) for item in running],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def report(self) -> int:
        """Say what happened; 1 when an explicit stop left a sandbox that may still run."""
        if self.suspended:
            print(f"suspended the research sandboxes {describe(self.suspended)}", flush=True)
        if self.running is None:
            print(
                "the research sandboxes may still be running: the worker could not list them",
                flush=True,
            )
        elif self.running and self.kept:
            print(f"kept the research sandboxes {describe(self.running)} running", flush=True)
        elif self.running and self.tried:
            print(
                f"these research sandboxes may still be running: {describe(self.running)}. One "
                "in use, such as one running a background job, keeps running",
                flush=True,
            )
        elif self.running:
            print(
                f"the research sandboxes {describe(self.running)} are still running: "
                "sandbox-science down stops them",
                flush=True,
            )
        return 1 if self.tried and (self.running is None or self.running) else 0


async def at_stop(api: WorkerApi, layout: Layout, outcome: Outcome) -> None:
    """Before the worker stops: suspend the sandboxes on an explicit stop, else list them."""
    if outcome.tried:
        print("suspending the research sandboxes", flush=True)
        try:
            outcome.suspended, outcome.running = await suspend_all(api, layout)
        except LauncherError:
            outcome.running = None
        return
    try:
        listed = await api.sandboxes()
    except LauncherError:
        outcome.running = None
        return
    remember(layout, listed)
    outcome.running = [sandbox for sandbox in listed if sandbox.state != "suspended"]


def lines(
    config: LauncherConfig, layout: Layout, listed: list[Sandbox] | None
) -> tuple[list[str], bool]:
    """``status``'s lines, from the worker's list when it gave one, else from OpenShell's
    word on the last list the launcher kept; and whether one may be running."""
    if listed is not None:
        remember(layout, listed)
        found = listed
        source = "as the worker says"
    else:
        known = remembered(layout)
        if known is None:
            return ["  research sandboxes: none known; the launcher has not listed any yet"], False
        try:
            found = asyncio.run(checked(config, known[1]))
        except LauncherError as exc:
            shown = (
                f"  the research sandboxes could not be checked ({exc}); as last listed, at "
                f"{known[0]}: " + (describe(known[1]) or "none")
            )
            return [shown], bool(known[1])
        source = f"as OpenShell says, of those listed at {known[0]}"
    if not found:
        return ["  research sandboxes: none"], False
    shown = [f"  research sandboxes, {source}:"] + [
        f"    {item.sandbox} ({item.environment}, project {item.project}): {item.state}"
        for item in found
    ]
    return shown, any(item.state not in ("suspended", "gone") for item in found)
