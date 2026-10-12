# SPDX-License-Identifier: MIT
"""Hostile pipes, cancellation and exact renderer protocol; no provider calls."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator import mathcheck
from sandbox_science.orchestrator.mathcheck import ENGINE, OPTIONS, Formula, MathChecker


def fixture_checker(tmp_path: Path, body: str, *, timeout: float = 1.0) -> MathChecker:
    executable = tmp_path / "fake-node"
    executable.write_text(
        f"#!{sys.executable}\nimport json,sys,time,os\n"
        "request=json.loads(sys.stdin.buffer.read())\n" + body
    )
    executable.chmod(0o700)
    bundle = tmp_path / "bundle"
    bundle.write_text("trusted fixture")
    launcher = tmp_path / "launcher"
    launcher.write_text("trusted launcher fixture")
    return MathChecker(executable, launcher, bundle, timeout=timeout)


def answer(**updates: object) -> str:
    result = {
        "protocol": 1,
        "id": "m1",
        "engine": ENGINE,
        "runtime": "24.20.0",
        "options": OPTIONS,
        "max_formula": 5000,
        "status": "accepted",
        "error": None,
        "remedy": None,
    }
    result.update(updates)
    return "print(" + repr(json.dumps(result)) + ")\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"id": "other"},
        {"engine": "wrong"},
        {"runtime": "20.0.0"},
        {"protocol": True},
        {"max_formula": 5000.0},
        {"options": {**OPTIONS, "maxSize": 20.0}},
        {"options": {**OPTIONS, "trust": True}},
        {"status": "accepted", "remedy": "x"},
        {"status": "rejected", "error": None},
        {"extra": "ignored?"},
    ],
)
async def test_response_mismatch_is_inconclusive(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    result = await fixture_checker(tmp_path, answer(**updates)).check(Formula(id="m1", tex="x"))
    assert result.status == "unavailable"
    assert result.remedy is None


@pytest.mark.asyncio
async def test_deeply_nested_reply_is_inconclusive_and_reaped(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    checker = fixture_checker(
        tmp_path,
        f"open({str(pidfile)!r},'w').write(str(os.getpid()))\nprint('['*16000+'0'+']'*16000)\n",
    )
    result = await checker.check(Formula(id="m1", tex="x"))
    assert result.status == "unavailable"
    assert result.remedy is None
    assert checker._pending == 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_chunked_answer_and_provenance(tmp_path: Path) -> None:
    raw = answer().removeprefix("print(").removesuffix(")\n")
    checker = fixture_checker(
        tmp_path, f"text={raw}\nfor char in text:\n sys.stdout.write(char);sys.stdout.flush()\n"
    )
    result = await checker.check(Formula(id="m1", tex="x"))
    assert result.status == "accepted"
    assert result.engine == ENGINE
    assert len(result.input_sha256) == 64
    assert result.bundle_sha256 is not None
    assert len(result.bundle_sha256) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
async def test_flood_is_bounded_and_reaped(tmp_path: Path, pipe: str) -> None:
    pidfile = tmp_path / "pid"
    checker = fixture_checker(
        tmp_path,
        f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        f"while True: sys.{pipe}.write('x'*4096);sys.{pipe}.flush()\n",
    )
    result = await checker.check(Formula(id="m1", tex="x"))
    assert result.status == "unavailable"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_reap_drains_paused_subprocess_pipes() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys,time;sys.stdout.buffer.write(b'x'*300000);sys.stdout.flush();time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024,
        start_new_session=True,
    )
    assert process.stdout is not None
    try:
        async with asyncio.timeout(2):
            # Exercise a genuinely paused transport, rather than relying on scheduling.
            while not getattr(process.stdout, "_paused", False):
                await asyncio.sleep(0.001)
        await asyncio.wait_for(mathcheck._reap(process), 1)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        await process.wait()


@pytest.mark.asyncio
async def test_deadline_and_cancellation_reap_children(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    checker = fixture_checker(
        tmp_path,
        f"open({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n",
        timeout=0.2,
    )
    result = await checker.check(Formula(id="m1", tex="x"))
    assert result.status == "unavailable"
    assert result.error == "math checker deadline exceeded"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    pidfile.unlink()
    task = asyncio.create_task(checker.check(Formula(id="m1", tex="x")))
    async with asyncio.timeout(2):
        while not pidfile.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert checker._pending == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["spawn", "cleanup"])
async def test_repeated_cancellation_keeps_ownership_until_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []
    original_spawn = asyncio.create_subprocess_exec
    original_reap = mathcheck._reap

    async def gated_spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        # Forward this private fixture's opaque subprocess keyword arguments unchanged.
        process = await original_spawn(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        children.append(process)
        if phase == "spawn":
            entered.set()
            await release.wait()
        return process

    async def gated_reap(process: asyncio.subprocess.Process) -> None:
        if phase == "cleanup":
            entered.set()
            await release.wait()
        await original_reap(process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    monkeypatch.setattr(mathcheck, "_reap", gated_reap)
    checker = fixture_checker(tmp_path, "time.sleep(30)\n", timeout=0.2)
    task = asyncio.create_task(checker.check(Formula(id="m1", tex="x")))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert checker._pending == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert checker._pending == 0
        assert children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in children:
            await original_reap(process)


@pytest.mark.asyncio
async def test_batch_and_admission_limits(tmp_path: Path) -> None:
    checker = fixture_checker(tmp_path, answer())
    formula = Formula(id="m1", tex="x")
    with pytest.raises(ValueError, match="repeats an ID"):
        await checker.check_many([formula, formula])
    with pytest.raises(ValueError, match="32 spans"):
        await checker.check_many([Formula(id=f"m{i}", tex="x") for i in range(33)])
    checker._pending = 32
    assert (await checker.check(formula)).status == "unavailable"


@pytest.mark.asyncio
async def test_large_unicode_request_never_spawns(tmp_path: Path) -> None:
    checker = fixture_checker(tmp_path, "raise AssertionError('must not spawn')\n")
    formula = Formula(
        id="m1", tex="x", macros={f"\\macro{i}": "\U0001d54f" * 1000 for i in range(200)}
    )
    assert (await checker.check(formula)).error == "math request exceeds byte limit"


@pytest.mark.asyncio
async def test_deadline_includes_the_admission_queue(tmp_path: Path) -> None:
    checker = fixture_checker(tmp_path, answer(), timeout=0.05)
    await checker._slots.acquire()
    await checker._slots.acquire()
    try:
        async with asyncio.timeout(1):
            result = await checker.check(Formula(id="m1", tex="x"))
        assert result.error == "math checker deadline exceeded"
        assert checker._pending == 0
    finally:
        checker._slots.release()
        checker._slots.release()


@pytest.mark.asyncio
async def test_config_fifo_and_missing_runtime_fail_without_blocking(tmp_path: Path) -> None:
    checker = fixture_checker(tmp_path, answer())
    checker.bundle.unlink()
    os.mkfifo(checker.bundle)
    async with asyncio.timeout(1):
        assert (await checker.check(Formula(id="m1", tex="x"))).status == "unavailable"
    checker.bundle.unlink()
    checker.bundle.write_text("fixture")
    checker.node.unlink()
    assert (await checker.check(Formula(id="m1", tex="x"))).status == "unavailable"


def test_non_utf8_text_is_refused_before_serialization() -> None:
    for raw in [{"tex": "\ud800"}, {"tex": "x", "macros": {"\\F": "\ud800"}}]:
        with pytest.raises(ValidationError):
            Formula(id="m1", **raw)


@pytest.mark.asyncio
async def test_real_node_renderer(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    bundle = root / "web/workspace/dist-math/mathcheck.mjs"
    node = shutil.which("node")
    if not bundle.is_file() or node is None:
        pytest.skip("build:math and Node needed; exercised in the web CI job")
    checker = MathChecker(
        Path(node), root / "web/workspace/server/check-math.mjs", bundle, timeout=2
    )
    results = await checker.check_many(
        [
            Formula(id="m1", tex=r"\F_{17}", macros={r"\F": r"\mathbb{F}"}),
            Formula(id="m2", tex=r"\frac{a}{"),
            Formula(id="m3", tex=r"\sqrt{"),
            Formula(id="m4", tex="x" * 5001),
        ]
    )
    assert [item.status for item in results] == ["accepted", "rejected", "rejected", "unavailable"]
    assert results[1].remedy == r"\frac{a}{}"
    assert results[2].remedy == r"\sqrt{}"
    assert all(item.bundle_sha256 == results[0].bundle_sha256 for item in results)
