# SPDX-License-Identifier: MIT
"""Stand-ins for the parts' commands in the launcher's tests (``fake_parts`` in conftest).

Each command is a small Python script that runs this file with the command's name.
Every step goes in the file ``FAKE_JOURNAL`` names, one line each, and ``FAKE_PARTS``
(JSON: command name to behaviour) makes a part misbehave:

- ``exit-at-start``: it says why and exits 4 before it is ready.
- ``exit-later``: it is ready, then exits 5 two seconds later.
- ``ignore-term``: it ignores SIGTERM.
- ``grant-fails``: the worker's ``policy add`` fails.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
import tomllib
from pathlib import Path
from typing import TextIO

LOGIN_TOKEN = "fake-login-token-0123456789abcdef"

name, args = sys.argv[1], sys.argv[2:]
behaviour = json.loads(os.environ.get("FAKE_PARTS", "{}")).get(name, "")


def note(text: str) -> None:
    with Path(os.environ["FAKE_JOURNAL"]).open("a") as journal:
        journal.write(f"{name} {text}\n")


def option(flag: str) -> str:
    return args[args.index(flag) + 1]


def stopped(*_: object) -> None:
    note("stop")
    sys.exit(0)


def serve(address: str | None, lines: list[str], *, stream: TextIO = sys.stdout) -> None:
    if behaviour == "exit-at-start":
        print(f"{name}: cannot start, as asked", flush=True)
        note("exits")
        sys.exit(4)
    signal.signal(signal.SIGTERM, signal.SIG_IGN if behaviour == "ignore-term" else stopped)
    listener = None
    if address is not None:
        host, port = address.rsplit(":", 1)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, int(port)))
        listener.listen()
        listener.settimeout(0.1)
    note("start")
    for line in lines:
        print(line, file=stream, flush=True)
    started = time.monotonic()
    while True:
        if behaviour == "exit-later" and time.monotonic() - started > 2:
            note("exits")
            sys.exit(5)
        if listener is None:
            time.sleep(0.1)
            continue
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            continue
        connection.close()


match (name, args[:2]):
    case ("sandbox-science-worker", ["policy", "add"]):
        projects = [args[i + 1] for i, arg in enumerate(args) if arg == "--project"]
        note(f"policy add {' '.join(projects)}")
        if behaviour == "grant-fails":
            print("error: the policy store is not there", flush=True)
            sys.exit(2)
        print(f"stored {args[-1]} as sha256:{'ab' * 32}", flush=True)
    case ("sandbox-science-worker", ["serve", *_]):
        settings = tomllib.loads(Path(option("--settings")).read_text())
        serve(settings["listen"], ["worker API on " + settings["listen"]])
    case ("sandbox-science-harness", ["project" | "session" as what, action]):
        session = f" {option('--session')}" if what == "session" else ""
        note(f"{what} {action} {option('--project')}{session}")
    case ("sandbox-science-harness", ["serve", *_]):
        serve(None, [f"serving the sessions of {option('--state-dir')}"], stream=sys.stderr)
    case ("sandbox-science-workspace", ["serve", *_]):
        listen = option("--listen")
        serve(
            listen,
            [
                f"PDF viewer origin on http://{option('--preview-listen')}/",
                f"Sandbox Science workspace on http://{listen}/",
                "Log in by opening this URL; it works until the server stops:",
                f"    http://{listen}/login?token={LOGIN_TOKEN}",
            ],
        )
    case _:
        print(f"{name}: unexpected arguments {args}", file=sys.stderr)
        sys.exit(64)
