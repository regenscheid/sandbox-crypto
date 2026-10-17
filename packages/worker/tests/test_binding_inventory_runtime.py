# SPDX-License-Identifier: MIT
"""Binding restrictions at real shell and surviving-kernel effect boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest

from sandbox_science.testing import eventually
from sandbox_science.worker import binding_inventory, provisioning

from .test_binding_inventory import package
from .test_executor import Recorder

pytestmark = pytest.mark.slow


def fixture_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    prefix = tmp_path / "installed"
    package(prefix)
    generate = binding_inventory.generate
    monkeypatch.setattr(
        binding_inventory, "generate", lambda p, s: generate(prefix, (), dpkg=prefix / "dpkg")
    )
    monkeypatch.setattr(binding_inventory, "system_fingerprint", lambda: "")
    monkeypatch.setattr(
        provisioning,
        "fingerprint",
        lambda places: hashlib.sha256((prefix / "conda-meta/zebra.json").read_bytes()).hexdigest(),
    )
    return prefix


async def ready(recorder: Recorder) -> None:
    hello = await recorder.ask({"op": "hello", "id": "hello"})
    reply = await recorder.ask(
        {
            "op": "package_gate",
            "id": "gate",
            "incarnation": hello["package_gate"],
            "generation": 0,
            "mode": "ready",
            "operation": "",
            "revision": None,
        }
    )
    assert reply["ok"]


async def run(recorder: Recorder, execution: str, **fields):
    await recorder.ask(
        {"id": execution, "execution": execution, "session": "s", "timeout": 20, **fields}
    )
    await eventually(
        lambda: any(
            m.get("execution") == execution and m.get("event") == "end" for m in recorder.messages
        ),
        timeout=25,
    )
    return next(
        m for m in recorder.messages if m.get("execution") == execution and m.get("event") == "end"
    )


@pytest.mark.parametrize("exact", [True, False])
async def test_package_addition_after_staging_refuses_cached_shell_and_inherited_kernel_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    prefix = fixture_inventory(tmp_path, monkeypatch)
    root = tmp_path / "project"
    (root / "sessions/s").mkdir(parents=True)
    recorder = Recorder(root)
    try:
        await ready(recorder)
        staged = await recorder.ask(
            {
                "op": "skills_check",
                "id": "stage",
                "source": "qzpack",
                "snapshot": "ab" * 32,
                "files": [],
                "binding_names": ["QZPACK_DIR", "ZEBRA_INPUT", "ZEBRA_"],
            }
        )
        assert staged["inventory"]["state"] == "ready"
        assert staged["inventory"]["reserved"] == ["ZEBRA_INPUT"]
        before = await run(
            recorder,
            "shell-before",
            op="shell",
            script="printf ok > before",
            bindings={"QZPACK_DIR": "/staged"},
        )
        assert before["state"] == "succeeded"
        kernel = await run(
            recorder,
            "kernel-before",
            op="cell",
            language="python",
            code="x = 41",
            bindings={"QZPACK_DIR": "/staged"},
        )
        assert kernel["state"] == "succeeded"
        package(prefix, "2", "QZPACK_DIR" if exact else "OTHER_VALUE")
        if not exact:
            # Add the namespace command without a discoverable literal env read.
            (prefix / "bin/zebra").rename(prefix / "bin/qzpack")
            metadata = prefix / "conda-meta/zebra.json"
            metadata.write_text(metadata.read_text().replace("bin/zebra", "bin/qzpack"))
        shell = await run(
            recorder,
            "shell-after",
            op="shell",
            script="touch forbidden-shell",
            bindings={"QZPACK_DIR": "/staged"},
            binding_inventory=staged["inventory"]["digest"],
        )
        assert shell["state"] == "failed"
        assert shell["result"]["error"]["kind"] == ("policy_denied" if exact else "unavailable")
        cell = await run(
            recorder,
            "kernel-after",
            op="cell",
            language="python",
            code="open('forbidden-cell', 'w').write(str(x))",
            bindings={},
        )
        assert cell["state"] == "failed"
        assert "restart" in cell["result"]["error"]["message"]
        assert not (root / "sessions/s/forbidden-shell").exists()
        assert not (root / "sessions/s/forbidden-cell").exists()
        # The refused unbound request supplied the next birth's empty binding set.
        restart = await recorder.ask(
            {"op": "restart", "id": "restart", "session": "s", "language": "python"}
        )
        assert restart["ok"]
        recovered = await run(
            recorder, "kernel-restarted", op="cell", language="python", code="42", bindings={}
        )
        assert recovered["state"] == "succeeded"
    finally:
        await recorder.executor.shutdown()


async def test_inventory_cancellation_keeps_actual_scan_and_package_lock_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_inventory(tmp_path, monkeypatch)
    scanning = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    generate = binding_inventory.generate

    def blocked(prefix, sites):
        loop.call_soon_threadsafe(scanning.set)
        assert release.wait(10)
        return generate(prefix, sites)

    monkeypatch.setattr(binding_inventory, "generate", blocked)
    root = tmp_path / "project"
    recorder = Recorder(root)

    async def guarded() -> None:
        async with recorder.executor.binding_guard({"QZPACK_DIR"}):
            pytest.fail("cancelled inventory must not admit an effect")

    task = asyncio.create_task(guarded())
    state = provisioning.State(root / ".executor/provisioning")
    try:
        await scanning.wait()
        task.cancel()
        task.cancel()
        assert state.try_lock(exclusive=True) is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        descriptor = state.try_lock(exclusive=True)
        assert descriptor is not None
        os.close(descriptor)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await recorder.executor.shutdown()
