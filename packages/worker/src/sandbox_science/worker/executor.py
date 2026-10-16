# SPDX-License-Identifier: MIT
"""The executor: runs shell commands, Jupyter kernels and file operations in a sandbox.

The trusted worker service starts it inside a research sandbox and speaks
newline-delimited JSON with it over stdin and stdout. It depends only on the
standard library, its siblings beside it (provisioning.py, capture.py,
sandbox_files.py for the file operations, sandbox_processes.py for the processes and
sandbox_setup.py for the isolation probe and configure's files)
and, once a kernel starts, jupyter_client, so the worker service can copy it into any
sandbox that has Python.
docs/specs/tools.md defines the behavior and docs/WORKER.md the protocol.

Requests carry an ``id`` and an ``op``; each gets one reply
(``{"type": "reply", "id": …, "ok": …}``). Shell commands and cells reply as
soon as they are accepted, then send events
(``{"type": "event", "execution": …, "event": …}``) ending with ``end``.
Every line it sends is shorter than MAX_LINE: output goes in chunks, and a
result too large for one line keeps less of its output. When stdin closes,
the executor stops every command and kernel, and what they left running.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, cast


async def _owned[T](work: Awaitable[T], cancelled: Callable[[], None] | None = None) -> T:
    """Cancellation cannot outlive an actual process launch or its cleanup."""
    task = asyncio.ensure_future(work)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if cancelled is not None:
                cancelled()
    return task.result()


def _sibling(name: str) -> ModuleType:
    """A module of the worker package; in a sandbox, the copy the worker put beside this file."""
    if __package__:
        return importlib.import_module(f"{__package__}.{name}")
    qualified = f"sandbox_science_{name}"
    spec = importlib.util.spec_from_file_location(qualified, Path(__file__).with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise ImportError(f"no {name}.py beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A half-built module is never left for anything to find.
        del sys.modules[qualified]
        raise
    return module


if TYPE_CHECKING:
    from . import capture, provisioning, sandbox_files, sandbox_processes, sandbox_setup
    from .sandbox_files import MAX_PATH_BYTES as MAX_PATH_BYTES
    from .sandbox_files import SHARED as SHARED
    from .sandbox_files import ToolError as ToolError
    from .sandbox_files import encode_line as encode_line
else:
    capture = _sibling("capture")
    provisioning = _sibling("provisioning")
    sandbox_files = _sibling("sandbox_files")
    sandbox_processes = _sibling("sandbox_processes")
    sandbox_setup = _sibling("sandbox_setup")
    ToolError = sandbox_files.ToolError
    encode_line = sandbox_files.encode_line
    SHARED = sandbox_files.SHARED
    MAX_PATH_BYTES = sandbox_files.MAX_PATH_BYTES

PROTOCOL = 2
"""The executor's protocol with the worker: 2 takes skill bindings in executions, which the
worker sends only to an executor of 2 or more (docs/specs/skills.md, SK-16)."""
MAX_LINE = 16 * 1024 * 1024
"""The longest line the executor sends; the worker drops longer ones."""
MAX_REQUEST = 64 * 1024 * 1024
"""The longest request line it reads (a file write carries the file)."""
MAX_READ = 8 * 1024 * 1024
MAX_LIST = 5000
MAX_WRITTEN = 1000
MAX_OUTPUTS = 10_000
"""Rich outputs a cell returns; one note stands for any beyond them or the limit."""
STREAM_CHUNK = 64 * 1024
"""Characters of output per stream event."""
MAX_ERROR_TEXT = 64 * 1024
MAX_TRACEBACK_LINES = 100
MAX_TRACEBACK_LINE = 8 * 1024
TERM_GRACE = 2.0
PACKAGE_GRACE = 10.0
"""Seconds a package command still in its turn gets after SIGTERM, to roll back (conda does)."""
INTERRUPT_GRACE = 5.0
KERNEL_START_TIMEOUT = 120.0
KERNEL_NAMES = {"python": "python3", "sage": "sagemath"}
"""Each language's Jupyter kernelspec; SageMath installs ``sagemath`` beside its Python."""
PDF_HELPER = Path(__file__).with_name("pdfhelper.py")
"""The PDF helper the worker copies beside this file (docs/specs/pdf-sources.md)."""
PROVISIONING = Path(provisioning.__file__)
"""What the package-command wrappers and hook run (provisioning.py, beside this file)."""
PACKAGE_POLL = 0.25
"""The longest pause between looks at the package lock while an attempt waits for it."""
LOCKFILE_WAIT = 30.0
"""Seconds ``lockfile`` waits for a package command to finish."""
MAX_LOCKFILE_EVENT = MAX_LINE // 2
"""A ``revision`` event carries its lockfile only below this many bytes of JSON."""
MAX_OWNERS = 4096
MAX_RUNS = 64
"""Attempts remembered per call or kernel, to tell which one ran a package command."""
MAX_RUN_KERNELS = 63
"""Delegated runs of one session with live kernels of their own at once (contracts'
bound)."""
MAX_ENDED_RUNS = 4096
"""Ended runs remembered per session, whose late cells are refused."""
MAX_COMMANDS = 8
"""Package commands a ``revision`` event names: the last of its change."""
MAX_LOOKS = 8
"""Rounds of journal records one look reads; any left wait for the next look."""
MAX_SENTINELS = 8
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_BINDING_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_COMMAND_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_BINDING_VALUE_RE = re.compile(r"[^\x00-\x20\x7f;]{1,4096}")
"""A binding's value: a path, without whitespace, control characters or ``;``."""
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

Message = dict[str, Any]
Send = Callable[[Message], Awaitable[None]]


class Clock(Protocol):
    """Time for stopping idle kernels; the worker's tests pass a fake one.

    The same shape as ``sandbox_science.contracts.Clock``, which the executor cannot
    import: it runs with the standard library only.
    """

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _NotStartedError(Exception):
    """An attempt cancelled before it started: a cell before it reached its kernel."""


@dataclass
class _Change:
    """A change to the environment's packages, as one ``revision`` event reports it."""

    fingerprint: str
    managed: bool
    cause: str | None
    """The execution that ran its package commands, or after which it was seen."""
    commands: list[Message] = field(default_factory=list[Message])
    lockfile: object = None
    names: list[str] = field(default_factory=list[str])
    """The journal records it accounts for, which leave the journal once it is sent."""


class _BusyError(Exception):
    """A package command held the environment's lock for an attempt's whole time limit."""

    def __init__(self, waited: float, holder: Message | None) -> None:
        super().__init__(waited)
        self.waited = waited
        self.holder = holder

    def error(self) -> Message:
        what = provisioning.describe(self.holder)
        return {
            "kind": "timed_out",
            "message": f"waited {self.waited:.0f} s for a package command in this environment "
            f"to finish ({what}); the attempt did not start",
        }


# Output capture.


class Capture:
    """One output stream: live text up to half the limit, then only a rolling tail."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes) -> str:
        """Record ``data`` and return the text to stream now (empty once the head is full)."""
        self.total += len(data)
        room = self.head_limit - len(self.head)
        live = b""
        if room > 0:
            live = data[:room]
            self.head += live
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]
        return self._decoder.decode(live) if live else ""

    def summary(self) -> Message:
        dropped = self.total - len(self.head) - len(self.tail)
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail).decode("utf-8", "replace")
        if dropped > 0:
            text = f"{head}\n[… {dropped} bytes omitted …]\n{tail}"
        else:
            text = (bytes(self.head) + bytes(self.tail)).decode("utf-8", "replace")
        return {
            "name": self.name,
            "text": text,
            "total_bytes": self.total,
            "truncated": dropped > 0,
        }


def write_log(descriptor: int, data: bytes) -> None:
    """Write a bounded chunk completely; propagate ENOSPC/EDQUOT to end the job."""
    view = memoryview(data)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("job log write made no progress")
        view = view[count:]


def bounded(text: str, limit: int) -> str:
    """``text``, keeping its head and tail if it is longer than ``limit`` characters."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"


def environment_fingerprint() -> str:
    """A digest of the packages installed for this interpreter (W3; provisioning.fingerprint)."""
    return provisioning.fingerprint(provisioning.Places.here())


def without_wrapped(
    processes: Sequence[sandbox_processes.Process],
) -> list[sandbox_processes.Process]:
    """``processes`` less each package command whose wrapper is among them.

    A wrapper passes the signals it gets on to its command (provisioning.run): signalled
    both directly and through its wrapper, the command would get each signal twice, and
    the second can cut short the clean-up the first began, such as conda's rollback.
    """
    pids = {info.pid for info in processes}
    kept: list[sandbox_processes.Process] = []
    for info in processes:
        holder = provisioning.holder_pid(
            sandbox_processes.started_with(info.pid, provisioning.HOLDER) or ""
        )
        if holder is not None and holder == info.ppid and holder in pids:
            continue
        kept.append(info)
    return kept


@dataclass
class Running:
    """An accepted shell command or cell."""

    execution: str
    kind: str
    owner: str = field(default_factory=lambda: f"call-{secrets.token_hex(8)}")
    task: asyncio.Task[None] | None = None
    queued: bool = False
    """A cell waiting for its kernel; cancelling drops it from the queue."""
    started: bool = False
    cancel_requested: bool = False
    restarted: bool = False
    """Its kernel was restarted under it."""
    closing: bool = False
    """Its session was closed under it, which stops the kernel."""
    process: asyncio.subprocess.Process | None = None
    leader: sandbox_processes.Leader | None = None
    kernel: Kernel | None = None
    session: str = ""
    captures: dict[str, Capture] = field(default_factory=dict[str, Capture])
    log_error: str | None = None
    waited: float = 0.0
    admitted: float = field(default_factory=time.monotonic)
    package_operation: str = ""
    package_generation: int = -1
    controller: Callable[[], bool] | None = None
    bindings: dict[str, str] = field(default_factory=dict[str, str])
    """A cell's skill bindings, to say when its kernel holds others."""
    """Seconds it waited for package commands before it started; its time limit covers them."""


def _not_retired(kernel: Kernel) -> None:
    """Refuse to start a run's kernel after the run ended (under its ``control``)."""
    if kernel.retired:
        raise ToolError("conflict", f"run {kernel.run} has ended, and its kernels with it")


