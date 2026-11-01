# SPDX-License-Identifier: MIT
"""Where executors run: a local subprocess (tests only) or an OpenShell sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shlex
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast

from sandbox_science.contracts import ErrorKind

from . import (
    binding_inventory,
    capture,
    daemon,
    executor,
    executor_admission,
    executor_lifecycle,
    pdfhelper,
    provisioning,
    request_frames,
    sandbox_files,
    sandbox_processes,
    sandbox_search,
    sandbox_setup,
    workspace_prefix,
)
from .channel import EventHandler, ExecutorChannel
from .errors import PrefixRecoveryError, WorkerError
from .package_clients import ClientConfiguration, render
from .settings import LocalBackendSettings, OpenShellBackendSettings

_log = logging.getLogger(__name__)

DAEMON_SOURCE = Path(daemon.__file__)
EXECUTOR_SOURCE = Path(executor.__file__)
CAPTURE_SOURCE = Path(capture.__file__)
PDF_HELPER_SOURCE = Path(pdfhelper.__file__)
PROVISIONING_SOURCE = Path(provisioning.__file__)
"""The package-command wrappers and hook (provisioning.py), which the executor loads."""
SANDBOX_FILES: tuple[Path, ...] = (
    EXECUTOR_SOURCE,
    Path(executor_lifecycle.__file__),
    Path(executor_admission.__file__),
    Path(binding_inventory.__file__),
    PROVISIONING_SOURCE,
    PDF_HELPER_SOURCE,
    DAEMON_SOURCE,
    Path(request_frames.__file__),
    CAPTURE_SOURCE,
    Path(sandbox_files.__file__),
    Path(sandbox_processes.__file__),
    Path(sandbox_search.__file__),
    Path(sandbox_setup.__file__),
    Path(workspace_prefix.__file__),
)
"""Every file the executor runs from inside a sandbox, uploaded side by side into the
executor's directory. They use only the standard library and each other (the executor
loads its siblings by path), plus jupyter_client and pdf_oxide where those are installed;
test_sandbox_payload.py checks both."""
SCHED_SHIM_SOURCE = Path(__file__).with_name("shim") / "sched_self.c"
SCHED_SHIM_NAME = "libsandbox-science-sched.so"
SCHED_SHIM_IMAGE_DIR = "/usr/local/lib/sandbox-science/shim"
# Runs with the sandbox's Python, never the worker's: both the ELF machine and
# the embedded source revision must match before this library is preloaded.
_SHIM_PROBE = r"""
import ctypes,json,os,platform,sys
from pathlib import Path
revision,image_dir,executor_dir,staged=sys.argv[1:]
machine={"x86_64":"amd64","aarch64":"arm64"}.get(platform.machine())
if sys.platform != "linux" or machine is None:
    raise RuntimeError("scheduler shim requires Linux amd64 or arm64")
name="libsandbox-science-sched.so"
image=Path(image_dir)/revision/name
uploaded=Path(executor_dir)/"shim"/(revision+"-"+machine)/name
def validate(path):
    with path.open("rb") as stream:
        header=stream.read(20)
    expected={"amd64":62,"arm64":183}[machine]
    if (header[:7] != b"\x7fELF\x02\x01\x01"
            or int.from_bytes(header[16:18],"little") != 3
            or int.from_bytes(header[18:20],"little") != expected):
        raise RuntimeError("scheduler shim is not a matching Linux ELF library: "+str(path))
    library=ctypes.CDLL(str(path))
    source=library.sandbox_science_sched_source_sha256
    source.restype=ctypes.c_char_p
    if source() != revision.encode():
        raise RuntimeError("scheduler shim source revision does not match: "+str(path))
    getattr(library,"pthread_setschedparam")
if staged:
    try:
        validate(Path(staged))
        try:
            os.link(staged,uploaded)
        except FileExistsError:
            pass
    finally:
        Path(staged).unlink(missing_ok=True)
