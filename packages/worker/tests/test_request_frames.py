# SPDX-License-Identifier: MIT
"""Bounded request assembly and actual daemon bridge transport; synthetic data only."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker import daemon, executor, request_frames
from sandbox_science.worker.backends import OpenShellBackend
from sandbox_science.worker.channel import ChannelTimeouts, ExecutorChannel
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import OpenShellBackendSettings

pytestmark = pytest.mark.slow
TRANSFER = "a" * 32


def original(request: int = 1, size: int = 10) -> bytes:
    return (json.dumps({"id": request, "op": "hello", "padding": "x" * size}) + "\n").encode()


def frames(data: bytes, request: int = 1) -> list[bytes]:
    size = request_frames.CHUNK_BYTES
    return [
        request_frames.frame(
            TRANSFER, request, seq, offset + size >= len(data), data[offset : offset + size]
        )
        for seq, offset in enumerate(range(0, len(data), size))
    ]


def test_large_and_cumulative_requests_are_exact_and_reset() -> None:
    receiver = request_frames.Receiver(executor.MAX_REQUEST)
    for request in range(1, 4):
        data = original(request, 5 * 1024 * 1024)
        received = None
        for line in frames(data, request):
            assert len(line) <= 65536
            ack, received = receiver.consume(line)
            assert json.loads(ack)["id"] == request
            if received is None:
                assert len(receiver.data) <= len(data)
        assert received == data
        assert not receiver.data
        assert receiver.transfer is None


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"seq": True},
        {"final": 1},
        {"transfer": "bad"},
        {"seq": 2},
        {"data": "!bad"},
        {"data": ""},
        {"extra": 1},
        {"transport": "chunk-v2"},
    ],
)
def test_malformed_frames_never_complete(change: dict[str, Any]) -> None:
    receiver = request_frames.Receiver(1024)
    value = json.loads(frames(original())[0])
    value.update(change)
    with pytest.raises(ValueError, match=r".+"):
        receiver.consume((json.dumps(value) + "\n").encode())


@pytest.mark.parametrize(
    "data",
    [
        b'{"id":2,"op":"hello"}\n',
        b'{"id":1,"id":1,"op":"hello"}\n',
        b'{"id":1,"op":"hello"}\n{}\n',
        b'{"id":1',
        b"[]\n",
    ],
)
def test_invalid_complete_original_never_dispatches(data: bytes) -> None:
    with pytest.raises(ValueError, match=r".+"):
        request_frames.Receiver(1024).consume(frames(data)[0])


def test_changed_transfer_or_request_and_aggregate_overflow_refused() -> None:
    for field, value in (("id", 2), ("transfer", "b" * 32), ("seq", 0)):
        receiver = request_frames.Receiver(executor.MAX_REQUEST)
        lines = frames(original(size=40000))
        assert receiver.consume(lines[0])[1] is None
        changed = json.loads(lines[1])
        changed[field] = value
        with pytest.raises(ValueError, match=r".+"):
            receiver.consume((json.dumps(changed) + "\n").encode())
    with pytest.raises(ValueError, match=r".+"):
        request_frames.Receiver(10).consume(frames(original())[0])


async def test_partial_disconnect_is_not_a_complete_request() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(frames(original(size=40000))[0])
    reader.feed_eof()
    acks: list[bytes] = []

    async def ack(value: bytes) -> None:
        acks.append(value)

    with pytest.raises(ValueError, match="incomplete"):
        await request_frames.receive(reader, request_frames.Receiver(100000), ack)
    assert len(acks) == 1


@pytest_asyncio.fixture
async def bridges(tmp_path: Path) -> AsyncIterator[Any]:
    channels: list[ExecutorChannel] = []

    async def event(_message: dict[str, Any]) -> None:
        pass

    async def closed(_channel: ExecutorChannel) -> None:
        pass

    async def start(on_event: Any = event) -> ExecutorChannel:
        channel = ExecutorChannel(
            [
                sys.executable,
                "-I",
                str(Path(daemon.__file__)),
                "--framed",
                "--root",
                str(tmp_path / "project"),
            ],
            on_event,
            closed,
            daemon_token="a" * 64,
            framed=True,
            timeouts=ChannelTimeouts(close_grace=1),
        )
        channels.append(channel)
        await channel.start()
        await channel.request(
            "package_gate",
            incarnation=channel.hello["package_gate"],
            generation=0,
            mode="ready",
            operation="",
            revision=None,
        )
        return channel

    try:
        yield start
    finally:
        if channels:
            channel = next((c for c in reversed(channels) if not c.closed), None)
            if channel is None:
                channel = await start()
            await channel.shutdown()
        await asyncio.gather(*(channel.close() for channel in channels))


async def test_large_concurrent_writes_reconnect_same_incarnation(
    bridges: Any, tmp_path: Path
) -> None:
    channel = await bridges()
    content = b"x" * (5 * 1024 * 1024)
    encoded = base64.b64encode(content).decode()

    async def write(name: str) -> None:
        await channel.request("write", session="s", path=name, content_b64=encoded)

    await asyncio.gather(write("one"), write("two"))
    root = tmp_path / "project/sessions/s"
    assert (root / "one").read_bytes() == (root / "two").read_bytes() == content
    incarnation = channel.incarnation
    await channel.detach()
    replacement = await bridges()
    assert replacement.incarnation == incarnation
    assert (await replacement.request("hello"))["ok"] is True


async def test_actual_daemon_event_callback_can_issue_a_request(bridges: Any) -> None:
    channel: ExecutorChannel
    callback_done = asyncio.Event()

    async def event(_message: dict[str, Any]) -> None:
        await channel.request("active")
        callback_done.set()

    channel = await bridges(event)
    await channel.request("shell", execution="exe-" + "b" * 24, session="s", script="printf framed")
    await asyncio.wait_for(callback_done.wait(), 5)


async def test_ack_reader_does_not_wait_for_event_callback_request(tmp_path: Path) -> None:
    callback_ready, release = asyncio.Event(), asyncio.Event()
    channel: ExecutorChannel

    async def event(_message: dict[str, Any]) -> None:
        await channel.request("active")
        callback_ready.set()
        await release.wait()

    channel, _record = await peer_channel(tmp_path, "event", event)
    try:
        pending = asyncio.create_task(channel.request("write", content_b64="a" * 100000))
        await asyncio.wait_for(callback_ready.wait(), 5)
        assert not pending.done()  # preceding callback must commit before normal reply returns
        release.set()
        await asyncio.wait_for(pending, 5)
    finally:
        release.set()
        await channel.close()


async def test_openshell_stdio_is_explicitly_unsupported_before_start(tmp_path: Path) -> None:
    backend = OpenShellBackend(OpenShellBackendSettings(daemon=False, isolation_tier=3), tmp_path)

    async def event(_message: dict[str, Any]) -> None:
        pass

    async def closed(_channel: ExecutorChannel) -> None:
        pass

    with pytest.raises(WorkerError, match="daemon=true"):
        await backend.open("anything", event, closed)
    assert list(tmp_path.iterdir()) == []


PEER = r"""
import json,select,sys,time
from pathlib import Path
record=Path(sys.argv[1]); mode=sys.argv[2]
for line in sys.stdin.buffer:
    frame=json.loads(line)
    # A sender that honors ACK backpressure has not queued another transport frame.
    ahead=bool(select.select([sys.stdin],[],[],0.01)[0])
    entry={'seq':frame['seq'],'final':frame['final'],'ahead':ahead}
    with record.open('a') as f: f.write(json.dumps(entry)+'\n')
    ack={'type':'transport_ack','transfer':frame['transfer'],'id':frame['id'],'seq':frame['seq']}
    if frame['seq']==0 and ((frame['id']==2 and mode in ('event','eof')) or
                           (frame['id'] in (2,3) and mode=='two-events')):
        print(json.dumps({'type':'event','event':'stream','execution':'synthetic'}),flush=True)
    if frame['id']>1 and mode=='stall':
        time.sleep(30)
    if frame['id']>1 and mode=='bad': ack['seq']+=1
    print(json.dumps(ack),flush=True)
    if frame['final']:
        print(json.dumps({'type':'reply','id':frame['id'],'ok':True,'max_request_bytes':1000000}),flush=True)
        if mode=='eof' and frame['id']==2:
            while not record.with_suffix('.entered').exists(): time.sleep(0.001)
            break
