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
    from . import (
        binding_inventory,
        capture,
        executor_admission,
        executor_lifecycle,
        provisioning,
        sandbox_files,
        sandbox_processes,
        sandbox_setup,
        workspace_prefix,
    )
    from .sandbox_files import MAX_PATH_BYTES as MAX_PATH_BYTES
    from .sandbox_files import SHARED as SHARED
    from .sandbox_files import ToolError as ToolError
    from .sandbox_files import encode_line as encode_line
else:
    binding_inventory = _sibling("binding_inventory")
    capture = _sibling("capture")
    executor_lifecycle = _sibling("executor_lifecycle")
    executor_admission = _sibling("executor_admission")
    provisioning = _sibling("provisioning")
    sandbox_files = _sibling("sandbox_files")
    sandbox_processes = _sibling("sandbox_processes")
    sandbox_setup = _sibling("sandbox_setup")
    workspace_prefix = _sibling("workspace_prefix")
    ToolError = sandbox_files.ToolError
    encode_line = sandbox_files.encode_line
    SHARED = sandbox_files.SHARED
    MAX_PATH_BYTES = sandbox_files.MAX_PATH_BYTES

_owned = executor_lifecycle.owned

PROTOCOL = 3
"""The executor's protocol: 3 checks generated restrictions at every binding-bearing
launch and surviving-kernel cell. Older executors must be recycled (SK-16, RC-24)."""
MAX_LINE = 16 * 1024 * 1024
"""The longest line the executor sends; the worker drops longer ones."""
MAX_REQUEST = 4 * ((100 * 1024 * 1024 + 2) // 3) + 64 * 1024
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
MAX_KERNEL_REASON = 300
"""Characters of the reason ``hello`` gives for offering no Python kernel."""
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
_BINDING_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_BINDING_VALUE_RE = re.compile(r"[^\x00-\x20\x7f;]{1,4096}")
"""A binding's value: a path, without whitespace, control characters or ``;``."""
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

Message = dict[str, Any]
Send = Callable[[Message], Awaitable[None]]


class Clock(Protocol):
    """Monotonic admission, execution and lifecycle time; tests pass a fake one.

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
    """Seconds of its time limit used before it started, such as a cell's wait behind
    another cell. Waiting behind package commands is not among them (CP-12)."""
    install_waited: float = 0.0
    """Seconds it waited behind package commands before it started, which its time limit
    does not cover (CP-12)."""
    deadline: float | None = None
    """When its time limit ends, by the monotonic clock, once it runs."""
    admitted: float = field(default_factory=time.monotonic)
    package_operation: str = ""
    package_generation: int = -1
    controller: Callable[[], bool] | None = None
    bindings: dict[str, str] = field(default_factory=dict[str, str])
    """A cell's skill bindings, to say when its kernel holds others."""
    binding_inventory: str | None = None
    bindings_dropped: tuple[str, ...] = ()
    """The skill bindings left out as naming the environment's own variables, which the
    result names for the run's notice."""


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
    start_binding_inventory: str | None = None
    binding_inventory: str | None = None
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
        require_workspace_prefix: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.root = root
        self.require_workspace_prefix = require_workspace_prefix
        # Opt-in only: a local executor must never mark the developer's Python prefix.
        self.clock: Clock = clock or _SystemClock()
        self.captures = capture.Pool(root)
        self.files = sandbox_files.Requests(root, max_read=MAX_READ, max_list=MAX_LIST)
        self.owner_prefix = ""
        self.kernel_idle = kernel_idle
        """Stop a kernel nobody has used for this many seconds; None keeps them."""
        self.send = send
        self.subreaper = subreaper
        self.protected = protected
        """Whether this process adopts orphans, so leftovers are found below it."""
        self.lifecycle = executor_lifecycle.Lifecycle(
            self.clock,
            make_kernel=self.make_kernel,
            error=ToolError,
            signal=self.signal_command,
            leftovers=self.leftovers,
            collect=end_and_collect,
            end_descendants=self.end_descendants,
            close_resources=self.captures.close,
            languages=tuple(KERNEL_NAMES),
            max_run_kernels=MAX_RUN_KERNELS,
            max_ended_runs=MAX_ENDED_RUNS,
            term_grace=TERM_GRACE,
            package_grace=PACKAGE_GRACE,
        )
        self.configured: dict[str, str] = {}
        """Variables from ``configure``, added to every command's and kernel's environment."""
        # Package commands (docs/specs/conda-provisioning.md, CP-4, CP-11, CP-12).
        self.packages = provisioning.State(root / ".executor" / "provisioning")
        self.places = provisioning.Places.here()
        self.provisioning: provisioning.Config | None = None
        """None when the wrappers could not be written: package commands then run as they are."""
        self.wrapped: list[str] = []
        self.admission = executor_admission.Admission(
            self.clock,
            closed=lambda: self.lifecycle.closing,
            error=ToolError,
            busy=_BusyError,
            not_started=_NotStartedError,
            send=send,
            remove_records=self.packages.remove_records,
            usable=provisioning.usable,
            max_lockfile_event=MAX_LOCKFILE_EVENT,
            max_commands=MAX_COMMANDS,
            max_owners=MAX_OWNERS,
            max_runs=MAX_RUNS,
            install_bound=self.install_bound,
        )
        self.binding_inventory = binding_inventory.Inventory()
        self.binding_inventory_lock = asyncio.Lock()
        """The packages' fingerprint as the worker knows it, from ``hello`` and the
        ``revision`` events since."""
        try:
            config = provisioning.Config(sys.executable, str(PROVISIONING), sys.prefix, self.places)
            self.wrapped = provisioning.refresh(self.packages, config, self.tool_path())
            self.admission.reported = (
                provisioning.fingerprint(self.places)
                if require_workspace_prefix
                else provisioning.baseline(self.packages, self.places)
            )
            self.provisioning = config
        except OSError as exc:
            print(f"executor: package commands run without turns: {exc}", file=sys.stderr)
            self.admission.reported = provisioning.fingerprint(self.places)

    async def op_package_gate(self, request: Message) -> None:
        restore = self.restore_prefix if self.require_workspace_prefix else None
        await self.reply(request, **await self.admission.update(request, restore))

    async def restore_prefix(self, baseline: Message, request: Message) -> None:
        """Settle a restored prefix's baseline only while its recorded evidence still holds.

        Runs under the launch permit; takes the package shared lock, then observation.
        """
        descriptor = self.packages.try_lock(exclusive=False)
        if descriptor is None:
            raise ToolError("unavailable", "prefix restoration has an active package turn")
        try:
            async with self.admission.observing:
                prefix = await asyncio.to_thread(workspace_prefix.report, sys.prefix)
                actual = await asyncio.to_thread(provisioning.fingerprint, self.places)
                if (
                    set(baseline) != {"identity", "fingerprint"}
                    or "error" in prefix
                    or prefix.get("identity") != baseline.get("identity")
                    or actual != baseline.get("fingerprint")
                    or await asyncio.to_thread(self.packages.pending)
                ):
                    raise ToolError("unavailable", "prefix restoration evidence changed")
                self.admission.controller_present(request)
                if self.admission.reported != actual:
                    self.admission.epoch += 1
                    self.admission.reported = actual
        finally:
            os.close(descriptor)

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

    def start_idle_reaper(self, idle: float) -> None:
        """Start this connection's owned idle-kernel task."""
        self.lifecycle.start_idle_reaper(idle)

    def spawn(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        return self.lifecycle.spawn(coroutine)

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
        await self.lifecycle.shutdown()

    async def end_descendants(self) -> None:
        await asyncio.to_thread(sandbox_processes.end_descendants)

    # Paths and environment.

    def session(self, request: Message) -> str:
        return self.files.session_name(request.get("session"))

    def bindings(
        self, request: Message, env: Mapping[str, str]
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """The skill bindings an execution adds to the environment's variables ``env``, and
        those left out as naming one of them (SK-16): the environment's value stands, and
        the run is told, as one such binding would otherwise fail every execution of it."""
        raw = request.get("bindings", {})
        if not isinstance(raw, dict):
            raise ToolError("invalid_request", "bindings must be an object")
        found: dict[str, str] = {}
        dropped: list[str] = []
        for name, value in cast(dict[object, object], raw).items():
            if not (isinstance(name, str) and _BINDING_RE.fullmatch(name)):
                raise ToolError("invalid_request", f"invalid variable name {name!r}")
            if not (isinstance(value, str) and _BINDING_VALUE_RE.fullmatch(value)):
                raise ToolError("invalid_request", f"invalid value for {name}")
            if name in env:
                dropped.append(name)
                continue
            found[name] = value
        return found, tuple(sorted(dropped))

    async def current_binding_inventory(self) -> None:
        """Refresh static restrictions from this guest, never the worker host."""
        sites = tuple(
            Path(p) for p in (*self.places.site, *([self.places.user] if self.places.user else []))
        )
        try:
            cancelled = asyncio.Event()
            async with self.binding_inventory_lock:
                await _owned(
                    asyncio.to_thread(
                        self.binding_inventory.current,
                        Path(sys.prefix),
                        sites,
                        lambda: (
                            provisioning.fingerprint(self.places)
                            + binding_inventory.system_fingerprint()
                        ),
                        self.root / ".executor" / "binding-inventory.json",
                    ),
                    cancelled.set,
                )
            if cancelled.is_set():
                raise asyncio.CancelledError
        except (binding_inventory.InventoryError, OSError) as error:
            raise ToolError("unavailable", str(error)) from None

    @contextlib.asynccontextmanager
    async def binding_guard(self, names: set[str], expected: str | None = None):
        """Exclude cooperating package changes through binding-bearing launch admission."""
        descriptor: int | None = None
        if names:
            try:
                if self.provisioning is None:
                    raise ToolError(
                        "unavailable", "package observation unavailable for skill bindings"
                    )
                descriptor = self.packages.try_lock(exclusive=False)
                if descriptor is None:
                    raise ToolError(
                        "unavailable", "packages changing; retry skill bindings after provisioning"
                    )
                await self.current_binding_inventory()
                self.binding_inventory.require(names)
                if expected is not None and (
                    self.binding_inventory.manifest is None
                    or expected != self.binding_inventory.manifest["digest"]
                ):
                    raise ToolError(
                        "unavailable",
                        "skill binding inventory changed; reload skills and explicitly restart "
                        "kernels retaining old bindings",
                    )
                yield
            except binding_inventory.InventoryError as error:
                raise ToolError("policy_denied", str(error)) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        else:
            yield

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
                directory = self.files.open_base(base)
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
        env["SESSION_WORKSPACE"] = str(self.files.session_dir(session))
        env["PROJECT_SHARED"] = str(self.root / SHARED)
        return env

    # Package commands: attempts wait while one changes the environment, and each change
    # reaches the worker as a ``revision`` event before any result that shows it.

    async def baseline(self) -> str:
        """The packages' fingerprint as this executor starts, which hello reports."""
        async with self.admission.observing:
            if self.require_workspace_prefix:
                # The daemon owns the settled baseline. Reconnecting must not discard
                # pending package journals or report changes before their replay.
                return self.admission.reported
            if self.provisioning is None:
                fingerprint = await asyncio.to_thread(provisioning.fingerprint, self.places)
            else:
                fingerprint = await asyncio.to_thread(
                    provisioning.baseline, self.packages, self.places
                )
            return self.admission.baseline(fingerprint)

    def install_bound(self, running: Running, fallback: float) -> float:
        """Until when ``running`` may wait behind a package command (CP-12), by the
        monotonic clock.

        The wait does not count against its own time limit, but it may not outlast the
        command's: a package command runs within an attempt beside it, which ends at its
        own limit. So the bound is the latest limit among those attempts, and at least
        ``fallback``, which the caller sets to its own limit from the start of the wait,
        for a lock that no running attempt can be holding.
        """
        limits = (
            other.deadline
            for other in self.lifecycle.running.values()
            if other is not running and other.deadline is not None
        )
        return max([fallback, *limits])

    async def begin(self, running: Running, timeout: float) -> str | None:
        """Wait while a package command changes the environment, then look at it (CP-12).

        Returns the packages' fingerprint as the attempt starts, after reporting what
        changed before it. Attempts already running are not held up. The wait does not
        count against the attempt's time limit (``running.install_waited``); it ends at
        ``install_bound``, then _BusyError.
        """
        if self.provisioning is None:
            return None
        started = self.clock.monotonic()
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
            now = self.clock.monotonic()
            bound = self.install_bound(running, started + timeout)
            if now >= bound:
                raise _BusyError(now - started, await asyncio.to_thread(self.packages.holding))
            await self.clock.sleep(min(delay, bound - now))
            delay = min(2 * delay, PACKAGE_POLL)
        running.install_waited += self.clock.monotonic() - started
        try:
            await self.admission.observing.acquire()
        except BaseException:
            os.close(held)
            raise
        try:
            descriptor: int | None = held
            for _ in range(MAX_LOOKS):
                # observe closes the descriptor, in its thread, however this task ends.
                observed = await asyncio.to_thread(
                    provisioning.observe,
                    self.packages,
                    self.places,
                    self.admission.reported,
                    descriptor,
                )
                descriptor = None
                await self.admission.report(running.execution, observed, None)
                if observed.complete:
                    break
        except OSError as exc:
            print(f"executor: cannot read the package journal: {exc}", file=sys.stderr)
            return None
        finally:
            self.admission.observing.release()
        # A manager installed since (pip install uv) gets its wrapper.
        try:
            self.wrapped = await asyncio.to_thread(
                provisioning.refresh, self.packages, self.provisioning, self.tool_path()
            )
        except OSError as exc:
            print(f"executor: cannot write the package wrappers: {exc}", file=sys.stderr)
        return self.admission.reported

    async def settle(self, running: Running) -> Message:
        """Report what changed while an attempt ran: the result's ``environment`` fields.

        ``environment`` is the packages' fingerprint as the worker now knows them, and
        ``environment_changing`` says that a package command was still running.
        """
        if self.provisioning is None:
            return {"environment": await asyncio.to_thread(environment_fingerprint)}
        async with self.admission.observing:
            observed = provisioning.Observation()
            try:
                for _ in range(MAX_LOOKS):
                    observed = await asyncio.to_thread(
                        functools.partial(
                            provisioning.observe,
                            self.packages,
                            self.places,
                            self.admission.reported,
                            lockfile_wanted=True,
                        )
                    )
                    await self.admission.report(running.execution, observed, running.execution)
                    if observed.complete:
                        break
            except OSError as exc:  # research code can break the state directory
                print(f"executor: cannot read the package lock: {exc}", file=sys.stderr)
                return {"environment": await asyncio.to_thread(environment_fingerprint)}
            return {
                "environment": self.admission.reported,
                "environment_changing": observed.busy is not None,
            }

    # Operations.

    async def op_hello(self, request: Message) -> None:
        languages, kernel_reason = available_languages()
        await self.reply(
            request,
            protocol=PROTOCOL,
            max_request_bytes=MAX_REQUEST,
            package_gate=self.admission.incarnation,
            languages=languages,
            kernel_reason=kernel_reason,
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
            conditional_kernel_restart=1,
            environment=await self.baseline(),
            package_epoch=self.admission.epoch,
            **(
                {
                    "workspace_prefix": {
                        **workspace_prefix.report(sys.prefix),
                        "fingerprint": await asyncio.to_thread(
                            provisioning.fingerprint, self.places
                        ),
                        "journal_pending": await asyncio.to_thread(self.packages.pending),
                    }
                }
                if self.require_workspace_prefix
                else {}
            ),
        )

    async def own_launch[T](self, running: Running, work: Awaitable[T]) -> T:
        """Keep launch serialization through actual work and repeated cancellation."""
        return await _owned(work, lambda: setattr(running, "cancel_requested", True))

    async def start_kernel(
        self, running: Running, kernel: Kernel, timeout: float, work: Callable[[], Awaitable[None]]
    ) -> None:
        async with self.admission.launch(running, timeout):
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
                # Waiting behind a package command does not count against the limit
                # (CP-12), but ends at install_bound.
                waiting_since: float | None = None
                while held is None:
                    if running.cancel_requested:
                        raise _NotStartedError
                    now = self.clock.monotonic()
                    since = now if waiting_since is None else waiting_since
                    charged = since - running.admitted - running.install_waited
                    if charged >= timeout or now >= self.install_bound(running, since + timeout):
                        raise _BusyError(now - running.admitted, None)
                    try:
                        held = self.packages.try_lock(exclusive=False)
                    except OSError:
                        break  # damaged lock: start is explicitly unknown
                    if held is None:
                        waiting_since = since
                        await self.clock.sleep(0.05)
                if waiting_since is not None:
                    running.install_waited += self.clock.monotonic() - waiting_since
                if held is not None:
                    async with self.admission.observing:
                        if running.kind == "cell":
                            for _ in range(MAX_LOOKS):
                                observed = await asyncio.to_thread(
                                    provisioning.observe,
                                    self.packages,
                                    self.places,
                                    self.admission.reported,
                                    os.dup(held),
                                )
                                await self.admission.report(running.execution, observed, None)
                                if observed.complete:
                                    fingerprint = observed.fingerprint
                                    break
                        else:
                            # Restart has no execution event stream. Do not consume
                            # another call's package journal or fabricate its cause.
                            fingerprint = await asyncio.to_thread(
                                provisioning.fingerprint, self.places
                            )
                        epoch = (
                            self.admission.epoch if fingerprint == self.admission.reported else None
                        )
            if running.cancel_requested:
                raise _NotStartedError
            if self.lifecycle.closing or (
                (running.kind == "restart" or running.package_operation)
                and running.controller
                and not running.controller()
            ):
                raise ToolError(
                    "unavailable", "controller detached or executor closing before kernel startup"
                )
            async with self.binding_guard(set(kernel.bindings), kernel.binding_inventory):
                await work()
            kernel.start_binding_inventory = (
                self.binding_inventory.manifest["digest"]
                if kernel.bindings and self.binding_inventory.manifest is not None
                else None
            )
            kernel.birth_id = "birth-" + secrets.token_hex(12)
            kernel.start_environment = fingerprint
            kernel.start_package_epoch = epoch
            kernel.start_execution = running.execution if running.kind == "cell" else None
        finally:
            if held is not None:
                os.close(held)

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
        if execution in self.lifecycle.running:
            raise ToolError("conflict", f"execution {execution} is already running")
        running = Running(execution=execution, kind=kind, admitted=self.clock.monotonic())
        inventory = request.get("binding_inventory")
        if inventory is not None and (
            not isinstance(inventory, str) or _SHA256_RE.fullmatch(inventory) is None
        ):
            raise ToolError("invalid_request", "invalid binding inventory digest")
        running.binding_inventory = inventory
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
        self.lifecycle.accept(running)
        return running

    async def op_pdf(self, request: Message) -> None:
        """Run the PDF helper as its own process, like a shell call's argv, in the session."""
        if "pdf" not in available_features():
            raise ToolError("unsupported", "pdf_oxide is not installed in this environment")
        await self.op_shell({**request, "argv": pdf_argv(request), "cwd": "."})

    async def op_shell(self, request: Message) -> None:
        session = self.session(request)
        try:
            cwd = self.files.directory(session, request.get("cwd", "."))
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
        bindings, dropped = self.bindings(request, env)
        running = self.accept(request, "job" if is_job else "shell")
        running.session = session
        running.bindings = bindings
        running.bindings_dropped = dropped
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
        started = self.clock.monotonic()
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
            self.admission.own(running.owner, execution)
            # The process is the executor's child before its pid is in spawned.
            with sandbox_processes.starting_children():
                try:
                    async with (
                        self.admission.launch(running, timeout),
                        self.binding_guard(set(running.bindings), running.binding_inventory),
                    ):
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
            remaining = max(0.0, timeout - running.waited)
            running.deadline = self.clock.monotonic() + remaining
            try:
                await asyncio.wait_for(process.wait(), remaining)
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
            self.lifecycle.finish(execution)
            observations = written.pop("output_observations", None)
            result = {
                "exit_code": exit_code,
                "signal": signal_number,
                "stdout": captures["stdout"].summary(),
                "stderr": captures["stderr"].summary(),
                "error": error,
                "elapsed_seconds": self.clock.monotonic() - started,
                "sandbox_started": wall_started,
                "sandbox_ended": time.time(),
                **written,
                "running_kernels": await self.session_kernels(session),
            }
            if running.bindings_dropped:
                result["bindings_dropped"] = list(running.bindings_dropped)
            if not self.lifecycle.closing:
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
            self.files.open_base(["sessions", session]), [".jobs"], ".jobs", create=True
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
        running = self.lifecycle.running.get(execution)
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
        await self.lifecycle.terminate(running)

    async def signal_command(self, owner: str, leader: sandbox_processes.Leader | None) -> None:
        await asyncio.to_thread(
            lambda: sandbox_processes.signal_processes(
                without_wrapped(sandbox_processes.owned_processes(owner, leader)), signal.SIGTERM
            )
        )

    def leftovers(
        self, owner: str, leader: sandbox_processes.Leader | None
    ) -> tuple[list[sandbox_processes.Process], bool]:
        """The call's processes still running, and whether a package command among them
        holds its turn."""
        left = sandbox_processes.owned_processes(owner, leader)
        holder = (self.packages.holding() or {}).get("pid")
        return left, isinstance(holder, int) and any(info.pid == holder for info in left)

    def make_kernel(self, session: str, language: str, run: str | None) -> Kernel:
        return Kernel(
            session,
            language,
            self.files.directory(session, "."),
            self.environment(session),
            last_used=self.clock.monotonic(),
            run=run,
        )

    def kernel(self, session: str, language: str, run: str | None = None) -> Kernel:
        return self.lifecycle.kernel(session, language, run)

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
        bindings, dropped = self.bindings(request, kernel.env)
        running = self.accept(request, "cell")
        running.kernel = kernel
        running.bindings = kernel.bindings = bindings
        kernel.binding_inventory = running.binding_inventory
        running.bindings_dropped = dropped
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
        started = self.clock.monotonic()
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
                self.admission.own(kernel.owner, execution)
                ran_on = kernel.info()
                state_lost, kernel.state_lost = kernel.state_lost, False
                before = await asyncio.to_thread(self.snapshot, kernel.session)
                async with (
                    self.admission.launch(running, timeout),
                    self.binding_guard(
                        set(running.bindings) | set(kernel.started_bindings),
                        kernel.start_binding_inventory
                        if kernel.started_bindings
                        else running.binding_inventory,
                    ),
                ):
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
            self.lifecycle.finish(execution)
            kernel.last_used = self.clock.monotonic()
            if ran_on is None and kernel.generation:
                ran_on = kernel.info()
            result.update(
                stdout=captures["stdout"].summary(),
                stderr=captures["stderr"].summary(),
                outputs=outputs,
                kernel=ran_on,
                running_kernels=await self.session_kernels(kernel.session),
                elapsed_seconds=self.clock.monotonic() - started,
                sandbox_started=wall_started,
                sandbox_ended=time.time(),
            )
            if running.bindings_dropped:
                result["bindings_dropped"] = list(running.bindings_dropped)
            if not self.lifecycle.closing:
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

        # This generation's client and manager, even once a restart replaces the kernel's.
        client, manager = kernel.client, kernel.manager
        if msg_id is None:
            msg_id = client.execute(code, store_history=True, allow_stdin=False)
        deadline = self.clock.monotonic() + max(0.0, timeout - running.waited)
        running.deadline = deadline
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
            now = self.clock.monotonic()
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
        running = self.lifecycle.request_cancel(str(request.get("execution")))
        await self.reply(request, found=running is not None)
        if running is not None:
            self.lifecycle.cancel(running)

    async def op_restart(self, request: Message) -> None:
        with self.lifecycle.control():
            await self.restart_request(request)

    async def restart_request(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(
            session, str(request.get("language", "python")), self.run_name(request)
        )
        restart = Running("restart", "restart", admitted=self.clock.monotonic())
        restart.kernel = kernel
        guard = request.get("_admission_guard")
        if callable(guard):
            restart.controller = lambda: bool(guard())
        await self.lifecycle.restart(
            kernel,
            only_if_absent=request.get("only_if_absent") is True,
            permit=lambda: self.admission.restart_permit(request),
            stop=lambda: self.own_launch(restart, kernel.stop()),
            start=lambda: kernel.ensure_started(
                lambda work: self.start_kernel(restart, kernel, 180, work)
            ),
        )
        await self.reply(request, kernel=kernel.info())

    async def reap_idle_kernels(self, idle: float) -> None:
        await self.lifecycle.reap_idle_kernels(idle)

    async def op_close_session(self, request: Message) -> None:
        with self.lifecycle.control():
            await _owned(self.close_session_request(request))

    async def close_session_request(self, request: Message) -> None:
        stopped = await self.lifecycle.close_session(self.session(request), self.run_name(request))
        await self.reply(request, kernels=stopped)

    async def session_kernels(self, session: str) -> list[Message]:
        return await self.lifecycle.session_kernels(session)

    async def op_kernels(self, request: Message) -> None:
        await self.reply(request, kernels=await self.session_kernels(self.session(request)))

    async def op_usage(self, request: Message) -> None:
        """The CPU time used below this executor, and its running kernels and calls.

        The worker samples it to tell an environment whose kernels still compute,
        say in a background thread, from an idle one.
        """
        cpu = await asyncio.to_thread(sandbox_processes.cpu_seconds_below, os.getpid())
        kernels = sum(1 for kernel in self.lifecycle.kernels.values() if kernel.manager is not None)
        await self.reply(
            request, cpu_seconds=cpu, kernels=kernels, running=len(self.lifecycle.running)
        )

    async def op_read(self, request: Message) -> None:
        await self.reply(request, **await self.files.read(request))

    async def op_capture_read(self, request: Message) -> None:
        with sandbox_processes.starting_children():
            result = await self.captures.read_request(request)
        await self.reply(request, **result)

    async def op_capture_cancel(self, request: Message) -> None:
        try:
            result = await self.captures.cancel_request(request)
        except capture.CaptureError as exc:
            raise ToolError(exc.kind, exc.message) from exc
        await self.reply(request, **result)

    async def op_skills_check(self, request: Message) -> None:
        found, names = await self.files.skills_check(request, self.tool_path())
        try:
            # Staging is observation; package admission repeats the authoritative check.
            async with self.binding_guard(set(names) or {"INVENTORY_QUERY"}):
                pass
        except ToolError as error:
            if error.kind == "policy_denied" and self.binding_inventory.manifest is not None:
                found["inventory"] = self.binding_inventory.projection(names, self.admission.epoch)
            else:
                found["inventory"] = {"state": "unavailable", "coverage": error.message[:2000]}
        else:
            found["inventory"] = self.binding_inventory.projection(names, self.admission.epoch)
        await self.reply(request, **found)

    async def op_skills_put(self, request: Message) -> None:
        await self.reply(request, **await self.files.skills_put(request))

    async def op_write(self, request: Message) -> None:
        await self.reply(request, **await self.files.write(request))

    async def op_shared(self, request: Message) -> None:
        await self.reply(request, **await self.files.shared(request))

    async def op_remove(self, request: Message) -> None:
        await self.reply(request, **await self.files.remove(request))

    async def op_move(self, request: Message) -> None:
        await self.reply(request, **await self.files.move(request))

    async def op_list(self, request: Message) -> None:
        await self.reply(request, **await self.files.list(request))

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
    if operation not in ("info", "pages", "search", "render", "draw"):
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
        case "draw":
            if (page := request.get("page")) is not None:
                argv += ["--page", str(page)]
            drawn: object = request.get("region")
            if isinstance(drawn, list):
                values = cast(list[object], drawn)
                argv += ["--region", ",".join(str(value) for value in values)]
        case _:
            pass
    return argv


def available_languages() -> tuple[list[str], str | None]:
    """The languages whose kernelspecs this interpreter's Jupyter installation finds, and, when
    Python is not among them, a short reason why (bounded, for the worker to surface)."""
    for name in ("jupyter_client", "ipykernel"):
        if importlib.util.find_spec(name) is None:
            return [], f"{name} is not importable by {sys.executable}"
    try:
        from jupyter_client.kernelspec import KernelSpecManager

        specs: dict[str, str] = KernelSpecManager().find_kernel_specs()
    except Exception as exc:  # a broken Jupyter installation offers no kernels
        return [], _bounded_reason(f"jupyter_client cannot list kernelspecs: {exc!r}")
    languages = [language for language, name in KERNEL_NAMES.items() if name in specs]
    if "python" in languages:
        return languages, None
    found = ", ".join(sorted(specs)) or "none"
    return languages, _bounded_reason(f"no kernelspec for python3 (found: {found})")


def _bounded_reason(text: str) -> str:
    """One printable line of at most ``MAX_KERNEL_REASON`` characters."""
    line = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return line[:MAX_KERNEL_REASON]


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
        executor.start_idle_reaper(kernel_idle)
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
