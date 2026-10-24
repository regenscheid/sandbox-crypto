# SPDX-License-Identifier: MIT
"""The worker-side unit: the worker, the policy middleware and the package cache as one
deployed, supervised group with one config file (D101, docs/UNIT.md).

```
sandbox-science-unit serve  --config unit.toml [--only worker|middleware|cache ...]
sandbox-science-unit check  --config unit.toml [--only ...]
sandbox-science-unit status --config unit.toml [--only ...]
```

```toml
[unit]                                   # optional
log_level = "info"
pass_env = { worker = ["AWS_REGION"] }   # variables each service gets from the unit's own

[worker]
listen = "127.0.0.1:8310"
# ... the worker's own settings (docs/WORKER.md), or only: settings_file = "worker.toml"

[middleware]
listen = "127.0.0.1:50061"
# ... the middleware's settings (docs/POLICY-MIDDLEWARE.md)

[cache]                                  # optional, as the bundle's package cache is
listen = "127.0.0.1:8390"
# ... the cache's settings (docs/PACKAGE-CACHE.md)
```

The three codebases stay apart: the unit runs each service's own command with a
settings file, as the launcher runs its parts, and imports none of them. One process
supervises three children (the launcher's ``Supervisor``, so the same new session per
child, parent-death signal, readiness by address and stop in reverse order). Hosting the
three servers in one process would mean importing them, and mixing a gRPC server with
two asyncio ones, to gain nothing the supervisor does not already give.

``serve`` checks the file, starts the cache, the middleware and then the worker, each
once the one before takes connections, and prints every line any of them writes with
the service's name in front (``[worker] ...``), so one log stream tells all three
apart. A service that stops by itself stops the other two, in reverse order, and
``serve`` exits 1, for systemd or the kubelet to start the unit again; ``[unit] restart
= true`` starts the service again instead, as the launcher does for a part that is to
be (supervise.py). A stop signal stops them in the same order: the worker first, since
it uses the other two.

Each service checks its own settings with its own model when it starts, since those
models are in the services' packages; the unit checks what it needs to run them: the
sections, their addresses, the commands and the settings files. ``--only`` runs one
service of the same file, which the Kubernetes bundle uses to keep a container for each
(docs/KUBERNETES-BUNDLE.md).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import signal
import socket
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep

from . import toml
from .config import ConfigPath, LauncherError, Variable
from .state import Layout, write_private
from .supervise import Part, StopRequested, Supervisor, environment, passed

SERVICES = ("cache", "middleware", "worker")
"""The services in the order they start; they stop in the reverse order."""
REQUIRED = ("worker", "middleware")
COMMANDS = {
    "worker": "sandbox-science-worker",
    "middleware": "sandbox-science-policy-middleware",
    "cache": "sandbox-science-package-cache",
}
WILDCARD = "0.0.0.0"  # noqa: S104 - compared with an address, never bound
SETTINGS_FILE = "settings_file"
MAX_CONFIG = 1 << 20
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
PROBE = 1.0
"""Seconds a health probe waits for a connection."""
UNHEALTHY_STRIKES = 3
"""Probes in a row that find a service silent before an adopting launcher gives up on it."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnitOptions(_Strict):
    """``[unit]``: how the group runs, never what a service does."""

    bin_dir: ConfigPath | None = None
    """Where the services' commands are; by default beside this Python."""
    runtime_dir: ConfigPath | None = None
    """Where the settings files made from inline sections are written (mode 600, removed
    when the unit stops), and the base of the relative paths in those sections. By
    default the config file's directory."""
    start_timeout: float = Field(default=120.0, gt=0, le=3600)
    """Seconds each service has to take connections."""
    stop_timeout: float = Field(default=30.0, gt=0, le=600)
    """Seconds each service has to stop before it is killed."""
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    restart: bool = False
    """Start a service that stops by itself again, with the launcher's delays and bound,
    rather than stop the group."""
    pass_env: dict[str, tuple[Variable, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    """The variables of the unit's own environment each service gets besides the usual few
    (``PATH``, ``HOME``, the locale, proxies and CA settings), by service."""

    @field_validator("pass_env")
    @classmethod
    def _services(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        unknown = sorted(set(value) - set(SERVICES))
        if unknown:
            raise ValueError(f"no such service: {', '.join(unknown)}")
        return value


def split_address(value: str, what: str) -> tuple[str, int]:
    """``host:port`` as the supervisor probes it: an IPv4 address or a name, and a port."""
    host, colon, port = value.rpartition(":")
    if (
        not colon
        or not host
        or ":" in host
        or "[" in host
        or not (port.isascii() and port.isdigit())
        or not 0 < int(port) < 65536
    ):
        raise LauncherError(
            f"{what}: {value!r} is not host:port, with an IPv4 address or a name and a port "
            "from 1 to 65535"
        )
    return host, int(port)


def _clash(first: tuple[str, int], second: tuple[str, int]) -> bool:
    return first[1] == second[1] and (first[0] == second[0] or WILDCARD in (first[0], second[0]))


@dataclass(frozen=True)
class Service:
    """One section of the file, as far as the unit uses it."""

    name: str
    settings_file: Path | None
    """The service's own settings file, when the section only names one."""
    inline: Mapping[str, object] | None
    """The section itself, when it holds the settings."""
    listen: str | None
    """Where the service listens, from its settings; None until they are read."""

    @property
    def address(self) -> tuple[str, int] | None:
        return None if self.listen is None else split_address(self.listen, f"[{self.name}] listen")


@dataclass(frozen=True)
class UnitConfig:
    path: Path
    options: UnitOptions
    services: dict[str, Service]
    """The services this run uses, in start order."""
    sections: tuple[str, ...]
    """Every service the file has a section for."""

    @property
    def runtime_dir(self) -> Path:
        return self.options.runtime_dir or self.path.parent

    @property
    def bin_dir(self) -> Path:
        return self.options.bin_dir or Path(sys.executable).parent

    def settings(self, service: Service) -> Path:
        """The settings file the service runs with."""
        if service.settings_file is not None:
            return service.settings_file
        return self.runtime_dir / f".unit-{service.name}.toml"

    def part(self, service: Service) -> Part:
        argv = [
            str(self.bin_dir / COMMANDS[service.name]), "serve",
            "--settings", str(self.settings(service)),
            "--log-level", self.options.log_level,
        ]  # fmt: skip
        return Part(
            service.name,
            argv,
            self.runtime_dir if self.runtime_dir.is_dir() else self.path.parent,
            address=service.listen,
            env=passed(self.options.pass_env.get(service.name, ())),
            restart=self.options.restart,
        )

    def problems(self) -> list[str]:
        """What would only fail when the services start: a missing command or file."""
        found = [
            f"no {COMMANDS[name]} in {self.bin_dir}"
            for name in self.services
            if shutil.which(COMMANDS[name], path=str(self.bin_dir)) is None
        ]
        found += [
            f"[{service.name}] settings_file {service.settings_file} is not a file"
            for service in self.services.values()
            if service.settings_file is not None and not service.settings_file.is_file()
        ]
        return found

    def materialize(self) -> list[Path]:
        """Write the settings files of the inline sections; the paths written."""
        written: list[Path] = []
        for service in self.services.values():
            if service.inline is None:
                continue
            path = self.settings(service)
            try:
                self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_private(path, toml.dumps(service.inline))
            except OSError as exc:
                raise LauncherError(
                    f"cannot write the [{service.name}] settings to {path}: {exc}; "
                    "set [unit] runtime_dir to a directory you can write"
                ) from None
            written.append(path)
        return written


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG + 1)
    except FileNotFoundError:
        raise LauncherError(f"no unit config at {path}: docs/UNIT.md shows one") from None
    except OSError as exc:
        raise LauncherError(f"cannot read {path}: {exc}") from None
    if len(raw) > MAX_CONFIG:
        raise LauncherError(f"{path} is larger than {MAX_CONFIG} bytes")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LauncherError(f"{path} is not TOML: {exc}") from None


def _listen(table: Mapping[str, object], name: str, where: str) -> str:
    listen = table.get("listen")
    if not isinstance(listen, str):
        raise LauncherError(
            f'{where} has no listen = "host:port": the unit waits for the {name} there'
        )
    split_address(listen, f"{where} listen")
    return listen


def load(path: Path, *, only: Sequence[str] = (), read: bool = True) -> UnitConfig:
    """The unit config in ``path``, checked; LauncherError says what is wrong with it.

    ``only`` limits the run to those services: the others' sections are checked for
    their shape and their files never read. With ``read`` false no settings file is
    read, so a service that names one has no address yet (the launcher checks the file
    before it makes the worker's).
    """
    path = path.absolute()
    raw = _read(path)
    unknown = sorted(set(raw) - {"unit", *SERVICES})
    if unknown:
        raise LauncherError(
            f"{path}: no such section: {', '.join(unknown)}; a unit config has [unit] "
            "(optional), [worker], [middleware] and [cache] (optional)"
        )
    missing = [name for name in REQUIRED if name not in raw]
    if missing:
        raise LauncherError(f"{path} has no [{']['.join(missing)}] section")
    for name in SERVICES:
        if name in raw and not isinstance(raw[name], dict):
            raise LauncherError(f"{path}: [{name}] is not a table")
    try:
        options = UnitOptions.model_validate(raw.get("unit", {}), context={"base": path.parent})
    except ValidationError as exc:
        problems = "\n".join(
            f"  unit.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise LauncherError(f"{path} has an invalid [unit]:\n{problems}") from None
    sections = tuple(name for name in SERVICES if name in raw)
    wanted = tuple(only) if only else sections
    absent = [name for name in wanted if name not in sections]
    if absent:
        raise LauncherError(f"{path} has no [{']['.join(absent)}] section to run")
    services: dict[str, Service] = {}
    for name in SERVICES:
        if name in wanted:
            services[name] = _service(path, name, cast(dict[str, Any], raw[name]), read)
    taken: list[tuple[str, tuple[str, int]]] = []
    for service in services.values():
        address = service.address
        if address is None:
            continue
        for other, held in taken:
            if _clash(address, held):
                raise LauncherError(
                    f"{path}: [{service.name}] and [{other}] both listen on port {address[1]}"
                )
        taken.append((service.name, address))
    return UnitConfig(path, options, services, sections)


def _service(path: Path, name: str, table: dict[str, Any], read: bool) -> Service:
    where = f"{path}: [{name}]"
    if SETTINGS_FILE not in table:
        return Service(name, None, table, _listen(table, name, where))
    if set(table) != {SETTINGS_FILE}:
        raise LauncherError(f"{where} names a settings_file, so it holds nothing else")
    named = table[SETTINGS_FILE]
    if not isinstance(named, str) or not named:
        raise LauncherError(f"{where} settings_file is not a path")
    file = path.parent / Path(named).expanduser()
    if not read:
        return Service(name, file, None, None)
    settings = _read(file)
    return Service(name, file, None, _listen(settings, name, f"{where} settings_file {file}"))


def health(unit: UnitConfig) -> list[tuple[str, str, bool]]:
    """Each service, its address and whether it takes connections."""
    found: list[tuple[str, str, bool]] = []
    for service in unit.services.values():
        address = service.address
        if address is None or service.listen is None:
            raise LauncherError(f"the [{service.name}] settings have not been read")
        host, port = address
        try:
            with socket.create_connection(
                ("127.0.0.1" if host == WILDCARD else host, port), timeout=PROBE
            ):
                answers = True
        except OSError:
            answers = False
        found.append((service.name, service.listen, answers))
    return found


def describe(states: Sequence[tuple[str, str, bool]]) -> str:
    return ", ".join(
        f"{name} on {listen} {'answers' if ok else 'is silent'}" for name, listen, ok in states
    )


async def unhealthy(
    unit: UnitConfig,
    stop: asyncio.Event,
    *,
    clock: Clock = SYSTEM_CLOCK,
    interval: float = 2.0,
    strikes: int = UNHEALTHY_STRIKES,
) -> str | None:
    """Wait until a service of a unit that runs apart stops answering ``strikes`` probes
    in a row (the reason), or ``stop`` is set (None): how a launcher that did not start
    the unit learns it has gone."""
    silent = 0
    while True:
        if await wait_or_sleep(clock, stop, interval):
            return None
        states = await asyncio.to_thread(health, unit)
        silent = 0 if all(ok for _, _, ok in states) else silent + 1
        if silent >= strikes:
            return f"the unit is no longer healthy: {describe(states)}"


def _echo(name: str, line: str) -> None:
    print(f"[{name}] {line}", flush=True)


def _say(text: str, *, error: bool = False) -> None:
    print(
        f"[unit] {'error: ' if error else ''}{text}",
        file=sys.stderr if error else sys.stdout,
        flush=True,
    )


async def run(unit: UnitConfig, *, ready: Callable[[], None] = lambda: None) -> int:
    """Start the services, supervise them until a stop signal or a service stops, and stop
    them. Exits 0 after a stop signal and 1 after anything else."""
    problems = unit.problems()
    if problems:
        raise LauncherError("cannot start:\n" + "\n".join(f"  {problem}" for problem in problems))
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for number in STOP_SIGNALS:
        loop.add_signal_handler(number, stop.set)
    written: list[Path] = []
    supervisor = Supervisor(
        Layout(unit.runtime_dir),
        stop,
        stop_timeout=unit.options.stop_timeout,
        env=environment(),
        echo=_echo,
        keep_logs=False,
    )
    status = 0
    try:
        written = unit.materialize()
        for service in unit.services.values():
            _say(f"starting the {service.name} on {service.listen}")
            await supervisor.start(unit.part(service), unit.options.start_timeout)
        _say("the unit is ready: " + ", ".join(unit.services))
        ready()
        reason = await supervisor.watch()
        if reason is not None:
            _say(f"{reason}; stopping the unit", error=True)
            status = 1
    except StopRequested:
        pass
    except LauncherError as exc:
        _say(f"{exc}; stopping the unit", error=True)
        status = 1
    finally:
        await supervisor.stop()
        for path in written:
            path.unlink(missing_ok=True)
        for number in STOP_SIGNALS:
            loop.remove_signal_handler(number)
    _say("the unit stopped")
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbox-science-unit",
        description="The worker, the policy middleware and the package cache, one unit.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("serve", "start the services and supervise them until a stop signal"),
        ("check", "check the config, the commands and the settings files"),
        ("status", "probe each service's address; exit 0 only if all answer"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument(
            "--only",
            action="append",
            default=[],
            choices=SERVICES,
            help="run only this service of the file (repeat for more)",
        )
    args = parser.parse_args(argv)
    try:
        unit = load(args.config, only=args.only)
        match args.command:
            case "serve":
                return asyncio.run(run(unit))
            case "check":
                problems = unit.problems()
                if problems:
                    raise LauncherError("\n".join(problems))
                for service in unit.services.values():
                    source = service.settings_file or "inline"
                    print(f"{service.name}: {service.listen} ({source})")
                print(f"valid; {len(unit.services)} services")
                return 0
            case _:
                states = health(unit)
                for name, listen, ok in states:
                    print(f"{name}: {listen} {'answers' if ok else 'is silent'}")
                return 0 if all(ok for _, _, ok in states) else 1
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
