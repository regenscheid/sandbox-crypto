# SPDX-License-Identifier: MIT
"""Exercise target-side ELF/source validation and no-replace installation on Linux."""

from __future__ import annotations

import asyncio
import hashlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox_science.worker import backends
from sandbox_science.worker.backends import (
    SCHED_SHIM_NAME,
    SCHED_SHIM_SOURCE,
    SIGNAL_SHIM,
    OpenShellBackend,
)
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import OpenShellBackendSettings

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform != "linux"
        or platform.machine() not in ("x86_64", "aarch64")
        or not shutil.which("cc"),
        reason="native shim checks need Linux amd64/arm64 and a C compiler",
    ),
]
REVISION = hashlib.sha256(SCHED_SHIM_SOURCE.read_bytes()).hexdigest()


class NativePeer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.executor = root / "executor"
        self.image = root / "image" / REVISION / SCHED_SHIM_NAME
        self.race_library: Path | None = None
        self.race_inode: int | None = None
        self.uploads: list[str] = []
        machine = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
        self.installed = self.executor / "shim" / f"{REVISION}-{machine}" / SCHED_SHIM_NAME

    async def command(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        if "ssh-config" in argv:
            return 1, b"stop before opening the executor transport"
        if "upload" in argv:
            if argv[-2].endswith(".so"):
                self.uploads.append(argv[-1])
                shutil.copyfile(argv[-2], argv[-1])
                if self.race_library is not None:
                    shutil.copyfile(self.race_library, self.installed)
                    self.race_inode = self.installed.stat().st_ino
            return 0, b""
        remote = argv[argv.index("--") + 1 :]
        if remote[0] == "mkdir":
            Path(remote[-1]).mkdir(parents=True, exist_ok=True)
            return 0, b""
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, *remote[1:]],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return completed.returncode, completed.stdout + completed.stderr

    async def connect(self, signal_shim: bool = False, cc: str = "cc") -> None:
        async def event(_message: object) -> None:
            pass

        async def closed(_channel: ExecutorChannel) -> None:
            pass

        backend = OpenShellBackend(
            OpenShellBackendSettings(
                isolation_tier=2,
                sandboxes={"env": "test"},
                executor_dir=str(self.executor),
                project_root=str(self.root / "project"),
                signal_shim=signal_shim,
                cc=cc,
                shim_dir=str(self.root / "worker-shims"),
            ),
            self.root / "state",
        )
        await backend.open("env", event, closed)


def compile_library(path: Path, revision: str = REVISION) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "cc", "-shared", "-fPIC", "-O2",
            f'-DSANDBOX_SCIENCE_SCHED_SOURCE_SHA256="{revision}"',
            "-o", str(path), str(SCHED_SHIM_SOURCE),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )  # fmt: skip


@pytest.fixture
def native_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativePeer:
    peer = NativePeer(tmp_path)
    original = backends._command

    async def command(*argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        if argv[0] == "openshell":
            return await peer.command(*argv, env=env)
        return await original(*argv, env=env)

    monkeypatch.setattr(backends, "_command", command)
    monkeypatch.setattr(backends, "SHIM_IMAGE_DIR", str(tmp_path / "image"))
    return peer


async def test_native_image_library_is_loadable_and_source_checked(native_peer: NativePeer) -> None:
    compile_library(native_peer.image)
    with pytest.raises(WorkerError, match="no usable SSH configuration"):
        await native_peer.connect()
    assert not native_peer.uploads


async def test_loadable_library_with_wrong_source_revision_is_refused(
    native_peer: NativePeer,
) -> None:
    compile_library(native_peer.image, "0" * 64)
    with pytest.raises(WorkerError, match="source revision does not match"):
        await native_peer.connect()
    assert not native_peer.uploads


async def test_target_rejects_wrong_elf_before_loading(native_peer: NativePeer) -> None:
    native_peer.image.parent.mkdir(parents=True)
    native_peer.image.write_bytes(b"not a matching ELF library")
    with pytest.raises(WorkerError, match="not a matching Linux ELF"):
        await native_peer.connect()
    assert not native_peer.uploads


@pytest.mark.parametrize("matching", [True, False])
async def test_concurrent_install_never_overwrites_an_existing_library(
    native_peer: NativePeer, matching: bool
) -> None:
    library = native_peer.root / "existing.so"
    compile_library(library, REVISION if matching else "0" * 64)
    native_peer.race_library = library
    reason = "no usable SSH configuration" if matching else "source revision does not match"
    with pytest.raises(WorkerError, match=reason):
        await native_peer.connect()
    assert len(native_peer.uploads) == 1
    assert native_peer.installed.stat().st_ino == native_peer.race_inode
    assert native_peer.installed.read_bytes() == library.read_bytes()
    assert not list(native_peer.installed.parent.glob("incoming-*.so"))


async def test_the_signal_shim_is_checked_and_built_the_same_way(native_peer: NativePeer) -> None:
    """The image's scheduler shim is used; the signal shim, missing from the image, is
    built with the host's compiler, uploaded once and validated in the target."""
    compile_library(native_peer.image)
    with pytest.raises(WorkerError, match="no usable SSH configuration"):
        await native_peer.connect(signal_shim=True)
    assert len(native_peer.uploads) == 1
    machine = native_peer.installed.parent.name.rsplit("-", 1)[1]
    installed = native_peer.executor / "shim" / f"{SIGNAL_SHIM.revision}-{machine}"
    assert (installed / SIGNAL_SHIM.name).is_file()
    assert not list(installed.glob("incoming-*.so"))


async def test_the_workers_own_library_is_uploaded_and_checked_in_the_target(
    native_peer: NativePeer,
) -> None:
    """An image without the shim gets the worker's prebuilt one, as the trusted worker
    image ships it, with no compiler on the worker."""
    library = native_peer.root / "worker-shims" / REVISION / SCHED_SHIM_NAME
    compile_library(library)
    with pytest.raises(WorkerError, match="no usable SSH configuration"):
        await native_peer.connect(cc=str(native_peer.root / "no-compiler"))
    assert len(native_peer.uploads) == 1
    assert native_peer.installed.read_bytes() == library.read_bytes()
    assert not list((native_peer.root / "state").rglob("*.so"))
