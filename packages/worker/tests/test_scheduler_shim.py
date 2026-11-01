# SPDX-License-Identifier: MIT
"""Native shim selection through the OpenShell backend's public connection API."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.worker import backends
from sandbox_science.worker.backends import SCHED_SHIM_NAME, SCHED_SHIM_SOURCE, OpenShellBackend
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import OpenShellBackendSettings

REVISION = hashlib.sha256(SCHED_SHIM_SOURCE.read_bytes()).hexdigest()
IMAGE_SHIM = f"/usr/local/lib/sandbox-science/shim/{REVISION}/{SCHED_SHIM_NAME}"


def elf(machine: str) -> bytes:
    header = bytearray(20)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = {"amd64": 62, "arm64": 183}[machine].to_bytes(2, "little")
    return bytes(header)


class Peer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.machine = "arm64"
        self.shim: str | None = IMAGE_SHIM
        self.probe_error: bytes | None = None
        self.compiled = elf("arm64")
        self.compiler_status = 0
        self.commands: list[tuple[str, ...]] = []
        self.argv: list[str] = []

    async def command(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        self.commands.append(argv)
        if argv[0] == "test-cc":
            if not self.compiler_status:
                Path(argv[argv.index("-o") + 1]).write_bytes(self.compiled)
            return self.compiler_status, b"test compiler error" if self.compiler_status else b""
        if "-c" in argv:
            if self.probe_error is not None:
                return 1, self.probe_error
            if argv[-1]:
                self.shim = (
                    f"/sandbox/.sandbox-science/shim/{REVISION}-{self.machine}/{SCHED_SHIM_NAME}"
                )
            return 0, json.dumps({"machine": self.machine, "shim": self.shim}).encode()
        if "ssh-config" in argv:
            return 0, b"Host test-sandbox\n  HostName test.invalid\n"
        return 0, b""

    @property
    def uploads(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if "upload" in command]


@pytest.fixture
def peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Peer:
    value = Peer(tmp_path)
    monkeypatch.setattr(backends, "_command", value.command)

    class Channel:
        def __init__(self, argv: list[str], _event: Any, closed: Any, **_kwargs: Any) -> None:
            value.argv = argv
            self.closed = closed

        async def start(self) -> None:
            pass

        async def shutdown(self) -> None:
            await self.closed(self)

    monkeypatch.setattr(backends, "ExecutorChannel", Channel)
    return value


def host(monkeypatch: pytest.MonkeyPatch, system: str, machine: str) -> None:
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: machine)


async def connect(peer: Peer) -> ExecutorChannel:
    backend = OpenShellBackend(
        OpenShellBackendSettings(isolation_tier=2, sandboxes={"env": "test"}, cc="test-cc"),
        peer.root,
    )

    async def event(_message: dict[str, Any]) -> None:
        pass

    async def closed(_channel: ExecutorChannel) -> None:
        pass

    return await backend.open("env", event, closed)


@pytest.mark.parametrize(("system", "machine"), [("darwin", "arm64"), ("linux", "x86_64")])
async def test_image_native_shim_works_from_another_host(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    host(monkeypatch, system, machine)
    channel = await connect(peer)
    try:
        assert f"LD_PRELOAD={IMAGE_SHIM}" in peer.argv[-1]
        assert not any(command[0] == "test-cc" for command in peer.commands)
        assert not any(command[-2].endswith(".so") for command in peer.uploads)
    finally:
        await channel.shutdown()


@pytest.mark.parametrize(("system", "machine"), [("darwin", "arm64"), ("linux", "x86_64")])
async def test_missing_native_cross_platform_shim_fails_before_upload(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    host(monkeypatch, system, machine)
    peer.shim = None
    with pytest.raises(WorkerError, match="rebuild its research image"):
        await connect(peer)
    assert not peer.uploads
    assert not any(command[0] == "test-cc" for command in peer.commands)


async def test_invalid_image_library_fails_instead_of_building_another(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.probe_error = b"scheduler shim source revision does not match"
    with pytest.raises(WorkerError, match="source revision does not match"):
        await connect(peer)
    assert not peer.uploads


async def test_matching_linux_fallback_is_versioned_and_reused(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim = None
    channel = await connect(peer)
    await channel.shutdown()
    library_uploads = [command for command in peer.uploads if command[-2].endswith(".so")]
    assert len(library_uploads) == 1
    assert f"/shim/{REVISION}-arm64/incoming-" in library_uploads[0][-1]
    assert not library_uploads[0][-1].endswith(SCHED_SHIM_NAME)
    assert f"/shim/{REVISION}-arm64/{SCHED_SHIM_NAME}" in peer.argv[-1]
    compiler = next(command for command in peer.commands if command[0] == "test-cc")
    assert f'-DSANDBOX_SCIENCE_SCHED_SOURCE_SHA256="{REVISION}"' in compiler
    channel = await connect(peer)
    await channel.shutdown()
    assert sum(command[0] == "test-cc" for command in peer.commands) == 1
    assert len([command for command in peer.uploads if command[-2].endswith(".so")]) == 1


@pytest.mark.parametrize("compiled", [elf("amd64"), b"not a Linux ELF library"])
async def test_host_compiler_cannot_upload_wrong_elf(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, compiled: bytes
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim, peer.compiled = None, compiled
    with pytest.raises(WorkerError, match="no matching Linux ELF"):
        await connect(peer)
    assert not peer.uploads
    assert not list((peer.root / "shim").rglob("*.so"))


async def test_build_failure_is_actionable(peer: Peer, monkeypatch: pytest.MonkeyPatch) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim, peer.compiler_status = None, 1
    with pytest.raises(WorkerError, match="install a Linux C compiler or rebuild"):
        await connect(peer)
    assert not peer.uploads


@pytest.mark.parametrize("path", ["/unexpected/lib.so", "/tmp/agent-choice.so"])
async def test_probe_cannot_select_an_unexpected_preload_path(peer: Peer, path: str) -> None:
    peer.shim = path
    with pytest.raises(WorkerError, match="unexpected scheduler shim path"):
        await connect(peer)
    assert not peer.uploads
