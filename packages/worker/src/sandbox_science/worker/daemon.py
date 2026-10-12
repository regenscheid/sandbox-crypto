# SPDX-License-Identifier: MIT
"""A sandbox-lifetime executor with an authenticated, single-active stdio bridge.

The daemon owns research processes; bridge EOF only detaches the worker. Its bounded
memory journal pins lifecycle snapshots and reports dropped stream ranges explicitly.
The token never crosses the Unix socket: peers authenticate mutually with HMAC nonces.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

if __package__:
    from . import executor
else:
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "sandbox_science_executor", Path(__file__).with_name("executor.py")
    )
    if _spec is None or _spec.loader is None:
        raise ImportError("executor.py is missing")
    executor = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = executor
    _spec.loader.exec_module(executor)

Message = dict[str, Any]
JOURNAL_BYTES = 64 * 1024 * 1024
EXECUTION_BYTES = 2 * 1024 * 1024
SNAPSHOT_BYTES = 64 * 1024
MAX_RECORDS = 4096
MAX_ACTIVE = 128
PAGE_EVENTS = 32
AUTH_TIMEOUT = 10.0


def _mac(token: str, value: str) -> str:
    return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()


def address(token: str, root: Path) -> str:
    """Linux abstract socket: no researcher-controlled filesystem component is followed."""
    return "\0ss-executor-" + hashlib.sha256((token + str(root)).encode()).hexdigest()[:40]


@dataclass
class History:
    sequence: int = 0
    events: OrderedDict[int, bytes] = field(default_factory=OrderedDict[int, bytes])
    size: int = 0
    ended: bool = False
    disposable: set[int] = field(default_factory=set[int])
    compact_end: tuple[int, bytes] | None = None
    state_seq: int | None = None


class Daemon:
    def __init__(self, root: Path, token: str, kernel_idle: float | None = None) -> None:
        self.root, self.token = root, token
        self.incarnation = "inc-" + secrets.token_hex(12)
        self.writer: asyncio.StreamWriter | None = None
        self.pending: dict[int, tuple[asyncio.StreamWriter, int, str, str | None]] = {}
        self.next_request = 0
        self.history: OrderedDict[str, History] = OrderedDict()
        self.bytes = 0
        self.stop = asyncio.Event()
        self.stopping = False
        self.executor = executor.Executor(
            root,
            self.send,
            subreaper=executor.enable_subreaper(),
            protected=executor.protect_process(),
            kernel_idle=kernel_idle,
        )
        self.owner_scope = "ss-daemon-" + address(token, root)[13:] + "-"
        self.executor.owner_prefix = self.owner_scope + self.incarnation + "-"
        if kernel_idle is not None:
            self.executor.reaper = self.executor.spawn(self.executor.reap_idle_kernels(kernel_idle))

    async def write(self, writer: asyncio.StreamWriter, message: Message | bytes) -> None:
        data = message if isinstance(message, bytes) else executor.fit_line(message)
        if data is None:
            return
        writer.write(data)
        try:
            await asyncio.wait_for(writer.drain(), 10)
        except (OSError, TimeoutError):
            writer.close()
            raise ConnectionError("bridge stopped reading") from None

    async def send(self, message: Message) -> None:
        if message.get("type") == "reply":
            target = self.pending.pop(int(message.get("id", -1)), None)
            if target is None:
                return
            writer, request_id, op, execution_id = target
            # A rejected request reserved capacity but will never produce an end event.
            if execution_id is not None and message.get("ok") is not True:
                history = self.history.get(execution_id)
                if history is not None and not history.ended:
                    await self.send(
                        {
                            "type": "event",
                            "execution": execution_id,
                            "event": "end",
                            "state": "failed",
                            "result": {"error": message.get("error")},
                        }
                    )
            if writer is not self.writer or writer.is_closing():
                return
            reply = {**message, "id": request_id}
            if op == "hello":
                reply["daemon_incarnation"] = self.incarnation
            with contextlib.suppress(ConnectionError):
                await self.write(writer, reply)
            return
        if message.get("type") != "event":
            return
        execution = str(message.get("execution", ""))
        history = self.history.get(execution)
        if history is None:
            return
        history.sequence += 1
        message = {**message, "source_seq": history.sequence}
        if message.get("event") == "end":
            message = executor.bounded_observations(message)
            history.ended = True
            # Preserve ordinary final output. A separate small snapshot survives journal
            # pressure; only that fallback advertises recovery output compaction.
            result = dict(cast(Message, message.get("result") or {}))
            compact = {**message, "result": result, "recovery_compacted": True}
            while len(executor.encode_line(compact)) > SNAPSHOT_BYTES:
                if not executor.shrink_result(result):
                    break
            if len(executor.encode_line(compact)) > SNAPSHOT_BYTES:
                compact["result"] = {}
            compact_data = executor.fit_line(compact)
            if compact_data is not None:
                history.compact_end = (history.sequence, compact_data)
                history.size += len(compact_data)
                self.bytes += len(compact_data)
        elif message.get("event") == "state":
            if history.state_seq is not None and history.state_seq in history.events:
                self.remove_event(history, history.state_seq)
            history.state_seq = history.sequence
            # Pin only bounded lifecycle metadata, even after executor compromise.
            fingerprint = message.get("environment")
            message = {key: message[key] for key in ("type", "execution", "event", "source_seq")}
            message["state"] = "running"
            if isinstance(fingerprint, str) and len(fingerprint) <= 256:
                message["environment"] = fingerprint
        else:
            history.disposable.add(history.sequence)
        data = executor.fit_line(message)
        if data is None:
            history.disposable.discard(history.sequence)
            return
        history.events[history.sequence] = data
        history.size += len(data)
        self.bytes += len(data)
        self.trim(execution)

    def remove_event(self, history: History, sequence: int) -> None:
        size = len(history.events.pop(sequence))
        history.disposable.discard(sequence)
        history.size -= size
        self.bytes -= size

    def trim(self, execution: str) -> None:
        history = self.history[execution]
        # Per-execution stream allowance excludes the final result, which already has
        # the protocol's line bound and must retain the caller's approved output limit.
        while history.size > EXECUTION_BYTES and history.disposable:
            self.remove_event(history, min(history.disposable))
        while self.bytes > JOURNAL_BYTES:
            # Select globally: the execution appending a state may have no streams.
            victim = next((item for item in self.history.values() if item.disposable), None)
            if victim is not None:
                self.remove_event(victim, min(victim.disposable))
                continue
            compacted = False
            for item in self.history.values():
                if item.compact_end is not None:
                    seq, compact = item.compact_end
                    original = item.events.get(seq)
                    if original is not None and len(original) > len(compact):
                        item.events[seq] = compact
                        difference = len(original) - len(compact)
                        difference += len(compact)
                        item.size -= difference
                        self.bytes -= difference
                        item.compact_end = None
                        compacted = True
                        break
            if compacted:
                continue
            ended = next((key for key, item in self.history.items() if item.ended), None)
            if ended is None:
                break  # MAX_ACTIVE bounds the small pinned running-state records.
            self.bytes -= self.history.pop(ended).size
        while len(self.history) > MAX_RECORDS:
            ended = next((key for key, item in self.history.items() if item.ended), None)
            if ended is None:
                break
            self.bytes -= self.history.pop(ended).size

    async def replay(self, writer: asyncio.StreamWriter, request: Message) -> None:
        execution, after = str(request.get("execution", "")), request.get("after", 0)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError("invalid replay cursor")
        history = self.history.get(execution)
        if history is None:
            await self.write(
                writer, {"type": "reply", "id": request["id"], "ok": True, "found": False}
            )
            return
        # Snapshot a bounded page before network awaits; appends cannot reorder it.
        events = [(seq, data) for seq, data in history.events.items() if seq > after][:PAGE_EVENTS]
        cursor = after
        for seq, data in events:
            if seq > cursor + 1:
                await self.write(
                    writer,
                    {
                        "type": "event",
                        "execution": execution,
                        "event": "stream",
                        "name": "stderr",
                        "text": (
                            "[output chunks omitted while disconnected; "
                            "whole job logs remain in the workspace]\n"
                        ),
                        "source_seq": seq - 1,
                        "source_from": cursor + 1,
                    },
                )
            await self.write(writer, data)
            cursor = seq
        if not events and history.sequence > cursor:
            cursor = history.sequence
            await self.write(
                writer,
                {
                    "type": "event",
                    "execution": execution,
                    "event": "stream",
                    "name": "stderr",
                    "text": (
                        "[output chunks omitted while disconnected; "
                        "whole job logs remain in the workspace]\n"
                    ),
                    "source_seq": cursor,
                    "source_from": after + 1,
                },
            )
        await self.write(
            writer,
            {
                "type": "reply",
                "id": request["id"],
                "ok": True,
                "found": True,
                "ended": history.ended,
                "last": history.sequence,
                "more": cursor < history.sequence,
            },
        )

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        claimed = False
        try:
            async with asyncio.timeout(AUTH_TIMEOUT):
                nonce = (await reader.readexactly(65)).decode().strip()
                if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
                    return
                challenge = secrets.token_hex(32)
                await self.write(
                    writer,
                    {
                        "incarnation": self.incarnation,
                        "challenge": challenge,
                        "proof": _mac(self.token, "server:" + nonce + self.incarnation + challenge),
                    },
                )
                proof = (await reader.readexactly(65)).decode().strip()
                if not hmac.compare_digest(proof, _mac(self.token, "client:" + challenge + nonce)):
                    return
                if self.stop.is_set() or getattr(self, "stopping", False):
                    await self.write(writer, {"error": "daemon stopping"})
                    return
                if self.writer is not None and not self.writer.is_closing():
                    await self.write(writer, {"error": "another bridge is active"})
                    return
                self.writer = writer
                claimed = True
                await self.write(writer, {"ok": True})
            while line := await reader.readline():
                # A drain failure may close the old writer while its reader still has
                # buffered requests. Authentication of a replacement fences all of them.
                if self.writer is not writer or writer.is_closing():
                    return
                decoded: object = json.loads(line)
                if not isinstance(decoded, dict):
                    raise ValueError("invalid request")
                request = cast(Message, decoded)
                if not isinstance(request.get("id"), int) or isinstance(request.get("id"), bool):
                    raise ValueError("invalid request")
                op = request.get("op")
                if op == "replay":
                    await self.replay(writer, request)
                elif op == "daemon_shutdown":
                    self.stopping = True
                    try:
                        await self.executor.shutdown()
                        await self.write(writer, {"type": "reply", "id": request["id"], "ok": True})
                    finally:
                        # The lifecycle is finished even if its acknowledgement is lost.
                        self.stop.set()
                    break
                elif op == "active":
                    active = [key for key, item in self.history.items() if not item.ended]
                    await self.write(
                        writer,
                        {"type": "reply", "id": request["id"], "ok": True, "executions": active},
                    )
                else:
                    execution_id: str | None = None
                    if len(self.pending) >= 256:
                        raise ValueError("too many requests")
                    if op in ("job", "shell", "cell", "pdf"):
                        execution = str(request.get("execution", ""))
                        if not re.fullmatch(r"exe-[0-9a-f]{24}", execution):
                            raise ValueError("invalid execution ID")
                        if execution in self.history:
                            await self.write(
                                writer,
                                {
                                    "type": "reply",
                                    "id": request["id"],
                                    "ok": False,
                                    "error": {
                                        "kind": "conflict",
                                        "message": "execution ID was already accepted",
                                    },
                                },
                            )
                            continue
                        if sum(not item.ended for item in self.history.values()) >= MAX_ACTIVE:
                            await self.write(
                                writer,
                                {
                                    "type": "reply",
                                    "id": request["id"],
                                    "ok": False,
                                    "error": {
                                        "kind": "policy_denied",
                                        "message": "executor active capacity reached",
                                    },
                                },
                            )
                            continue
                        self.history[execution] = History()
                        execution_id = execution
                        self.trim(execution)
                    self.next_request += 1
                    self.pending[self.next_request] = (writer, request["id"], str(op), execution_id)
                    self.executor.spawn(
                        self.handle_request(writer, {**request, "id": self.next_request})
                    )
        except (OSError, ValueError, TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                if claimed and self.writer is writer:
                    self.writer = None
                    # Research jobs survive bridge detach, temporary file readers do not.
                    # Snapshot admission before another controller can enter.
                    await self.executor.captures.cancel_active()
            finally:
                writer.close()

    async def handle_request(self, writer: asyncio.StreamWriter, request: Message) -> None:
        """Fence a queued handler too, before it can submit or cancel anything."""
        if self.writer is not writer or writer.is_closing() or self.stopping:
            await self.send(
                {
                    "type": "reply",
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "kind": "unavailable",
                        "message": "controller detached before acceptance",
                    },
                }
            )
            return
        await self.executor.handle(
            {
                **request,
                "_admission_guard": lambda: (
                    self.writer is writer and not writer.is_closing() and not self.stopping
                ),
            }
        )

    async def serve(self) -> None:
        server = await asyncio.start_unix_server(
            self.client, address(self.token, self.root), limit=executor.MAX_REQUEST
        )
        # A dead daemon cannot own jobs any more. Reap only this endpoint's old
        # incarnation marks, with start-time checks against PID reuse.
        for _ in range(executor.KILL_ROUNDS):
            targets = [
                info
                for info in executor.process_table().values()
                if (owner := executor.process_owner(info.pid))
                and owner.startswith(self.owner_scope)
                and not owner.startswith(self.executor.owner_prefix)
            ]
            if not targets:
                break
            executor.signal_processes(targets, signal.SIGKILL)
            await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, self.stop.set)
        try:
            async with server:
                await self.stop.wait()
        finally:
            self.stopping = True
            await self.executor.shutdown()
            if self.writer is not None:
                self.writer.close()


async def connect(token: str, root: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(
        address(token, root), limit=executor.MAX_REQUEST
    )
    nonce = secrets.token_hex(32)
    writer.write((nonce + "\n").encode())
    await writer.drain()
    greeting = json.loads(await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT))
    incarnation, challenge = greeting.get("incarnation"), greeting.get("challenge")
    if (
        not isinstance(incarnation, str)
        or not isinstance(challenge, str)
        or not hmac.compare_digest(
            str(greeting.get("proof", "")), _mac(token, "server:" + nonce + incarnation + challenge)
        )
    ):
        writer.close()
        raise ConnectionError("daemon authentication failed")
    writer.write((_mac(token, "client:" + challenge + nonce) + "\n").encode())
    await writer.drain()
    accepted = json.loads(await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT))
    if accepted.get("ok") is not True:
        writer.close()
        raise ConnectionError("another bridge is active")
    return reader, writer


async def bridge(root: Path, kernel_idle: float | None) -> None:
    loop = asyncio.get_running_loop()
    incoming = asyncio.StreamReader(limit=executor.MAX_REQUEST)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(incoming), sys.stdin)
    first = await incoming.readline()
    request = json.loads(first)
    token = request.pop("daemon_token", "")
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError("missing daemon token")
    connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
    try:
        connection = await connect(token, root)
    except (ConnectionRefusedError, FileNotFoundError):
        argv = [sys.executable, "-I", str(Path(__file__)), "--serve", "--root", str(root)]
        if kernel_idle is not None:
            argv += ["--kernel-idle", str(kernel_idle)]
        process = subprocess.Popen(  # noqa: S603 - fixed sandbox-local Python bridge
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdin is None:
            raise RuntimeError("bootstrap pipe unavailable") from None
        process.stdin.write((token + "\n").encode())
        process.stdin.close()
        for attempt in range(100):
            await asyncio.sleep(0.05)
            try:
                connection = await connect(token, root)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                if attempt == 99:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=5)
                    raise
    if connection is None:
        raise ConnectionError("daemon bootstrap failed")
    reader, writer = connection
    writer.write(executor.encode_line(request))
    await writer.drain()

    async def to_daemon() -> None:
        while line := await incoming.readline():
            writer.write(line)
            await writer.drain()

    async def to_worker() -> None:
        while line := await reader.readline():
            await asyncio.to_thread(executor.write_log, 1, line)

    tasks = [asyncio.create_task(to_daemon()), asyncio.create_task(to_worker())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--kernel-idle", type=float)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.serve:
        token = sys.stdin.readline(256).strip()
        if len(token) != 64:
            raise ValueError("invalid bootstrap token")
        state = root / ".executor"
        (state / "runtime").mkdir(parents=True, exist_ok=True)
        (state / "ipython").mkdir(parents=True, exist_ok=True)
        (root / "shared").mkdir(exist_ok=True)
        os.environ.setdefault("JUPYTER_RUNTIME_DIR", str(state / "runtime"))
        os.environ.setdefault("IPYTHONDIR", str(state / "ipython"))
        asyncio.run(Daemon(root, token, args.kernel_idle).serve())
    else:
        asyncio.run(bridge(root, args.kernel_idle))


if __name__ == "__main__":
    main()
