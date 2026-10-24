# SPDX-License-Identifier: MIT
"""``sandbox-science-runtime-manager``: ``serve``, and the operator's requests to it.

The operator runs them in the manager's Pod, for example ``kubectl exec
deploy/runtime-manager -- sandbox-science-runtime-manager request bootstrap``:

- ``request bootstrap`` creates the first runtime sandbox and attaches the runtime;
  it requires that no sandbox is recorded (KRB-12).
- ``request replace`` stops the runtime, deletes the recorded sandbox once its identity
  is checked and its deletion confirmed, and creates and attaches a new one with the
  configured placement (KRB-14).
- ``request stop --delete`` is teardown: the runtime stops and the sandbox is deleted
  the same way, its record removed once confirmed. A stop that deletes nothing is the
  manager's SIGTERM (scaling the Deployment to zero), which keeps the sandbox (KRB-15).
- ``request forget`` clears a record whose create never happened: one without a gateway
  ID, whose name the gateway answers it does not have. Absence observed without an
  identity is no proof that nothing was made, so this is the operator's decision; it
  refuses any other record.
- ``request status`` shows the serving process, the record and the recovery journal.

A request reaches only the serving process that ``status.json`` names, bound to its
random instance nonce as the host launcher's replacement requests are bound to the
launcher: the command writes ``request.json``, wakes the process with SIGUSR2 and waits
for the receipt carrying its nonce, as long as the manager's bounds allow
(``request_deadline``). One request waits at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from sandbox_science.contracts.logs import configure_logging
from sandbox_science.launcher import openshell_version
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.state import check_private, write_private

from .manager import request_deadline, serve
from .records import ManagerOwnership, ManagerRequest, Result, Status, read_bounded
from .settings import RuntimeManagerSettings, load

Action = Literal["bootstrap", "replace", "stop", "forget"]
SETTINGS_ENV = "SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS"
DEFAULT_SETTINGS = Path("/etc/sandbox-science/runtime-manager/runtime-manager.json")
DONE = {
    "bootstrap": "runtime sandbox created and the runtime attached",
    "replace": "runtime sandbox replaced; explicitly Resume unfinished work in the workspace",
    "stop": "runtime stopped and its sandbox deleted",
    "forget": "the record of a sandbox never created is cleared; request bootstrap next",
}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _serving(root: Path) -> Status | None:
    data = read_bounded(root / "status.json")
    return None if data is None else Status.model_validate_json(data)


def request(settings: RuntimeManagerSettings, action: Action, *, poll: float = 0.1) -> int:
    """Hand ``action`` to the serving manager and wait for its receipt."""
    root = settings.state_dir
    check_private(root)
    serving = _serving(root)
    if serving is None or not _alive(serving.pid):
        raise LauncherError("no runtime manager is serving from this state directory")
    descriptor = os.open(root / "request.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LauncherError("another runtime manager request is still waiting") from None
        selected = ManagerRequest(
            nonce="replacement-" + secrets.token_hex(16),
            instance=serving.instance,
            action=action,
            delete=action == "stop",
        )
        write_private(root / "request.json", selected.model_dump_json(exclude_none=True) + "\n")
        os.kill(serving.pid, signal.SIGUSR2)
        deadline = time.monotonic() + request_deadline(settings)
        while _alive(serving.pid):
            data = read_bounded(root / "result.json")
            receipt = None if data is None else Result.model_validate_json(data)
            if receipt is not None and receipt.nonce == selected.nonce:
                if receipt.error is not None:
                    raise LauncherError(receipt.error)
                print(DONE[action])
                return 0
            if time.monotonic() >= deadline:
                raise LauncherError("the request's outcome is unknown; see status and the log")
            time.sleep(poll)
        raise LauncherError("the runtime manager stopped before answering the request")
    finally:
        os.close(descriptor)


def status(settings: RuntimeManagerSettings) -> dict[str, object]:
    """What ``request status`` prints; it needs no serving process."""
    root = settings.state_dir
    check_private(root)
    serving = _serving(root)
    recovery = read_bounded(root / "infrastructure-recovery.json")
    return {
        "serving": serving is not None and _alive(serving.pid),
        "manager": serving.model_dump(mode="json") if serving is not None else None,
        "sandbox": ManagerOwnership(root / "sandbox.json").document(),
        "recovery": json.loads(recovery) if recovery is not None else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """The ``sandbox-science-runtime-manager`` command."""
    parser = argparse.ArgumentParser(
        prog="sandbox-science-runtime-manager",
        description="Own the agent runtime's OpenShell sandbox on Kubernetes.",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(os.environ.get(SETTINGS_ENV) or DEFAULT_SETTINGS),
        help=f"runtime-manager.json (default: ${SETTINGS_ENV}, else {DEFAULT_SETTINGS})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="own, reconnect and watch the runtime until SIGTERM")
    asking = commands.add_parser("request", help="ask the serving manager to act")
    asking.add_argument("action", choices=("bootstrap", "replace", "stop", "forget", "status"))
    asking.add_argument(
        "--delete", action="store_true", help="with stop: delete the runtime sandbox (teardown)"
    )
    args = parser.parse_args(argv)
    if args.command == "request" and args.delete != (args.action == "stop"):
        parser.error("stop needs --delete (SIGTERM stops without deleting); nothing else takes it")
    try:
        settings = load(args.settings)
        if args.command == "serve":
            configure_logging("runtime-manager")
            openshell_version.require(settings.openshell.cli)
            asyncio.run(serve(settings))
            return 0
        if args.action == "status":
            print(json.dumps(status(settings), indent=2, sort_keys=True))
            return 0
        return request(settings, args.action)
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