@dataclass
class Kernel:
    """A session's kernel for one language, or a run's own (``run``); its generation
    grows on every restart."""

    session: str
    language: str
    cwd: Path
    env: dict[str, str]
    generation: int = 0
    state_lost: bool = False
    birth_id: str | None = None
    start_environment: str | None = None
    start_execution: str | None = None
    start_package_epoch: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held by the running cell: cells queue on it in submission order."""
    control: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while the kernel starts, stops or restarts."""
    manager: Any = None
    client: Any = None
    owner: str = ""
    leader: sandbox_processes.Leader | None = None
    last_used: float = field(kw_only=True)
    """When a cell last started or ended, by the executor's clock, for stopping idle
    kernels."""
    run: str | None = field(default=None, kw_only=True)
    """The run whose own kernel this is; ``None`` for the session's."""
    retired: bool = field(default=False, kw_only=True)
    """Its run has ended: it is never started again (set under ``control``)."""
    bindings: dict[str, str] = field(default_factory=dict[str, str], kw_only=True)
    """The skill bindings its next start takes: the last cell's, the whole set (SK-16)."""
    started_bindings: dict[str, str] = field(default_factory=dict[str, str], kw_only=True)
    """The skill bindings it started with, which it keeps until it starts again."""

    @property
    def kernel_id(self) -> str:
        if self.run is not None:
            # Named by a digest after "krn.", which no session's kernel ID begins with
            # ("krn-"), since joining a session and a run ID could repeat one.
            parts = f"{self.session}\0{self.run}\0{self.language}"
            return f"krn.r{hashlib.sha256(parts.encode()).hexdigest()[:24]}-{self.language}"
        name = f"krn-{self.session}-{self.language}"
        if len(name) <= 128:
            return name
        # A long session ID would make an ID longer than the worker can report.
        digest = hashlib.sha256(f"{self.session}\0{self.language}".encode()).hexdigest()[:24]
        return f"krn-{digest}-{self.language}"

    def info(self) -> Message:
        return {
            "kernel_id": self.kernel_id,
            "language": self.language,
            "generation": self.generation,
            "run": self.run,
            "birth_id": self.birth_id,
            "start_environment": self.start_environment,
            "start_execution": self.start_execution,
            "start_package_epoch": self.start_package_epoch,
        }

    async def alive(self) -> bool:
        return self.manager is not None and await _alive(self.manager)

    async def ensure_started(
        self, launcher: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None
    ) -> None:
        if await self.alive():
            return
        if self.manager is not None:
            # It died since the last cell: whatever it held is gone.
            await self.stop()
            self.state_lost = True
        try:
            from jupyter_client.manager import AsyncKernelManager
        except ImportError as exc:
            raise ToolError("unsupported", "jupyter_client is not installed here") from exc
        owner = self.env.get("SANDBOX_SCIENCE_DAEMON_MARK", "") + f"kernel-{secrets.token_hex(8)}"
        manager: Any = AsyncKernelManager(
            kernel_name=KERNEL_NAMES[self.language], transport="ipc", ip=kernel_address()
        )
        client: Any = None
        leader: sandbox_processes.Leader | None = None
        try:

            async def spawn() -> None:
                nonlocal leader, client
                with sandbox_processes.starting_children():
                    # The environment's own variables win over any binding (SK-16).
                    env = {**self.bindings, **self.env, sandbox_processes.OWNER: owner}
                    await manager.start_kernel(cwd=str(self.cwd), env=env)
                    self.started_bindings = dict(self.bindings)
                    pid = getattr(manager.provisioner, "pid", None)
                    if (
                        isinstance(pid, int)
                        and (info := sandbox_processes.process_info(pid)) is not None
                    ):
                        sandbox_processes.spawned.add(pid)
                        leader = (pid, info.start)
                client = manager.client()
                client.start_channels()
                await client.wait_for_ready(timeout=KERNEL_START_TIMEOUT)

            self.birth_id = self.start_environment = self.start_execution = None
            self.start_package_epoch = None
            if launcher is None:
                await spawn()  # legacy/internal restart: its revision stays unknown
            else:
                await launcher(spawn)
        except BaseException as exc:

            async def cleanup() -> None:
                with contextlib.suppress(Exception):
                    await manager.shutdown_kernel(now=True)
                await end_and_collect(owner, leader)

            await _owned(cleanup())
            if leader is not None:
                sandbox_processes.spawned.discard(leader[0])
            if not isinstance(exc, Exception) or isinstance(
                exc, ToolError | _BusyError | _NotStartedError
            ):
                raise
            raise ToolError("unsupported", f"cannot start a {self.language} kernel: {exc}") from exc
        self.manager, self.client, self.owner, self.leader = manager, client, owner, leader
        self.generation += 1

    async def stop(self) -> None:
        """Shut the kernel down, and end what it started."""
        client, manager, owner, leader = self.client, self.manager, self.owner, self.leader
        self.client = self.manager = self.leader = None
        self.owner = ""
        if client is not None:
            with contextlib.suppress(Exception):
                client.stop_channels()
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)
        if owner:
            await end_and_collect(owner, leader)
        if leader is not None:
            sandbox_processes.spawned.discard(leader[0])


async def end_and_collect(owner: str, leader: sandbox_processes.Leader | None) -> int:
    """End what ``owner`` left running, then collect the zombies this executor adopted
    that nothing else waits for; return how many processes it ended."""
    ended = await asyncio.to_thread(sandbox_processes.end_owned, owner, leader)
    zombies = await asyncio.to_thread(sandbox_processes.zombie_children)
    sandbox_processes.collect_adopted(zombies)
    return ended


def kernel_address() -> str:
    """A new kernel's channel address: a random Linux abstract Unix socket name.

    Over TCP, Jupyter picks free ports and the kernel binds them only later, so another
    kernel, a kernel's client or research code can take one meanwhile: the kernel then
    dies or never answers. An abstract name is never reused and takes no port. ZeroMQ
    reads the leading ``@`` as the abstract namespace.
    """
    return "@ss-kernel-" + secrets.token_hex(16)


async def _alive(manager: Any) -> bool:
    try:
        return bool(await manager.is_alive())
    except Exception:
        return False


async def _interrupt(manager: Any) -> None:
    with contextlib.suppress(Exception):
        await manager.interrupt_kernel()


@dataclass
class CellMessages:
    """A cell's messages from its kernel, taken one at a time: what each gives to send,
    and what the cell keeps for its result. It sends nothing itself.

    The kernel runs research code: its messages are checked like anything else, and
    those of another cell or in another form are passed over.
    """

    msg_id: str | None
    limit: int
    """Bytes of rich output the cell may return."""
    captures: dict[str, Capture]
    outputs: list[Message]
    execution_count: int | None = None
    error: Message | None = None
    """The cell's exception, as the kernel reported it."""
    idle: bool = False
    """The kernel is idle again: the cell is over, and later messages are not its own."""
    spent: int = 0
    omitted: int = 0

    def take(self, message: object) -> list[Message]:
        """The events ``message`` gives, in order: ``stream`` steps with ``name`` and
        ``text``, and ``output`` steps with ``output``, already among ``outputs``."""
        if not isinstance(message, dict):
            return []
        message = cast(Message, message)
        header = message.get("parent_header")
        if not isinstance(header, dict) or cast(Message, header).get("msg_id") != self.msg_id:
            return []
        kind, content = message.get("msg_type"), message.get("content")
        if not isinstance(content, dict):
            return []
        content = cast(Message, content)
        if kind == "status" and content.get("execution_state") == "idle":
            self.idle = True
            return []
        if kind == "stream":
            capture = self.captures.get(str(content.get("name", "stdout")), self.captures["stdout"])
            data = str(content.get("text", "")).encode("utf-8", "replace")
            text = capture.feed(data)
            return [{"event": "stream", "name": capture.name, "text": text}] if text else []
        if kind == "execute_input":
            self.execution_count = _count(content.get("execution_count"))
        elif kind in ("display_data", "execute_result"):
            return self._output(kind, content)
        elif kind == "error":
            self.error = _exception(content)
        return []

    def _output(self, kind: object, content: Message) -> list[Message]:
        data = _mime_bundle(content.get("data", {}))
        size = sum(len(value.encode("utf-8", "replace")) for value in data.values())
        self.spent += size
        if self.spent > self.limit or len(self.outputs) >= MAX_OUTPUTS:
            self.omitted += 1
            if self.omitted > 1:
                return []  # one note at the end stands for the rest
            data = {"text/plain": f"[output of {size} bytes omitted: over the limit]"}
        output: Message = {
            "type": "result" if kind == "execute_result" else "display",
            "data": data,
            "execution_count": _count(content.get("execution_count")),
        }
        self.outputs.append(output)
        return [{"event": "output", "output": output}]

    def closing_note(self) -> Message | None:
        """The note for outputs omitted after the first, added to ``outputs``, if any."""
        if self.omitted <= 1:
            return None
        note: Message = {
            "type": "display",
            "data": {"text/plain": f"[{self.omitted - 1} more outputs omitted: over the limit]"},
            "execution_count": None,
        }
        self.outputs.append(note)
        return note


def _exception(content: Message) -> Message:
    """A kernel's ``error`` message as the cell's exception, bounded."""
    name = bounded(str(content.get("ename") or ""), 256)
    value = bounded(str(content.get("evalue") or ""), MAX_ERROR_TEXT)
    lines = content.get("traceback")
    lines = cast(list[object], lines)[-MAX_TRACEBACK_LINES:] if isinstance(lines, list) else []
    return {
        "kind": "exception",
        "message": f"{name}: {value}",
        "name": name,
        "value": value,
        "traceback": [bounded(_ANSI_RE.sub("", str(line)), MAX_TRACEBACK_LINE) for line in lines],
    }


def cell_outcome(
    status: str,
    execution_count: int | None,
    error: Message | None,
    *,
    cancelled: bool,
    timed_out: bool,
    timeout: float,
) -> tuple[str, Message]:
    """A finished cell's state and result, from the kernel's reply ``status`` and what its
    messages said."""
    result: Message = {"status": status, "execution_count": execution_count}
    if cancelled or timed_out:
        kind = "cancelled" if cancelled else "timed_out"
        reason = "interrupted on request" if cancelled else f"exceeded the {timeout:g} s limit"
        interruption: Message = {"kind": kind, "message": reason}
        if error is not None:
            # The kernel's own account of the interrupt: KeyboardInterrupt in Python.
            interruption.update(
                message=f"{reason}: {error['name']}",
                name=error["name"],
                value=error["value"],
                traceback=error["traceback"],
            )
        result["error"] = interruption
        return kind, result
    if error is not None:
        # A cell that raised: its exception, as the kernel reported it.
        result["error"] = error
        return "failed", result
    return "succeeded", result