"""


async def peer_channel(
    tmp_path: Path, mode: str, on_event: Any = None, on_closed: Any = None
) -> tuple[ExecutorChannel, Path]:
    script, record = tmp_path / "peer.py", tmp_path / "frames.jsonl"
    script.write_text(PEER)

    async def event(_message: dict[str, Any]) -> None:
        pass

    async def closed(_channel: ExecutorChannel) -> None:
        pass

    channel = ExecutorChannel(
        [sys.executable, "-I", str(script), str(record), mode],
        on_event or event,
        on_closed or closed,
        framed=True,
        timeouts=ChannelTimeouts(write=2, close_grace=0.1),
    )
    await channel.start()
    # ``stall`` delays requests after the handshake. Give the synthetic peer a
    # normal startup allowance, then narrow only the transfer under test; on a
    # busy runner, applying the 0.2 s deadline to hello can cancel its first ACK
    # and turn a write-timeout test into an unrelated protocol failure.
    if mode == "stall":
        channel.timeouts = ChannelTimeouts(write=0.2, close_grace=0.1)
    return channel, record


async def test_slow_receiver_has_one_unacknowledged_frame(tmp_path: Path) -> None:
    channel, record = await peer_channel(tmp_path, "slow")
    try:
        await channel.request("write", content_b64="a" * 100000)
        seen = [json.loads(line) for line in record.read_text().splitlines()]
        assert len(seen) > 3
        assert not any(frame["ahead"] for frame in seen)
        assert seen[-1]["final"] is True
    finally:
        await channel.close()


@pytest.mark.parametrize("mode", ["stall", "bad"])
async def test_missing_or_wrong_ack_closes_partial_connection(tmp_path: Path, mode: str) -> None:
    channel, record = await peer_channel(tmp_path, mode)
    try:
        with pytest.raises(WorkerError, match="executor"):
            await channel.request("write", content_b64="a" * 100000)
        await channel.close()
        seen = [
            json.loads(line)
            for line in record.read_text().splitlines()
            if json.loads(line)["seq"] > 0
        ]
        assert not any(frame["final"] for frame in seen)
        assert channel.closed
    finally:
        await channel.close()


async def test_cancel_partial_transfer_closes_instead_of_dispatch(tmp_path: Path) -> None:
    channel, record = await peer_channel(tmp_path, "stall")
    try:
        pending = asyncio.create_task(channel.request("write", content_b64="a" * 100000))
        from sandbox_science.testing import eventually

        await eventually(lambda: len(record.read_text().splitlines()) >= 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await channel.close()
        assert not json.loads(record.read_text().splitlines()[-1])["final"]
        assert channel.closed
    finally:
        await channel.close()


async def test_stale_authority_rechecked_before_final_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker import channel as channels

    channel, record = await peer_channel(tmp_path, "slow")
    checks = 0

    def gate(_op: str) -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise WorkerError(ErrorKind.CONFLICT, "stale test authority")

    monkeypatch.setattr(channels, "check_dispatch", gate)
    try:
        with pytest.raises(WorkerError, match="stale test authority"):
            await channel.request("write", content_b64="a" * 40000)
        await channel.close()
        assert checks == 3
        assert not json.loads(record.read_text().splitlines()[-1])["final"]
    finally:
        await channel.close()


async def test_eof_drains_callback_before_recovery_and_refuses_uncommitted_reply(
    tmp_path: Path,
) -> None:
    entered, release, handed_off = asyncio.Event(), asyncio.Event(), asyncio.Event()
    committed = False

    async def event(_message: dict[str, Any]) -> None:
        nonlocal committed
        record.with_suffix(".entered").touch()
        entered.set()
        await release.wait()
        committed = True

    async def closed(_channel: ExecutorChannel) -> None:
        assert committed
        handed_off.set()

    channel, record = await peer_channel(tmp_path, "eof", event, closed)
    pending = asyncio.create_task(channel.request("write", content_b64="a" * 40000))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(WorkerError, match="interrupted"):
            await pending
        assert not handed_off.is_set()
        release.set()
        await asyncio.wait_for(handed_off.wait(), 5)
        assert committed
    finally:
        release.set()
        await channel.close()


async def test_background_task_cannot_reuse_finished_callback_barrier_bypass(
    tmp_path: Path,
) -> None:
    trigger, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    channel: ExecutorChannel
    background: asyncio.Task[Any] | None = None
    calls = 0

    async def later() -> None:
        await trigger.wait()
        await channel.request("active")

    async def event(_message: dict[str, Any]) -> None:
        nonlocal calls, background
        calls += 1
        if calls == 1:
            background = asyncio.create_task(later())
        else:
            second.set()
            await release.wait()

    channel, _record = await peer_channel(tmp_path, "two-events", event)
    try:
        await channel.request("write", content_b64="a" * 40000)
        pending = asyncio.create_task(channel.request("write", content_b64="b" * 40000))
        await asyncio.wait_for(second.wait(), 5)
        assert background is not None
        trigger.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(background), 0.2)
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, background), 5)
    finally:
        trigger.set()
        release.set()
        await channel.close()
