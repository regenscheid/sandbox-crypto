# SPDX-License-Identifier: MIT
"""Package commands in a sandbox: one at a time, each recorded (Conda provisioning, CP-4, CP-11).

The worker copies this file beside the executor, which loads it; it depends only on
the standard library. The executor writes, in its state directory, a wrapper for each
package manager on ``PATH`` (pip, uv, conda, mamba, micromamba) and puts the wrappers
first on ``PATH``. It also writes a ``sitecustomize`` hook that the environment's
Python loads, for the same managers run as modules: ``python -m pip``, which IPython's
``%pip`` runs, and ``python -m uv``.

A command that may change the environment takes the environment's lock, waiting its
turn, runs as a child of the wrapper, and leaves a record in a journal: who ran it,
the packages' fingerprint before and after, and a lockfile when they changed. Commands
that only read (``pip list``, ``conda info``) run at once, without the lock. The
executor waits while the lock is held before it starts an execution (CP-12), and turns
the journal into the environment's revisions; a change that no record explains is
unmanaged (docs/WORKER.md, "Package commands").

Research code shares the sandbox and can bypass all of this: by running a manager by
its full path, clearing ``PATH`` or ``PYTHONPATH``, or writing the journal itself. The
lock serializes cooperating commands; it is no security boundary.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, cast

OWNER: Final = "SANDBOX_SCIENCE_OWNER"
"""The executor's mark: names the call or kernel that started a process."""
HOLDER: Final = "SANDBOX_SCIENCE_PACKAGE_LOCK"
"""Set for what a package command starts: the process holding the lock, ``pid:start``."""
LOCKFILE_FORMAT: Final = "sandbox-science-lockfile"
LOCKFILE_VERSION: Final = 1
RECORD_VERSION: Final = 1
MAX_ARGUMENTS: Final = 64
MAX_ARGUMENT: Final = 1024
MAX_FIELD: Final = 256
"""Characters in a lockfile's names, versions, builds, subdirs and licenses."""
MAX_TEXT: Final = 2048
"""Characters in a lockfile's channels, file names and URLs."""
MAX_PACKAGES: Final = 20_000
"""Entries in each section of a lockfile."""
MAX_META_BYTES: Final = 64 * 1024 * 1024
"""The largest ``conda-meta`` record read; their file lists make some large."""
MAX_RECORD_BYTES: Final = 32 * 1024 * 1024
MAX_RECORDS: Final = 256
"""Journal records the executor reads at a time; the rest wait for its next look."""
MAX_ANCESTORS: Final = 64
SI_KERNEL: Final = 0x80
"""``si_code`` of a signal the kernel sent, such as a terminal's: the whole process group
gets it, the command too, so the wrapper does not pass it on again."""

CHANGING: Final[Mapping[str, frozenset[str]]] = {
    "pip": frozenset({"install", "uninstall"}),
    "uv": frozenset({"install", "uninstall", "sync"}),
    "conda": frozenset({"install", "update", "upgrade", "remove", "uninstall"}),
}
"""Subcommands that change an environment, for each family of package managers."""
MODULES: Final = frozenset({"pip", "uv", "conda"})
"""Package managers that run as ``python -m <module>``."""
FORWARDED: Final = frozenset(
    {
        signal.SIGHUP,
        signal.SIGINT,
        signal.SIGQUIT,
        signal.SIGTERM,
        signal.SIGUSR1,
        signal.SIGUSR2,
        signal.SIGALRM,
        signal.SIGWINCH,
    }
)
"""Signals a wrapper passes on to its command."""
SUBDIRS: Final = {
    "x86_64": "linux-64",
    "aarch64": "linux-aarch64",
    "arm64": "linux-aarch64",
    "ppc64le": "linux-ppc64le",
    "s390x": "linux-s390x",
}
UPSTREAM_CONDA: Final = "conda.anaconda.org"
UPSTREAM_FILES: Final = "https://files.pythonhosted.org/"

_PIP: Final = re.compile(r"pip(?:[0-9]+(?:\.[0-9]+)?)?")
_CHANNEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/label/[A-Za-z0-9._-]+)?")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT: Final = re.compile(r"[0-9a-f]{32}")
_TOKEN: Final = re.compile(r"^/t/[^/]+")

Message = dict[str, Any]