selected=None
for path in (image,uploaded):
    if path.exists():
        validate(path)
        selected=str(path)
        break
print(json.dumps({"machine":machine,"shim":selected}))
"""
COMMAND_TIMEOUT = 120.0
"""Seconds a helper command (the OpenShell CLI, the compiler) may take."""
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ClosedHandler = Callable[[ExecutorChannel], Awaitable[None]]


async def _command(*argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Run a helper command to completion, within COMMAND_TIMEOUT; return its code and output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise WorkerError(ErrorKind.UNAVAILABLE, f"cannot run {argv[0]}: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise WorkerError(
            ErrorKind.UNAVAILABLE, f"{argv[0]} did not finish within {COMMAND_TIMEOUT:g} s"
        ) from exc
    return process.returncode or 0, output


class Backend(Protocol):
    """Starts executors for logical environments."""

    @property
    def name(self) -> Literal["local", "openshell"]: ...

    async def prepare(self, environment_id: str) -> None:
        """Make the environment ready for an executor, such as starting its stopped sandbox."""
        ...

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel: ...

    def sandbox_id(self, environment_id: str) -> str | None: ...

    @property
    def isolation_tier(self) -> int: ...

    @property
    def node_kernel_releases(self) -> tuple[str, ...]:
        """Patterns for the releases of the nodes' own kernels, for the isolation probe."""
        ...

    @property
    def daemon(self) -> bool:
        """Whether executors run under a sandbox daemon that outlives the worker."""
        ...

    def binding_key(self, environment_id: str) -> str:
        """The environment's sandbox binding as the backend knows it, for package gates
        when no managed environment names one."""
        ...


class LocalBackend:
    """Runs the executor beside the worker. No isolation at all: development and tests."""

    name: Literal["local"] = "local"

    def __init__(
        self,
        settings: LocalBackendSettings,
        state_dir: Path,
        executor_args: Sequence[str] = (),
    ) -> None:
        self.settings = settings
        self.executor_args = tuple(executor_args)
        self.root = state_dir / "environments"
        self.state_dir = state_dir

    async def prepare(self, environment_id: str) -> None:
        return None

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        # The ID becomes a directory name: nothing but an identifier may reach the path.
        if not _IDENTIFIER.fullmatch(environment_id):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid environment ID")
        project = self.root / environment_id / "project"
        project.mkdir(parents=True, exist_ok=True)
        # -I keeps the executor's directory off sys.path, so the worker's other
        # modules cannot shadow anything it imports.
        argv = [self.settings.python, "-I", str(EXECUTOR_SOURCE), "--stdio", "--root", str(project)]
        argv += self.executor_args
        token = None
        if self.settings.daemon:
            argv = [
                self.settings.python,
                "-I",
                str(DAEMON_SOURCE),
                "--root",
                str(project),
                *self.executor_args,
            ]
            token = daemon_token(self.state_dir, environment_id)
        channel = ExecutorChannel(argv, on_event, on_closed, env=_child_env(), daemon_token=token)
        await channel.start()
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        return None

    @property
    def isolation_tier(self) -> int:
        return 0

    @property
    def node_kernel_releases(self) -> tuple[str, ...]:
        return ()

    @property
    def daemon(self) -> bool:
        return self.settings.daemon

    def binding_key(self, environment_id: str) -> str:
        return repr((self.name, str(self.root), self.sandbox_id(environment_id)))


