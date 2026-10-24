# SPDX-License-Identifier: MIT
"""Check a profile image from the inside (docs/IMAGES.md, "Tests").

images/test.sh runs this in the image as uid 1000, with a read-only root filesystem and only the
declared paths writable.

In the research profiles (minimal, crypto-base) it starts the worker's executor the way the
OpenShell backend does (``python -I /sandbox/.sandbox-science/executor.py --stdio --root
/sandbox/project``), asks it for its languages, and runs cells and shell commands through it.
Kernels therefore start through jupyter_client with the executor's environment.

In the orchestrator profile it runs the harness's command by its full path, without the image's
environment variables, as an OpenShell sandbox would: its help, a project record in the
workdir, and a scripted task against a stub of the worker API on loopback.

In the mcp-proxy profile it starts the image's launcher the same way, as the proxy's sandbox
runs its command: it waits for the settings, starts the proxy, which then answers on loopback
and keeps its stores in the workdir, and starts it again when it dies.

It prints one JSON object per check (``check``, ``ok``, ``seconds``, ``detail``) and exits 1 if
any failed. It needs only the standard library.

Usage: check_image.py --profile minimal|crypto-base|orchestrator|mcp-proxy --worker-files DIR
       [--network] [--landlock]
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fnmatch
import hashlib
import http.client
import http.server
import importlib.util
import itertools
import json
import os
import pwd
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROFILES = ("minimal", "crypto-base", "orchestrator", "mcp-proxy")
SERVICES = ("orchestrator", "mcp-proxy")
"""The platform services' images: no executor or kernels, and a read-only prefix."""
PREFIX = "/opt/sandbox-science"
EXECUTOR_DIR = Path("/sandbox/.sandbox-science")
WORKER_FILES = (
    "executor.py",
    "executor_lifecycle.py",
    "executor_admission.py",
    "binding_inventory.py",
    "provisioning.py",
    "pdfhelper.py",
    "daemon.py",
    "request_frames.py",
    "capture.py",
    "sandbox_files.py",
    "sandbox_processes.py",
    "sandbox_setup.py",
    "workspace_prefix.py",
)
"""The worker's files that run inside a sandbox (packages/worker/src/sandbox_science/worker)."""
PROJECT = Path("/sandbox/project")
SESSION = "image-check"
WORKSPACE = PROJECT / "sessions" / SESSION
LANGUAGES = {"minimal": ["python"], "crypto-base": ["python", "sage"]}
FEATURES = {"minimal": ["jobs"], "crypto-base": ["jobs", "pdf"]}
"""What the executor offers beyond kernels: ``jobs``, background shell jobs, everywhere, and
``pdf`` where the PDF helper finds pdf_oxide."""
MARKER = "Sandbox Science image check"

HARNESS = f"{PREFIX}/bin/sandbox-science-harness"
HARNESS_PACKAGES = (
    "sandbox-science-contracts",
    "sandbox-science-policy",
    "sandbox-science-orchestrator",
)
HARNESS_DISTS = (*HARNESS_PACKAGES, "aiohttp", "pydantic", "pydantic-core")
HARNESS_MODULES = (
    "sandbox_science.orchestrator.cli",
    "sandbox_science.contracts",
    "aiohttp",
    "pydantic",
    "pydantic_core",
)
HARNESS_DIR = Path("/sandbox/harness")
"""Where the orchestrator checks keep the harness's files: in the workdir, as a deployment would."""
SERVICE_ENV = {"HOME": "/sandbox", "PATH": "/usr/local/bin:/usr/bin:/bin"}
"""The services' environment in the checks: a home and a system ``PATH``, not the image's
variables, which OpenShell's VM driver does not apply."""
KERNEL_PACKAGES = ("ipykernel", "jupyter_client", "zmq")
TEST_TOKEN = "test-secret-image-check"  # noqa: S105 - synthetic, for the stub worker and the proxy

PROXY = f"{PREFIX}/bin/sandbox-science-mcp-proxy"
EVENTS = f"{PREFIX}/bin/sandbox-science-events"
LAUNCHER = f"{PREFIX}/bin/mcp-proxy-run"
PROXY_PACKAGES = (
    "sandbox-science-contracts",
    "sandbox-science-policy",
    "sandbox-science-event-store",
    "sandbox-science-mcp-proxy",
)
PROXY_DISTS = (*PROXY_PACKAGES, "aiohttp", "pydantic", "pydantic-core", "pyyaml")
PROXY_MODULES = (
    "sandbox_science.mcp_proxy.cli",
    "sandbox_science.policy",
    "sandbox_science.event_store",
    "sandbox_science.contracts",
    "aiohttp",
    "pydantic",
    "pydantic_core",
    "yaml",
)
SERVICE_PACKAGES = {"orchestrator": HARNESS_PACKAGES, "mcp-proxy": PROXY_PACKAGES}
"""The repository's packages each service image installs from wheels."""
PACKAGE_LICENSES = {
    "sandbox-science-contracts": "MIT",
    "sandbox-science-orchestrator": "MIT AND Apache-2.0",
    "sandbox-science-policy": "MIT",
    "sandbox-science-event-store": "MIT",
    "sandbox-science-mcp-proxy": "MIT",
}
"""Each of those packages' license, as its ``pyproject.toml`` declares it (a recipe test keeps
them in step): the orchestrator's holds Apache-2.0 code since b426d7df."""
PROXY_BINARIES = f"{PREFIX}/bin/python*"
"""The targets file's ``mcp_proxy.binaries``: OpenShell sees the interpreter (lab 13)."""
PROXY_DIR = Path("/sandbox/mcp-proxy")
"""The proxy's directory in the workdir: its settings, where the launcher waits for them, and
its stores (``state``)."""
PROXY_SETTINGS = PROXY_DIR / "proxy.toml"
PROXY_ADDRESS = ("127.0.0.1", 8080)
PROXY_SERVER = "papers"
PROXY_PLACEHOLDER = ("PAPERS_TOKEN", "openshell:resolve:env:v1_PAPERS_TOKEN")
"""A credential variable as OpenShell sets it in the sandbox: a placeholder, never a secret."""
PROXY_CEILING = "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
PROXY_POLICY = f"""\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: image-check
mcp:
  servers:
    {PROXY_SERVER}:
      url: https://mcp.example.invalid/mcp
      revision: "2025-11-25"
      auth: {{mode: none}}
      tools: {{allow: [search]}}
"""
"""A policy with one server, which the checks never reach: no call gets past the session check."""
PROXY_TOML = f"""\
listen = "{PROXY_ADDRESS[0]}:{PROXY_ADDRESS[1]}"
state_dir = "state"
ceiling = "pool-ceiling.yaml"

[[callers]]
name = "image-check"
token_sha256 = "{hashlib.sha256(TEST_TOKEN.encode()).hexdigest()}"
kind = "orchestrator"
project = "{SESSION}"
"""
"""The proxy's settings, as an operator uploads them: one caller, with the token's hash."""
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

