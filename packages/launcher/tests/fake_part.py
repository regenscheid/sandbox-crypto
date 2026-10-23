# SPDX-License-Identifier: MIT
"""Stand-ins for the parts' commands in the launcher's tests (``fake_parts`` in conftest).

Each command is a small Python script that runs this file with the command's name.
Every step goes in the file ``FAKE_JOURNAL`` names, one line each, and ``FAKE_PARTS``
(JSON: command name to behaviour) makes a part misbehave:

- ``exit-at-start``: it says why and exits 4 before it is ready.
- ``exit-later``: it is ready, then exits 5 two seconds later.
- ``ignore-term``: it ignores SIGTERM.
- ``grant-fails``: the worker's ``policy add`` fails.
- ``child-in-own-group``: it starts a child in a process group of its own.
- ``child-stays``: it starts a child in its own session, which outlives it.
- ``suspend-busy``: the worker suspends nothing, as for sandboxes in use.
- ``suspend-slow``: the worker takes 20 s to answer a suspension.
- ``old-protocol``: the worker speaks protocol 5.

The harness's ``skills`` commands answer as for a control store with no import and no
pin: ``import`` and ``pin`` answer an import at the commit ``c`` * 40 with one skill.

The worker answers ``GET /v1/environments`` and ``POST /v1/environments/{id}/suspend``
for its clients' projects, from the environments in the OpenShell stand-in's file
(``FAKE_OPENSHELL``); a suspension stops the sandbox there too. As the worker does, it
answers from the sandbox's phase: one recorded stopped whose sandbox is still Ready is
stopped again.

The run service and the workspace note ``LEAK_CHECK`` and ``PASSED_ON`` when they see
them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

from sandbox_science.contracts.worker import PROTOCOL_VERSION

LOGIN_TOKEN = "fake-login-token-0123456789abcdef"

name, args = sys.argv[1], sys.argv[2:]
behaviour = json.loads(os.environ.get("FAKE_PARTS", "{}")).get(name, "")


class NoDNSHTTPServer(ThreadingHTTPServer):
    """A local test server must not wait for a runner's reverse DNS lookup."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def note(text: str) -> None:
    with Path(os.environ["FAKE_JOURNAL"]).open("a") as journal:
        journal.write(f"{name} {text}\n")


def option(flag: str) -> str:
    return args[args.index(flag) + 1]


def stopped(*_: object) -> None:
    # A Darwin watcher and its supervisor can deliver TERM during the same
    # shutdown. A second signal must not re-enter the journal write.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    note("stop")
    sys.exit(0)


def openshell_state() -> dict[str, Any]:
    path = Path(os.environ.get("FAKE_OPENSHELL", "/nonexistent"))
    if not path.exists():
        return {"providers": [], "sandboxes": {}, "environments": []}
    return json.loads(path.read_text())