class OpenShellBackend:
    """Copies the executor into a sandbox and opens a bounded, isolated SSH daemon bridge."""

    name: Literal["openshell"] = "openshell"

    def __init__(
        self,
        settings: OpenShellBackendSettings,
        state_dir: Path,
        resolve: Callable[[str], str | None] | None = None,
        executor_args: Sequence[str] = (),
        prepare: Callable[[str], Awaitable[None]] | None = None,
        manages: Callable[[str], bool] | None = None,
        requires_workspace_prefix: Callable[[str], bool] | None = None,
    ) -> None:
        if (resolve is None) != (manages is None):
            # Without ``manages``, a deleted environment would fall back to the static map.
            raise ValueError("resolve and manages go together")
        self.settings = settings
        self.state_dir = state_dir
        self.resolve = resolve
        """Finds the sandboxes the worker created (environments.py) before the static map."""
        self.manages = manages
        """Tells environments the worker created, which never fall back to the static map."""
        self.executor_args = tuple(executor_args)
        self._prepare = prepare
        self.requires_workspace_prefix = requires_workspace_prefix
        """Starts a stopped sandbox the worker created before an executor runs there."""
        self._shim_lock = asyncio.Lock()
        self._clients = None if settings.package_cache is None else render(settings.package_cache)

    def _sandbox(self, environment_id: str) -> str:
        sandbox = self.sandbox_id(environment_id)
        if sandbox is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"no sandbox is configured for environment {environment_id!r}",
            )
        return sandbox

    def _env(self) -> dict[str, str]:
        """The CLI's environment: the worker's, without the ``OPENSHELL_*`` variables it
        inherited, which could point the CLI at another gateway or workspace, and with
        those its settings give."""
        inherited = {k: v for k, v in _child_env().items() if not k.startswith("OPENSHELL_")}
        return {**inherited, **self.settings.env}

    async def _run(self, *args: str) -> None:
        code, output = await _command(self.settings.cli, *args, env=self._env())
        if code != 0:
            text = output.decode("utf-8", "replace").strip()[-500:]
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {args[0]} {args[1]} failed: {text}"
            )

    async def _shim(self, sandbox: str) -> str:
        """Prefer the image's native shim; build only for a matching Linux host."""
        source = SCHED_SHIM_SOURCE.read_bytes()
        revision = hashlib.sha256(source).hexdigest()
        executor_dir = str(PurePosixPath(self.settings.executor_dir))

        async def probe(staged: str = "") -> tuple[str, str | None]:
            code, output = await _command(
                self.settings.cli,
                "sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell", "--",
                self.settings.python, "-I", "-c", _SHIM_PROBE,
                revision, SCHED_SHIM_IMAGE_DIR, executor_dir, staged,
                env=self._env(),
            )  # fmt: skip
            if code != 0:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "cannot validate the sandbox scheduler shim: "
                    + output.decode("utf-8", "replace")[-500:],
                )
            try:
                result: object = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError("expected an object")
                document = cast(dict[str, object], result)
                if set(document) != {"machine", "shim"}:
                    raise ValueError("unexpected probe fields")
                machine, path = document.get("machine"), document.get("shim")
                if machine not in ("amd64", "arm64") or (
                    path is not None and not isinstance(path, str)
                ):
                    raise ValueError("invalid machine or path")
            except (ValueError, TypeError) as exc:
                raise WorkerError(ErrorKind.UNAVAILABLE, "invalid scheduler shim probe") from exc
            expected = {
                f"{SCHED_SHIM_IMAGE_DIR}/{revision}/{SCHED_SHIM_NAME}",
                f"{executor_dir}/shim/{revision}-{machine}/{SCHED_SHIM_NAME}",
            }
            if path is not None and path not in expected:
                raise WorkerError(ErrorKind.UNAVAILABLE, "unexpected scheduler shim path")
            return str(machine), path

        machine, path = await probe()
        if path is not None:
            return path
        host = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
        if sys.platform != "linux" or host != machine:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"sandbox lacks the native {machine} scheduler shim for source {revision}; "
                "rebuild its research image with the current shim. Host compilation requires "
                "a Linux worker of the same architecture",
            )
        target = self.state_dir / "shim" / f"{revision}-{machine}" / SCHED_SHIM_NAME
        async with self._shim_lock:
            candidate = target
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(".tmp")
                code, output = await _command(
                    self.settings.cc, "-shared", "-fPIC", "-O2",
                    f'-DSANDBOX_SCIENCE_SCHED_SOURCE_SHA256="{revision}"',
                    "-o", str(partial), str(SCHED_SHIM_SOURCE),
                )  # fmt: skip
                if code != 0:
                    partial.unlink(missing_ok=True)
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE,
                        "cannot build the scheduler shim; install a Linux C compiler or rebuild "
                        "the research image with its native shim: "
                        + output.decode("utf-8", "replace")[-300:],
                    )
                candidate = partial
            with candidate.open("rb") as stream:
                header = stream.read(20)
            if (
                header[:7] != b"\x7fELF\x02\x01\x01"
                or int.from_bytes(header[16:18], "little") != 3
                or int.from_bytes(header[18:20], "little") != {"amd64": 62, "arm64": 183}[machine]
            ):
                if candidate != target:
                    candidate.unlink(missing_ok=True)
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "host compiler produced no matching Linux ELF shim"
                )
            if candidate != target:
                candidate.replace(target)
            directory = f"{executor_dir}/shim/{revision}-{machine}"
            staged = f"{directory}/incoming-{secrets.token_hex(12)}.so"
            await self._run(
                "sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell", "--",
                "mkdir", "-p", directory,
            )  # fmt: skip
            await self._run("sandbox", "upload", "--no-git-ignore", sandbox, str(target), staged)
            _, path = await probe(staged)
            if path is None:
                raise WorkerError(ErrorKind.UNAVAILABLE, "scheduler shim installation is missing")
            return path

    async def prepare(self, environment_id: str) -> None:
        if self._prepare is not None:
            await self._prepare(environment_id)

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        if not self.settings.daemon:
            raise WorkerError(ErrorKind.UNSUPPORTED, "OpenShell streaming requires daemon=true")
        sandbox = self._sandbox(environment_id)
        settings = self.settings
        required = self.requires_workspace_prefix is not None and self.requires_workspace_prefix(
            environment_id
        )
        if required:
            # The required Python may itself be gone. Use the fixed system shell, and
            # classify only a positive target reply, never a gateway/transport error.
            code, output = await _command(
                settings.cli,
                "sandbox",
                "exec",
                "-n",
                sandbox,
                "--no-tty",
                "--no-login-shell",
                "--",
                "/bin/sh",
                "-c",
                "if [ -d /sandbox/.sandbox-science-prefix ] && "
                "[ ! -L /sandbox/.sandbox-science-prefix ] && "
                "[ -L /opt/sandbox-science ] && "
                '[ "$(readlink -f /opt/sandbox-science)" = /sandbox/.sandbox-science-prefix ] && '
                "[ -x /opt/sandbox-science/bin/python ]; then "
                "printf 'SS_PREFIX_OK\\n'; else printf 'SS_PREFIX_MISSING\\n'; fi",
                env=self._env(),
            )
            if code == 0 and output.strip() == b"SS_PREFIX_MISSING":
                raise PrefixRecoveryError(
                    ErrorKind.UNAVAILABLE,
                    "required workspace prefix is missing or incompatible; preserve the workspace "
                    "and recover its known package prefix before resuming",
                )
            if code != 0 or output.strip() != b"SS_PREFIX_OK":
                raise WorkerError(ErrorKind.UNAVAILABLE, "workspace prefix preflight unavailable")
        await self._run(
            "sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell", "--",
            "mkdir", "-p", settings.executor_dir, settings.project_root,
        )  # fmt: skip
        shim = await self._shim(sandbox) if settings.sched_shim else None
        for path in SANDBOX_FILES:
            await self._run(
                "sandbox", "upload", "--no-git-ignore", sandbox, str(path),
                f"{settings.executor_dir}/",
            )  # fmt: skip
        remote = [
            settings.python,
            "-I",
            f"{settings.executor_dir}/{DAEMON_SOURCE.name}",
            "--framed",
            "--root",
            settings.project_root,
            *self.executor_args,
        ]
        if required:
            remote.append("--require-workspace-prefix")
        if shim is not None:
            remote = ["env", f"LD_PRELOAD={shim}", *remote]
        code, config = await _command(
            settings.cli, "sandbox", "ssh-config", sandbox, env=self._env()
        )
        hosts = re.findall(rb"^Host ([A-Za-z0-9._-]+)$", config, re.MULTILINE)
        if code != 0 or len(config) > 65536 or len(hosts) != 1:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "OpenShell returned no usable SSH configuration"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        private = tempfile.TemporaryDirectory(prefix="worker-ssh-", dir=self.state_dir)
        config_path = Path(private.name) / "config"
        config_path.write_bytes(config)
        config_path.chmod(0o600)
        argv = [
            "ssh",
            "-T",
            "-F",
            str(config_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "SetEnv=OPENSHELL_NO_LOGIN_SHELL=1",
            hosts[0].decode("ascii"),
            shlex.join(remote),
        ]

        async def closed(channel: ExecutorChannel) -> None:
            try:
                await on_closed(channel)
            finally:
                private.cleanup()

        channel = ExecutorChannel(
            argv,
            on_event,
            closed,
            env=self._env(),
            daemon_token=daemon_token(self.state_dir, environment_id),
            framed=True,
        )
        try:
            await channel.start()
        except BaseException:
            private.cleanup()
            raise
        if self._clients is not None:
            try:
                await configure_package_clients(channel, self._clients, environment_id)
            except BaseException:
                await channel.shutdown()
                raise
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        if self.resolve is not None:
            created = self.resolve(environment_id)
            if created is not None:
                return created
            if self.manages is not None and self.manages(environment_id):
                return None  # created by the worker and not ready: never a sandbox made by hand
        return self.settings.sandboxes.get(environment_id)

    def cli_env(self) -> dict[str, str]:
        """The environment the ``openshell`` CLI runs with."""
        return self._env()

    @property
    def isolation_tier(self) -> int:
        return self.settings.isolation_tier

    @property
    def node_kernel_releases(self) -> tuple[str, ...]:
        return self.settings.node_kernel_releases

    @property
    def daemon(self) -> bool:
        return self.settings.daemon

    def binding_key(self, environment_id: str) -> str:
        return repr((self.name, "", self.sandbox_id(environment_id)))


def daemon_token(state_dir: Path, environment_id: str) -> str:
    """A stable private worker-side token; never a research-supplied path or credential."""
    if not _IDENTIFIER.fullmatch(environment_id):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid environment ID")
    directory = state_dir / "daemon-tokens"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / environment_id
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            import stat

            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkerError(ErrorKind.UNAVAILABLE, "daemon token is not a regular file")
            token = os.read(fd, 256).decode().strip()
        finally:
            os.close(fd)
    else:
        token = secrets.token_hex(32)
        try:
            os.write(fd, token.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise WorkerError(ErrorKind.UNAVAILABLE, "invalid daemon token")
    return token


async def configure_package_clients(
    channel: ExecutorChannel, clients: ClientConfiguration, environment_id: str
) -> None:
    """Have the executor write the package clients' configuration (docs/PACKAGE-CACHE.md).

    The executor keeps a file that someone changed since it wrote it, or that
    it did not write; each one is logged, since its client may then miss the cache.
    """
    reply = await channel.request(
        "configure",
        files=[dataclasses.asdict(file) for file in clients.files],
        env=dict(clients.env),
    )
    for path in reply.get("written", ()):
        _log.info("environment %s: wrote %s", environment_id, path)
    for kept in reply.get("skipped", ()):
        _log.warning(
            "environment %s: left %s as it is (%s); check that its client uses the package cache",
            environment_id,
            kept.get("path"),
            kept.get("reason"),
        )


def _child_env() -> dict[str, str]:
    """The worker's environment, without variables that would change Python's behavior."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON") and key not in ("VIRTUAL_ENV",)
    }