Message = dict[str, Any]


class CheckError(Exception):
    """A check's expectation did not hold."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


PROTOCOL_PROBE = (
    "from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION; "
    "print(PROTOCOL_HEADER, PROTOCOL_VERSION)"
)
"""Prints the header and version of the worker protocol the image's harness speaks."""


def stub_worker(protocol: tuple[str, int], requests: list[str]) -> http.server.ThreadingHTTPServer:
    """A stub of the worker API on loopback, serving in a thread: ``GET /v1/capabilities``
    with ``TEST_TOKEN`` and nothing else, every response carrying ``protocol``, the worker
    protocol's header and version, as the harness refuses a worker without them. Each
    request's path goes into ``requests``."""
    header, version = protocol
    capabilities = json.dumps(
        {
            "protocol": version,
            "languages": ["python"],
            "location": {"pool_id": SESSION, "backend": "local", "hostname": "stub-worker"},
        }
    ).encode()

    class StubWorker(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            authorized = self.headers.get("Authorization") == f"Bearer {TEST_TOKEN}"
            found = urlsplit(self.path).path == "/v1/capabilities"
            status = 200 if authorized and found else 404 if authorized else 401
            body = capabilities if status == 200 else b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header(header, str(version))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Keep this script's output to its JSON lines."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubWorker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def harness_run_args(worker: str, directory: Path, transcript: Path) -> list[str]:
    """``sandbox-science-harness run``'s arguments for a scripted task against ``worker``,
    with its token and script written in ``directory``."""
    token, script = directory / "worker.token", directory / "script.json"
    token.write_text(TEST_TOKEN + "\n")
    script.write_text(json.dumps([{"text": MARKER}]))
    return [
        *("--worker", worker, "--token-file", str(token)),
        *("--project", SESSION, "--session", SESSION, "--environment", SESSION),
        *("--script", str(script), "--allow-unisolated"),
        *("--transcript", str(transcript)),
        "Answer with the marker.",
    ]


class Executor:
    """The worker's executor over stdin and stdout, one request at a time."""

    def __init__(self, script: Path, root: Path, log: Path) -> None:
        self.log = log
        self.process = subprocess.Popen(  # noqa: S603 - as the OpenShell backend starts it
            [sys.executable, "-I", str(script), "--stdio", "--root", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log.open("wb"),
        )
        self.messages: queue.Queue[Message] = queue.Queue()
        self.ids = itertools.count(1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout or ():
            self.messages.put(json.loads(line))
        self.messages.put({"type": "closed"})

    def _wait(self, wanted: Callable[[Message], bool], timeout: float) -> Message:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(f"no answer from the executor within {timeout:g} s")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if message.get("type") == "closed":
                raise CheckError(f"the executor exited: {self.stderr_tail()}")
            if wanted(message):
                return message

    def request(self, op: str, wait: float = 30, **fields: Any) -> Message:
        """Send ``op`` with ``fields``, as the executor reads them, and wait ``wait`` seconds
        at most for its reply; a ``timeout`` among the fields is the execution's own."""
        ident = next(self.ids)
        stdin = self.process.stdin
        if stdin is None:
            raise CheckError("the executor has no stdin")
        stdin.write((json.dumps({"id": ident, "op": op, **fields}) + "\n").encode())
        stdin.flush()
        reply = self._wait(lambda m: m.get("type") == "reply" and m.get("id") == ident, wait)
        expect(bool(reply.get("ok")), f"{op} refused: {reply.get('error')}")
        return reply

    def open(self) -> Message:
        """Its ``hello``, once package admission is open: the executor starts with it held
        and runs nothing until the worker opens it, as the worker's first sync of an
        environment does (generation 0, ``ready``)."""
        reply = self.request("hello", wait=60)
        self.request(
            "package_gate",
            incarnation=reply.get("package_gate"),
            generation=0,
            mode="ready",
            operation="",
            revision=None,
        )
        return reply

    def run(self, op: str, limit: float, **fields: Any) -> Message:
        """Run a shell command or cell; return its result after checking that it succeeded."""
        execution = f"check-{next(self.ids)}"
        self.request(op, execution=execution, session=SESSION, timeout=limit, **fields)
        end = self._wait(
            lambda m: m.get("execution") == execution and m.get("event") == "end", limit + 150
        )
        result: Message = end.get("result", {})
        if end.get("state") != "succeeded":
            error = result.get("error") or {}
            output = result.get("stdout", {}).get("text", "") + result.get("stderr", {}).get(
                "text", ""
            )
            traceback = "\n".join(error.get("traceback", [])[-5:])
            state, message = end.get("state"), error.get("message")
            raise CheckError(f"{op} ended {state}: {message}\n{traceback}\n{output[-1500:]}")
        return result

    def shell(self, script: str, limit: float = 120) -> str:
        result = self.run("shell", limit, script=script)
        return str(result.get("stdout", {}).get("text", ""))

    def pdf(self, operation: str, path: str, **fields: Any) -> Message:
        """A PDF operation, as the worker sends a ``PdfRequest``; the helper's JSON."""
        result = self.run("pdf", 120, operation=operation, path=path, **fields)
        return json.loads(str(result.get("stdout", {}).get("text", "")))

    def cell(self, code: str, language: str = "python", limit: float = 120) -> tuple[str, str]:
        """Run a cell; return its standard output and the text of its result, if any."""
        result = self.run("cell", limit, code=code, language=language)
        texts = [
            str(output.get("data", {}).get("text/plain", ""))
            for output in result.get("outputs", [])
            if output.get("type") == "result"
        ]
        return str(result.get("stdout", {}).get("text", "")), "\n".join(texts)

    def stderr_tail(self) -> str:
        try:
            return self.log.read_text(errors="replace")[-1500:]
        except OSError:
            return ""

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


class Launcher:
    """The mcp-proxy image's command, started as the proxy's sandbox starts it: by its full path,
    without the image's environment, with a credential placeholder as OpenShell sets one."""

    def __init__(self, log: Path) -> None:
        self.log = log
        with log.open("wb") as output:
            self.process = subprocess.Popen(  # noqa: S603 - the image's own command
                [LAUNCHER],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**SERVICE_ENV, PROXY_PLACEHOLDER[0]: PROXY_PLACEHOLDER[1]},
            )

    def text(self) -> str:
        """What the launcher and the proxy have logged."""
        return self.log.read_text(errors="replace")

    def proxies(self) -> list[int]:
        """The proxy processes the launcher started, oldest first, as its log names them."""
        return [int(pid) for pid in re.findall(r"started the proxy \(pid (\d+)\)", self.text())]

    def expect_running(self) -> None:
        code = self.process.poll()
        expect(code is None, f"the launcher exited with {code}: {self.text()[-1500:]}")

    def wait_for(self, what: str, found: Callable[[], bool], timeout: float = 60) -> None:
        """Wait until ``found()``, while the launcher runs."""
        deadline = time.monotonic() + timeout
        while not found():
            self.expect_running()
            if time.monotonic() > deadline:
                raise CheckError(f"no {what} within {timeout:g} s: {self.text()[-1500:]}")
            time.sleep(0.1)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()


def proxy_post(token: str | None, message: Message) -> tuple[int, Message]:
    """POST a JSON-RPC message to the proxy's endpoint for the checks' server, as a caller does;
    return the status and the JSON answer. Raises OSError while nothing listens."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(*PROXY_ADDRESS, timeout=30)
    try:
        connection.request("POST", f"/mcp/{PROXY_SERVER}", json.dumps(message), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def refusal(answer: Message) -> object:
    """The reason code of the proxy's refusal (``error.data.reason``)."""
    return answer.get("error", {}).get("data", {}).get("reason")


def proxy_listening() -> bool:
    try:
        socket.create_connection(PROXY_ADDRESS, timeout=5).close()
    except ConnectionRefusedError:
        return False
    return True


class Checks:
    def __init__(self, profile: str, worker_files: Path, network: bool, landlock: bool) -> None:
        self.profile = profile
        self.network = network
        self.under_landlock = landlock
        self.worker_files = worker_files
        self.client: Executor | None = None
        self.proxy_launcher: Launcher | None = None
        self.failed = 0

    @property
    def crypto(self) -> bool:
        return self.profile == "crypto-base"

    @property
    def orchestrator(self) -> bool:
        return self.profile == "orchestrator"

    @property
    def service(self) -> bool:
        return self.profile in SERVICES

    def check(self, name: str, function: Callable[[], str]) -> None:
        started = time.monotonic()
        try:
            detail, ok = function(), True
        except Exception as error:  # every check reports, whatever went wrong
            detail, ok = f"{type(error).__name__}: {error}", False
            self.failed += 1
        line = {"check": name, "ok": ok, "seconds": round(time.monotonic() - started, 2)}
        print(json.dumps({**line, "detail": detail}), flush=True)

    # The sandbox as the executor sees it.

    def scheduler_shim(self) -> str:
        revision = hashlib.sha256((self.worker_files / "sched_self.c").read_bytes()).hexdigest()
        path = (
            Path("/usr/local/lib/sandbox-science/shim") / revision / "libsandbox-science-sched.so"
        )
        shim = ctypes.CDLL(str(path))
        source = shim.sandbox_science_sched_source_sha256
        source.restype = ctypes.c_char_p
        expect(source() == revision.encode(), "scheduler shim source revision differs")
        libc = ctypes.CDLL(None)
        libc.pthread_self.restype = ctypes.c_ulong
        thread = libc.pthread_self()
        policy, priority = ctypes.c_int(), ctypes.c_int()
        libc.pthread_getschedparam.argtypes = (
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        )
        expect(
            libc.pthread_getschedparam(thread, ctypes.byref(policy), ctypes.byref(priority)) == 0,
            "cannot read the calling thread's scheduler",
        )
        shim.pthread_setschedparam.argtypes = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        )
        expect(
            shim.pthread_setschedparam(thread, policy, ctypes.byref(priority)) == 0,
            "scheduler shim refused the calling thread",
        )
        expect(
            shim.pthread_setschedparam(thread + 1, policy, ctypes.byref(priority)) == errno.EPERM,
            "scheduler shim admitted another thread",
        )
        return f"native scheduler shim, source {revision}"

    def identity(self) -> str:
        entry = pwd.getpwuid(os.getuid())
        expect((os.getuid(), os.getgid()) == (1000, 1000), f"uid {os.getuid()}, gid {os.getgid()}")
        expect(entry.pw_name == "sandbox", f"uid 1000 is {entry.pw_name!r}, not 'sandbox'")
        expect(entry.pw_dir == "/sandbox", f"the sandbox account's home is {entry.pw_dir}")
        expect(os.environ.get("HOME") == "/sandbox", f"HOME is {os.environ.get('HOME')}")
        # Git finds no bare repository by itself, such as one a skill pack is shaped as.
        system = os.environ.get("GIT_CONFIG_SYSTEM")
        expect(system == "/etc/gitconfig", f"GIT_CONFIG_SYSTEM is {system}")
        config = Path("/etc/gitconfig").read_text()
        expect("bareRepository = explicit" in config, "git's safe.bareRepository is not explicit")
        return f"uid=1000({entry.pw_name}) gid=1000 home={entry.pw_dir}"

    def filesystem(self) -> str:
        def writable(directory: str) -> bool:
            path = Path(directory) / f".image-check-{os.getpid()}"
            try:
                path.write_text("x")
            except OSError:
                return False
            path.unlink()
            return True

        refused = ["/", "/usr/local", "/etc", "/opt", "/var/tmp", "/usr/share/sandbox-science"]  # noqa: S108
        allowed = ["/sandbox", "/tmp"]  # noqa: S108 - writable paths under test
        if self.service:
            # The services install nothing at runtime: their prefix is only read.
            refused.append(PREFIX)
            records = sorted(Path(PREFIX, "conda-meta").glob("*.json"))
            expect(bool(records), f"nothing readable in {PREFIX}/conda-meta")
            json.loads(records[0].read_text())
        else:
            # Runtime installs go into the prefix (D48); multiprocessing needs /dev/shm.
            allowed += ["/dev/shm", PREFIX]  # noqa: S108
        wrongly = [path for path in refused if writable(path)]
        missing = [path for path in allowed if not writable(path)]
        expect(not wrongly, f"writable but should not be: {wrongly}")
        expect(not missing, f"not writable: {missing}")
        detail = f"writable: {', '.join(allowed)}; read-only: {', '.join(refused)}"
        return detail + (f"; {PREFIX} readable" if self.service else "")

    def landlock(self) -> str:
        """The ruleset is in effect: a system file outside OpenShell's baseline is unreadable."""
        try:
            Path("/var/lib/dpkg/status").read_bytes()
        except PermissionError:
            return "/var/lib/dpkg/status is refused, as in an OpenShell sandbox"
        raise CheckError("/var/lib/dpkg/status is readable: no Landlock ruleset applies")

    def record(self) -> str:
        record = json.loads(Path("/usr/share/sandbox-science/image.json").read_text())
        expect(record["profile"] == self.profile, f"the record names {record['profile']}")
        # OpenShell would use a policy baked into the image when given none; the worker gives one.
        expect(not Path("/etc/openshell").exists(), "the image carries /etc/openshell")
        counts = {kind: len(items) for kind, items in record["packages"].items()}
        if self.service:
            pip = {item["name"]: item for item in record["packages"]["pip"]}
            for name in SERVICE_PACKAGES[self.profile]:
                expect(name in pip, f"the record lists no {name}")
                wanted = PACKAGE_LICENSES[name]
                expect(pip[name]["license"] == wanted, f"{name} is {pip[name]['license']}")
        return f"revision {record['revision']}; packages {counts}; no image policy"

    # The executor and its kernels.

    def executor(self) -> str:
        # The worker uploads its in-sandbox files here, beside each other.
        EXECUTOR_DIR.mkdir(parents=True, exist_ok=True)
        for name in WORKER_FILES:
            shutil.copyfile(self.worker_files / name, EXECUTOR_DIR / name)
        script = EXECUTOR_DIR / "executor.py"
        self.client = Executor(script, PROJECT, Path("/tmp/executor.log"))  # noqa: S108
        reply = self.client.open()
        languages, features = reply.get("languages"), reply.get("features")
        expect(languages == LANGUAGES[self.profile], f"languages {languages}")
        expect(features == FEATURES[self.profile], f"features {features}")
        location = reply.get("location", {})
        expect(location.get("uid") == 1000, f"executor uid {location.get('uid')}")
        return f"languages {languages}; features {features}; python {location.get('python')}"

    def ex(self) -> Executor:
        if self.client is None:
            raise CheckError("the executor did not start")
        return self.client

    def python_kernel(self) -> str:
        stdout, result = self.ex().cell("import sys\nprint(sys.executable)\n6 * 7")
        expect(result == "42", f"result {result!r}")
        expect(
            Path(stdout.strip()).resolve() == (Path(PREFIX) / "bin/python").resolve(),
            f"kernel interpreter {stdout.strip()}",
        )
        return f"6 * 7 = {result} in {stdout.strip()}"

    def tools_on_path(self) -> str:
        tools = ["python", "pip", "micromamba"]
        if self.crypto:
            tools += ["sage", "gp", "gap", "Singular", "gcc", "g++", "make", "latexmk", "pdflatex"]
            tools += ["pdftotext", "qpdf", "git"]
        # The package-command wrappers' directory comes first on PATH, where python is a
        # stand-in that runs the prefix's own (provisioning.py, _mirror): it is the prefix's
        # Python that runs, not a path, that is checked.
        out = self.ex().shell(
            f'for t in {" ".join(tools)}; do echo "$t $(command -v $t)"; done; '
            "echo \"prefix $(python -c 'import sys; print(sys.prefix)')\""
        )
        found = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        missing = [tool for tool in tools if not found.get(tool, "").strip()]
        expect(not missing, f"not on PATH: {missing}")
        prefix = found.get("prefix", "").strip()
        expect(
            Path(prefix).resolve() == Path(PREFIX).resolve(),
            f"python ({found['python'].strip()}) runs in {prefix}",
        )
        return ", ".join(f"{tool}={found[tool].strip()}" for tool in tools[:4]) + ", ..."

    def multiprocessing(self) -> str:
        code = (
            "import multiprocessing as mp, concurrent.futures as cf\n"
            "lock = mp.Lock()\n"
            "with lock:\n"
            "    pass\n"
            "with mp.Pool(2) as pool:\n"
            "    a = pool.map(abs, [-1, -2, -3])\n"
            "with cf.ProcessPoolExecutor(2) as ex:\n"
            "    b = list(ex.map(abs, [-4, -5]))\n"
            "print(a, b, mp.get_start_method())"
        )
        stdout, _ = self.ex().cell(code)
        expect(stdout.startswith("[1, 2, 3] [4, 5]"), f"output {stdout!r}")
        return f"Lock, Pool and ProcessPoolExecutor: {stdout.strip()}"

    # crypto-base.

    def sage_kernel(self) -> str:
        code = (
            "F = GF(17); a = F(3)\n"
            "print(a^16, F.multiplicative_generator(), F(5)/F(3), F.order())\n"
            "(2^10, 1/3 + 1/6)"
        )
        stdout, result = self.ex().cell(code, language="sage")
        expect(stdout.split() == ["1", "3", "13", "17"], f"output {stdout!r}")
        expect(result == "(1024, 1/2)", f"result {result!r}")
        return f"GF(17): {stdout.strip()}; (2^10, 1/3 + 1/6) = {result}"

    def sage_interfaces(self) -> str:
        code = "print(gp('2 + 2'), libgap.SymmetricGroup(5).Size(), singular('3 * 7'))"
        stdout, _ = self.ex().cell(code, language="sage")
        expect(stdout.split() == ["4", "120", "21"], f"output {stdout!r}")
        return f"gp and Singular through pexpect, libgap: {stdout.strip()}"

    def fpylll(self) -> str:
        code = (
            "from fpylll import FPLLL, IntegerMatrix, LLL\n"
            "FPLLL.set_random_seed(1337)\n"
            "A = IntegerMatrix.random(60, 'qary', k=30, bits=20)\n"
            "LLL.reduction(A)\n"
            "print(LLL.is_reduced(A), A.nrows, round(A[0].norm()))"
        )
        stdout, _ = self.ex().cell(code)
        expect(stdout.split()[:2] == ["True", "60"], f"output {stdout!r}")
        return f"LLL on a 60-dimensional q-ary lattice: reduced, |b0| = {stdout.split()[2]}"

    def g6k(self) -> str:
        code = (
            "from fpylll import FPLLL, IntegerMatrix, LLL\n"
            "from g6k import Siever\n"
            "FPLLL.set_random_seed(1337)\n"
            "A = LLL.reduction(IntegerMatrix.random(50, 'qary', k=25, bits=10))\n"
            "g6k = Siever(A, seed=0x1337)\n"
            "g6k.initialize_local(0, 0, 50)\n"
            "g6k(alg='gauss')\n"
            "g6k()\n"
            "print(len(g6k), g6k.max_sieving_dim)"
        )
        stdout, _ = self.ex().cell(code)
        size, maximum = (int(part) for part in stdout.split())
        expect(size > 0, f"empty database: {stdout!r}")
        return f"sieve in dimension 50: {size} vectors; maximum sieving dimension {maximum}"

    def lattice_estimator(self) -> str:
        code = (
            "from estimator import LWE, schemes\n"
            "cost = LWE.primal_usvp(schemes.Kyber512)\n"
            "print(schemes.Kyber512.n, float(log(cost['rop'], 2)).__round__(1))"
        )
        stdout, _ = self.ex().cell(code, language="sage", limit=300)
        n, bits = stdout.split()
        expect(n == "512" and 100 < float(bits) < 200, f"output {stdout!r}")
        return f"primal uSVP on Kyber512: 2^{bits} operations"

    def latexmk(self) -> str:
        document = r"""\documentclass{llncs}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,mathtools}
\usepackage[n,advantage,operators,sets,adversary,landau,probability,notions,logic,ff,mm,%
  primitives,events,complexity,asymptotics,keys]{cryptocode}
\usepackage{algorithm}
\usepackage{algpseudocode}
\usepackage{tikz}
\usepackage{hyperref}
\usepackage{cleveref}
\begin{document}
\section{Lattices}
MARKER.
\begin{theorem}\label{thm:mink}
For a lattice $\Lambda\subset\mathbb{R}^n$,
$\lambda_1(\Lambda)\le\sqrt{n}\,\det(\Lambda)^{1/n}$.
\end{theorem}
By \cref{thm:mink}, $\mathbb{F}_{17}^\times$ has order $16$.
\begin{algorithm}\caption{Sieve}\begin{algorithmic}
\State $L \gets \textsc{Sample}(B)$
\end{algorithmic}\end{algorithm}
\procedure{$\mathsf{Exp}_{\adv}$}{b \sample \bin \\ \pcreturn b}
\begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture}
\end{document}
""".replace("MARKER", MARKER)
        (WORKSPACE / "tex").mkdir(parents=True, exist_ok=True)
        (WORKSPACE / "tex" / "paper.tex").write_text(document)
        fonts = self.ex().shell(
            "cd tex && latexmk -pdf -interaction=nonstopmode -halt-on-error paper.tex >latexmk.log"
            " 2>&1 || { tail -30 latexmk.log; exit 1; }; pdffonts paper.pdf"
        )
        expect((WORKSPACE / "tex" / "paper.pdf").is_file(), "no paper.pdf")
        expect("Type 3" not in fonts, f"bitmap (Type 3) fonts in the PDF:\n{fonts}")
        packages = "llncs, T1 fonts, amsmath, cryptocode, algorithmicx, TikZ, cleveref"
        return f"latexmk -pdf built paper.pdf ({packages}) with no bitmap fonts"

    def pdftotext(self) -> str:
        text = self.ex().shell("pdftotext tex/paper.pdf - && qpdf --check tex/paper.pdf >/dev/null")
        expect(MARKER in text and "Theorem 1" in text, f"text: {text[:200]!r}")
        return "pdftotext found the document's text; qpdf --check passed"

    def pdf_oxide(self) -> str:
        code = (
            "import pdf_oxide\n"
            f"doc = pdf_oxide.PdfDocument({str(WORKSPACE / 'tex' / 'paper.pdf')!r})\n"
            f"print({MARKER!r} in doc.extract_text(0), pdf_oxide.__version__)"
        )
        stdout, _ = self.ex().cell(code)
        found, version = stdout.split()
        expect(found == "True", f"output {stdout!r}")
        return f"pdf_oxide {version} extracted the text"

    def pdf_tools(self) -> str:
        """The worker's PDF operations (docs/specs/pdf-sources.md), as the executor runs them."""
        client = self.ex()
        info = client.pdf("info", "tex/paper.pdf")
        expect(info["page_count"] == 1 and info["pages"][0]["has_text"], f"info {info}")
        pages = client.pdf("pages", "tex/paper.pdf", pages="1")
        lines = [line["text"] for line in pages["pages"][0]["lines"]]
        expect(any(MARKER in line for line in lines), f"lines {lines[:5]}")
        hits = client.pdf("search", "tex/paper.pdf", query="lattice")["hits"]
        expect(len(hits) >= 1, "no search hits")
        image = client.pdf("render", "tex/paper.pdf", page=1, dpi=100)["image"]
        expect((WORKSPACE / image["path"]).is_file(), f"no render at {image['path']}")
        parser = f"{info['parser']['name']} {info['parser']['version']}"
        size = f"{image['width']}x{image['height']}"
        return f"{parser}: page facts, line anchors, {len(hits)} search hits and a {size} render"

    def runtime_install(self) -> str:
        # Neither package is in either image.
        out = self.ex().shell(
            "micromamba install -y -q -c conda-forge --override-channels toolz >/dev/null"
            " && python -m pip install -q --no-deps --no-cache-dir tomli-w"
            " && python -c 'import toolz, tomli_w; print(toolz.__file__, tomli_w.__file__)'",
            limit=600,
        )
        paths = out.split()
        expect(
            len(paths) == 2
            and all(Path(path).resolve().is_relative_to(Path(PREFIX).resolve()) for path in paths),
            f"{out!r}",
        )
        return "micromamba and pip installed into the Conda prefix"

    # The services' images (orchestrator, mcp-proxy).

    def command(self, program: str, *args: str) -> str:
        """Run one of the image's commands by its full path, as a service's sandbox would;
        return its standard output, or fail with its error."""
        done = subprocess.run(  # noqa: S603 - the image's own command, fixed arguments
            [program, *args], capture_output=True, text=True, env=SERVICE_ENV, timeout=120
        )
        if done.returncode != 0:
            output = (done.stderr or done.stdout)[-1500:]
            words = " ".join([Path(program).name, *args[:1]])
            raise CheckError(f"{words} exited {done.returncode}: {output}")
        return done.stdout

    def prefix_imports(self, modules: tuple[str, ...], dists: tuple[str, ...]) -> dict[str, str]:
        """Import ``modules`` with the prefix's interpreter, in isolated mode and without the
        image's environment; fail unless all come from the prefix. The versions of ``dists``."""
        code = (
            "import importlib, importlib.metadata as metadata, json\n"
            f"modules = {list(modules)!r}\n"
            "files = {name: importlib.import_module(name).__file__ for name in modules}\n"
            f"versions = {{name: metadata.version(name) for name in {list(dists)!r}}}\n"
            "print(json.dumps({'files': files, 'versions': versions}))"
        )
        found = json.loads(self.command(f"{PREFIX}/bin/python", "-I", "-c", code))
        outside = {
            name: file for name, file in found["files"].items() if not file.startswith(PREFIX)
        }
        expect(not outside, f"imported from outside the prefix: {outside}")
        return found["versions"]

    def no_kernels(self) -> str:
        """The services run no research code (D11): no kernelspecs, no kernel packages."""
        kernels = Path(PREFIX, "share", "jupyter", "kernels")
        specs = sorted(path.name for path in kernels.iterdir()) if kernels.is_dir() else []
        expect(not specs, f"kernelspecs in the image: {specs}")
        present = [name for name in KERNEL_PACKAGES if importlib.util.find_spec(name) is not None]
        expect(not present, f"kernel packages in the image: {present}")
        return f"no kernelspecs, and none of {', '.join(KERNEL_PACKAGES)}"

    # orchestrator: the harness as its sandbox runs it.

    def harness(self, *args: str) -> str:
        """Run the harness's command; return its standard output, or fail with its error."""
        return self.command(HARNESS, *args)

    def harness_cli(self) -> str:
        """The harness's command: on ``PATH``, and by its full path without it."""
        found = shutil.which("sandbox-science-harness")
        expect(found == HARNESS, f"sandbox-science-harness on PATH is {found}")
        # The full path must work where the image's PATH does not apply.
        interpreter = Path(HARNESS).read_text().splitlines()[0]
        expect(interpreter.startswith(f"#!{PREFIX}/bin/python"), f"it starts {interpreter!r}")
        for command in ([], ["run"], ["project"], ["session"]):
            usage = self.harness(*command, "--help")
            expect(usage.startswith("usage: sandbox-science-harness"), f"help: {usage[:200]!r}")
        for entrypoint in ("sandbox-science-control", "sandbox-science-agent-runtime"):
            usage = self.command(f"{PREFIX}/bin/{entrypoint}", "--help")
            expect("--settings" in usage, f"{entrypoint} has no settings contract")
        return f"{HARNESS} ({interpreter}): --help for itself, run, project and session"

    def harness_imports(self) -> str:
        """The harness and its dependencies import from the prefix, with their versions."""
        versions = self.prefix_imports(HARNESS_MODULES, HARNESS_DISTS)
        return ", ".join(f"{name} {versions[name]}" for name in HARNESS_DISTS) + f" from {PREFIX}"

    def harness_state(self) -> str:
        """A project record in the workdir, where the harness keeps its state (``--state-dir``)."""
        state = str(HARNESS_DIR / "state")
        policy = "sha256:" + hashlib.sha256(MARKER.encode()).hexdigest()
        project = ["--state-dir", state, "--project", SESSION]
        self.harness("project", "create", *project, "--profile", "minimal", "--policy", policy)
        shown = json.loads(self.harness("project", "show", *project))
        expect((shown["profile"], shown["policy"]) == ("minimal", policy), f"record {shown}")
        environment = shown["default_environment_id"]
        return f"project create and show in {state}: default environment {environment}"

    def harness_run(self) -> str:
        """A scripted task through to its answer, against a stub of the worker API on loopback.

        The stub answers only ``GET /v1/capabilities`` with the token; the scripted model answers
        without tools, so the harness needs nothing else from a worker. It speaks the worker
        protocol of the image's own contracts package, as the harness refuses any other.
        """
        found = self.command(f"{PREFIX}/bin/python", "-I", "-c", PROTOCOL_PROBE).split()
        protocol = (found[0], int(found[1]))
        requests: list[str] = []
        HARNESS_DIR.mkdir(parents=True, exist_ok=True)
        transcript = HARNESS_DIR / "transcript.json"
        server = stub_worker(protocol, requests)
        worker = f"http://127.0.0.1:{server.server_port}"
        try:
            out = self.harness("run", *harness_run_args(worker, HARNESS_DIR, transcript))
        finally:
            server.shutdown()
            server.server_close()
        expect(requests[:1] == [f"/v1/capabilities?environment_id={SESSION}"], f"{requests}")
        expect(out.rstrip().endswith(MARKER), f"output {out[-500:]!r}")
        stopped = json.loads(transcript.read_text())["stopped"]
        expect(stopped == "answered", f"stopped: {stopped}")
        return (
            f"run against {worker}: capabilities with the token and protocol {protocol[1]}, "
            "then the scripted answer"
        )

    # mcp-proxy: the proxy as its sandbox runs it, kept running by the image's launcher.

    def proxy_cli(self) -> str:
        """The proxy's command and the event store's: on ``PATH``, and by full path without it."""
        found = shutil.which("sandbox-science-mcp-proxy")
        expect(found == PROXY, f"sandbox-science-mcp-proxy on PATH is {found}")
        interpreter = Path(PROXY).read_text().splitlines()[0]
        expect(interpreter.startswith(f"#!{PREFIX}/bin/python"), f"it starts {interpreter!r}")
        for command in ([], ["serve"], ["bind"], ["pending"], ["approve"], ["deny"]):
            usage = self.command(PROXY, *command, "--help")
            expect(usage.startswith("usage: sandbox-science-mcp-proxy"), f"help: {usage[:200]!r}")
        usage = self.command(EVENTS, "--help")
        expect(usage.startswith("usage: sandbox-science-events"), f"help: {usage[:200]!r}")
        return (
            f"{PROXY} ({interpreter}): --help for itself, serve, bind, pending, approve and deny;"
            f" {EVENTS} --help"
        )

    def proxy_imports(self) -> str:
        """The proxy and its dependencies import from the prefix, with their versions; the
        default TLS context trusts the prefix's CA bundle (OpenShell sets its own in a sandbox)."""
        versions = self.prefix_imports(PROXY_MODULES, PROXY_DISTS)
        code = (
            "import ssl\n"
            "print(ssl.get_default_verify_paths().cafile,"
            " ssl.create_default_context().cert_store_stats()['x509_ca'])"
        )
        cafile, count = self.command(f"{PREFIX}/bin/python", "-I", "-c", code).split()
        expect(cafile.startswith(PREFIX) and int(count) > 0, f"CA bundle {cafile}, {count} CAs")
        found = ", ".join(f"{name} {versions[name]}" for name in PROXY_DISTS)
        return f"{found} from {PREFIX}; {count} CAs in {cafile}"

    def launched(self) -> Launcher:
        if self.proxy_launcher is None:
            raise CheckError("the launcher did not start")
        return self.proxy_launcher

    def proxy_answer(self, token: str | None) -> tuple[int, Message]:
        """The proxy's answer to a call once it listens, while the launcher runs."""
        launcher = self.launched()
        deadline = time.monotonic() + 60
        while True:
            try:
                return proxy_post(token, LIST_TOOLS)
            except (OSError, http.client.HTTPException):
                launcher.expect_running()
                if time.monotonic() > deadline:
                    raise CheckError(f"no answer within 60 s: {launcher.text()[-1500:]}") from None
                time.sleep(0.2)

    def launcher(self) -> str:
        """The sandbox's command: in the prefix, executable, and waiting for the settings."""
        path = Path(LAUNCHER)
        mode = stat.S_IMODE(path.stat().st_mode)
        expect(mode == 0o755 and os.access(path, os.X_OK), f"{LAUNCHER} has mode {mode:o}")
        first = path.read_text().splitlines()[0]
        expect(first == "#!/bin/sh", f"it starts {first!r}")
        expect(not PROXY_DIR.exists(), f"{PROXY_DIR} exists before the settings are written")
        launcher = Launcher(Path("/tmp/mcp-proxy-run.log"))  # noqa: S108 - writable in the checks
        self.proxy_launcher = launcher
        launcher.wait_for("wait", lambda: f"waiting for {PROXY_SETTINGS}" in launcher.text())
        time.sleep(2)  # it looks for the settings every second
        launcher.expect_running()
        expect(not launcher.proxies(), f"it started the proxy without settings: {launcher.text()}")
        expect(not proxy_listening(), f"something listens on {PROXY_ADDRESS}")
        return (
            f"{LAUNCHER} (mode 755, {first}), started without the image's environment, waits"
            f" for {PROXY_SETTINGS} and starts nothing"
        )

    def proxy_serve(self) -> str:
        """Once the settings are there, the launcher starts the proxy, which keeps its stores in
        the workdir and decides each call under the project's current binding."""
        launcher = self.launched()
        # As the operator uploads them: the ceiling, then the settings.
        PROXY_DIR.mkdir(parents=True, exist_ok=True)
        (PROXY_DIR / "pool-ceiling.yaml").write_text(PROXY_CEILING)
        PROXY_SETTINGS.write_text(PROXY_TOML)
        status, answer = self.proxy_answer(None)
        expect((status, refusal(answer)) == (401, "unauthenticated"), f"{status} {answer}")
        status, answer = proxy_post(TEST_TOKEN, LIST_TOOLS)
        expect((status, refusal(answer)) == (503, "project_unbound"), f"{status} {answer}")
        policy = PROXY_DIR / "policy.yaml"
        policy.write_text(PROXY_POLICY)
        project = ["--state-dir", str(PROXY_DIR / "state"), "--project", SESSION]
        bound = self.command(PROXY, "bind", *project, "--policy", str(policy))
        expect(bound.startswith(f"bound {SESSION} to image-check sha256:"), f"bind: {bound!r}")
        status, answer = proxy_post(TEST_TOKEN, LIST_TOOLS)
        expect((status, refusal(answer)) == (400, "session_required"), f"{status} {answer}")
        pending = self.command(PROXY, "pending", "--settings", str(PROXY_SETTINGS)).strip()
        expect(pending == "no permission requests are waiting", f"pending: {pending!r}")
        # The process OpenShell sees connect to the servers, and the environment it got.
        pid = launcher.proxies()[-1]
        executable = os.readlink(f"/proc/{pid}/exe")
        expect(fnmatch.fnmatchcase(executable, PROXY_BINARIES), f"the proxy runs {executable}")
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        expect("=".join(PROXY_PLACEHOLDER).encode() in environ, "the placeholder did not reach it")
        return (
            f"{executable} (pid {pid}), matching {PROXY_BINARIES}, with the launcher's"
            f" environment: 401 without the token; with it project_unbound, then"
            f" session_required once bound; no pending requests"
        )

    def proxy_restart(self) -> str:
        """When the proxy dies the launcher starts it again, with its binding and records
        intact; SIGTERM stops both."""
        launcher = self.launched()
        first = launcher.proxies()[-1]
        os.kill(first, signal.SIGKILL)
        launcher.wait_for("new proxy", lambda: len(launcher.proxies()) > 1)
        second = launcher.proxies()[-1]
        status, answer = self.proxy_answer(TEST_TOKEN)
        expect((status, refusal(answer)) == (400, "session_required"), f"{status} {answer}")
        expect("exited with status 137" in launcher.text(), f"log: {launcher.text()[-1500:]}")
        # One record per authenticated call, and the chain verifies after the crash.
        store = str(PROXY_DIR / "state" / "events.db")
        verified = self.command(EVENTS, "verify", store).strip()
        expect(verified.startswith("ok: "), f"verify: {verified!r}")
        exported = [json.loads(line) for line in self.command(EVENTS, "export", store).splitlines()]
        calls = [(r["kind"], r["source"], r["payload"]["reason_code"]) for r in exported]
        reasons = ("project_unbound", "session_required", "session_required")
        expect(calls == [("mcp-call", "mcp-proxy", reason) for reason in reasons], f"{calls}")
        launcher.process.terminate()
        code = launcher.process.wait(timeout=30)
        expect(code == 0, f"the launcher exited with {code} on SIGTERM: {launcher.text()[-500:]}")
        expect(not Path(f"/proc/{second}").exists(), f"the proxy (pid {second}) is still there")
        return (
            f"after SIGKILL to pid {first}, the launcher started pid {second}, which kept the"
            f" binding; events.db {verified.removeprefix('ok: ')}; SIGTERM stopped both"
        )

    def run(self) -> int:
        for name in planned(self.profile, landlock=self.under_landlock, network=self.network):
            self.check(name, getattr(self, name.replace("-", "_")))
        if self.client is not None:
            self.client.close()
        if self.proxy_launcher is not None:
            self.proxy_launcher.close()
        return 1 if self.failed else 0


def planned(profile: str, *, landlock: bool, network: bool) -> list[str]:
    """The checks for a profile, in order; each is the ``Checks`` method of the same name."""
    names = ["identity", "filesystem", *(["landlock"] if landlock else []), "record"]
    if profile == "orchestrator":
        # The harness's image: no executor or kernels, and nothing installed at runtime.
        harness = ["harness-cli", "harness-imports", "no-kernels", "harness-state", "harness-run"]
        return names + harness
    if profile == "mcp-proxy":
        # The MCP proxy's image: the same, and a launcher as the sandbox's command.
        proxy = ["proxy-cli", "proxy-imports", "no-kernels"]
        proxy += ["launcher", "proxy-serve", "proxy-restart"]
        return names + proxy
    names += ["scheduler-shim", "executor", "python-kernel", "tools-on-path", "multiprocessing"]
    if profile == "crypto-base":
        names += ["sage-kernel", "sage-interfaces", "fpylll", "g6k", "lattice-estimator"]
        names += ["latexmk", "pdftotext", "pdf-oxide", "pdf-tools"]
    if network:
        names.append("runtime-install")
    return names


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check a profile image from the inside.")
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument(
        "--worker-files",
        required=True,
        type=Path,
        help="a directory with the worker's in-sandbox files (unused for the services' images)",
    )
    parser.add_argument("--network", action="store_true", help="also install packages at runtime")
    parser.add_argument("--landlock", action="store_true", help="expect a Landlock ruleset")
    args = parser.parse_args(argv)
    return Checks(args.profile, args.worker_files, args.network, args.landlock).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