HOOK: Final = """\
# Written by the Sandbox Science executor, which rewrites it whenever it starts.
# Package managers run as modules (python -m pip, IPython's %pip) take their turn in
# this environment, as they do through its wrappers (docs/WORKER.md).
import sys


def _sandbox_science_turn():
    argv = getattr(sys, "orig_argv", None)
    if not argv or not sys.argv or sys.argv[0] != "-m" or len(argv) < len(sys.argv):
        return
    token = argv[len(argv) - len(sys.argv)]
    module = token[1:].partition("m")[2] if token.startswith("-") else token
    changing = @CHANGING@
    words = set(sys.argv[1:])
    if module not in changing or words.isdisjoint(changing[module]):
        return  # not a package manager, or one that only reads: no turn
    if module == "uv" and "pip" not in words:
        return
    import os

    if os.path.realpath(sys.prefix) != os.path.realpath(@PREFIX@):
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_sandbox_science_provisioning", @SCRIPT@)
    if spec is None or spec.loader is None:
        return
    provisioning = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = provisioning
    spec.loader.exec_module(provisioning)
    provisioning.hook(@STATE@, module, list(argv), list(sys.argv[1:]))


def _sandbox_science_next():
    # The environment's own sitecustomize, which this one shadows on sys.path.
    import importlib.machinery
    import importlib.util
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    rest = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


for _sandbox_science_step in (_sandbox_science_turn, _sandbox_science_next):
    try:
        _sandbox_science_step()
    except Exception as _sandbox_science_error:  # never stop Python from starting
        sys.stderr.write("sandbox-science: %s\\n" % (_sandbox_science_error,))
"""

WRAPPER: Final = """\
#!/bin/sh
# Written by the Sandbox Science executor, which rewrites it before each attempt. A
# package command that may change this environment takes its turn and is recorded;
# any other runs at once (docs/WORKER.md).
for argument do
  case $argument in
    @CHANGING@) exec @PYTHON@ -I -S @SCRIPT@ wrap @STATE@ @TOOL@ @REAL@ "$@" ;;
  esac
done
exec @REAL@ "$@"
"""
"""A wrapper for pip, conda, mamba or micromamba: any changing subcommand among its
arguments takes a turn, as changes_environment says."""

UV_WRAPPER: Final = """\
#!/bin/sh
# Written by the Sandbox Science executor, which rewrites it before each attempt. A
# package command that may change this environment takes its turn and is recorded;
# any other runs at once (docs/WORKER.md).
for argument do
  case $argument in
    pip)
      for word do
        case $word in
          @CHANGING@) exec @PYTHON@ -I -S @SCRIPT@ wrap @STATE@ @TOOL@ @REAL@ "$@" ;;
        esac
      done ;;
  esac
done
exec @REAL@ "$@"
"""
"""uv's wrapper: only its pip interface changes the environment."""


# Commands.


def family(tool: str) -> str | None:
    """The package manager a command name runs: ``pip`` (``pip3``, ``pip3.14``), ``uv`` or
    ``conda`` (also mamba and micromamba); None for anything else."""
    if _PIP.fullmatch(tool):
        return "pip"
    if tool == "uv":
        return "uv"
    if tool in ("conda", "mamba", "micromamba"):
        return "conda"
    return None


def changes_environment(tool: str, args: Sequence[str]) -> bool:
    """Whether a package command may change the environment, and so needs its turn.

    Any argument that names a changing subcommand counts, wherever it stands: an
    option's value that happens to be ``install`` costs a turn, never a missed change.
    uv changes the environment only through its pip interface (``uv pip install``).
    """
    kind = family(tool)
    if kind is None:
        return False
    words = set(args)
    if kind == "uv" and "pip" not in words:
        return False
    return not words.isdisjoint(CHANGING[kind])


def tool_names() -> tuple[str, ...]:
    """The commands that get a wrapper when they are on ``PATH``."""
    major, minor = sys.version_info[:2]
    return ("pip", f"pip{major}", f"pip{major}.{minor}", "uv", "conda", "mamba", "micromamba")


def bounded_command(argv: Sequence[str]) -> list[str]:
    """A command line as records keep it: at most 64 arguments of 1,024 characters."""
    kept = [argument[:MAX_ARGUMENT] for argument in argv[:MAX_ARGUMENTS]]
    if len(argv) > MAX_ARGUMENTS:
        kept.append(f"[… {len(argv) - MAX_ARGUMENTS} more arguments]")
    return kept


# The packages: where they are recorded, their fingerprint and their lockfile.


@dataclass(frozen=True, slots=True)
class Places:
    """Where an environment records its packages: Conda's ``conda-meta``, and pip's
    ``dist-info`` and ``egg-info`` directories in the prefix's and the user's site-packages."""

    conda_meta: str
    site: tuple[str, ...]
    user: str | None = None

    @classmethod
    def here(cls) -> Places:
        """The places of the running interpreter's environment."""
        import site
        import sysconfig

        paths = sysconfig.get_paths()
        user: str | None = None
        with contextlib.suppress(Exception):
            user = site.getusersitepackages()
        site_paths = tuple(dict.fromkeys([paths["purelib"], paths["platlib"]]))
        return cls(os.path.join(sys.prefix, "conda-meta"), site_paths, user)

    def all(self) -> list[str]:
        user = [self.user] if self.user else []
        return list(dict.fromkeys([self.conda_meta, *self.site, *user]))

    def to_json(self) -> Message:
        return {"conda_meta": self.conda_meta, "site": list(self.site), "user": self.user}

    @classmethod
    def from_json(cls, value: object) -> Places:
        fields = cast(Message, value) if isinstance(value, dict) else {}
        conda_meta, site, user = fields.get("conda_meta"), fields.get("site"), fields.get("user")
        entries = cast(list[object], site) if isinstance(site, list) else []
        if not isinstance(conda_meta, str) or not all(isinstance(item, str) for item in entries):
            raise ValueError("malformed places")
        return cls(
            conda_meta,
            tuple(str(item) for item in entries),
            user if isinstance(user, str) else None,
        )


