# SPDX-License-Identifier: MIT
"""A stand-in for the OpenShell CLI in the launcher's tests (``fake_openshell`` in conftest).

Its providers and sandboxes are a JSON file, the one ``FAKE_OPENSHELL`` names, and what
it does goes in ``FAKE_JOURNAL`` as the parts' stand-ins' steps do. With ``FAKE_PARTS``
saying ``{"openshell": "forward-exits-once"}``, the first forward exits a moment after
it starts, as one does when its sandbox stops; with ``forward-exits-always``, every one
does; with ``create-fails``, ``sandbox create`` fails and makes nothing; with
``get-fails``, ``sandbox get`` cannot reach the gateway; with ``provider-fails``,
``provider create`` and ``update`` fail; with ``old-version``, ``--version`` names
OpenShell 0.1.3.

``provider create`` and ``update`` read their credentials from the environment, as
``--credential KEY`` does, and say the values on stdout and stderr, as a careless CLI
could: the caller must not show them. With ``FAKE_OPENSHELL_CALLS``, each call's
arguments, environment and core-file limit go there, a JSON line each.

The runtime manager's tests drive it through the state file as well: its
``behaviour`` key (comma-separated) adds to ``FAKE_PARTS``'s, so a test can change it
between commands. ``create-provisioning`` leaves a new sandbox Provisioning;
``create-pending`` makes create fail after making the sandbox; ``get-gateway-not-found``
fails ``sandbox get`` with a gateway error whose text says "not found" (not absence);
``delete-ignored`` answers ``sandbox delete`` without deleting; ``exec-fails`` ends the
runtime's exec before it is ready and ``exec-exits`` soon after; ``exec-floods`` has it
write a line longer than 64 KiB and then far more than a pipe holds, then ``flood done``
and a ``flooded`` file beside the state file; ``exec-ignores-term`` has it ignore
SIGTERM (noting so), so only SIGKILL ends it. ``gateway-refuses`` fails every
``sandbox`` and ``provider`` command as a CLI whose gateway refuses connections does;
``create-silent`` fails ``sandbox create`` without a word on stderr; and
``create-rolls-back`` makes the sandbox Provisioning with a ``message``, answers one
``sandbox get`` with it and then forgets it, as a gateway that rolled it back. ``sandbox create``
keeps its ``--driver-config-json``, its ``--label`` pairs, and its ``--provider`` names and
policy (refusing a provider the state lacks); with the state's ``record`` naming a file, it notes
that file's content as it was when create ran. ``sandbox upload`` copies each file to
``uploads/<sandbox>/`` beside the state file.

The runtime's exec writes ``runtime-<pid>.live`` (its sandbox's name) beside the state
file while it runs, and ends when its sandbox is deleted or on SIGTERM, as the gateway
ends an exec whose stream closes. With the state's ``runtime`` naming a Python, it runs
the real runtime instead, from the uploaded settings with ``/sandbox/runtime`` mapped to
the upload directory: in place of this process, or with ``exec-orphans`` in a session of
its own that outlives this process, as an abrupt gateway loss can leave it (KRB-11).
``sandbox delete`` kills the sandbox's live runtimes.

The registration helper refuses to run with ``FAKE_*`` set, so the wrappers that run
this stand-in set what it needs themselves (``fake_openshell`` in conftest).
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION_OUTPUT

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_OPENSHELL"])
behaviour = json.loads(os.environ.get("FAKE_PARTS", "{}")).get("openshell", "")
RUNTIME = "sandbox_science.orchestrator.confinement.runtime"
if args == ["--version"]:  # not a call: no journal, no state; "old-version" is another release
    print("openshell 0.1.3" if "old-version" in behaviour else OPENSHELL_VERSION_OUTPUT)
    sys.exit(0)


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
    if "provider-fails" in behaviours() or not all(os.environ.get(key) for key in keys):
        sys.exit(1)
    state = load()
    if name not in state["providers"]:
        state["providers"].append(name)
    save(state)
    note(f"provider {what} {name} {' '.join(keys)}")


def save(state: dict[str, Any]) -> None:
    # In one step: the manager's exec watches the state while other commands change it.
    temporary = state_file.with_name(f".{state_file.name}.{os.getpid()}")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_file)


def behaviours() -> set[str]:
    return {*behaviour.split(","), *str(load().get("behaviour", "")).split(",")} - {""}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie child of a test process counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][0] != "Z"
    except OSError:
        return True


def live(name: str | None = None) -> list[tuple[Path, int]]:
    """The live runtime markers, of one sandbox or of all."""
    found = []
    for marker in state_file.parent.glob("runtime-*.live"):
        pid = int(marker.name.split("-")[1].split(".")[0])
        try:
            owner = marker.read_text()
        except FileNotFoundError:
            continue
        if (name is None or owner == name) and alive(pid):
            found.append((marker, pid))
    return found


def uploads(name: str) -> Path:
    return state_file.parent / "uploads" / name


def runtime(name: str) -> None:
    """The runtime's exec: the stand-in's, or the real runtime from the uploaded settings."""
    sandbox = load()["sandboxes"].get(name)
    if sandbox is None:
        print('Error: status: NotFound, self: "sandbox not found"', file=sys.stderr)
        sys.exit(1)
    if "exec-fails" in behaviours():
        print("error: exec failed", file=sys.stderr)
        sys.exit(1)
    python = load().get("runtime")
    if python is not None:
        real(name, python)
    marker = state_file.parent / f"runtime-{os.getpid()}.live"

    def stopped(*_: object) -> None:
        if "exec-ignores-term" in behaviours():
            note(f"exec {name} ignores SIGTERM")
            return
        marker.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stopped)
    marker.write_text(name)
    print("confined agent runtime ready", flush=True)
    if "exec-floods" in behaviours():
        print("x" * 70000, flush=True)
        for number in range(2000):
            print(f"line {number} " + "y" * 200, flush=True)
        print("flood done", flush=True)
        (state_file.parent / "flooded").touch()
    started = time.monotonic()
    while True:
        time.sleep(0.05)
        if name not in load()["sandboxes"]:
            marker.unlink(missing_ok=True)
            sys.exit(1)
        if "exec-exits" in behaviours() and time.monotonic() - started > 0.3:
            marker.unlink(missing_ok=True)
            note(f"exec {name} exits")
            sys.exit(1)


def real(name: str, python: str) -> None:
    directory = uploads(name)
    settings = json.loads((directory / "runtime.json").read_text())
    for key, value in list(settings.items()):
        if isinstance(value, str) and value.startswith("/sandbox/runtime/"):
            settings[key] = str(directory / value.removeprefix("/sandbox/runtime/"))
    settings["scratch"] = str(directory / "scratch")
    settings["image_record"] = str(state_file.parent / "image.json")
    local = directory / "local-runtime.json"
    local.write_text(json.dumps(settings))
    command = [python, "-m", RUNTIME, "--settings", str(local)]
    if "exec-orphans" not in behaviours():
        (state_file.parent / f"runtime-{os.getpid()}.live").write_text(name)
        os.execv(python, command)  # noqa: S606
    log = directory / f"runtime-{time.monotonic_ns()}.log"
    with log.open("w") as output:
        child = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
    (state_file.parent / f"runtime-{child.pid}.live").write_text(name)
    note(f"exec {name} orphan {child.pid}")
    with log.open() as output:
        while True:
            line = output.readline()
            if line:
                print(line.rstrip(), flush=True)
            elif child.poll() is not None:
                sys.exit(child.returncode)
            else:
                time.sleep(0.05)


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
        once = "forward-exits-once" in behaviours() and not marker.exists()
        if (once or "forward-exits-always" in behaviours()) and time.monotonic() - started > 0.5:
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

if "gateway-refuses" in behaviours() and args[:1] in (["sandbox"], ["provider"]):
    print(
        "\x1b[31mError:\x1b[0m   \u00d7 transport error\n"
        "  \u2570\u2500\u25b6 tcp connect error: Connection refused (os error 111)",
        file=sys.stderr,
    )
    sys.exit(1)

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
        if "get-fails" in behaviours():
            print("error: the gateway is not reachable", file=sys.stderr)
            sys.exit(1)
        if "get-gateway-not-found" in behaviours():
            said = "status: 'Unavailable', self: \"tcp connect error: gateway endpoint not found\""
            print(f"\x1b[31mError:\x1b[0m   \u00d7 {said}", file=sys.stderr)
            sys.exit(1)
        state = load()
        found = state["sandboxes"].get(name)
        if found is not None and found.pop("rolls_back", False):
            del state["sandboxes"][name]  # this answer is the last before the rollback
            save(state)
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
        if "create-fails" in behaviours():
            print(f"error: sandbox {name} already exists", file=sys.stderr)
            sys.exit(1)
        state = load()
        state["sandboxes"][name] = {
            "id": uuid.uuid4().hex,
            "phase": "Ready",
            "image": image,
            "providers": [provider],
            "policy": Path(policy).read_text(),
        }
        save(state)
        note(f"sandbox create {name} {image} provider {provider} {' '.join(command)}")
    case ["sandbox", "create", "--name", name, "--from", image, "--policy", policy, *rest] if (
        rest and rest[0] in ("--no-auto-providers", "--driver-config-json", "--provider", "--label")
    ):
        placement = rest[:2] if rest[:1] == ["--driver-config-json"] else []
        tail = rest[len(placement) :]
        attached: list[str] = []
        while tail[:1] == ["--provider"] and len(tail) > 1:
            attached.append(tail[1])
            tail = tail[2:]
        labels: dict[str, str] = {}
        while tail[:1] == ["--label"] and len(tail) > 1:
            key, _, value = tail[1].partition("=")
            labels[key] = value
            tail = tail[2:]
        if tail[:3] != ["--no-auto-providers", "--detach", "--"]:
            print(f"openshell: unexpected arguments {args}", file=sys.stderr)
            sys.exit(64)
        command = tail[3:]
        if "create-fails" in behaviours():
            print("error: create failed", file=sys.stderr)
            sys.exit(1)
        if "create-silent" in behaviours():
            sys.exit(1)
        state = load()
        if record := state.get("record"):
            seen = Path(record).read_text() if Path(record).exists() else "none"
            note(f"record at create {seen.strip()}")
        phase = "Provisioning" if "create-provisioning" in behaviours() else "Ready"
        made: dict[str, Any] = {"id": uuid.uuid4().hex, "phase": phase, "image": image}
        if "create-rolls-back" in behaviours():
            made |= {
                "phase": "Provisioning",
                "message": "sandbox did not become ready within the bootstrap grace",
                "rolls_back": True,
            }
        if placement:
            made["driver_config"] = placement[-1]
            made["policy"] = Path(policy).read_text()
        if labels:
            made["labels"] = labels
        if attached:
            missing = [provider for provider in attached if provider not in state["providers"]]
            if missing:
                print(f"error: provider '{missing[0]}' not found", file=sys.stderr)
                sys.exit(1)
            made["providers"] = attached
            made["policy"] = Path(policy).read_text()
        state["sandboxes"][name] = made
        save(state)
        with_providers = "".join(f" provider {provider}" for provider in attached)
        note(f"sandbox create {name} {image}{with_providers} {' '.join(command)}")
        if "create-pending" in behaviours():
            print("error: timed out waiting for the gateway", file=sys.stderr)
            sys.exit(1)
    case ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--", *command]:
        note(f"exec {name} {' '.join(command)}")
        if RUNTIME in command:
            runtime(name)
        if command[:2] == ["chmod", "600"]:
            for remote in command[2:]:
                copied = uploads(name) / Path(remote).name
                if copied.exists():
                    copied.chmod(0o600)
    case ["sandbox", "upload", "--no-git-ignore", name, local, remote]:
        if not Path(local).is_file():
            sys.exit(2)
        uploads(name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, uploads(name) / Path(remote).name)
        note(f"upload {name} {Path(local).name} {remote}")
    case ["sandbox", "delete", name]:
        state = load()
        if "delete-ignored" not in behaviours():
            state["sandboxes"].pop(name, None)
            for marker, pid in live(name):
                os.kill(pid, signal.SIGKILL)
                marker.unlink(missing_ok=True)
        save(state)
        note(f"sandbox delete {name}")
    case ["forward", "service", "--target-port", "8080", "--local", local, _]:
        forward(local)
    case _:
        print(f"openshell: unexpected arguments {args}", file=sys.stderr)
        sys.exit(64)
