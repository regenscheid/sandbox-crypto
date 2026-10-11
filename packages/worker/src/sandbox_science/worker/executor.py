# SPDX-License-Identifier: MIT
"""The executor: runs shell commands, Jupyter kernels and file operations in a sandbox.

The trusted worker service starts it inside a research sandbox and speaks
newline-delimited JSON with it over stdin and stdout. It depends only on the
standard library and, once a kernel starts, jupyter_client, so the worker
service can copy this one file into any sandbox that has Python.
docs/specs/tools.md defines the behavior and docs/WORKER.md the protocol.

Requests carry an ``id`` and an ``op``; each gets one reply
(``{"type": "reply", "id": …, "ok": …}``). Shell commands and cells reply as
soon as they are accepted, then send events
(``{"type": "event", "execution": …, "event": …}``) ending with ``end``.
When stdin closes, the executor stops every command and kernel and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import re
import signal
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

PROTOCOL = 1
MAX_LINE = 64 * 1024 * 1024
MAX_READ = 8 * 1024 * 1024
MAX_LIST = 5000
TERM_GRACE = 2.0
INTERRUPT_GRACE = 5.0
KERNEL_START_TIMEOUT = 60.0
SHARED = "shared"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

Message = dict[str, Any]
Send = Callable[[Message], Awaitable[None]]


class ToolError(Exception):
    """A failure reported to the worker service with one of the spec's error kinds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


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


# Paths.


def check_relative(path: str) -> str:
    """The same rules as the contracts' validate_relative_path."""
    if path == ".":
        return path
    if not path or "\x00" in path or len(path.encode()) > 4096 or path.startswith("/"):
        raise ToolError("invalid_request", f"invalid path {path!r}")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ToolError("invalid_request", f"path {path!r} has an empty, '.' or '..' segment")
    return path


@dataclass
class Running:
    """An accepted shell command or cell."""

    execution: str
    kind: str
    task: asyncio.Task[None] | None = None
    started: bool = False
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = None
    kernel: Kernel | None = None


@dataclass
class Kernel:
    """A session's kernel for one language; its generation grows on every restart."""

    session: str
    language: str
    cwd: Path
    env: dict[str, str]
    generation: int = 0
    state_lost: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    manager: Any = None
    client: Any = None

    @property
    def kernel_id(self) -> str:
        return f"krn-{self.session}-{self.language}"

    def info(self) -> Message:
        return {
            "kernel_id": self.kernel_id,
            "language": self.language,
            "generation": self.generation,
        }

    async def alive(self) -> bool:
        return self.manager is not None and bool(await self.manager.is_alive())

    async def ensure_started(self) -> None:
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
        manager: Any = AsyncKernelManager(kernel_name="python3")
        try:
            await manager.start_kernel(cwd=str(self.cwd), env=self.env)
            client = manager.client()
            client.start_channels()
            await client.wait_for_ready(timeout=KERNEL_START_TIMEOUT)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)
            raise ToolError("unsupported", f"cannot start a {self.language} kernel: {exc}") from exc
        self.manager, self.client = manager, client
        self.generation += 1

    async def stop(self) -> None:
        client, manager = self.client, self.manager
        self.client = self.manager = None
        if client is not None:
            with contextlib.suppress(Exception):
                client.stop_channels()
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)

    async def restart(self) -> None:
        await self.stop()
        self.state_lost = True
        await self.ensure_started()