def fingerprint(places: Places) -> str:
    """A digest of the packages installed in an environment (W3).

    Conda records each package of the prefix in ``conda-meta``, and pip each
    distribution in a ``.dist-info`` or ``.egg-info`` directory. Installing, removing or
    upgrading one changes the digest. The files the packages own do not count, which
    keeps the scan cheap.
    """
    digest = hashlib.sha256()
    for place in places.all():
        try:
            with os.scandir(place) as scan:
                entries = sorted((entry.name, entry) for entry in scan)
        except OSError:
            continue
        for name, entry in entries:
            if not (name.endswith((".json", ".dist-info", ".egg-info")) or name == "history"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            line = f"{place}\0{name}\0{info.st_size}\0{info.st_mtime_ns}\n"
            digest.update(line.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:32]


def lockfile(places: Places) -> Message:
    """The packages installed in an environment, as the platform's lockfile (CP-18).

    Conda packages are named by channel, subdir, file name and SHA-256, never by the
    package cache's address. Distributions pip or uv installed follow, by name and
    version, with the URL and hash of a direct-URL install; those a Conda package
    installed are left out, since the Conda entry stands for them.
    """
    conda, owned = _conda_packages(places.conda_meta)
    pip: list[Message] = []
    for directory in places.site:
        pip += _distributions(directory, "prefix", owned)
    if places.user and places.user not in places.site:
        pip += _distributions(places.user, "user", owned)
    conda.sort(
        key=lambda item: (item["name"], item["version"], item["build"], item["subdir"] or "")
    )
    pip.sort(key=lambda item: (item["name"].lower(), item["version"], item["location"]))
    return {
        "format": LOCKFILE_FORMAT,
        "version": LOCKFILE_VERSION,
        "python": platform.python_version(),
        "platform": SUBDIRS.get(platform.machine().lower()),
        "conda": conda[:MAX_PACKAGES],
        "pip": pip[:MAX_PACKAGES],
    }


def _conda_packages(directory: str) -> tuple[list[Message], set[str]]:
    """The lockfile's Conda entries, and the pip metadata directories their packages own."""
    try:
        with os.scandir(directory) as scan:
            names = sorted(entry.name for entry in scan if entry.name.endswith(".json"))
    except OSError:
        return [], set()
    packages: list[Message] = []
    owned: set[str] = set()
    for name in names[:MAX_PACKAGES]:
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                data = handle.read(MAX_META_BYTES + 1)
            record: object = json.loads(data) if len(data) <= MAX_META_BYTES else None
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        fields = cast(Message, record)
        package = conda_package(fields)
        if package is not None:
            packages.append(package)
        files = fields.get("files")
        for path in cast(list[object], files) if isinstance(files, list) else []:
            if isinstance(path, str) and (entry := _site_entry(path)) is not None:
                owned.add(entry)
    return packages, owned


def _site_entry(path: str) -> str | None:
    """The pip metadata directory a package file belongs to: ``x-1.0.dist-info`` for
    ``lib/python3.14/site-packages/x-1.0.dist-info/METADATA``."""
    segments = path.split("/")
    for index, segment in enumerate(segments[:-1]):
        if segment == "site-packages":
            entry = segments[index + 1]
            return entry if entry.endswith((".dist-info", ".egg-info")) else None
    return None


def conda_package(record: Mapping[str, object]) -> Message | None:
    """A ``conda-meta`` record as a lockfile entry; None if it names no package."""
    name, version = _text(record.get("name")), _text(record.get("version"))
    build = _text(record.get("build")) or _text(record.get("build_string"))
    if name is None or version is None or build is None:
        return None
    url = _text(record.get("url"), MAX_TEXT)
    file = _text(record.get("fn"), MAX_TEXT)
    channel: str | None = None
    subdir: str | None = None
    if url is not None and url.count("/") >= 3:
        base, _, last = url.rpartition("/")
        channel, _, subdir = base.rpartition("/")
        file = file or last
    if not channel or not subdir:
        channel, subdir = _text(record.get("channel"), MAX_TEXT), _text(record.get("subdir"))
        if channel is not None and subdir is not None:
            # conda writes the channel with its subdir: …/conda-forge/linux-64.
            trimmed = channel.rstrip("/")
            if trimmed.endswith("/" + subdir):
                channel = trimmed[: -len(subdir) - 1]
    return {
        "name": name,
        "version": version,
        "build": build,
        "channel": canonical_channel(channel) if channel else None,
        "subdir": _text(subdir),
        "file": file,
        "sha256": _hex(record.get("sha256"), 64),
        "md5": _hex(record.get("md5"), 32),
        "license": _text(record.get("license")),
    }


def canonical_channel(channel: str) -> str:
    """A Conda channel as a lockfile names it: by name, never by the package cache's address.

    ``https://conda.anaconda.org/conda-forge`` and the package cache's
    ``<cache>/conda/conda-forge`` (docs/PACKAGE-CACHE.md, "URL layout"), on whatever
    host the cache had, are both ``conda-forge``. Any other channel keeps its URL,
    without user information or an anaconda.org token.
    """
    value = channel.strip().rstrip("/")
    if "://" not in value:
        return value
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return value
    path = _TOKEN.sub("", parts.path)
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https"):
        if host == UPSTREAM_CONDA and _CHANNEL.fullmatch(path.strip("/")):
            return path.strip("/")
        segments = path.strip("/").split("/")
        for index in range(len(segments) - 2, -1, -1):
            name = "/".join(segments[index + 1 :])
            if segments[index] == "conda" and _CHANNEL.fullmatch(name):
                return name
    return urllib.parse.urlunsplit(
        (parts.scheme, _netloc(host, port), path.rstrip("/"), "", "")
    ).rstrip("/")


def canonical_url(url: str) -> str:
    """A download URL as a lockfile keeps it: without user information, query or fragment,
    and the package cache's PyPI files (``<cache>/pypi/files/<project>/<sha256>/<path>``)
    named by their URL on files.pythonhosted.org."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url
    segments = parts.path.split("/")
    if parts.scheme in ("http", "https"):
        for index in range(len(segments) - 4):
            if (
                segments[index] == "pypi"
                and segments[index + 1] == "files"
                and _SHA256.fullmatch(segments[index + 3])
            ):
                return UPSTREAM_FILES + "/".join(segments[index + 4 :])
    if not parts.netloc:
        return urllib.parse.urlunsplit((parts.scheme, "", parts.path, "", ""))
    host = (parts.hostname or "").lower()
    return urllib.parse.urlunsplit((parts.scheme, _netloc(host, port), parts.path, "", ""))


def _netloc(host: str, port: int | None) -> str:
    named = f"[{host}]" if ":" in host else host
    return named if port is None else f"{named}:{port}"


def _distributions(directory: str, location: str, owned: set[str]) -> list[Message]:
    """The lockfile's pip entries for one site-packages directory."""
    try:
        with os.scandir(directory) as scan:
            names = sorted(
                entry.name for entry in scan if entry.name.endswith((".dist-info", ".egg-info"))
            )
    except OSError:
        return []
    found: list[Message] = []
    for name in names[:MAX_PACKAGES]:
        if name in owned:
            continue
        path = os.path.join(directory, name)
        directory_entry = os.path.isdir(path)
        installer = _first_line(os.path.join(path, "INSTALLER")) if directory_entry else None
        if installer is not None and installer.lower() == "conda":
            continue
        described = _metadata(path, name, directory_entry)
        if described is None:
            continue
        project, version = described
        url, sha256 = _direct_url(path) if directory_entry else (None, None)
        found.append(
            {
                "name": project,
                "version": version,
                "installer": _text(installer),
                "location": location,
                "url": url,
                "sha256": sha256,
            }
        )
    return found


def _metadata(path: str, name: str, directory: bool) -> tuple[str, str] | None:
    """A distribution's name and version, from its metadata or else its directory's name."""
    source = path
    if directory:
        source = os.path.join(path, "METADATA" if name.endswith(".dist-info") else "PKG-INFO")
    project = version = None
    with contextlib.suppress(OSError):
        with open(source, "rb") as handle:
            head = handle.read(64 * 1024).decode("utf-8", "replace")
        for line in head.splitlines():
            if not line.strip():
                break
            key, _, value = line.partition(":")
            if key.lower() == "name" and project is None:
                project = _text(value.strip())
            elif key.lower() == "version" and version is None:
                version = _text(value.strip())
    if project is None or version is None:
        stem = name.rsplit(".", 1)[0]
        guessed, _, rest = stem.partition("-")
        project = project or _text(guessed)
        version = version or _text(rest.split("-py", 1)[0])
    return (project, version) if project and version else None


def _direct_url(path: str) -> tuple[str | None, str | None]:
    """The URL and SHA-256 of a direct-URL install (PEP 610), canonical."""
    try:
        with open(os.path.join(path, "direct_url.json"), "rb") as handle:
            data: object = json.loads(handle.read(1024 * 1024))
    except (OSError, ValueError, RecursionError):
        return None, None
    fields = cast(Message, data) if isinstance(data, dict) else {}
    raw = fields.get("url")
    url = _text(canonical_url(raw), MAX_TEXT) if isinstance(raw, str) else None
    archive = fields.get("archive_info")
    info = cast(Message, archive) if isinstance(archive, dict) else {}
    hashes = info.get("hashes")
    sha256 = _hex(cast(Message, hashes).get("sha256"), 64) if isinstance(hashes, dict) else None
    legacy = info.get("hash")
    if sha256 is None and isinstance(legacy, str) and legacy.startswith("sha256="):
        sha256 = _hex(legacy[len("sha256=") :], 64)
    if sha256 is None and url is not None:
        # The cache's PyPI files carry their SHA-256 in the path, which canonical_url drops.
        for segment in str(raw).split("/"):
            if _SHA256.fullmatch(segment):
                sha256 = segment
    return url, sha256


def _text(value: object, limit: int = MAX_FIELD) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit and value.isprintable() else None


def _hex(value: object, length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if len(value) == length and all(c in "0123456789abcdef" for c in value) else None


def _first_line(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(256).decode("utf-8", "replace").strip().split("\n", 1)[0] or None
    except OSError:
        return None


# Processes.


def process_start(pid: int) -> int | None:
    """A process's start time in clock ticks; with its pid, it names the process."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        return int(data[data.rfind(b")") + 2 :].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _parent(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        return int(data[data.rfind(b")") + 2 :].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def is_ancestor(pid: int, start: int | None) -> bool:
    """Whether the process ``pid``, started at ``start``, is an ancestor of this one."""
    current = os.getppid()
    for _ in range(MAX_ANCESTORS):
        if current <= 1:
            return False
        if current == pid:
            return start is None or process_start(current) == start
        parent = _parent(current)
        if parent is None:
            return False
        current = parent
    return False


def _marker() -> str:
    return f"{os.getpid()}:{process_start(os.getpid()) or 0}"


def inside_turn() -> bool:
    """Whether this process descends from a package command that holds the lock.

    Such a command's own steps (pip's build isolation running ``python -m pip``, a
    post-link script running pip) run within its turn; waiting for the lock would wait
    for themselves.
    """
    pid, _, start = os.environ.get(HOLDER, "").partition(":")
    if not pid.isdigit() or not start.isdigit():
        return False
    return is_ancestor(int(pid), int(start) or None)


# The state an executor shares with its package commands.


class State:
    """The directory an executor and the package commands it starts share.

    ``lock`` is the environment's lock: a command that may change the environment holds
    it exclusively, and the executor checks it shared. ``journal/`` holds a record of
    each such command. ``fingerprint`` is the packages' fingerprint as last recorded,
    ``holder.json`` names the command holding the lock, and ``config.json`` says where
    the environment records its packages.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = directory / "lock"
        self.journal = directory / "journal"
        self.holder = directory / "holder.json"
        self.known = directory / "fingerprint"
        self.config = directory / "config.json"
        self.bin = directory / "bin"
        self.hook = directory / "python"

    # The lock.

    def _open_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        try:
            return os.open(self.lock, flags, 0o600)
        except FileNotFoundError:  # research code removed the directory: make it again
            self.directory.mkdir(parents=True, exist_ok=True)
            return os.open(self.lock, flags, 0o600)

    def try_lock(self, *, exclusive: bool) -> int | None:
        """The lock, held on the returned descriptor; None while someone holds it exclusively
        (or, for ``exclusive``, at all)."""
        descriptor = self._open_lock()
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def wait_lock(self) -> int:
        """The lock, held exclusively on the returned descriptor, once it is free."""
        descriptor = self._open_lock()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def holding(self) -> Message | None:
        """What holds the lock, as its holder wrote it; None if it says nothing."""
        try:
            data: object = json.loads(self.holder.read_bytes()[:65536])
        except (OSError, ValueError, RecursionError):
            return None
        return cast(Message, data) if isinstance(data, dict) else None

    # The last recorded fingerprint.

    def read_known(self) -> str | None:
        try:
            value = self.known.read_text().strip()
        except OSError:
            return None
        return value if _FINGERPRINT.fullmatch(value) else None

    def write_known(self, value: str) -> None:
        _replace(self.known, value.encode())

    # The journal.

    def record(self, entry: Message) -> None:
        """Add a package command's record, named so that records sort in the order they ended."""
        name = f"{time.time_ns():020d}-{os.getpid()}.json"
        _replace(self.journal / name, json.dumps(entry, separators=(",", ":")).encode())

    def take_records(self) -> list[Message]:
        """The records in the order the commands ended, removed from the journal."""
        try:
            with os.scandir(self.journal) as scan:
                names = sorted(entry.name for entry in scan if entry.name.endswith(".json"))
        except OSError:
            return []
        records: list[Message] = []
        for name in names[:MAX_RECORDS]:
            path = self.journal / name
            try:
                with open(path, "rb") as handle:
                    data = handle.read(MAX_RECORD_BYTES + 1)
                path.unlink()
                decoded: object = json.loads(data) if len(data) <= MAX_RECORD_BYTES else None
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(decoded, dict):
                records.append(cast(Message, decoded))
        return records

    def clear_journal(self) -> None:
        with contextlib.suppress(OSError), os.scandir(self.journal) as scan:
            for entry in list(scan):
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)

    # Configuration.

    def read_config(self) -> Config:
        data: object = json.loads(self.config.read_bytes())
        fields = cast(Message, data) if isinstance(data, dict) else {}
        python, script, prefix = fields.get("python"), fields.get("script"), fields.get("prefix")
        if not (isinstance(python, str) and isinstance(script, str) and isinstance(prefix, str)):
            raise ValueError(f"malformed {self.config}")
        return Config(python, script, prefix, Places.from_json(fields.get("places")))


@dataclass(frozen=True, slots=True)
class Config:
    """What the executor tells its package commands: its interpreter, this file, the prefix
    and where the prefix records its packages."""

    python: str
    script: str
    prefix: str
    places: Places


def _replace(target: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file whole: readers see the old content or the new, never part of it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# The executor's side: wrappers, the hook, observing changes.


def prepare(state: State, config: Config) -> None:
    """Create the state directory and write the configuration and the hook, unless they are
    there already."""
    for directory in (state.directory, state.journal, state.bin, state.hook):
        directory.mkdir(parents=True, exist_ok=True)
    fields = {
        "python": config.python,
        "script": config.script,
        "prefix": config.prefix,
        "places": config.places.to_json(),
    }
    _update(state.config, json.dumps(fields, indent=1).encode())
    changing = {module: sorted(CHANGING[module]) for module in sorted(MODULES)}
    source = (
        HOOK.replace("@CHANGING@", repr(changing))
        .replace("@PREFIX@", repr(config.prefix))
        .replace("@SCRIPT@", repr(config.script))
        .replace("@STATE@", repr(str(state.directory)))
    )
    _update(state.hook / "sitecustomize.py", source.encode())


def refresh(state: State, config: Config, path: str) -> list[str]:
    """Give each package manager on ``path`` a wrapper, and remove those whose manager is
    gone, so that ``command -v`` tells the truth; return the wrapped names. The rest of
    the state directory is put back too, should research code have removed it."""
    prepare(state, config)
    wrapped: list[str] = []
    for name in tool_names():
        target = state.bin / name
        real = find(name, path, state.bin)
        kind = family(name)
        if real is None or kind is None:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            continue
        template = UV_WRAPPER if kind == "uv" else WRAPPER
        source = (
            template.replace("@CHANGING@", "|".join(sorted(CHANGING[kind])))
            .replace("@PYTHON@", shlex.quote(config.python))
            .replace("@SCRIPT@", shlex.quote(config.script))
            .replace("@STATE@", shlex.quote(str(state.directory)))
            .replace("@TOOL@", shlex.quote(name))
            .replace("@REAL@", shlex.quote(real))
        )
        _update(target, source.encode(), 0o755)
        wrapped.append(name)
    return wrapped


def _update(target: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file unless it already has this content: a shell reading a wrapper as it
    runs keeps the file it opened."""
    with contextlib.suppress(OSError):
        if target.read_bytes() == data and (target.stat().st_mode & 0o777) == mode:
            return
    _replace(target, data, mode)


def variables(state: State, env: Mapping[str, str], wrapped: Iterable[str]) -> dict[str, str]:
    """``env``'s variables that change for commands and kernels: the wrappers first on
    ``PATH``, the hook first on ``PYTHONPATH``, and ``CONDA_EXE`` and ``MAMBA_EXE`` naming
    the wrappers, which IPython's ``%conda`` and ``%mamba`` run, unless they are set."""
    names = set(wrapped)
    bin_dir, hook = str(state.bin), str(state.hook)
    rest = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != bin_dir]
    python = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e and e != hook]
    changed = {
        "PATH": os.pathsep.join([bin_dir, *rest]),
        "PYTHONPATH": os.pathsep.join([hook, *python]),
    }
    if "CONDA_EXE" not in env and "conda" in names:
        changed["CONDA_EXE"] = str(state.bin / "conda")
    if "MAMBA_EXE" not in env:
        for name in ("micromamba", "mamba"):
            if name in names:
                changed["MAMBA_EXE"] = str(state.bin / name)
                break
    return changed


def find(name: str, path: str, skip: Path) -> str | None:
    """The command ``name`` on ``path``, past the wrappers' directory ``skip``."""
    skipped = os.path.realpath(skip)
    for entry in path.split(os.pathsep):
        if not entry or os.path.realpath(entry) == skipped:
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def baseline(state: State, places: Places) -> str:
    """The fingerprint as an executor starts: records left from before it are dropped,
    since the worker may have seen their changes another way."""
    state.clear_journal()
    now = fingerprint(places)
    state.write_known(now)
    return now


@dataclass(slots=True)
class Observation:
    """What changed since the executor last looked."""

    records: list[Message]
    """The journal's records, in the order their commands ended."""
    fingerprint: str | None
    """The packages' fingerprint now; None while a package command holds the lock."""
    lockfile: Message | None
    """The lockfile for ``fingerprint``, when no record accounts for it."""
    busy: Message | None
    """What held the lock, when a package command did."""


def observe(state: State, places: Places, known: str, held: int | None = None) -> Observation:
    """Read the journal and the packages, unless a package command is changing them.

    ``held`` is a descriptor on which the caller holds the lock shared, and which this
    closes; otherwise this tries to take it. Under the lock no record can appear between
    reading the journal and reading the packages, so a change no record accounts for is
    one no package command made.
    """
    descriptor = held if held is not None else state.try_lock(exclusive=False)
    try:
        records = [record for record in state.take_records() if _usable(record)]
        if descriptor is None:
            return Observation(records, None, None, state.holding() or {})
        now = fingerprint(places)
        last = str(records[-1]["after"]) if records else known
        made = lockfile(places) if now != last else None
        if state.read_known() != now:
            state.write_known(now)
        return Observation(records, now, made, None)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _usable(record: Message) -> bool:
    before, after = record.get("before"), record.get("after")
    return (
        isinstance(before, str)
        and isinstance(after, str)
        and _FINGERPRINT.fullmatch(before) is not None
        and _FINGERPRINT.fullmatch(after) is not None
    )


def current(state: State, places: Places, wait: float) -> tuple[str, Message] | None:
    """The fingerprint and lockfile now, waiting up to ``wait`` seconds for a package
    command to finish; None if one still runs."""
    deadline = time.monotonic() + wait
    while (descriptor := state.try_lock(exclusive=False)) is None:
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)
    try:
        return fingerprint(places), lockfile(places)
    finally:
        os.close(descriptor)