class Executor:
    """Handles one connection's requests. ``root`` holds sessions/ and shared/."""

    def __init__(
        self,
        root: Path,
        send: Send,
        *,
        subreaper: bool = False,
        protected: bool = False,
        kernel_idle: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.root = root
        self.clock: Clock = clock or _SystemClock()
        self.captures = capture.Pool(root)
        self.owner_prefix = ""
        self.kernel_idle = kernel_idle
        """Stop a kernel nobody has used for this many seconds; None keeps them."""
        self.reaper: asyncio.Task[None] | None = None
        self.send = send
        self.subreaper = subreaper
        self.protected = protected
        """Whether this process adopts orphans, so leftovers are found below it."""
        self.running: dict[str, Running] = {}
        self.kernels: dict[tuple[str, str, str], Kernel] = {}
        """By session, run (empty for the session's own) and language."""
        self.ended_runs: dict[str, dict[str, None]] = {}
        """By session, the runs whose kernels were closed, oldest first."""
        self.tasks: set[asyncio.Task[None]] = set()
        self.launch_controls: set[asyncio.Task[Any]] = set()
        self.configured: dict[str, str] = {}
        """Variables from ``configure``, added to every command's and kernel's environment."""
        self.closing = False
        self.package_gate_id = "gate-" + secrets.token_hex(12)
        self.package_generation = -1
        self.package_mode = "hold"
        self.package_operation = ""
        self.package_revision: int | None = None
        self.package_launch = asyncio.Lock()
        """Shutting down: attempts it stops are not reported as ended. The worker
        reports them lost when this connection closes, since it stopped them."""
        # Package commands (docs/specs/conda-provisioning.md, CP-4, CP-11, CP-12).
        self.packages = provisioning.State(root / ".executor" / "provisioning")
        self.places = provisioning.Places.here()
        self.provisioning: provisioning.Config | None = None
        """None when the wrappers could not be written: package commands then run as they are."""
        self.wrapped: list[str] = []
        self.observing = asyncio.Lock()
        """Held while looking at the environment and reporting what changed, so that the
        worker learns of each revision before any result that shows it."""
        self.owners: dict[str, list[tuple[float, str]]] = {}
        """The attempts each call or kernel ran, with when they started, by its OWNER mark."""
        self.reported = ""
        self.package_epoch = 0
        """The packages' fingerprint as the worker knows it, from ``hello`` and the
        ``revision`` events since."""
        try:
            config = provisioning.Config(sys.executable, str(PROVISIONING), sys.prefix, self.places)
            self.wrapped = provisioning.refresh(self.packages, config, self.tool_path())
            self.reported = provisioning.baseline(self.packages, self.places)
            self.provisioning = config
        except OSError as exc:
            print(f"executor: package commands run without turns: {exc}", file=sys.stderr)
            self.reported = provisioning.fingerprint(self.places)

    # Plumbing.

    async def reply(self, request: Message, **payload: Any) -> None:
        await self.send({"type": "reply", "id": request.get("id"), "ok": True, **payload})

    async def fail(self, request: Message, error: ToolError) -> None:
        await self.send(
            {
                "type": "reply",
                "id": request.get("id"),
                "ok": False,
                "error": {"kind": error.kind, "message": error.message},
            }
        )

    async def event(self, execution: str, event: str, **payload: Any) -> None:
        await self.send({"type": "event", "execution": execution, "event": event, **payload})

    async def stream(self, execution: str, name: str, text: str) -> None:
        """Send live output, in events of at most STREAM_CHUNK characters."""
        for start in range(0, len(text), STREAM_CHUNK):
            chunk = text[start : start + STREAM_CHUNK]
            await self.event(execution, "stream", name=name, text=chunk)

    def spawn(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def handle(self, request: Message) -> None:
        handlers: dict[str, Callable[[Message], Awaitable[None]]] = {
            "hello": self.op_hello,
            "probe": self.op_probe,
            "configure": self.op_configure,
            "package_gate": self.op_package_gate,
            "shell": self.op_shell,
            "job": self.op_shell,
            "job_status": self.op_job_status,
            "cell": self.op_cell,
            "pdf": self.op_pdf,
            "cancel": self.op_cancel,
            "restart": self.op_restart,
            "close_session": self.op_close_session,
            "usage": self.op_usage,
            "kernels": self.op_kernels,
            "read": self.op_read,
            "capture_read": self.op_capture_read,
            "capture_cancel": self.op_capture_cancel,
            "write": self.op_write,
            "list": self.op_list,
            "lockfile": self.op_lockfile,
            "shared": self.op_shared,
            "remove": self.op_remove,
            "move": self.op_move,
            "skills_check": self.op_skills_check,
            "skills_put": self.op_skills_put,
        }
        handler = handlers.get(str(request.get("op")))
        try:
            if handler is None:
                raise ToolError("invalid_request", f"unknown operation {request.get('op')!r}")
            await handler(request)
        except ToolError as error:
            await self.fail(request, error)
        except Exception as exc:  # every request gets a reply
            message = bounded(f"{type(exc).__name__}: {exc}", 2000)
            await self.fail(request, ToolError("internal", message))

    async def shutdown(self) -> None:
        """Stop every command and kernel, then anything still running below this process."""
        await _owned(self.finish_shutdown(asyncio.current_task()))

    async def finish_shutdown(self, requester: asyncio.Task[Any] | None) -> None:
        self.closing = True
        await self.captures.close()
        if self.reaper is not None:
            self.reaper.cancel()
        running = list(self.running.values())
        await asyncio.gather(
            *(self.terminate(item) for item in running if item.process is not None),
            return_exceptions=True,
        )
        for item in running:
            item.cancel_requested = True
            if item.task is not None:
                item.task.cancel()
        await asyncio.gather(
            *(item.task for item in running if item.task is not None), return_exceptions=True
        )
        controls = [
            task for task in self.launch_controls if task not in (requester, asyncio.current_task())
        ]
        for task in controls:
            task.cancel()
        await asyncio.gather(*controls, return_exceptions=True)
        await asyncio.gather(
            *(kernel.stop() for kernel in self.kernels.values()), return_exceptions=True
        )
        await asyncio.to_thread(sandbox_processes.end_descendants)

    # Paths and environment.

    def session(self, request: Message) -> str:
        return self.session_name(request.get("session"))

    def bindings(self, request: Message, env: Mapping[str, str]) -> dict[str, str]:
        """The skill bindings an execution adds to the environment's variables ``env``,
        none of which a binding may name (SK-16)."""
        raw = request.get("bindings", {})
        if not isinstance(raw, dict):
            raise ToolError("invalid_request", "bindings must be an object")
        found: dict[str, str] = {}
        for name, value in cast(dict[object, object], raw).items():
            if not (isinstance(name, str) and _BINDING_RE.fullmatch(name)):
                raise ToolError("invalid_request", f"invalid variable name {name!r}")
            if name in env:
                raise ToolError("invalid_request", f"{name} is the environment's own variable")
            if not (isinstance(value, str) and _BINDING_VALUE_RE.fullmatch(value)):
                raise ToolError("invalid_request", f"invalid value for {name}")
            found[name] = value
        return found

    def session_name(self, session: object) -> str:
        if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
            raise ToolError("invalid_request", f"invalid session {session!r}")
        return session

    def session_dir(self, session: object) -> Path:
        return self.root / "sessions" / self.session_name(session)

    def split(self, session: object, path: object) -> tuple[list[str], list[str]]:
        """A workspace path's base (its directories below the root) and the rest of it."""
        if not isinstance(path, str):
            raise ToolError("invalid_request", "path must be a string")
        sandbox_files.check_relative(path)
        if path == SHARED or path.startswith(SHARED + "/"):
            return [SHARED], path.split("/")[1:]
        return ["sessions", self.session_name(session)], [] if path == "." else path.split("/")

    def open_base(self, base: Sequence[str]) -> int:
        """Open the session workspace or the shared area, creating it, through no symlink."""
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        return sandbox_files.walk(root, base, "/".join(base), create=True)

    def directory(self, session: object, path: object) -> Path:
        """A workspace directory's path, once it is known to exist with no symlink on the way."""
        base, rest = self.split(session, path)
        os.close(sandbox_files.walk(self.open_base(base), rest, str(path), create=False))
        return self.root.joinpath(*base, *rest)

    def snapshot(self, session: str) -> tuple[dict[str, tuple[int, int, int]], bool]:
        """The regular files of the session workspace and the shared area.

        Maps each path to its modification time, size and inode number. No
        symlink is followed, to the areas themselves included.
        """
        files = sandbox_files.SnapshotFiles()
        for area, prefix, base in (
            ("session", "", ["sessions", session]),
            ("shared", SHARED + "/", [SHARED]),
        ):
            try:
                directory = self.open_base(base)
            except (OSError, ToolError):
                files.error = True
                continue
            if sandbox_files.scan_files(directory, prefix, files, area=area):
                files.finished_ns = time.monotonic_ns()
                return files, True
        files.finished_ns = time.monotonic_ns()
        return files, False

    async def written(
        self, session: str, before: tuple[dict[str, tuple[int, int, int]], bool]
    ) -> Message:
        """The files created, changed or deleted since ``before``, for the attempt's
        result. Deletions are known only when both walks covered both areas whole."""
        after = await asyncio.to_thread(self.snapshot, session)
        changed = sorted(path for path, stamp in after[0].items() if before[0].get(path) != stamp)
        names = [sandbox_files.printable_path(path) for path in changed[:MAX_WRITTEN]]
        whole = not (
            before[1]
            or after[1]
            or getattr(before[0], "error", False)
            or getattr(after[0], "error", False)
        )
        deleted = sorted(path for path in before[0] if path not in after[0]) if whole else []
        gone = [sandbox_files.printable_path(path) for path in deleted[:MAX_WRITTEN]]
        try:
            observations = sandbox_files.observation_report(before[0], after[0])
        except Exception:
            observations = None  # derived metadata must never change research completion
        return {
            "written_files": [name for name in names if len(name.encode()) <= MAX_PATH_BYTES],
            "written_files_truncated": before[1] or after[1] or len(changed) > MAX_WRITTEN,
            "deleted_files": [name for name in gone if len(name.encode()) <= MAX_PATH_BYTES],
            "deleted_files_truncated": not whole or len(deleted) > MAX_WRITTEN,
            "output_observations": observations,
        }

    def tool_path(self) -> str:
        """``PATH`` for commands and kernels, before the wrappers join it."""
        return tool_environment(os.environ, sys.executable, sys.prefix)["PATH"]

    def environment(self, session: str) -> dict[str, str]:
        env = tool_environment(os.environ, sys.executable, sys.prefix)
        env.update(self.configured)
        if self.provisioning is not None:
            env.update(provisioning.variables(self.packages, env, self.wrapped))
        env["SANDBOX_SCIENCE_DAEMON_MARK"] = self.owner_prefix
        env["SESSION_WORKSPACE"] = str(self.session_dir(session))
        env["PROJECT_SHARED"] = str(self.root / SHARED)
        return env

    # Package commands: attempts wait while one changes the environment, and each change
    # reaches the worker as a ``revision`` event before any result that shows it.

    async def baseline(self) -> str:
        """The packages' fingerprint as this executor starts, which ``hello`` reports."""
        async with self.observing:
            previous = self.reported
            if self.provisioning is None:
                self.reported = await asyncio.to_thread(provisioning.fingerprint, self.places)
            else:
                self.reported = await asyncio.to_thread(
                    provisioning.baseline, self.packages, self.places
                )
            if previous and previous != self.reported:
                self.package_epoch += 1
            return self.reported

    async def begin(self, running: Running, timeout: float) -> str | None:
        """Wait while a package command changes the environment, then look at it (CP-12).

        Returns the packages' fingerprint as the attempt starts, after reporting what
        changed before it. Attempts already running are not held up. The wait counts
        against the attempt's time limit (``running.waited``): past it, _BusyError.
        """
        if self.provisioning is None:
            return None
        started = time.monotonic()
        delay = 0.02
        while True:
            try:
                held = self.packages.try_lock(exclusive=False)
            except OSError as exc:  # research code can break the state directory
                print(f"executor: cannot read the package lock: {exc}", file=sys.stderr)
                return None
            if held is not None:
                break
            if running.cancel_requested:
                raise _NotStartedError
            waited = time.monotonic() - started
            if waited >= timeout:
                raise _BusyError(waited, await asyncio.to_thread(self.packages.holding))
            await asyncio.sleep(min(delay, timeout - waited))
            delay = min(2 * delay, PACKAGE_POLL)
        running.waited = time.monotonic() - started
        try:
            await self.observing.acquire()
        except BaseException:
            os.close(held)
            raise
        try:
            descriptor: int | None = held
            for _ in range(MAX_LOOKS):
                # observe closes the descriptor, in its thread, however this task ends.
                observed = await asyncio.to_thread(
                    provisioning.observe, self.packages, self.places, self.reported, descriptor
                )
                descriptor = None
                await self.report(running.execution, observed, None)
                if observed.complete:
                    break
        except OSError as exc:
            print(f"executor: cannot read the package journal: {exc}", file=sys.stderr)
            return None
        finally:
            self.observing.release()
        # A manager installed since (pip install uv) gets its wrapper.
        try:
            self.wrapped = await asyncio.to_thread(
                provisioning.refresh, self.packages, self.provisioning, self.tool_path()
            )
        except OSError as exc:
            print(f"executor: cannot write the package wrappers: {exc}", file=sys.stderr)
        return self.reported

    async def settle(self, running: Running) -> Message:
        """Report what changed while an attempt ran: the result's ``environment`` fields.

        ``environment`` is the packages' fingerprint as the worker now knows them, and
        ``environment_changing`` says that a package command was still running.
        """
        if self.provisioning is None:
            return {"environment": await asyncio.to_thread(environment_fingerprint)}
        async with self.observing:
            observed = provisioning.Observation()
            try:
                for _ in range(MAX_LOOKS):
                    observed = await asyncio.to_thread(
                        functools.partial(
                            provisioning.observe,
                            self.packages,
                            self.places,
                            self.reported,
                            lockfile_wanted=True,
                        )
                    )
                    await self.report(running.execution, observed, running.execution)
                    if observed.complete:
                        break
            except OSError as exc:  # research code can break the state directory
                print(f"executor: cannot read the package lock: {exc}", file=sys.stderr)
                return {"environment": await asyncio.to_thread(environment_fingerprint)}
            return {"environment": self.reported, "environment_changing": observed.busy is not None}

    async def report(
        self, execution: str, observed: provisioning.Observation, cause: str | None
    ) -> None:
        """Send a ``revision`` event, as part of ``execution``, for each change, in order.

        The records of one attempt's package commands, one after another, make one
        managed change, set against that attempt, with its last commands. A change before
        a record, or one no record accounts for, is unmanaged (CP-4), set against
        ``cause``: the attempt after which it was seen, if any. The last change of each
        attempt carries a lockfile, the packages as that attempt left them; the worker
        keeps one for each attempt. A record leaves the journal once its event is sent,
        so that one a cancelled attempt did not send is read again by the next. Hold
        ``observing``.
        """
        changes, spare = self.changes(observed, cause)
        self.packages.remove_records(spare)  # records that changed nothing
        last = {change.cause: index for index, change in enumerate(changes)}
        for index, change in enumerate(changes):
            raw = (
                change.lockfile
                if change.cause is not None and last[change.cause] == index
                else None
            )
            lockfile = cast(Message, raw) if isinstance(raw, dict) else None
            if lockfile is not None:
                size = len(json.dumps(lockfile, separators=(",", ":")))
                if size > MAX_LOCKFILE_EVENT:
                    print(f"executor: a lockfile of {size} bytes is too large", file=sys.stderr)
                    lockfile = None
            self.package_epoch += 1
            await self.event(
                execution,
                "revision",
                fingerprint=change.fingerprint,
                package_epoch=self.package_epoch,
                managed=change.managed,
                cause=change.cause,
                commands=change.commands[-MAX_COMMANDS:],
                omitted_commands=max(0, len(change.commands) - MAX_COMMANDS),
                lockfile=lockfile,
            )
            self.reported = change.fingerprint
            self.packages.remove_records(change.names)

    def changes(
        self, observed: provisioning.Observation, cause: str | None
    ) -> tuple[list[_Change], list[str]]:
        """The changes an observation shows, in order, and the records that changed nothing."""
        changes: list[_Change] = []
        spare: list[str] = []
        reported = self.reported
        for name, record in observed.records:
            if not provisioning.usable(record):
                spare.append(name)
                continue
            before, after = str(record["before"]), str(record["after"])
            if before != reported:  # changed meanwhile by something no record accounts for
                changes.append(_Change(before, False, cause))
                reported = before
            if after == reported:
                spare.append(name)
                continue
            command = {
                key: record.get(key)
                for key in ("command", "via", "exit_code", "signal", "started", "ended")
            }
            who = self.cause(record)
            if changes and changes[-1].managed and changes[-1].cause == who:
                change = changes[-1]
                change.fingerprint = after
                change.lockfile = record.get("lockfile")
            else:
                change = _Change(after, True, who, lockfile=record.get("lockfile"))
                changes.append(change)
            change.commands.append(command)
            change.names.append(name)
            reported = after
        if observed.fingerprint is not None and observed.fingerprint != reported:
            changes.append(_Change(observed.fingerprint, False, cause, lockfile=observed.lockfile))
        return changes, spare

    def own(self, owner: str, execution: str) -> None:
        """Remember that ``execution`` runs under ``owner``, a call's or a kernel's mark."""
        runs = self.owners.pop(owner, [])
        runs.append((time.time(), execution))
        self.owners[owner] = runs[-MAX_RUNS:]
        while len(self.owners) > MAX_OWNERS:
            del self.owners[next(iter(self.owners))]

    def cause(self, record: Message) -> str | None:
        """The attempt that ran a package command: the latest of its owner's to start before it."""
        owner, invoked = record.get("owner"), record.get("invoked")
        runs = self.owners.get(owner) if isinstance(owner, str) else None
        if not runs:
            return None
        if not isinstance(invoked, int | float) or isinstance(invoked, bool):
            return runs[-1][1]
        earlier = [execution for started, execution in runs if started <= invoked]
        return earlier[-1] if earlier else runs[0][1]

    # Operations.

    async def op_hello(self, request: Message) -> None:
        languages = available_languages()
        await self.reply(
            request,
            protocol=PROTOCOL,
            package_gate=self.package_gate_id,
            languages=languages,
            location={
                "hostname": socket.gethostname(),
                "system": platform.platform(),
                "python": sys.version.split()[0],
                "pid": os.getpid(),
                "uid": os.getuid(),
                "root": str(self.root),
            },
            subreaper=self.subreaper,
            not_dumpable=self.protected,
            features=available_features(),
            artifact_capture_protocol=capture.PROTOCOL,
            output_observation_protocol=1,
            kernel_revision_protocol=1,
            run_kernels=1,
            environment=await self.baseline(),
            package_epoch=self.package_epoch,
        )

    async def op_package_gate(self, request: Message) -> None:
        """ACK a hold only after every launch ordered before it has completed admission."""
        generation, mode = request.get("generation"), request.get("mode")
        operation, revision = request.get("operation"), request.get("revision")
        if (
            request.get("incarnation") != self.package_gate_id
            or type(generation) is not int
            or not 0 <= generation < 2**62
            or mode not in ("ready", "hold", "needs_recovery")
            or not isinstance(operation, str)
            or len(operation) > 128
            or (operation and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", operation) is None)
            or (mode == "ready" and operation)
            or (mode == "needs_recovery" and not operation)
            or (revision is not None and (type(revision) is not int or not 0 <= revision < 2**62))
        ):
            raise ToolError("invalid_request", "invalid package admission identity or state")
        async with self.package_launch:
            self.controller_present(request)
            existing = (
                self.package_generation,
                self.package_mode,
                self.package_operation,
                self.package_revision,
            )
            incoming = (generation, mode, operation, revision)
            if generation < self.package_generation or (
                generation == self.package_generation and incoming != existing
            ):
                raise ToolError("conflict", "stale package admission generation")
            self.package_generation, self.package_mode = generation, mode
            self.package_operation, self.package_revision = operation, revision
        await self.reply(
            request,
            incarnation=self.package_gate_id,
            generation=generation,
            mode=mode,
            operation=operation,
            revision=revision,
        )

    def controller_present(self, request: Message) -> None:
        guard = request.get("_admission_guard")
        if callable(guard) and not guard():
            raise ToolError("unavailable", "controller detached before admission")

    async def own_launch[T](self, running: Running, work: Awaitable[T]) -> T:
        """Keep launch serialization through actual work and repeated cancellation."""
        return await _owned(work, lambda: setattr(running, "cancel_requested", True))

    async def start_kernel(
        self, running: Running, kernel: Kernel, timeout: float, work: Callable[[], Awaitable[None]]
    ) -> None:
        async with self.launch(running, timeout):
            await self.own_launch(running, self.kernel_birth(running, kernel, timeout, work))

    async def kernel_birth(
        self, running: Running, kernel: Kernel, timeout: float, work: Callable[[], Awaitable[None]]
    ) -> None:
        """Hold managed linking out through actual kernel startup/readiness (CP-12).

        Direct package-record writes can bypass this lock. A fingerprint is an
        observation of managed package state, never proof of all loaded code.
        """
        held: int | None = None
        fingerprint: str | None = None
        epoch: int | None = None
        try:
            if self.provisioning is not None:
                while held is None:
                    if running.cancel_requested:
                        raise _NotStartedError
                    if time.monotonic() - running.admitted >= timeout:
                        raise _BusyError(time.monotonic() - running.admitted, None)
                    try:
                        held = self.packages.try_lock(exclusive=False)
                    except OSError:
                        break  # damaged lock: start is explicitly unknown
                    if held is None:
                        await asyncio.sleep(0.05)
                if held is not None:
                    async with self.observing:
                        if running.kind == "cell":
                            for _ in range(MAX_LOOKS):
                                observed = await asyncio.to_thread(
                                    provisioning.observe,
                                    self.packages,
                                    self.places,
                                    self.reported,
                                    os.dup(held),
                                )
                                await self.report(running.execution, observed, None)
                                if observed.complete:
                                    fingerprint = observed.fingerprint
                                    break
                        else:
                            # Restart has no execution event stream. Do not consume
                            # another call's package journal or fabricate its cause.
                            fingerprint = await asyncio.to_thread(
                                provisioning.fingerprint, self.places
                            )
                        epoch = self.package_epoch if fingerprint == self.reported else None
            if running.cancel_requested:
                raise _NotStartedError
            if self.closing or (
                (running.kind == "restart" or running.package_operation)
                and running.controller
                and not running.controller()
            ):
                raise ToolError(
                    "unavailable", "controller detached or executor closing before kernel startup"
                )
            await work()
            kernel.birth_id = "birth-" + secrets.token_hex(12)
            kernel.start_environment = fingerprint
            kernel.start_package_epoch = epoch
            kernel.start_execution = running.execution if running.kind == "cell" else None
        finally:
            if held is not None:
                os.close(held)

    @contextlib.asynccontextmanager
    async def launch(self, running: Running, timeout: float):
        """Wait without retaining the launch lock; serialize the final launch with hold ACK."""
        while True:
            await self.package_launch.acquire()
            if self.closing:
                self.package_launch.release()
                raise ToolError("unavailable", "executor is closing; new launch refused")
            if (
                (running.package_operation or running.kind == "restart")
                and running.controller
                and not running.controller()
            ):
                self.package_launch.release()
                raise ToolError("unavailable", "provisioning controller detached before launch")
            if self.package_mode == "needs_recovery":
                self.package_launch.release()
                raise ToolError("unavailable", "environment needs_recovery; new work is refused")
            exempt = (
                running.package_operation
                and running.package_operation == self.package_operation
                and running.package_generation == self.package_generation
            )
            if self.package_mode == "ready" or exempt:
                break
            self.package_launch.release()
            if running.cancel_requested:
                raise _NotStartedError
            if time.monotonic() - running.admitted >= timeout:
                raise _BusyError(time.monotonic() - running.admitted, None)
            await asyncio.sleep(0.05)
        try:
            if running.cancel_requested:
                raise _NotStartedError
            running.waited = max(running.waited, time.monotonic() - running.admitted)
            if running.waited >= timeout:
                raise _BusyError(running.waited, None)
            yield
        finally:
            self.package_launch.release()

    async def op_probe(self, request: Message) -> None:
        """Describe this sandbox, and say whether each of the worker's sentinel paths shows here."""
        raw: object = request.get("sentinels", [])
        items = cast(list[object], raw) if isinstance(raw, list) else []
        paths = [item for item in items if isinstance(item, str)]
        if not isinstance(raw, list) or len(paths) != len(items) or len(paths) > MAX_SENTINELS:
            raise ToolError("invalid_request", "sentinels must be a short list of paths")
        facts = await asyncio.to_thread(sandbox_setup.probe_facts)
        seen = {path: await asyncio.to_thread(sandbox_setup.sentinel_state, path) for path in paths}
        await self.reply(request, facts=facts, sentinels=seen)

    async def op_configure(self, request: Message) -> None:
        """Write configuration files, and add variables to every command's and kernel's environment.

        The worker sends the package clients' configuration this way. Files go
        in ``.executor/config`` or the home directory, and a file's ``variable``
        names it. A file that differs from what this op last wrote there was
        changed by someone else, or was never written by it: it is kept, and
        the reply says so.
        """
        raw_files, raw_env = request.get("files", []), request.get("env", {})
        if not isinstance(raw_files, list) or not isinstance(raw_env, dict):
            raise ToolError("invalid_request", "files must be a list and env an object")
        env: dict[str, str] = {}
        for name, value in cast(dict[object, object], raw_env).items():
            if not (isinstance(name, str) and _ENV_NAME_RE.fullmatch(name)):
                raise ToolError("invalid_request", f"invalid variable name {name!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ToolError("invalid_request", f"invalid value for {name}")
            env[name] = value
        state = self.root / ".executor"
        files: list[tuple[Path, str, str | None]] = []
        for item in cast(list[object], raw_files):
            fields = cast(dict[str, object], item) if isinstance(item, dict) else {}
            base, path, content = fields.get("base"), fields.get("path"), fields.get("content")
            variable = fields.get("variable")
            if base not in ("config", "home") or not isinstance(content, str):
                raise ToolError("invalid_request", "a file needs a base and string content")
            if variable is not None and not (
                isinstance(variable, str) and _ENV_NAME_RE.fullmatch(variable)
            ):
                raise ToolError("invalid_request", f"invalid variable name {variable!r}")
            if not isinstance(path, str) or path == ".":
                raise ToolError("invalid_request", f"invalid path {path!r}")
            directory = state / "config" if base == "config" else Path.home()
            files.append((directory / sandbox_files.check_relative(path), content, variable))
        written, skipped, pointers = await asyncio.to_thread(
            sandbox_setup.write_config_files, state, files
        )
        self.configured = {**env, **pointers}
        await self.reply(request, written=written, skipped=skipped)

    def accept(self, request: Message, kind: str) -> Running:
        execution = request.get("execution")
        if not isinstance(execution, str) or not execution:
            raise ToolError("invalid_request", "missing execution id")
        if execution in self.running:
            raise ToolError("conflict", f"execution {execution} is already running")
        running = Running(execution=execution, kind=kind)
        operation = request.get("package_operation", "")
        generation = request.get("package_generation", -1)
        if not isinstance(operation, str) or type(generation) is not int:
            raise ToolError("invalid_request", "invalid internal package admission exemption")
        running.package_operation, running.package_generation = operation, generation
        guard = request.get("_admission_guard")
        if callable(guard):
            running.controller = lambda: bool(guard())
        if self.owner_prefix:
            running.owner = self.owner_prefix + secrets.token_hex(8)
        self.running[execution] = running
        return running

    async def op_pdf(self, request: Message) -> None:
        """Run the PDF helper as its own process, like a shell call's argv, in the session."""
        if "pdf" not in available_features():
            raise ToolError("unsupported", "pdf_oxide is not installed in this environment")
        await self.op_shell({**request, "argv": pdf_argv(request), "cwd": "."})

    async def op_shell(self, request: Message) -> None:
        session = self.session(request)
        try:
            cwd = self.directory(session, request.get("cwd", "."))
        except ToolError as error:
            if error.kind != "not_found":
                raise
            raise ToolError(
                "invalid_request", f"working directory {request.get('cwd')!r} does not exist"
            ) from None
        raw_argv: object = request.get("argv")
        if raw_argv is None:
            raw_argv = ["/bin/bash", "-c", str(request.get("script", ""))]
        if not isinstance(raw_argv, list):
            raise ToolError("invalid_request", "argv must be a list of strings")
        argv = [item for item in cast(list[object], raw_argv) if isinstance(item, str)]
        if not argv or len(argv) != len(cast(list[object], raw_argv)):
            raise ToolError("invalid_request", "argv must be a non-empty list of strings")
        is_job = request.get("op") == "job"
        execution = str(request.get("execution", ""))
        if is_job and re.fullmatch(r"exe-[0-9a-f]{24}", execution) is None:
            raise ToolError("invalid_request", "invalid job execution id")
        env = self.environment(session)
        bindings = self.bindings(request, env)
        running = self.accept(request, "job" if is_job else "shell")
        running.session = session
        await self.reply(request)
        running.task = self.spawn(
            self.run_shell(
                running,
                argv,
                cwd,
                bindings | env,
                float(request.get("timeout", 120)),
                int(request.get("limit", 1024 * 1024)),
            )
        )

    async def run_shell(
        self,
        running: Running,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        limit: int,
    ) -> None:
        execution = running.execution
        started = time.monotonic()
        wall_started = time.time()
        captures = {name: Capture(name, limit) for name in ("stdout", "stderr")}
        running.captures = captures
        logs: dict[str, int] = {}
        readers: list[asyncio.Task[None]] = []
        state, error, exit_code, signal_number = "failed", None, None, None
        written: Message = {}
        session = env["SESSION_WORKSPACE"].rsplit("/", 1)[-1]
        not_started = {"kind": "cancelled", "message": "cancelled before it started"}
        try:
            if running.cancel_requested:
                state, error = "cancelled", not_started
                return
            try:
                start = await self.begin(running, timeout)
            except _NotStartedError:
                state, error = "cancelled", not_started
                return
            except _BusyError as busy:
                state, error = "timed_out", busy.error()
                return
            before = await asyncio.to_thread(self.snapshot, session)
            if running.cancel_requested:
                state, error = "cancelled", not_started
                return
            if running.kind == "job":
                logs = await asyncio.to_thread(self.job_logs, session, execution)
            self.own(running.owner, execution)
            # The process is the executor's child before its pid is in spawned.
            with sandbox_processes.starting_children():
                try:
                    async with self.launch(running, timeout):
                        process = await self.own_launch(
                            running,
                            asyncio.create_subprocess_exec(
                                *argv,
                                cwd=cwd,
                                env={**env, sandbox_processes.OWNER: running.owner},
                                stdin=asyncio.subprocess.DEVNULL,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                start_new_session=True,
                            ),
                        )
                        running.process = process
                        running.started = True
                except OSError as exc:
                    error = {"kind": "invalid_request", "message": f"cannot run {argv[0]!r}: {exc}"}
                    return
                sandbox_processes.spawned.add(process.pid)
            info = sandbox_processes.process_info(process.pid)
            running.leader = (process.pid, info.start) if info is not None else None
            running.process = process
            running.started = True
            await self.event(execution, "state", **_running(start))
            readers = [
                asyncio.ensure_future(
                    self.pump(execution, process.stdout, captures["stdout"], logs.get("stdout"))
                ),
                asyncio.ensure_future(
                    self.pump(execution, process.stderr, captures["stderr"], logs.get("stderr"))
                ),
            ]
            for reader in readers:
                reader.add_done_callback(functools.partial(self.log_done, running))
            if running.cancel_requested:  # asked for while the command was starting
                self.spawn(self.terminate(running))
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), max(0.0, timeout - running.waited))
            except TimeoutError:
                timed_out = True
                await self.terminate(running)
            # What the command left running may not outlive it, and would hold its pipes.
            await end_and_collect(running.owner, running.leader)
            _, pending = await asyncio.wait(readers, timeout=5)
            for reader in pending:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            written = await self.written(session, before)
            written.update(await self.settle(running))
            code = process.returncode
            exit_code = code if code is not None and code >= 0 else None
            signal_number = -code if code is not None and code < 0 else None
            if running.log_error is not None:
                state = "failed"
                error = {"kind": "internal", "message": running.log_error}
            elif running.cancel_requested:
                state = "cancelled"
                error = {"kind": "cancelled", "message": "cancelled on request"}
            elif timed_out:
                state = "timed_out"
                error = {"kind": "timed_out", "message": f"exceeded the {timeout:g} s limit"}
            else:
                state = "succeeded" if code == 0 else "failed"
        except ToolError as exc:
            error = {"kind": exc.kind, "message": exc.message}
        except _BusyError as exc:
            state, error = "timed_out", exc.error()
        except _NotStartedError:
            state, error = "cancelled", not_started
        except OSError as exc:
            error = {
                "kind": "internal",
                "message": f"job output could not be saved ({type(exc).__name__}); "
                "whole logs may be incomplete",
            }
            if running.process is not None:
                await self.terminate(running)
                await end_and_collect(running.owner, running.leader)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            for descriptor in logs.values():
                os.close(descriptor)
            if running.process is not None:
                sandbox_processes.spawned.discard(running.process.pid)
            self.running.pop(execution, None)
            observations = written.pop("output_observations", None)
            result = {
                "exit_code": exit_code,
                "signal": signal_number,
                "stdout": captures["stdout"].summary(),
                "stderr": captures["stderr"].summary(),
                "error": error,
                "elapsed_seconds": time.monotonic() - started,
                "sandbox_started": wall_started,
                "sandbox_ended": time.time(),
                **written,
                "running_kernels": await self.session_kernels(session),
            }
            if not self.closing:
                await self.event(
                    execution, "end", state=state, result=result, output_observations=observations
                )

    async def pump(
        self,
        execution: str,
        stream: asyncio.StreamReader | None,
        capture: Capture,
        log: int | None = None,
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            text = capture.feed(chunk)
            if log is not None:
                write = asyncio.create_task(asyncio.to_thread(write_log, log, chunk))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    await write
                    raise
            if text:
                await self.stream(execution, capture.name, text)

    def job_logs(self, session: str, execution: str) -> dict[str, int]:
        """Create two private regular files through held, symlink-free directory FDs."""
        parent = sandbox_files.walk(
            self.open_base(["sessions", session]), [".jobs"], ".jobs", create=True
        )
        descriptors: dict[str, int] = {}
        try:
            # A preexisting job path is never reused, even if research code planted it.
            os.mkdir(execution, mode=0o700, dir_fd=parent)
            directory = os.open(execution, sandbox_files.DIRECTORY_FLAGS, dir_fd=parent)
            try:
                for name in ("stdout", "stderr"):
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK
                        | os.O_CLOEXEC
                    )
                    descriptor = os.open(f"{name}.log", flags, 0o600, dir_fd=directory)
                    descriptors[name] = descriptor
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("job log is not a regular file")
            finally:
                os.close(directory)
        except BaseException:
            for descriptor in descriptors.values():
                os.close(descriptor)
            raise
        finally:
            os.close(parent)
        return descriptors

    def log_done(self, running: Running, task: asyncio.Task[None]) -> None:
        """An exhausted disk ends the command promptly instead of abandoning its pipes."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and running.log_error is None:
            running.log_error = "job output could not be saved; whole logs may be incomplete"
            self.spawn(self.terminate(running))

    async def op_job_status(self, request: Message) -> None:
        execution = request.get("execution")
        if type(execution) is not str or re.fullmatch(r"exe-[0-9a-f]{24}", execution) is None:
            raise ToolError("invalid_request", "invalid child execution identity")
        running = self.running.get(execution)
        if running is None or running.kind != "job":
            await self.reply(request, found=False)
            return
        cpu, memory = await asyncio.to_thread(
            sandbox_processes.job_usage, running.owner, running.leader
        )
        streams: dict[str, Message] = {}
        for name, capture in running.captures.items():
            summary = capture.summary()
            text = str(summary["text"])
            summary["text"] = bounded(text, 16384)
            summary["truncated"] = bool(summary["truncated"]) or len(text) > 16384
            streams[name] = summary
        await self.reply(request, found=True, cpu_seconds=cpu, memory_bytes=memory, **streams)

    async def terminate(self, running: Running) -> None:
        """SIGTERM to the command and what it started, then SIGKILL after a grace period.

        A package command's wrapper passes its SIGTERM on, so the command gets one only.
        The grace is for every process of the call, not only its shell, which may end at
        once and leave the others; a package command still in its turn gets PACKAGE_GRACE.
        """
        process = running.process
        if process is None:
            return
        owner, leader = running.owner, running.leader
        await asyncio.to_thread(
            lambda: sandbox_processes.signal_processes(
                without_wrapped(sandbox_processes.owned_processes(owner, leader)), signal.SIGTERM
            )
        )
        started = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), TERM_GRACE)
        while True:
            left, in_turn = await asyncio.to_thread(self.leftovers, owner, leader)
            grace = PACKAGE_GRACE if in_turn else TERM_GRACE
            if not left or time.monotonic() - started >= grace:
                break
            await asyncio.sleep(0.1)
        if left or process.returncode is None:
            await end_and_collect(owner, leader)
        await process.wait()

    def leftovers(
        self, owner: str, leader: sandbox_processes.Leader | None
    ) -> tuple[list[sandbox_processes.Process], bool]:
        """The call's processes still running, and whether a package command among them
        holds its turn."""
        left = sandbox_processes.owned_processes(owner, leader)
        holder = (self.packages.holding() or {}).get("pid")
        return left, isinstance(holder, int) and any(info.pid == holder for info in left)

    def kernel(self, session: str, language: str, run: str | None = None) -> Kernel:
        """The session's kernel of ``language``, or the run's own, made on first use."""
        if language not in KERNEL_NAMES:
            raise ToolError("unsupported", f"no {language!r} kernel here")
        key = (session, run or "", language)
        if run is not None and run in self.ended_runs.get(session, {}):
            raise ToolError("conflict", f"run {run} has ended, and its kernels with it")
        if key not in self.kernels:
            in_use = {id(running.kernel) for running in self.running.values()}
            runs = {
                kernel.run
                for kernel in self.kernels.values()
                if kernel.session == session
                and kernel.run not in (None, run)
                and (kernel.manager is not None or id(kernel) in in_use)
            }
            if run is not None and len(runs) >= MAX_RUN_KERNELS:
                raise ToolError(
                    "unavailable",
                    f"{MAX_RUN_KERNELS} runs of this session have kernels of their own; "
                    "one must end first",
                )
            cwd = self.directory(session, ".")
            kernel = Kernel(
                session,
                language,
                cwd,
                self.environment(session),
                last_used=self.clock.monotonic(),
                run=run,
            )
            self.kernels[key] = kernel
        return self.kernels[key]

    def run_name(self, request: Message) -> str | None:
        """The run a request names for its own kernel; ``None`` for the session's."""
        run = request.get("run")
        if run is None:
            return None
        if not isinstance(run, str) or not _SESSION_RE.fullmatch(run):
            raise ToolError("invalid_request", f"invalid run {run!r}")
        return run

    async def op_cell(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(
            session, str(request.get("language", "python")), self.run_name(request)
        )
        code = request.get("code")
        if not isinstance(code, str):
            raise ToolError("invalid_request", "code must be a string")
        # The cell's bindings are the whole set the kernel's next start takes; a running
        # kernel keeps those it started with, and the result says so (SK-16).
        bindings = self.bindings(request, kernel.env)
        running = self.accept(request, "cell")
        running.kernel = kernel
        running.bindings = kernel.bindings = bindings
        await self.reply(request)
        running.task = self.spawn(
            self.run_cell(
                running,
                kernel,
                code,
                float(request.get("timeout", 120)),
                int(request.get("limit", 1024 * 1024)),
            )
        )

    async def run_cell(
        self, running: Running, kernel: Kernel, code: str, timeout: float, limit: int
    ) -> None:
        execution = running.execution
        started = time.monotonic()
        wall_started = time.time()
        captures = {name: Capture(name, limit) for name in ("stdout", "stderr")}
        outputs: list[Message] = []
        result: Message = {}
        state = "failed"
        ran_on: Message | None = None
        try:
            if running.cancel_requested:
                raise _NotStartedError
            running.queued = True
            async with kernel.lock:
                # Still queued while a package command changes the environment.
                start = await self.begin(running, timeout)
                running.queued = False
                async with kernel.control:
                    _not_retired(kernel)
                    await kernel.ensure_started(
                        lambda work: self.start_kernel(running, kernel, timeout, work)
                    )
                if running.cancel_requested:  # asked for while the kernel started
                    raise _NotStartedError
                self.own(kernel.owner, execution)
                ran_on = kernel.info()
                state_lost, kernel.state_lost = kernel.state_lost, False
                before = await asyncio.to_thread(self.snapshot, kernel.session)
                async with self.launch(running, timeout):
                    msg_id = kernel.client.execute(code, store_history=True, allow_stdin=False)
                    running.started = True
                if kernel.start_execution == execution:
                    start = kernel.start_environment
                await self.event(execution, "state", **_running(start), kernel=ran_on)
                state, result = await self.execute(
                    running, kernel, code, timeout, limit, captures, outputs, msg_id=msg_id
                )
                result["state_lost"] = state_lost or result.get("state_lost", False)
                if kernel.started_bindings != running.bindings:
                    result["bindings_kept"] = True
                result.update(await self.written(kernel.session, before))
                result.update(await self.settle(running))
        except (asyncio.CancelledError, _NotStartedError):
            state = "cancelled"
            result = {"error": {"kind": "cancelled", "message": "cancelled before it started"}}
        except _BusyError as busy:
            state = "timed_out"
            result = {"error": busy.error()}
        except ToolError as error:
            state = "failed"
            result = {"error": {"kind": error.kind, "message": error.message}}
        except Exception as exc:
            state = "failed"
            message = bounded(f"{type(exc).__name__}: {exc}", 2000)
            result = {"error": {"kind": "internal", "message": message}}
        finally:
            running.queued = False
            self.running.pop(execution, None)
            kernel.last_used = self.clock.monotonic()
            if ran_on is None and kernel.generation:
                ran_on = kernel.info()
            result.update(
                stdout=captures["stdout"].summary(),
                stderr=captures["stderr"].summary(),
                outputs=outputs,
                kernel=ran_on,
                running_kernels=await self.session_kernels(kernel.session),
                elapsed_seconds=time.monotonic() - started,
                sandbox_started=wall_started,
                sandbox_ended=time.time(),
            )
            if not self.closing:
                observations = result.pop("output_observations", None)
                await self.event(
                    execution, "end", state=state, result=result, output_observations=observations
                )

    async def execute(
        self,
        running: Running,
        kernel: Kernel,
        code: str,
        timeout: float,
        limit: int,
        captures: dict[str, Capture],
        outputs: list[Message],
        *,
        msg_id: str | None = None,
    ) -> tuple[str, Message]:
        from queue import Empty

        loop = asyncio.get_running_loop()
        # This generation's client and manager, even once a restart replaces the kernel's.
        client, manager = kernel.client, kernel.manager
        if msg_id is None:
            msg_id = client.execute(code, store_history=True, allow_stdin=False)
        deadline = loop.time() + max(0.0, timeout - running.waited)
        interrupted_at: float | None = None
        timed_out = False
        cell = CellMessages(msg_id, limit, captures, outputs)
        died: Message = {
            "status": "error",
            "error": {"kind": "kernel_died", "message": "the kernel process ended"},
        }
        while True:
            if running.restarted or running.closing:
                message = (
                    "the session was closed" if running.closing else "the kernel was restarted"
                )
                return "cancelled", {
                    "status": "aborted",
                    "error": {"kind": "cancelled", "message": message},
                    "state_lost": True,
                }
            now = loop.time()
            if not timed_out and now >= deadline:
                timed_out = True
                interrupted_at = now
                await _interrupt(manager)
            if interrupted_at is not None and now - interrupted_at > INTERRUPT_GRACE:
                # The kernel ignored the interrupt: restart it, losing its state.
                async with kernel.control:
                    if not (running.restarted or running.closing):
                        # A replacement is new work and must pass CP13 admission.
                        # Stop now; the next admitted cell starts a fresh kernel.
                        kernel.state_lost = True
                        await _owned(kernel.stop())
                kind = "timed_out" if timed_out else "cancelled"
                return kind, {
                    "status": "aborted",
                    "error": {
                        "kind": kind,
                        "message": (
                            "the kernel ignored interruption and was stopped; "
                            "the next admitted cell starts fresh"
                        ),
                    },
                    "state_lost": True,
                }
            if running.cancel_requested and interrupted_at is None:
                interrupted_at = now
                await _interrupt(manager)
            try:
                message = await client.get_iopub_msg(timeout=0.5)
            except Empty:
                if running.restarted or running.closing:
                    continue
                if not await _alive(manager):
                    kernel.state_lost = True
                    return "failed", died
                continue
            except (Exception, asyncio.CancelledError):
                # Its channels closed under it, which cancels a pending receive: a
                # restart, or a kernel gone away. Unless this task itself is cancelled.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if running.restarted or running.closing:
                    continue
                kernel.state_lost = True
                return "failed", died
            for step in cell.take(message):
                if step["event"] == "stream":
                    await self.stream(running.execution, step["name"], step["text"])
                else:
                    await self.event(running.execution, "output", output=step["output"])
            if cell.idle:
                break
        if (note := cell.closing_note()) is not None:
            await self.event(running.execution, "output", output=note)
        status = "error" if cell.error else "ok"
        with contextlib.suppress(Empty, Exception):
            reply = await client.get_shell_msg(timeout=5)
            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                reported = reply["content"].get("status")
                status = reported if reported in ("ok", "error", "aborted") else status
        return cell_outcome(
            status,
            cell.execution_count,
            cell.error,
            cancelled=running.cancel_requested,
            timed_out=timed_out,
            timeout=timeout,
        )

    async def op_cancel(self, request: Message) -> None:
        running = self.running.get(str(request.get("execution")))
        if running is None:
            await self.reply(request, found=False)
            return
        running.cancel_requested = True
        await self.reply(request, found=True)
        if running.kind in ("shell", "job"):
            if running.process is not None:
                self.spawn(self.terminate(running))
            # Before then, run_shell sees cancel_requested and never starts the command.
        elif running.queued and running.task is not None:
            running.task.cancel()  # waiting for its kernel: it leaves the queue
        # A cell starting or running sees cancel_requested itself: one starting never
        # runs, and a running one is interrupted by its loop.

    async def op_restart(self, request: Message) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.launch_controls.add(task)
        try:
            await self.restart_request(request)
        finally:
            if task is not None:
                self.launch_controls.discard(task)

    async def restart_request(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(
            session, str(request.get("language", "python")), self.run_name(request)
        )
        restart = Running("restart", "restart")
        restart.kernel = kernel
        guard = request.get("_admission_guard")
        if callable(guard):
            restart.controller = lambda: bool(guard())
        async with kernel.control:
            await self.package_launch.acquire()
            try:
                self.controller_present(request)
                if self.closing or self.package_mode != "ready":
                    raise ToolError(
                        "unavailable", "package admission is closed; kernel restart refused"
                    )
                # The running cell does not hold up a restart: it ends, cancelled.
                for running in self.running.values():
                    if running.kernel is kernel and running.started:
                        running.restarted = True
                if kernel.manager is not None:
                    await self.own_launch(restart, kernel.stop())
                    kernel.state_lost = True
            finally:
                self.package_launch.release()
            _not_retired(kernel)
            await kernel.ensure_started(lambda work: self.start_kernel(restart, kernel, 180, work))
        await self.reply(request, kernel=kernel.info())

    async def reap_idle_kernels(self, idle: float) -> None:
        """Stop kernels unused for ``idle`` seconds; the session's next cell reports lost state."""
        interval = max(0.2, min(30.0, idle / 2))
        while True:
            await self.clock.sleep(interval)
            for kernel in tuple(self.kernels.values()):
                if kernel.manager is None or self.clock.monotonic() - kernel.last_used < idle:
                    continue
                async with kernel.control:
                    busy = any(running.kernel is kernel for running in self.running.values())
                    if kernel.manager is None or busy:
                        continue
                    if self.clock.monotonic() - kernel.last_used < idle:
                        continue
                    await kernel.stop()
                    kernel.state_lost = True
                print(
                    f"executor: stopped kernel {kernel.kernel_id} after {idle:.0f} s unused",
                    file=sys.stderr,
                )

    async def op_close_session(self, request: Message) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.launch_controls.add(task)
        try:
            await _owned(self.close_session_request(request))
        finally:
            if task is not None:
                self.launch_controls.discard(task)

    async def close_session_request(self, request: Message) -> None:
        """Stop all session kernels; reply with the kernels it stopped.

        Its running cells end cancelled and its queued ones never start. Each kernel
        keeps its generation, so the session's next cell starts a new one and reports
        that earlier state is gone. Shell calls are not affected. Runs' own kernels are
        retired and forgotten, and their runs remembered as ended, so a late cell is
        refused rather than starting one again. A request naming a run stops only that
        run's own kernels; the session's kernels and jobs stay.
        """
        session = self.session(request)
        run = self.run_name(request)
        stopped: list[Message] = []
        jobs = [
            running
            for running in self.running.values()
            if running.kind == "job" and running.session == session and run is None
        ]
        for running in jobs:
            running.cancel_requested = True
        await asyncio.gather(
            *(self.terminate(running) for running in jobs if running.process is not None)
        )
        await asyncio.gather(
            *(running.task for running in jobs if running.task is not None), return_exceptions=True
        )
        for key, kernel in list(self.kernels.items()):
            if kernel.session != session or (run is not None and kernel.run != run):
                continue
            for running in self.running.values():
                if running.kernel is kernel:
                    running.cancel_requested = True
                    if running.started:
                        running.closing = True
            async with kernel.control:
                if kernel.manager is not None:
                    stopped.append(kernel.info())
                    await kernel.stop()
                    kernel.state_lost = True
                if kernel.run is not None:
                    kernel.retired = True
                    self.kernels.pop(key, None)
                    self.end_run(session, kernel.run)
        if run is not None:
            self.end_run(session, run)
        await self.reply(request, kernels=stopped)

    def end_run(self, session: str, run: str) -> None:
        """Remember ``run`` as ended, within ``MAX_ENDED_RUNS`` per session."""
        ended = self.ended_runs.setdefault(session, {})
        ended.pop(run, None)
        ended[run] = None
        while len(ended) > MAX_ENDED_RUNS:
            del ended[next(iter(ended))]

    async def session_kernels(self, session: str) -> list[Message]:
        """Only this session's live kernels, its own and its runs', in the order of their
        runs and then the fixed supported-language order."""
        found: list[Message] = []
        order = {language: number for number, language in enumerate(KERNEL_NAMES)}
        keys = sorted(
            (key for key in self.kernels if key[0] == session),
            key=lambda key: (key[1], order.get(key[2], len(order))),
        )
        for key in keys:
            kernel = self.kernels[key]
            if await kernel.alive():
                found.append(kernel.info())
        return found

    async def op_kernels(self, request: Message) -> None:
        await self.reply(request, kernels=await self.session_kernels(self.session(request)))

    async def op_usage(self, request: Message) -> None:
        """The CPU time used below this executor, and its running kernels and calls.

        The worker samples it to tell an environment whose kernels still compute,
        say in a background thread, from an idle one.
        """
        cpu = await asyncio.to_thread(sandbox_processes.cpu_seconds_below, os.getpid())
        kernels = sum(1 for kernel in self.kernels.values() if kernel.manager is not None)
        await self.reply(request, cpu_seconds=cpu, kernels=kernels, running=len(self.running))

    async def op_read(self, request: Message) -> None:
        path = request.get("path")
        offset = int(request.get("offset", 0))
        length = min(int(request.get("length", MAX_READ)), MAX_READ)
        if offset < 0 or length < 0:
            raise ToolError("invalid_request", "offset and length must not be negative")
        base, rest = self.split(request.get("session"), path)
        if not rest:
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        parent = sandbox_files.walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            # Without O_NONBLOCK, opening a FIFO would wait for a writer.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(rest[-1], flags, dir_fd=parent)
        except FileNotFoundError:
            raise ToolError("not_found", f"no file {path!r}") from None
        except OSError as exc:
            raise sandbox_files.path_refusal(parent, rest[-1], str(path), exc) from exc
        finally:
            os.close(parent)
        # In a thread: hashing a large file takes a while, and must not stall other work.
        hashed = request.get("hash", True) is not False
        reply = await asyncio.to_thread(
            sandbox_files.read_range, descriptor, offset, length, hashed, str(path)
        )
        await self.reply(request, **reply)

    async def op_capture_read(self, request: Message) -> None:
        """A bounded read/hash helper; ordinary file reads retain their legacy protocol."""
        data = {
            "session": request.get("session"),
            "path": request.get("path"),
            "offset": request.get("offset"),
            "length": request.get("length"),
            "max_bytes": request.get("max_bytes"),
            "hash": request.get("hash"),
        }
        try:
            with sandbox_processes.starting_children():
                result = await self.captures.read(
                    request.get("capture_id"), data, request.get("timeout")
                )
        except asyncio.CancelledError:
            await self.reply(
                request,
                capture_id=request.get("capture_id"),
                cleaned=True,
                capture_error={"kind": "cancelled", "message": "capture cancelled after cleanup"},
            )
            return
        except capture.CaptureError as exc:
            await self.reply(
                request,
                capture_id=request.get("capture_id"),
                cleaned=exc.cleaned,
                capture_error={"kind": exc.kind, "message": exc.message},
            )
            return
        await self.reply(request, **result, capture_id=request["capture_id"], cleaned=True)

    async def op_capture_cancel(self, request: Message) -> None:
        """End only the named capture helper, with cleanup acknowledged after reap."""
        capture_id = request.get("capture_id")
        if not isinstance(capture_id, str) or re.fullmatch(r"cap-[0-9a-f]{24}", capture_id) is None:
            raise ToolError("invalid_request", "invalid capture ID")
        await self.captures.cancel(capture_id)
        await self.reply(request, capture_id=capture_id, cleaned=True)

    def staged(self, request: Message) -> tuple[str, str]:
        source, snapshot = request.get("source"), request.get("snapshot")
        if not (isinstance(source, str) and _SOURCE_RE.fullmatch(source)):
            raise ToolError("invalid_request", f"invalid source {source!r}")
        if not (isinstance(snapshot, str) and _SHA256_RE.fullmatch(snapshot)):
            raise ToolError("invalid_request", "snapshot must be a SHA-256")
        return source, snapshot

    async def op_skills_check(self, request: Message) -> None:
        """Whether a pack is staged as the harness expects it, and with ``seal``, made
        read-only once it is (docs/specs/skills.md, SK-16)."""
        source, snapshot = self.staged(request)
        raw = request.get("files", [])
        items = cast(list[object], raw) if isinstance(raw, list) else []
        files = [cast(Message, item) for item in items if isinstance(item, dict)]
        if not isinstance(raw, list) or len(files) != len(items):
            raise ToolError("invalid_request", "files must be a list of objects")
        for item in files:
            sandbox_files.check_relative(str(item.get("path")))
        try:
            found = await asyncio.to_thread(
                sandbox_files.stage_check,
                self.root,
                source,
                snapshot,
                files,
                request.get("seal") is True,
            )
        except OSError as exc:
            raise sandbox_files.file_error(exc, f"{sandbox_files.STAGED}/{source}") from exc
        raw = request.get("commands", [])
        names = (
            [n for n in cast(list[object], raw) if isinstance(n, str)][:256]
            if isinstance(raw, list)
            else []
        )
        path = self.tool_path()
        found["commands"] = [
            n for n in names if _COMMAND_RE.fullmatch(n) and shutil.which(n, path=path)
        ]
        await self.reply(request, **found)

    async def op_skills_put(self, request: Message) -> None:
        """Write a batch of a pack's files where its snapshot is staged (SK-16)."""
        source, snapshot = self.staged(request)
        raw = request.get("files", [])
        files: list[tuple[str, bytes, bool]] = []
        for item in cast(list[object], raw) if isinstance(raw, list) else []:
            fields = cast(Message, item) if isinstance(item, dict) else {}
            path = sandbox_files.check_relative(str(fields.get("path")))
            try:
                content = base64.b64decode(str(fields.get("content_b64", "")), validate=True)
            except ValueError as exc:
                raise ToolError("invalid_request", f"{path}: content_b64 is not base64") from exc
            files.append((path, content, fields.get("executable") is True))
        try:
            written = await asyncio.to_thread(
                sandbox_files.stage_put,
                self.root,
                source,
                snapshot,
                request.get("reset") is True,
                files,
            )
        except OSError as exc:
            raise sandbox_files.file_error(exc, f"{sandbox_files.STAGED}/{source}") from exc
        await self.reply(request, written=written)

    async def op_write(self, request: Message) -> None:
        path = request.get("path")
        base, rest = self.split(request.get("session"), path)
        if not rest:
            raise ToolError("invalid_request", f"{path!r} is a directory")
        try:
            content = base64.b64decode(str(request.get("content_b64", "")), validate=True)
        except ValueError as exc:
            raise ToolError("invalid_request", "content_b64 is not base64") from exc
        expected = request.get("expected_sha256")
        create_only = bool(request.get("create_only", False))
        if base == [SHARED] and expected is None and not create_only:
            raise ToolError(
                "invalid_request", "writes to the shared area need expected_sha256 or create_only"
            )
        executable = request.get("executable")
        try:
            parent = sandbox_files.walk(self.open_base(base), rest[:-1], str(path), create=True)
            reply = await asyncio.to_thread(
                sandbox_files.write_file,
                parent,
                rest[-1],
                content,
                expected,
                create_only,
                str(path),
                executable if isinstance(executable, bool) else None,
                request.get("if_unused") is True,
            )
        except OSError as exc:
            raise sandbox_files.file_error(exc, str(path)) from exc
        await self.reply(request, **reply)

    async def op_shared(self, request: Message) -> None:
        """The shared area's files with their stamps, and the SHA-256 of some (D79).

        ``hash`` names the files whose contents the worker needs, hashed up to
        ``hash_limit`` bytes each and ``hash_budget`` bytes in all; larger ones are
        listed in ``too_large``, and the rest wait for the next scan. ``mark`` replaces
        the worker's mark of this environment, which the reply carries.
        """
        raw: object = request.get("hash", [])
        named = [path for path in cast(list[object], raw) if isinstance(path, str)]
        wanted = named[: sandbox_files.MAX_SNAPSHOT]
        limit = max(0, int(request.get("hash_limit", 0)))
        budget = max(0, int(request.get("hash_budget", 0)))
        mark = request.get("mark")
        reply = await asyncio.to_thread(
            self.shared_state,
            wanted,
            limit,
            budget,
            mark if isinstance(mark, str) else None,
        )
        await self.reply(request, **reply)

    def shared_state(
        self,
        wanted: Sequence[str],
        limit: int,
        budget: int,
        mark: str | None,
    ) -> Message:
        if mark is not None:
            sandbox_files.write_mark(self.root, mark[:256])
        current = sandbox_files.read_mark(self.root)
        files, truncated, partial, odd = sandbox_files.scan_shared(self.open_base([SHARED]))
        digests: dict[str, str] = {}
        too_large: list[str] = []
        for path in wanted:
            stamp = files.get(path)
            if stamp is None:
                continue
            if stamp[1] > limit:
                too_large.append(path)
                continue
            if stamp[1] > budget:
                continue  # hashed at a later scan
            try:
                _, rest = self.split(None, path)
                parent = sandbox_files.walk(self.open_base([SHARED]), rest[:-1], path, create=False)
            except (OSError, ToolError):
                continue
            try:
                digest = sandbox_files.stamped_sha256(parent, rest[-1], stamp)
            except (OSError, ToolError):
                continue  # gone or replaced meanwhile: compared again next time
            finally:
                os.close(parent)
            if digest is not None:
                digests[path] = digest
                budget -= stamp[1]
        reply: Message = {
            "files": files,
            "truncated": truncated,
            "partial": partial,
            "odd": odd,
            "sha256": digests,
            "too_large": too_large,
            "mark": current,
            "now": time.time_ns(),
        }
        return reply

    async def op_remove(self, request: Message) -> None:
        """Remove a regular file in the shared area if it still has the expected SHA-256.

        Directories the removal leaves empty are removed too, up to the shared area.
        """
        path = request.get("path")
        base, rest = self.split(request.get("session"), path)
        if base != [SHARED] or not rest:
            raise ToolError("invalid_request", "only files in the shared area are removed")
        expected = request.get("expected_sha256")
        if not isinstance(expected, str):
            raise ToolError("invalid_request", "removing a shared file needs expected_sha256")
        directories = [self.open_base(base)]
        try:
            for name in rest[:-1]:
                directories.append(
                    sandbox_files.walk(os.dup(directories[-1]), [name], str(path), create=False)
                )
            removed = await asyncio.to_thread(
                sandbox_files.remove_checked,
                directories[-1],
                rest[-1],
                expected,
                str(path),
                if_unused=request.get("if_unused") is True,
            )
            if not removed:
                await self.reply(request, removed=False, busy=True)
                return
            for depth in range(len(rest) - 1, 0, -1):
                try:
                    os.rmdir(rest[depth - 1], dir_fd=directories[depth - 1])
                except OSError:
                    break  # not empty, or not ours to remove
        except OSError as exc:
            raise sandbox_files.file_error(exc, str(path)) from exc
        finally:
            for directory in directories:
                os.close(directory)
        await self.reply(request, removed=True)

    async def op_move(self, request: Message) -> None:
        """Give a file in the shared area another name there, which must be free (D79).

        The file keeps its inode, so a process still writing to it writes under the new
        name: the sync moves an environment's version of a file to its conflict copy.
        """
        path, target = request.get("path"), request.get("target")
        base, rest = self.split(None, path)
        target_base, target_rest = self.split(None, target)
        if base != [SHARED] or target_base != [SHARED] or not rest or not target_rest:
            raise ToolError("invalid_request", "only files in the shared area are moved")
        source = sandbox_files.walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            destination = sandbox_files.walk(
                self.open_base(target_base), target_rest[:-1], str(target), create=True
            )
            try:
                stamp = await asyncio.to_thread(
                    sandbox_files.move_file,
                    source,
                    rest[-1],
                    destination,
                    target_rest[-1],
                    str(path),
                )
            except OSError as exc:
                raise sandbox_files.file_error(exc, str(path)) from exc
            finally:
                os.close(destination)
        finally:
            os.close(source)
        await self.reply(request, stamp=stamp)

    async def op_list(self, request: Message) -> None:
        path = request.get("path", ".")
        base, rest = self.split(request.get("session"), path)
        try:
            directory = sandbox_files.walk(self.open_base(base), rest, str(path), create=False)
        except ToolError as error:
            if error.kind == "not_found":
                raise ToolError("not_found", f"no directory {path!r}") from None
            raise
        limit = min(int(request.get("limit", MAX_LIST)), MAX_LIST)
        entries, truncated = await asyncio.to_thread(sandbox_files.list_directory, directory, limit)
        await self.reply(request, entries=entries, truncated=truncated)

    async def op_lockfile(self, request: Message) -> None:
        """The packages installed now: their fingerprint and lockfile (CP-18).

        The worker asks when its revision has no lockfile, as revision 0 has none at
        first. A package command still running after 30 s makes it ``unavailable``.
        """
        if self.provisioning is None:
            found = await asyncio.to_thread(
                lambda: (provisioning.fingerprint(self.places), provisioning.lockfile(self.places))
            )
        else:
            found = await asyncio.to_thread(
                provisioning.current, self.packages, self.places, LOCKFILE_WAIT
            )
        if found is None:
            raise ToolError("unavailable", "a package command is changing the environment")
        fingerprint, lockfile = found
        await self.reply(request, fingerprint=fingerprint, lockfile=lockfile)


def tool_environment(base: Mapping[str, str], python: str, prefix: str) -> dict[str, str]:
    """``base`` with the environment's tools first on PATH, as the tools spec requires.

    The executor runs with the environment's interpreter, so its directory holds the
    environment's commands; without it, Sage's interfaces could not find ``gp`` or ``gap``
    (lab 5). A Conda environment is also named in ``CONDA_PREFIX``.
    """
    env = dict(base)
    tools = os.path.dirname(python)
    rest = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != tools]
    env["PATH"] = os.pathsep.join([tools, *rest])
    if os.path.isdir(os.path.join(prefix, "conda-meta")):
        env["CONDA_PREFIX"] = prefix
    return env


def available_features() -> list[str]:
    """What this environment offers beyond kernels: ``pdf`` with the helper and pdf_oxide."""
    features: list[str] = ["jobs"]
    if PDF_HELPER.is_file() and importlib.util.find_spec("pdf_oxide") is not None:
        features.append("pdf")
    return features


def pdf_argv(request: Message) -> list[str]:
    """The PDF helper's command line for a ``pdf`` request, run with this interpreter."""
    operation, path = str(request.get("operation", "")), str(request.get("path", ""))
    if operation not in ("info", "pages", "search", "render"):
        raise ToolError("invalid_request", f"no PDF operation {operation!r}")
    argv = [sys.executable, "-I", str(PDF_HELPER), operation, path]
    match operation:
        case "pages":
            argv += ["--pages", str(request.get("pages", ""))]
            if request.get("include_artifacts") is True:
                argv.append("--include-artifacts")
        case "search":
            argv += ["--query", str(request.get("query", ""))]
        case "render":
            argv += ["--page", str(request.get("page", "")), "--dpi", str(request.get("dpi", 150))]
            region: object = request.get("region")
            if isinstance(region, list):
                values = cast(list[object], region)
                argv += ["--region", ",".join(str(value) for value in values)]
            out: object = request.get("out")
            if isinstance(out, str):
                argv += ["--out", out]
        case _:
            pass
    return argv


def available_languages() -> list[str]:
    """The languages whose kernelspecs this interpreter's Jupyter installation finds."""
    if not all(importlib.util.find_spec(name) for name in ("jupyter_client", "ipykernel")):
        return []
    try:
        from jupyter_client.kernelspec import KernelSpecManager

        specs: dict[str, str] = KernelSpecManager().find_kernel_specs()
    except Exception:  # a broken Jupyter installation offers no kernels
        return []
    return [language for language, name in KERNEL_NAMES.items() if name in specs]


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _running(environment: str | None) -> Message:
    """A ``state`` event's fields as an attempt starts, with the packages' fingerprint then."""
    if environment is None:
        return {"state": "running"}
    return {"state": "running", "environment": environment}


def _mime_bundle(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        str(mime): value if isinstance(value, str) else json.dumps(value)
        for mime, value in cast(dict[object, object], data).items()
    }


# Lines.


def fit_line(message: Message) -> bytes | None:
    """``message`` as a line no longer than MAX_LINE, or None if it cannot be one.

    An ``end`` event's result gives up output, its largest part first, until it
    fits. A reply that does not fit becomes an error reply.
    """
    message = sandbox_files.bounded_observations(message)
    line = encode_line(message)
    if len(line) <= MAX_LINE:
        return line
    if message.get("type") == "event" and message.get("event") == "end":
        result = dict(cast(Message, message.get("result") or {}))
        while shrink_result(result):
            line = encode_line({**message, "result": result})
            if len(line) <= MAX_LINE:
                return line
        error = {"kind": "internal", "message": "the result was too large to send"}
        return encode_line({**message, "state": "failed", "result": {"error": error}})
    if message.get("type") == "reply":
        error = {"kind": "internal", "message": f"the reply was over {MAX_LINE} bytes"}
        return encode_line({"type": "reply", "id": message.get("id"), "ok": False, "error": error})
    return None


def shrink_result(result: Message) -> bool:
    """Halve the largest part of a result over 1 KiB; False when no part is that large."""
    sizes: dict[str, int] = {}
    parts: dict[str, object] = {
        "stdout": _item(result.get("stdout"), "text"),
        "stderr": _item(result.get("stderr"), "text"),
        "outputs": result.get("outputs"),
        "traceback": _item(result.get("error"), "traceback"),
        "written_files": result.get("written_files"),
        "deleted_files": result.get("deleted_files"),
    }
    for name, part in parts.items():
        if part:
            sizes[name] = len(json.dumps(part, ensure_ascii=False))
    largest = max(sizes, key=sizes.__getitem__, default=None)
    if largest is None or sizes[largest] <= 1024:
        return False
    if largest in ("stdout", "stderr"):
        text = str(parts[largest])
        keep = len(text) // 4
        marker = f"\n[… {len(text) - 2 * keep} characters omitted to fit the result …]\n"
        shorter = text[:keep] + marker + text[len(text) - keep :]
        result[largest] = {**result[largest], "text": shorter, "truncated": True}
    elif largest == "outputs":
        outputs = cast(list[Message], parts[largest])
        kept = outputs[: len(outputs) // 2]
        note = f"[{len(outputs) - len(kept)} outputs omitted to fit the result]"
        result["outputs"] = [*kept, {"type": "display", "data": {"text/plain": note}}]
    elif largest == "traceback":
        lines = cast(list[str], parts[largest])
        shorter = (
            lines[len(lines) // 2 :] if len(lines) > 1 else [bounded(lines[0], len(lines[0]) // 2)]
        )
        result["error"] = {**result["error"], "traceback": shorter}
    else:
        files = cast(list[str], parts[largest])
        result[largest] = files[: len(files) // 2]
        result[f"{largest}_truncated"] = True
    return True


def _item(container: object, key: str) -> object:
    return cast(Message, container).get(key) if isinstance(container, dict) else None


async def serve_stdio(
    root: Path,
    *,
    subreaper: bool = False,
    protected: bool = False,
    kernel_idle: float | None = None,
) -> None:
    """Serve one connection on stdin and stdout until stdin closes."""
    loop = asyncio.get_running_loop()
    # Keep the protocol on its own descriptor; anything else printing to stdout
    # (a library, a kernel's launcher) goes to stderr instead.
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    reader = asyncio.StreamReader(limit=MAX_REQUEST)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(protocol_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    lock = asyncio.Lock()

    async def send(message: Message) -> None:
        line = fit_line(message)
        if line is None:
            print("executor: dropped an event too large to send", file=sys.stderr)
            return
        async with lock:
            writer.write(line)
            await writer.drain()

    executor = Executor(
        root, send, subreaper=subreaper, protected=protected, kernel_idle=kernel_idle
    )
    if kernel_idle is not None:
        executor.reaper = executor.spawn(executor.reap_idle_kernels(kernel_idle))
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # a request over MAX_REQUEST, already discarded
                continue
            if not line:
                break
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                await send(
                    {
                        "type": "reply",
                        "id": None,
                        "ok": False,
                        "error": {"kind": "invalid_request", "message": "not JSON"},
                    }
                )
                continue
            if not isinstance(request, dict):
                continue
            executor.spawn(executor.handle(cast(Message, request)))
    finally:
        await executor.shutdown()
        with contextlib.suppress(Exception):
            writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sandbox Science executor")
    parser.add_argument("--stdio", action="store_true", required=True, help="serve on stdin/stdout")
    parser.add_argument("--root", required=True, type=Path, help="project directory")
    parser.add_argument(
        "--kernel-idle", type=float, default=None, help="stop kernels unused this many seconds"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".executor"
    (state / "runtime").mkdir(parents=True, exist_ok=True)
    (root / SHARED).mkdir(exist_ok=True)
    (state / "ipython").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("JUPYTER_RUNTIME_DIR", str(state / "runtime"))
    os.environ.setdefault("IPYTHONDIR", str(state / "ipython"))
    protected = sandbox_processes.protect_process()
    # A subreaper adopts its descendants' orphans, so leftovers stay findable below it.
    subreaper = sandbox_processes.prctl(sandbox_processes.PR_SET_CHILD_SUBREAPER, 1)
    idle: float | None = args.kernel_idle
    if idle is not None and not idle > 0:
        parser.error("--kernel-idle must be a positive number of seconds")
    asyncio.run(
        serve_stdio(root.resolve(), subreaper=subreaper, protected=protected, kernel_idle=idle)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