class Executor:
    """Handles one connection's requests. ``root`` holds sessions/ and shared/."""

    def __init__(self, root: Path, send: Send) -> None:
        self.root = root
        self.send = send
        self.running: dict[str, Running] = {}
        self.kernels: dict[tuple[str, str], Kernel] = {}
        self.tasks: set[asyncio.Task[None]] = set()

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

    def spawn(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def handle(self, request: Message) -> None:
        handlers: dict[str, Callable[[Message], Awaitable[None]]] = {
            "hello": self.op_hello,
            "shell": self.op_shell,
            "cell": self.op_cell,
            "cancel": self.op_cancel,
            "restart": self.op_restart,
            "read": self.op_read,
            "write": self.op_write,
            "list": self.op_list,
        }
        handler = handlers.get(str(request.get("op")))
        try:
            if handler is None:
                raise ToolError("invalid_request", f"unknown operation {request.get('op')!r}")
            await handler(request)
        except ToolError as error:
            await self.fail(request, error)
        except Exception as exc:  # every request gets a reply
            await self.fail(request, ToolError("internal", f"{type(exc).__name__}: {exc}"))

    async def shutdown(self) -> None:
        for running in list(self.running.values()):
            if running.process is not None:
                await self.terminate(running.process)
            if running.task is not None:
                running.task.cancel()
        for kernel in self.kernels.values():
            await kernel.stop()

    # Paths and environment.

    def session(self, request: Message) -> str:
        session = request.get("session")
        if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
            raise ToolError("invalid_request", f"invalid session {session!r}")
        return session

    def session_dir(self, session: object) -> Path:
        if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
            raise ToolError("invalid_request", f"invalid session {session!r}")
        return self.root / "sessions" / session

    def resolve(self, session: object, path: object) -> Path:
        """Map a workspace path to a file system path, refusing symlinks on the way."""
        if not isinstance(path, str):
            raise ToolError("invalid_request", "path must be a string")
        check_relative(path)
        if path == SHARED or path.startswith(SHARED + "/"):
            base, rest = self.root / SHARED, path[len(SHARED) + 1 :]
        else:
            base, rest = self.session_dir(session), "" if path == "." else path
        base.mkdir(parents=True, exist_ok=True)
        target = base
        for segment in rest.split("/") if rest else ():
            target = target / segment
            if target.is_symlink():
                raise ToolError("invalid_request", f"path {path!r} goes through a symlink")
        return target

    def environment(self, session: str) -> dict[str, str]:
        env = dict(os.environ)
        env["SESSION_WORKSPACE"] = str(self.session_dir(session))
        env["PROJECT_SHARED"] = str(self.root / SHARED)
        return env

    # Operations.

    async def op_hello(self, request: Message) -> None:
        kernels = ("jupyter_client", "ipykernel")
        languages = ["python"] if all(importlib.util.find_spec(name) for name in kernels) else []
        await self.reply(
            request,
            protocol=PROTOCOL,
            languages=languages,
            location={
                "hostname": socket.gethostname(),
                "system": platform.platform(),
                "python": sys.version.split()[0],
                "pid": os.getpid(),
                "uid": os.getuid(),
                "root": str(self.root),
            },
        )

    def accept(self, request: Message, kind: str) -> Running:
        execution = request.get("execution")
        if not isinstance(execution, str) or not execution:
            raise ToolError("invalid_request", "missing execution id")
        if execution in self.running:
            raise ToolError("conflict", f"execution {execution} is already running")
        running = Running(execution=execution, kind=kind)
        self.running[execution] = running
        return running

    async def op_shell(self, request: Message) -> None:
        session = self.session(request)
        cwd = self.resolve(session, request.get("cwd", "."))
        if not cwd.is_dir():
            raise ToolError(
                "invalid_request", f"working directory {request.get('cwd')!r} does not exist"
            )
        raw_argv: object = request.get("argv")
        if raw_argv is None:
            raw_argv = ["/bin/bash", "-c", str(request.get("script", ""))]
        if not isinstance(raw_argv, list):
            raise ToolError("invalid_request", "argv must be a list of strings")
        argv = [item for item in cast(list[object], raw_argv) if isinstance(item, str)]
        if not argv or len(argv) != len(cast(list[object], raw_argv)):
            raise ToolError("invalid_request", "argv must be a non-empty list of strings")
        running = self.accept(request, "shell")
        await self.reply(request)
        running.task = self.spawn(
            self.run_shell(
                running,
                argv,
                cwd,
                self.environment(session),
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
        state, error, exit_code, signal_number = "failed", None, None, None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                error = {"kind": "invalid_request", "message": f"cannot run {argv[0]!r}: {exc}"}
                return
            running.process = process
            running.started = True
            await self.event(execution, "state", state="running")
            readers = [
                asyncio.ensure_future(self.pump(execution, process.stdout, captures["stdout"])),
                asyncio.ensure_future(self.pump(execution, process.stderr, captures["stderr"])),
            ]
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                await self.terminate(process)
            # Processes the command left behind would hold its pipes open.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            _, pending = await asyncio.wait(readers, timeout=5)
            for reader in pending:
                reader.cancel()
            code = process.returncode
            exit_code = code if code is not None and code >= 0 else None
            signal_number = -code if code is not None and code < 0 else None
            if running.cancel_requested:
                state = "cancelled"
                error = {"kind": "cancelled", "message": "cancelled on request"}
            elif timed_out:
                state = "timed_out"
                error = {"kind": "timed_out", "message": f"exceeded the {timeout:g} s limit"}
            else:
                state = "succeeded" if code == 0 else "failed"
        finally:
            self.running.pop(execution, None)
            result = {
                "exit_code": exit_code,
                "signal": signal_number,
                "stdout": captures["stdout"].summary(),
                "stderr": captures["stderr"].summary(),
                "error": error,
                "elapsed_seconds": time.monotonic() - started,
                "sandbox_started": wall_started,
                "sandbox_ended": time.time(),
            }
            await self.event(execution, "end", state=state, result=result)

    async def pump(
        self, execution: str, stream: asyncio.StreamReader | None, capture: Capture
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            if text := capture.feed(chunk):
                await self.event(execution, "stream", name=capture.name, text=text)

    async def terminate(self, process: asyncio.subprocess.Process) -> None:
        """SIGTERM to the process group, then SIGKILL after a grace period."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), TERM_GRACE)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    def kernel(self, session: str, language: str) -> Kernel:
        if language != "python":
            raise ToolError("unsupported", f"no {language!r} kernel here")
        key = (session, language)
        if key not in self.kernels:
            cwd = self.resolve(session, ".")
            self.kernels[key] = Kernel(session, language, cwd, self.environment(session))
        return self.kernels[key]

    async def op_cell(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(session, str(request.get("language", "python")))
        code = request.get("code")
        if not isinstance(code, str):
            raise ToolError("invalid_request", "code must be a string")
        running = self.accept(request, "cell")
        running.kernel = kernel
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
        try:
            async with kernel.lock:
                running.started = True
                await kernel.ensure_started()
                state_lost, kernel.state_lost = kernel.state_lost, False
                await self.event(execution, "state", state="running")
                state, result = await self.execute(
                    running, kernel, code, timeout, limit, captures, outputs
                )
                result["state_lost"] = state_lost or result.get("state_lost", False)
        except asyncio.CancelledError:
            state = "cancelled"
            result = {"error": {"kind": "cancelled", "message": "cancelled before it started"}}
        except ToolError as error:
            state = "failed"
            result = {"error": {"kind": error.kind, "message": error.message}}
        finally:
            self.running.pop(execution, None)
            result.update(
                stdout=captures["stdout"].summary(),
                stderr=captures["stderr"].summary(),
                outputs=outputs,
                kernel=kernel.info() if kernel.generation else None,
                elapsed_seconds=time.monotonic() - started,
                sandbox_started=wall_started,
                sandbox_ended=time.time(),
            )
            await self.event(execution, "end", state=state, result=result)

    async def execute(
        self,
        running: Running,
        kernel: Kernel,
        code: str,
        timeout: float,
        limit: int,
        captures: dict[str, Capture],
        outputs: list[Message],
    ) -> tuple[str, Message]:
        from queue import Empty

        loop = asyncio.get_running_loop()
        client = kernel.client
        msg_id = client.execute(code, store_history=True, allow_stdin=False)
        deadline = loop.time() + timeout
        interrupted_at: float | None = None
        timed_out = False
        error: Message | None = None
        execution_count: int | None = None
        spent = 0
        while True:
            now = loop.time()
            if not timed_out and now >= deadline:
                timed_out = True
                interrupted_at = now
                await kernel.manager.interrupt_kernel()
            if interrupted_at is not None and now - interrupted_at > INTERRUPT_GRACE:
                # The kernel ignored the interrupt: restart it, losing its state.
                await kernel.restart()
                kind = "timed_out" if timed_out else "cancelled"
                return kind, {
                    "status": "aborted",
                    "error": {"kind": kind, "message": "the kernel did not stop and was restarted"},
                    "state_lost": True,
                }
            if running.cancel_requested and interrupted_at is None:
                interrupted_at = now
                await kernel.manager.interrupt_kernel()
            try:
                message = await client.get_iopub_msg(timeout=0.5)
            except Empty:
                if not await kernel.alive():
                    kernel.state_lost = True
                    return "failed", {
                        "status": "error",
                        "error": {"kind": "kernel_died", "message": "the kernel process ended"},
                    }
                continue
            if message.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind, content = message["msg_type"], message["content"]
            if kind == "status" and content.get("execution_state") == "idle":
                break
            if kind == "stream":
                capture = captures.get(content.get("name", "stdout"), captures["stdout"])
                if text := capture.feed(str(content.get("text", "")).encode()):
                    await self.event(running.execution, "stream", name=capture.name, text=text)
            elif kind == "execute_input":
                execution_count = content.get("execution_count")
            elif kind in ("display_data", "execute_result"):
                data = _mime_bundle(content.get("data", {}))
                size = sum(len(value) for value in data.values())
                spent += size
                if spent > limit:
                    data = {"text/plain": f"[output of {size} bytes omitted: over the limit]"}
                output: Message = {
                    "type": "result" if kind == "execute_result" else "display",
                    "data": data,
                    "execution_count": content.get("execution_count"),
                }
                outputs.append(output)
                await self.event(running.execution, "output", output=output)
            elif kind == "error":
                error = {
                    "kind": "exception",
                    "message": f"{content.get('ename')}: {content.get('evalue')}",
                    "name": content.get("ename"),
                    "value": content.get("evalue"),
                    "traceback": [_ANSI_RE.sub("", line) for line in content.get("traceback", [])],
                }
        status = "error" if error else "ok"
        with contextlib.suppress(Empty, Exception):
            reply = await client.get_shell_msg(timeout=5)
            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                status = str(reply["content"].get("status", status))
        result: Message = {"status": status, "execution_count": execution_count}
        if running.cancel_requested:
            result["error"] = {"kind": "cancelled", "message": "interrupted on request"}
            return "cancelled", result
        if timed_out:
            result["error"] = {"kind": "timed_out", "message": f"exceeded the {timeout:g} s limit"}
            return "timed_out", result
        if error is not None:
            # A cell that raised: its exception, as the kernel reported it.
            result["error"] = error
            return "failed", result
        return "succeeded", result

    async def op_cancel(self, request: Message) -> None:
        running = self.running.get(str(request.get("execution")))
        if running is None:
            await self.reply(request, found=False)
            return
        running.cancel_requested = True
        await self.reply(request, found=True)
        if running.kind == "shell" and running.process is not None:
            self.spawn(self.terminate(running.process))
        elif not running.started and running.task is not None:
            running.task.cancel()
        # A running cell is interrupted by its own loop, which sees cancel_requested.

    async def op_restart(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(session, str(request.get("language", "python")))
        async with kernel.lock:
            if kernel.manager is None:
                await kernel.ensure_started()
            else:
                await kernel.restart()
        await self.reply(request, kernel=kernel.info())

    async def op_read(self, request: Message) -> None:
        target = self.resolve(request.get("session"), request.get("path"))
        offset = int(request.get("offset", 0))
        length = min(int(request.get("length", MAX_READ)), MAX_READ)
        if offset < 0 or length < 0:
            raise ToolError("invalid_request", "offset and length must not be negative")
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise ToolError("not_found", f"no file {request.get('path')!r}") from exc
        except OSError as exc:
            raise ToolError(
                "invalid_request", f"cannot read {request.get('path')!r}: {exc}"
            ) from exc
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("invalid_request", f"{request.get('path')!r} is not a regular file")
            digest = hashlib.sha256()
            size = 0
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
            handle.seek(offset)
            content = handle.read(length)
        await self.reply(
            request,
            sha256=digest.hexdigest(),
            size=size,
            modified=info.st_mtime,
            offset=offset,
            content_b64=base64.b64encode(content).decode(),
            eof=offset + len(content) >= size,
        )

    async def op_write(self, request: Message) -> None:
        path = request.get("path")
        target = self.resolve(request.get("session"), path)
        try:
            content = base64.b64decode(str(request.get("content_b64", "")), validate=True)
        except ValueError as exc:
            raise ToolError("invalid_request", "content_b64 is not base64") from exc
        expected = request.get("expected_sha256")
        create_only = bool(request.get("create_only", False))
        shared = isinstance(path, str) and (path == SHARED or path.startswith(SHARED + "/"))
        if shared and expected is None and not create_only:
            raise ToolError(
                "invalid_request", "writes to the shared area need expected_sha256 or create_only"
            )
        if target.is_dir():
            raise ToolError("invalid_request", f"{path!r} is a directory")
        exists = target.exists()
        if create_only and exists:
            raise ToolError("conflict", f"{path!r} already exists")
        if expected is not None:
            if not exists:
                raise ToolError("conflict", f"{path!r} does not exist")
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if current != expected:
                raise ToolError("conflict", f"{path!r} changed: its SHA-256 is {current}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".write-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        await self.reply(request, sha256=hashlib.sha256(content).hexdigest(), size=len(content))

    async def op_list(self, request: Message) -> None:
        target = self.resolve(request.get("session"), request.get("path", "."))
        if not target.is_dir():
            raise ToolError("not_found", f"no directory {request.get('path')!r}")
        limit = min(int(request.get("limit", MAX_LIST)), MAX_LIST)
        entries: list[Message] = []
        with os.scandir(target) as listing:
            items = sorted(listing, key=lambda entry: entry.name)
        for entry in items[:limit]:
            info = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                kind = "symlink"
            elif entry.is_dir(follow_symlinks=False):
                kind = "directory"
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            else:
                kind = "other"
            entries.append(
                {"name": entry.name, "type": kind, "size": info.st_size, "modified": info.st_mtime}
            )
        await self.reply(request, entries=entries, truncated=len(items) > limit)


def _mime_bundle(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        str(mime): value if isinstance(value, str) else json.dumps(value)
        for mime, value in cast(dict[object, object], data).items()
    }


async def serve_stdio(root: Path) -> None:
    """Serve one connection on stdin and stdout until stdin closes."""
    loop = asyncio.get_running_loop()
    # Keep the protocol on its own descriptor; anything else printing to stdout
    # (a library, a kernel's launcher) goes to stderr instead.
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    reader = asyncio.StreamReader(limit=MAX_LINE)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(protocol_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    lock = asyncio.Lock()

    async def send(message: Message) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        async with lock:
            writer.write(line.encode())
            await writer.drain()

    executor = Executor(root, send)
    try:
        while line := await reader.readline():
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
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
    args = parser.parse_args(argv)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".executor"
    os.environ.setdefault("JUPYTER_RUNTIME_DIR", str(state / "runtime"))
    os.environ.setdefault("IPYTHONDIR", str(state / "ipython"))
    asyncio.run(serve_stdio(root.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