def describe(holder: Message | None) -> str:
    """The command holding the lock, in words."""
    if not holder:
        return "a package command"
    command = holder.get("command")
    words = [str(item) for item in cast(list[object], command)] if isinstance(command, list) else []
    text = " ".join(words)[:200] or "a package command"
    pid, since = holder.get("pid"), holder.get("since")
    if isinstance(pid, int) and isinstance(since, int | float) and not isinstance(since, bool):
        return f"{text} (pid {pid}, for {max(0.0, time.time() - since):.0f} s)"
    return text


# The package command's side: its turn.


class Turn:
    """A package command's turn: the lock, the packages before, and the record after."""

    def __init__(self, state: State, config: Config, command: Sequence[str], via: str) -> None:
        self.state = state
        self.config = config
        self.command = bounded_command(command)
        self.via = via
        self.invoked = time.time()
        self.descriptor: int | None = None
        self.started = self.invoked
        self.before = ""
        self.before_lockfile: Message | None = None

    def begin(self) -> bool:
        """Take the lock, waiting for it; False if the holder is an ancestor, whose turn
        this command is part of."""
        descriptor = self.state.try_lock(exclusive=True)
        if descriptor is None:
            holder = self.state.holding() or {}
            pid, start = holder.get("pid"), holder.get("start")
            known = start if isinstance(start, int) else None
            if isinstance(pid, int) and is_ancestor(pid, known):
                # A step of the holder's own command whose environment lost the mark.
                os.environ[HOLDER] = f"{pid}:{known or 0}"
                return False
            sys.stderr.write(
                "sandbox-science: waiting for a package command to finish in this "
                f"environment: {describe(holder)}\n"
            )
            sys.stderr.flush()
            descriptor = self.state.wait_lock()
        self.descriptor = descriptor
        self.started = time.time()
        holder = {
            "pid": os.getpid(),
            "start": process_start(os.getpid()),
            "command": self.command,
            "owner": os.environ.get(OWNER, ""),
            "since": self.started,
        }
        with contextlib.suppress(OSError):
            _replace(self.state.holder, json.dumps(holder).encode())
        self.before = fingerprint(self.config.places)
        known = self.state.read_known()
        if known is not None and known != self.before:
            # Changed since the last record or look, by something else: keep that state too.
            self.before_lockfile = lockfile(self.config.places)
        return True

    def end(self, code: int | None) -> None:
        """Record the command, then give the lock up. A record that cannot be written is
        lost, never the command's exit status: the executor then sees an unmanaged change."""
        try:
            after = fingerprint(self.config.places)
            entry: Message = {
                "version": RECORD_VERSION,
                "command": self.command,
                "via": self.via,
                "owner": os.environ.get(OWNER, "")[:MAX_FIELD],
                "pid": os.getpid(),
                "invoked": self.invoked,
                "started": self.started,
                "ended": time.time(),
                "exit_code": code if code is not None and code >= 0 else None,
                "signal": -code if code is not None and code < 0 else None,
                "before": self.before,
                "after": after,
                "before_lockfile": self.before_lockfile,
                "lockfile": lockfile(self.config.places) if after != self.before else None,
            }
            self.state.record(entry)
            self.state.write_known(after)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"sandbox-science: the package command was not recorded: {exc}\n")
        finally:
            with contextlib.suppress(OSError):
                self.state.holder.unlink()
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None


