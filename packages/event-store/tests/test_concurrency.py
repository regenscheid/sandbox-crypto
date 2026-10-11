# SPDX-License-Identifier: MIT
"""Concurrent appends never fork the chain: threads, asyncio tasks and processes."""

import asyncio
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.event_store import EventStore, StoreReader, verify_store

SOURCE = "test"


def assert_one_chain(path: Path, expected: int) -> list[str]:
    """The store holds ``expected`` records in one intact chain; return their kinds."""
    result = verify_store(path)
    assert result.ok, result.problems
    assert result.records == result.head.seq == expected
    with StoreReader(path) as reader:
        return [record.kind for record in reader.records()]


def test_threads_sharing_a_store(path: Path) -> None:
    with EventStore(path) as store, ThreadPoolExecutor(max_workers=8) as pool:

        def work(worker: int) -> list[int]:
            return [
                store.append("alert", SOURCE, {"worker": worker, "n": n}).seq for n in range(50)
            ]

        seqs = [seq for result in pool.map(work, range(8)) for seq in result]
    assert sorted(seqs) == list(range(1, 401))
    assert_one_chain(path, 400)


def test_threads_with_their_own_connections(path: Path) -> None:
    EventStore(path).close()

    def work(worker: int) -> None:
        with EventStore(path, timeout=30) as store:
            for n in range(25):
                store.append(f"worker-{worker}", SOURCE, {"n": n})

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(6)))
    assert Counter(assert_one_chain(path, 150)) == {f"worker-{w}": 25 for w in range(6)}


@pytest.mark.asyncio
async def test_asyncio_tasks(path: Path) -> None:
    with EventStore(path) as store:
        records = await asyncio.gather(
            *(store.append_async("alert", SOURCE, {"task": task}) for task in range(200))
        )
    assert sorted(record.seq for record in records) == list(range(1, 201))
    assert_one_chain(path, 200)


@pytest.mark.asyncio
async def test_async_appends_leave_the_event_loop_running(path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_clock() -> datetime:
        # Stands in for a slow commit: the append waits here until released.
        entered.set()
        release.wait(10)
        return datetime.now(UTC)

    with EventStore(path, clock=slow_clock) as store:
        task = asyncio.create_task(store.append_async("alert", SOURCE, {}))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        # Were the append running on the loop, this coroutine could not run
        # until it finished.
        assert not task.done()
        release.set()
        assert (await task).seq == 1


_WRITER = """
import sys
from pathlib import Path
from sandbox_science.event_store import EventStore

path, kind, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
with EventStore(path, timeout=60) as store:
    for n in range(count):
        store.append(kind, "test", {"n": n})
"""


def test_processes_appending_to_one_store(path: Path) -> None:
    EventStore(path).close()
    writers = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _WRITER, str(path), f"process-{index}", "100"],
        )
        for index in range(4)
    ]
    assert [writer.wait(timeout=120) for writer in writers] == [0, 0, 0, 0]
    assert Counter(assert_one_chain(path, 400)) == {f"process-{i}": 100 for i in range(4)}
