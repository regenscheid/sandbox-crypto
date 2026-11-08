# SPDX-License-Identifier: MIT
"""Native shim selection through the OpenShell backend's public connection API: the
scheduler shim, preloaded in every process, and the signal shim, which the executor
preloads in a delegated run's (DA-20)."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker import backends, executor
from sandbox_science.worker.backends import (
    SCHED_SHIM,
    SCHED_SHIM_NAME,
    SCHED_SHIM_SOURCE,
    SIGNAL_SHIM,
    LocalBackend,
    OpenShellBackend,
    Shim,
)
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import LocalBackendSettings, OpenShellBackendSettings

REVISION = hashlib.sha256(SCHED_SHIM_SOURCE.read_bytes()).hexdigest()
IMAGE_SHIM = f"/usr/local/lib/sandbox-science/shim/{REVISION}/{SCHED_SHIM_NAME}"
SIGNAL_REVISION = SIGNAL_SHIM.revision
IMAGE_SIGNAL = f"/usr/local/lib/sandbox-science/shim/{SIGNAL_REVISION}/{SIGNAL_SHIM.name}"


def elf(machine: str) -> bytes:
    header = bytearray(20)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = {"amd64": 62, "arm64": 183}[machine].to_bytes(2, "little")
    return bytes(header)


def prebuilt(root: Path, shim: Shim, machine: str = "arm64") -> Path:
    """A library in the worker's own shim directory, as the trusted worker image ships it."""
    path = root / "worker-shims" / shim.revision / shim.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(elf(machine) + shim.symbol.encode() + b"\0" + shim.revision.encode())
    return path


class Peer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.machine = "arm64"
        self.shim: str | None = IMAGE_SHIM
        self.signal: str | None = IMAGE_SIGNAL
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
            at = argv.index("-c")
            revision, staged, name = argv[at + 2], argv[at + 5], argv[at + 6]
            installed = f"/sandbox/.sandbox-science/shim/{revision}-{self.machine}/{name}"
            if name == SCHED_SHIM_NAME:
                self.shim = installed if staged else self.shim
                found = self.shim
            else:
                self.signal = installed if staged else self.signal
                found = self.signal
            return 0, json.dumps({"machine": self.machine, "shim": found}).encode()
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


