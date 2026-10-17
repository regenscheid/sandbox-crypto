# SPDX-License-Identifier: MIT
"""Decoding what an executor sends: every line is untrusted (review H2, H3 and M3)."""

from __future__ import annotations

import json
import time

import pytest

from sandbox_science.worker import executor
from sandbox_science.worker.channel import MAX_CONTAINERS, MAX_LINE, ProtocolError, decode_line


def test_the_worker_accepts_every_line_the_executor_sends() -> None:
    assert MAX_LINE == executor.MAX_LINE


@pytest.mark.parametrize(
    "line",
    [
        b"[" * 100000 + b"]" * 100000,  # deep nesting
        b'{"n": ' + b"1" * 5000 + b"}",  # an integer too long to convert
        b"[1, 2]",  # not an object
        b"null",
        b"",
        b'{"a": "unfinished',
    ],
)
def test_lines_that_are_not_messages_are_refused(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_line(line)


def test_a_line_of_many_small_objects_is_refused_before_parsing() -> None:
    # M3: 60 MiB of [{},{},…] took the worker from 53 to 1,618 MiB.
    line = b"[" + b"{}," * (MAX_CONTAINERS + 10) + b"{}]"
    started = time.monotonic()
    with pytest.raises(ProtocolError, match="objects and arrays"):
        decode_line(line)
    assert time.monotonic() - started < 1


def test_brackets_inside_strings_do_not_count() -> None:
    text = "{[" * MAX_CONTAINERS
    message = decode_line(json.dumps({"type": "event", "text": text}).encode())
    assert message["text"] == text


def test_values_that_cannot_be_stored_are_cleaned() -> None:
    # H2: a lone surrogate could not be serialized again; Infinity was stored as null and
    # never validated again.
    line = b'{"text": "a\\ud800b", "\\udfff": 1, "inf": Infinity, "nan": NaN, "big": 1e999}'
    message = decode_line(line)
    assert message == {"text": "a�b", "�": 1, "inf": None, "nan": None, "big": None}
    pair = decode_line(b'{"text": "\\ud83d\\ude00"}')
    assert pair["text"] == "\U0001f600"


def test_invalid_utf8_is_replaced() -> None:
    assert decode_line(b'{"text": "\xff"}') == {"text": "�"}


async def test_waiting_for_large_write_does_not_close_healthy_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import Any

    from sandbox_science.contracts import ErrorKind
    from sandbox_science.worker.channel import ChannelTimeouts, ExecutorChannel
    from sandbox_science.worker.errors import WorkerError

    entered, release = asyncio.Event(), asyncio.Event()

    class Pipe:
        def __init__(self, process: Any) -> None:
            self.process = process
            self.large = False

        def write(self, line: bytes) -> None:
            request = json.loads(line)
            self.large = request["op"] == "write"
            if self.large:
                entered.set()
            self.process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "reply",
                        "id": request["id"],
                        "ok": True,
                        "max_request_bytes": 140 * 1024 * 1024,
                    }
                ).encode()
                + b"\n"
            )

        async def drain(self) -> None:
            if self.large:
                await release.wait()

        def close(self) -> None:
            self.process.stdout.feed_eof()
            self.process.returncode = 0

    class Process:
        def __init__(self) -> None:
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stderr.feed_eof()
            self.stdin = Pipe(self)
            self.returncode: int | None = None

        async def wait(self) -> int:
            return 0

    async def spawn(*args: object, **kwargs: object) -> Process:
        return Process()

    async def ignore(*args: object) -> None:
        pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    channel = ExecutorChannel(
        ["synthetic-executor"],
        ignore,
        ignore,
        timeouts=ChannelTimeouts(write=0.02, transfer_write=2),
    )
    await channel.start()
    sending = asyncio.create_task(channel.request("write", content_b64="eA==" * (3 * 1024 * 1024)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(WorkerError, match="busy") as caught:
            await channel.request("hello")
        assert caught.value.kind is ErrorKind.UNAVAILABLE
        assert not channel.closed
        release.set()
        assert (await sending)["ok"] is True
        assert (await channel.request("hello"))["ok"] is True
    finally:
        release.set()
        await sending
        await channel.close()