def run(path: str, argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run a command to its end, passing on the signals this process gets; return its exit
    code, negative for the signal that ended it.

    The command gets this process's standard streams and other inheritable descriptors,
    signal mask and signal dispositions, apart from those Python itself ignores. A signal
    the kernel sent, as a terminal sends one to its whole foreground process group,
    reached the command already. The child is forked, as multiprocessing forks, rather
    than spawned through ``clone3``, which a sandbox's seccomp filter may refuse.
    """
    import subprocess

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    waited = {*FORWARDED, signal.SIGCHLD}
    original = signal.pthread_sigmask(signal.SIG_BLOCK, waited)

    def unblock() -> None:  # in the child, just before it runs the command
        signal.pthread_sigmask(signal.SIG_SETMASK, original)

    try:
        # restore_signals (the default) resets the dispositions Python ignores.
        child = subprocess.Popen(  # noqa: S603 - the command as it was invoked
            list(argv), executable=path, env=dict(env), close_fds=False, preexec_fn=unblock
        )
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, original)
        raise
    pid = child.pid
    while True:
        info = signal.sigtimedwait(waited, 1.0)
        if info is not None and info.si_signo != signal.SIGCHLD:
            if info.si_code != SI_KERNEL:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, info.si_signo)
            continue
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return 255
        if done == pid:
            return os.waitstatus_to_exitcode(status)


def exit_as(code: int) -> NoReturn:
    """End this process as its command ended: with its exit code, or by its signal."""
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    if code >= 0:
        os._exit(code)
    number = -code
    with contextlib.suppress(Exception):
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # the command dumped core already
    with contextlib.suppress(Exception):
        signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {number})
        os.kill(os.getpid(), number)
    os._exit(128 + number)


def take_turn(state: State, config: Config, path: str, argv: Sequence[str], via: str) -> int | None:
    """Run a command that may change the environment in its turn, and record it.

    Returns its exit code (negative: the signal that ended it), or None without running
    it when the command holding the lock started this one, which then runs within that
    turn.
    """
    turn = Turn(state, config, [os.path.basename(argv[0]), *argv[1:]], via)
    try:
        if not turn.begin():
            return None
    except KeyboardInterrupt:
        exit_as(130)
    code: int | None = None
    try:
        code = run(path, argv, {**os.environ, HOLDER: _marker()})
    finally:
        turn.end(code)
    return code


def wrap(directory: str, tool: str, path: str, args: Sequence[str]) -> NoReturn:
    """A wrapper's work for a command that may change the environment: run ``tool``, which
    is ``path``, in its turn. The shell wrapper runs any other command itself."""
    state = State(Path(directory))
    if not os.path.isfile(path):
        sys.stderr.write(f"{tool}: command not found ({path} is gone)\n")
        exit_as(127)
    argv = [tool, *args]
    if not changes_environment(tool, args) or inside_turn():
        os.execv(path, argv)  # noqa: S606 - the command as it was invoked
    try:
        config = state.read_config()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"sandbox-science: running {tool} without its turn: {exc}\n")
        os.execv(path, argv)  # noqa: S606
    try:
        code = take_turn(state, config, path, argv, "wrapper")
    except OSError as exc:
        sys.stderr.write(f"{tool}: cannot run {path}: {exc}\n")
        exit_as(126)
    if code is None:
        os.execv(path, argv)  # noqa: S606
    exit_as(code)


def hook(directory: str, module: str, argv: Sequence[str], args: Sequence[str]) -> None:
    """The ``sitecustomize`` hook's work, as Python starts with ``-m <module>``.

    If the module may change the environment, this process becomes the command's
    wrapper: it takes the turn, runs the same command line as a child, records it and
    exits as the child did. The module never runs in this process, since some replace
    themselves with another program (``python -m uv`` does), which would end the turn.
    Within another command's turn, it returns, and the module runs here.
    """
    if module not in MODULES or not changes_environment(module, args) or inside_turn():
        return
    state = State(Path(directory))
    try:
        config = state.read_config()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"sandbox-science: running {module} without its turn: {exc}\n")
        return
    code = take_turn(state, config, sys.executable, [sys.executable, *argv[1:]], "module")
    if code is not None:
        exit_as(code)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) >= 4 and arguments[0] == "wrap":
        wrap(arguments[1], arguments[2], arguments[3], arguments[4:])
    if arguments == ["lockfile"]:
        print(json.dumps(lockfile(Places.here()), indent=1))
        return 0
    sys.stderr.write("usage: provisioning.py wrap <state> <tool> <path> [args…] | lockfile\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
