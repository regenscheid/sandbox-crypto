# SPDX-License-Identifier: MIT
"""A stand-in for the OpenShell CLI in the launcher's tests (``fake_openshell`` in conftest).

Its providers and sandboxes are a JSON file, the one ``FAKE_OPENSHELL`` names, and what
it does goes in ``FAKE_JOURNAL`` as the parts' stand-ins' steps do. With ``FAKE_PARTS``
saying ``{"openshell": "forward-exits-once"}``, the first forward exits a moment after
it starts, as one does when its sandbox stops; with ``forward-exits-always``, every one
does; with ``create-fails``, ``sandbox create`` fails and makes nothing; with
``get-fails``, ``sandbox get`` cannot reach the gateway; with ``provider-fails``,
``provider create`` and ``update`` fail.

``provider create`` and ``update`` read their credentials from the environment, as
``--credential KEY`` does, and say the values on stdout and stderr, as a careless CLI
could: the caller must not show them. With ``FAKE_OPENSHELL_CALLS``, each call's
arguments, environment and core-file limit go there, a JSON line each.

The registration helper refuses to run with ``FAKE_*`` set, so the wrappers that run
this stand-in set what it needs themselves (``fake_openshell`` in conftest).
"""

from __future__ import annotations

import json
import os
import resource
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


def register(name: str, keys: list[str], what: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        print(" ".join(f"{key}={os.environ.get(key)}" for key in keys), file=stream)
    if behaviour == "provider-fails" or not all(os.environ.get(key) for key in keys):
        sys.exit(1)
    state = load()
    if name not in state["providers"]:
        state["providers"].append(name)
    save(state)
    note(f"provider {what} {name} {' '.join(keys)}")


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


if calls := os.environ.get("FAKE_OPENSHELL_CALLS"):
    with Path(calls).open("a") as log:
        core = resource.getrlimit(resource.RLIMIT_CORE)
        log.write(json.dumps({"argv": args, "env": dict(os.environ), "core": core}) + "\n")

match args:
    case ["provider", "profile", "list", "-o", "json"]:
        print(json.dumps({"profiles": [{"id": found} for found in load().get("profiles", [])]}))
    case ["provider", "profile", "lint" | "import" as step, "-f", profile]:
        if not Path(profile).is_file():
            print(f"error: no profile at {profile}", file=sys.stderr)
            sys.exit(1)
        if step == "import":
            state = load()
            state["profiles"] = [*state.get("profiles", []), "cloudflare-access"]
            save(state)
        note(f"provider profile {step} {Path(profile).name}")
    case [
        "provider", "create", "--name", name, "--type", kind,
        "--credential", one, "--credential", two,
    ]:  # fmt: skip
        register(name, [one, two], f"create {kind}")
    case ["provider", "update", name, "--credential", one, "--credential", two, "--wait"]:
        if name not in load()["providers"]:
            sys.exit(1)
        register(name, [one, two], "update")
    case ["provider", "list", "-o", "json"]:
        providers = [{"name": name, "type": "cloudflare-access"} for name in load()["providers"]]
        print(json.dumps({"providers": providers}))
    case ["sandbox", "get", name, "-o", "json"]:
        if behaviour == "get-fails":
            print("error: the gateway is not reachable", file=sys.stderr)
            sys.exit(1)
        found = load()["sandboxes"].get(name)
        if found is None:
            # As OpenShell 0.1.5 reports the gateway's NotFound (tonic 0.14's status).
            said = "status: 'Some requested entity was not found', self: \"sandbox not found\""
            print(f"\x1b[31mError:\x1b[0m   \u00d7 {said}", file=sys.stderr)
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
    case [
        "sandbox",
        "create",
        "--name",
        name,
        "--from",
        image,
        "--policy",
        policy,
        "--no-auto-providers",
        "--detach",
        "--",
        *command,
    ]:
        if behaviour == "create-fails":
            print("error: create failed", file=sys.stderr)
            sys.exit(1)
        state = load()
        state["sandboxes"][name] = {"id": uuid.uuid4().hex, "phase": "Ready", "image": image}
        save(state)
        note(f"sandbox create {name} {image} {' '.join(command)}")
    case ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--", *command]:
        note(f"exec {name} {' '.join(command)}")
        if "sandbox_science.orchestrator.confinement.runtime" in command:
            print("confined agent runtime ready", flush=True)
            while True:
                time.sleep(0.1)
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