async def connect(peer: Peer, signal_shim: bool = True) -> ExecutorChannel:
    backend = OpenShellBackend(
        OpenShellBackendSettings(
            isolation_tier=2,
            sandboxes={"env": "test"},
            cc="test-cc",
            signal_shim=signal_shim,
            shim_dir=str(peer.root / "worker-shims"),
        ),
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


async def test_the_image_signal_shim_is_named_to_the_executor(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor learns the validated signal shim's path; it preloads it only in a
    delegated run's processes, so it is not in the executor's own LD_PRELOAD."""
    host(monkeypatch, "darwin", "arm64")
    channel = await connect(peer)
    try:
        assert (
            f"LD_PRELOAD={IMAGE_SHIM} {executor.SIGNAL_SHIM_VARIABLE}={IMAGE_SIGNAL} "
            in peer.argv[-1]
        )
    finally:
        await channel.shutdown()


async def test_a_missing_signal_shim_is_built_like_the_scheduler_shim(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.signal = None
    channel = await connect(peer)
    await channel.shutdown()
    compiler = next(command for command in peer.commands if command[0] == "test-cc")
    assert f'-DSANDBOX_SCIENCE_SIGNAL_SOURCE_SHA256="{SIGNAL_REVISION}"' in compiler
    assert str(SIGNAL_SHIM.source) in compiler
    installed = f"/shim/{SIGNAL_REVISION}-arm64/{SIGNAL_SHIM.name}"
    assert f"{executor.SIGNAL_SHIM_VARIABLE}=/sandbox/.sandbox-science{installed}" in peer.argv[-1]


@pytest.mark.parametrize("problem", ["missing", "invalid"])
async def test_without_a_usable_signal_shim_the_environment_still_opens(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, problem: str
) -> None:
    """Unlike the scheduler shim, the signal shim changes no guarantee: where the image
    lacks it and the host cannot build it, or it fails validation, the executor starts
    without it (held runs report ``own_signals: false``) and the worker warns."""
    host(monkeypatch, "darwin", "arm64")
    original = peer.command

    async def command(*argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        if problem == "invalid" and "-c" in argv and SIGNAL_SHIM.name in argv:
            return 1, b"signal shim source revision does not match"
        return await original(*argv, env=env)

    monkeypatch.setattr(backends, "_command", command)
    if problem == "missing":
        peer.signal = None
    channel = await connect(peer)
    try:
        assert executor.SIGNAL_SHIM_VARIABLE not in peer.argv[-1]
        assert f"LD_PRELOAD={IMAGE_SHIM}" in peer.argv[-1]
        assert not peer.uploads or not any(c[-2].endswith(".so") for c in peer.uploads)
        assert "no signal shim for delegated runs" in caplog.text
    finally:
        await channel.shutdown()


async def test_the_signal_shim_can_be_turned_off(peer: Peer) -> None:
    peer.signal = "/unexpected/never-probed.so"
    channel = await connect(peer, signal_shim=False)
    await channel.shutdown()
    assert executor.SIGNAL_SHIM_VARIABLE not in peer.argv[-1]


async def test_the_workers_own_shim_is_uploaded_before_any_compiler(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sandbox whose image predates the shim's revision gets the worker's prebuilt
    library: the trusted worker image has no compiler and needs none."""
    host(monkeypatch, "linux", "aarch64")
    peer.shim = peer.signal = None
    libraries = [prebuilt(peer.root, SCHED_SHIM), prebuilt(peer.root, SIGNAL_SHIM)]
    channel = await connect(peer)
    await channel.shutdown()
    assert not any(command[0] == "test-cc" for command in peer.commands)
    assert [command[-2] for command in peer.uploads if command[-2].endswith(".so")] == [
        str(library) for library in libraries
    ]
    assert f"/shim/{REVISION}-arm64/{SCHED_SHIM_NAME}" in peer.argv[-1]
    assert f"/shim/{SIGNAL_REVISION}-arm64/{SIGNAL_SHIM.name}" in peer.argv[-1]
    assert "building one with" not in caplog.text


@pytest.mark.parametrize("content", ["other-machine", "other-revision"])
async def test_a_workers_shim_that_does_not_match_is_refused_not_rebuilt(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim = None
    path = prebuilt(peer.root, SCHED_SHIM, "amd64" if content == "other-machine" else "arm64")
    if content == "other-revision":
        path.write_bytes(elf("arm64") + SCHED_SHIM.symbol.encode() + b"\0" + b"0" * 64)
    with pytest.raises(WorkerError, match="worker's scheduler shim is not a matching"):
        await connect(peer)
    assert not peer.uploads
    assert not any(command[0] == "test-cc" for command in peer.commands)


async def test_compiling_for_want_of_the_workers_shim_warns_once_per_revision(
    peer: Peer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim = None
    backend = OpenShellBackend(
        OpenShellBackendSettings(
            isolation_tier=2,
            sandboxes={"env": "test"},
            cc="test-cc",
            signal_shim=False,
            shim_dir=str(peer.root / "worker-shims"),
        ),
        peer.root,
    )
    caplog.set_level(logging.WARNING)
    for sandbox in ("first", "second"):
        await backend._shim(sandbox, SCHED_SHIM)
    warnings = [r for r in caplog.records if "building one with test-cc" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert REVISION in warnings[0].getMessage()
    assert sum(command[0] == "test-cc" for command in peer.commands) == 1


async def test_without_the_workers_shim_or_a_compiler_the_error_says_what_is_missing(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    host(monkeypatch, "linux", "aarch64")
    peer.shim = None
    original = peer.command

    async def command(*argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        if argv[0] == "test-cc":
            raise WorkerError(ErrorKind.UNAVAILABLE, "cannot run test-cc: no such file")
        return await original(*argv, env=env)

    monkeypatch.setattr(backends, "_command", command)
    with pytest.raises(WorkerError) as caught:
        await connect(peer)
    message = caught.value.message
    assert f"sandbox lacks the native arm64 scheduler shim for source {REVISION}" in message
    assert f"the worker has none under {peer.root / 'worker-shims'}" in message
    assert "install a Linux C compiler or rebuild the research image" in message
    assert "cannot run test-cc: no such file" in message
    assert not peer.uploads
    assert not list((peer.root / "shim").rglob("*.so"))


async def test_the_local_backend_prefers_the_workers_own_signal_shim(
    peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    host(monkeypatch, "linux", "aarch64")
    library = prebuilt(peer.root, SIGNAL_SHIM)
    backend = LocalBackend(
        LocalBackendSettings(cc="test-cc", shim_dir=str(peer.root / "worker-shims")), peer.root
    )
    assert await backend._shim() == str(library)
    assert not any(command[0] == "test-cc" for command in peer.commands)