def worker_api(settings: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    clients = {client["token_sha256"]: client for client in settings.get("clients", [])}
    lock = threading.Lock()

    class Api(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def client(self) -> dict[str, Any] | None:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            return clients.get(hashlib.sha256(token.encode()).hexdigest())

        def answer(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header(
                "X-Worker-Protocol", "5" if behaviour == "old-protocol" else str(PROTOCOL_VERSION)
            )
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def refuse(self, status: int, kind: str) -> None:
            self.answer(status, {"error": {"kind": kind, "message": kind}})

        def do_GET(self) -> None:
            client = self.client()
            if client is None:
                return self.refuse(401, "unauthorized")
            if self.path != "/v1/environments":
                return self.refuse(404, "not_found")
            with lock:
                found = openshell_state().get("environments", [])
            mine = [item for item in found if item["project_id"] in client["projects"]]
            return self.answer(200, {"environments": mine})

        def do_POST(self) -> None:
            client = self.client()
            if client is None:
                return self.refuse(401, "unauthorized")
            match = re.fullmatch(r"/v1/environments/([^/]+)/suspend", self.path)
            if match is None:
                return self.refuse(404, "not_found")
            if behaviour == "suspend-slow":
                time.sleep(20)
            with lock:
                state = openshell_state()
                environment = next(
                    (
                        item
                        for item in state.get("environments", [])
                        if item["environment_id"] == match[1]
                        and item["project_id"] in client["projects"]
                    ),
                    None,
                )
                if environment is None:
                    return self.refuse(404, "not_found")
                sandbox = state["sandboxes"][environment["sandbox"]]
                suspended = sandbox["phase"] == "Stopped"
                if not suspended and behaviour != "suspend-busy":
                    environment["state"] = "stopped"
                    sandbox["phase"] = "Stopped"
                    Path(os.environ["FAKE_OPENSHELL"]).write_text(json.dumps(state))
                    note(f"suspend {environment['environment_id']}")
                    suspended = True
            return self.answer(200, {"environment": environment, "suspended": suspended})

    return Api


class ControlHealth(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        data = b'{"ready": true, "runtime_connected": false}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(
    address: str | None,
    lines: list[str],
    *,
    stream: TextIO = sys.stdout,
    api: type[BaseHTTPRequestHandler] | None = None,
) -> None:
    if behaviour == "exit-at-start":
        print(f"{name}: cannot start, as asked", flush=True)
        note("exits")
        sys.exit(4)
    signal.signal(signal.SIGTERM, signal.SIG_IGN if behaviour == "ignore-term" else stopped)
    listener = None
    if address is not None and api is not None:
        host, port = address.rsplit(":", 1)
        server = NoDNSHTTPServer((host, int(port)), api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    elif address is not None:
        host, port = address.rsplit(":", 1)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, int(port)))
        listener.listen()
        listener.settimeout(0.1)
    note("start")
    if behaviour in ("child-in-own-group", "child-stays"):
        group = 0 if behaviour == "child-in-own-group" else None
        child = subprocess.Popen(["sleep", "300"], process_group=group)
        note(f"child {child.pid}")
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
        serve(settings["listen"], ["worker API on " + settings["listen"]], api=worker_api(settings))
    case ("sandbox-science-harness", ["project", "show"]):
        # The projects made so far, beside the journal: a project not made is not there.
        note(f"project show {option('--project')}")
        made = Path(os.environ["FAKE_JOURNAL"] + ".projects")
        known = json.loads(made.read_text()) if made.exists() else {}
        if option("--project") not in known:
            print(f"error: no project {option('--project')!r}", flush=True)
            sys.exit(1)
        print(
            json.dumps({"project_id": option("--project"), "profile": known[option("--project")]})
        )
    case ("sandbox-science-harness", ["project" | "session" as what, action]):
        session = f" {option('--session')}" if what == "session" else ""
        note(f"{what} {action} {option('--project')}{session}")
        if (what, action) == ("project", "create"):
            made = Path(os.environ["FAKE_JOURNAL"] + ".projects")
            known = json.loads(made.read_text()) if made.exists() else {}
            made.write_text(json.dumps({**known, option("--project"): option("--profile")}))
    case ("sandbox-science-harness", ["skills", action]):
        named = [option(flag) for flag in ("--project", "--source", "--name") if flag in args]
        note(f"skills {action} {' '.join(named)}")
        imported = {"kind": "git", "ref": None, "commit": "c" * 40, "report": {"skills": ["a"]}}
        if action == "list":
            print("[]")
        elif action == "import":
            if "--embedding-model" in args:
                note(f"skills embed with {option('--embedding-model')}")
            print("warning: a path differs from another only by case", flush=True)
            print(json.dumps({**imported, "unchanged": False}, indent=2))
        else:
            print(json.dumps({"source": option("--source"), "pinned": imported, "changes": None}))
    case ("sandbox-science-control", ["--settings", *_]):
        for seen in ("LEAK_CHECK", "PASSED_ON"):
            if seen in os.environ:
                note(f"saw {seen}")
        settings = json.loads(Path(option("--settings")).read_text())
        serve(settings["listen"], ["control capability service ready"], api=ControlHealth)
    case ("sandbox-science-harness", ["serve", *_]):
        for seen in ("LEAK_CHECK", "PASSED_ON"):
            if seen in os.environ:
                note(f"saw {seen}")
        if "--skills-embedding-model" in args:
            note(f"embeds with {option('--skills-embedding-model')}")
        if "--mcp-proxy" in args:
            servers = [args[i + 1] for i, arg in enumerate(args) if arg == "--mcp-server"]
            note(f"has MCP {option('--mcp-proxy')} for {option('--mcp-project')}: {servers}")
        serve(None, [f"serving the sessions of {option('--state-dir')}"], stream=sys.stderr)
    case ("sandbox-science-mcp-proxy", ["compile-policy", *_]):
        Path(option("--out")).write_text("# compiled\n")
        note("compile-policy" + (" --admit-escalated" if "--admit-escalated" in args else ""))
    case ("sandbox-science-workspace", ["serve", *_]):
        for seen in ("LEAK_CHECK", "PASSED_ON"):
            if seen in os.environ:
                note(f"saw {seen}")
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
