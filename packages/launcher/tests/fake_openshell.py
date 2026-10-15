# SPDX-License-Identifier: MIT
"""A stand-in for the OpenShell CLI in the launcher's tests (``fake_openshell`` in conftest).

Its providers and sandboxes are a JSON file, the one ``FAKE_OPENSHELL`` names, and what
it does goes in ``FAKE_JOURNAL`` as the parts' stand-ins' steps do. With ``FAKE_PARTS``
saying ``{"openshell": "forward-exits-once"}``, the first forward exits a moment after
it starts, as one does when its sandbox stops; with ``forward-exits-always``, every one
does; with ``create-fails``, ``sandbox create`` fails and makes nothing; with
``get-fails``, ``sandbox get`` cannot reach the gateway.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_OPENSHELL"])
behaviour = json.loads(os.environ.get("FAKE_PARTS", "{}")).get("openshell", "")


def note(text: str) -> None:
    with Path(os.environ["FAKE_JOURNAL"]).open("a") as journal:
        journal.write(f"openshell {text}\n")


def load() -> dict[str, Any]:
    if not state_file.exists():
        return {"providers": [], "sandboxes": {}}
    return json.loads(state_file.read_text())


def save(state: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(state))


def forward(local: str) -> None:
    def stopped(*_: object) -> None:
        note("forward stop")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stopped)
    host, port = local.rsplit(":", 1)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, int(port)))
    listener.listen()
    listener.settimeout(0.1)
    note("forward start")
    marker = state_file.with_name("forward-exited")
    started = time.monotonic()
    while True:
        once = behaviour == "forward-exits-once" and not marker.exists()
        if (once or behaviour == "forward-exits-always") and time.monotonic() - started > 0.5:
            marker.touch()
            note("forward exits")
            sys.exit(1)
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            continue
        connection.close()


match args:
    case ["provider", "list", "-o", "json"]:
        providers = [{"name": name, "type": "cloudflare-access"} for name in load()["providers"]]
        print(json.dumps({"providers": providers}))
    case ["sandbox", "get", name, "-o", "json"]:
        if behaviour == "get-fails":
            print("error: the gateway is not reachable", file=sys.stderr)
            sys.exit(1)
        found = load()["sandboxes"].get(name)
        if found is None:
            print(f"\x1b[31mError:\x1b[0m sandbox {name} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"name": name, **found}))
    case [
        "sandbox", "create", "--name", name, "--from", image, "--policy", policy,
        "--provider", provider, "--no-auto-providers", "--detach", "--", *command,
    ]:  # fmt: skip
        if not Path(policy).is_file():
            sys.exit(2)
        if behaviour == "create-fails":
            print(f"error: sandbox {name} already exists", file=sys.stderr)
            sys.exit(1)
        state = load()
        state["sandboxes"][name] = {"id": uuid.uuid4().hex, "phase": "Ready", "image": image}
        save(state)
        note(f"sandbox create {name} {image} provider {provider} {' '.join(command)}")
    case ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--", *command]:
        note(f"exec {name} {' '.join(command)}")
    case ["sandbox", "upload", "--no-git-ignore", name, local, remote]:
        if not Path(local).is_file():
            sys.exit(2)
        note(f"upload {name} {Path(local).name} {remote}")
    case ["sandbox", "delete", name]:
        state = load()
        state["sandboxes"].pop(name, None)
        save(state)
        note(f"sandbox delete {name}")
    case ["forward", "service", "--target-port", "8080", "--local", local, _]:
        forward(local)
    case _:
        print(f"openshell: unexpected arguments {args}", file=sys.stderr)
        sys.exit(64)
