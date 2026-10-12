# SPDX-License-Identifier: MIT
"""Package commands in a sandbox: one at a time, each recorded (Conda provisioning, CP-4, CP-11).

The worker copies this file beside the executor, which loads it; it depends only on
the standard library. The executor writes, in its state directory, a wrapper for each
package manager on ``PATH`` (pip, uv, conda, mamba, micromamba) and puts the wrappers
first on ``PATH``, in a directory laid out like the prefix's ``bin``. It also writes a
``sitecustomize`` hook that the environment's Python loads, for the same managers run as
modules: ``python -m pip``, which IPython's ``%pip`` runs, and ``python -m uv``.

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
import ipaddress
import json
import os
import platform
import re
import shlex
import signal
import socket
import stat
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NoReturn, cast

OWNER: Final = "SANDBOX_SCIENCE_OWNER"
"""The executor's mark: names the call or kernel that started a process."""
HOLDER: Final = "SANDBOX_SCIENCE_PACKAGE_LOCK"
"""Set for what a package command starts: the process holding the lock, ``pid:start``."""
LOCKFILE_FORMAT: Final = "sandbox-science-lockfile"
LOCKFILE_VERSION: Final = 1
RECORD_VERSION: Final = 2
MAX_ARGUMENTS: Final = 32
MAX_ARGUMENT: Final = 512
MAX_COMMAND: Final = 4096
"""Characters of a command line a record keeps, all arguments together."""
MAX_PACKAGES: Final = 20_000
"""Entries in each section of a lockfile."""
MAX_META_BYTES: Final = 64 * 1024 * 1024
"""The largest ``conda-meta`` record read; their file lists make some large."""
MAX_RECORD_BYTES: Final = 32 * 1024 * 1024
MAX_RECORDS: Final = 256
"""Journal records the executor reads at a time."""
MAX_ANCESTORS: Final = 64
SI_KERNEL: Final = 0x80
"""``si_code`` of a signal the kernel sent, such as a terminal's: the whole process group
gets it, the command too, so the wrapper does not pass it on again."""
PR_SET_PDEATHSIG: Final = 1
LEFTOVER_GRACE: Final = 2.0
"""Seconds a command's own steps get to end after the command did, before its turn ends
them."""
KILL_ROUNDS: Final = 5
KILL_WAIT: Final = 1.0

CHANGING: Final[Mapping[str, frozenset[str]]] = {
    "pip": frozenset({"install", "uninstall"}),
    "uv": frozenset({"install", "uninstall", "sync"}),
    "conda": frozenset({"install", "update", "upgrade", "remove", "uninstall"}),
}
"""Subcommands that change an environment, for each family of package managers."""
MODULES: Final = frozenset({"pip", "uv", "conda"})
"""Package managers that run as ``python -m <module>``."""
DRY_RUN: Final = "--dry-run"
"""The option with which every package manager here only reports what it would change."""
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
CHANNEL_SCHEMES: Final = frozenset({"http", "https", "file"})
SOURCE_SCHEMES: Final = frozenset(
    {"http", "https", "file"}
    | {f"{vcs}+{scheme}" for vcs in ("git", "hg", "svn", "bzr") for scheme in ("http", "https")}
    | {f"{vcs}+{scheme}" for vcs in ("git", "hg", "svn", "bzr") for scheme in ("ssh", "file")}
)
PYTHONS: Final = ("python", "python3", f"python{sys.version_info[0]}.{sys.version_info[1]}")
"""What gets a stand-in beside the wrappers, for scripts that run the Python next to pip: a
Python started by a link there would take the link's directory for its own."""

# The lockfile's fields, as packages/contracts says them (sandbox_science.contracts.revisions).
_NAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,255}")
_VERSION: Final = re.compile(r"[A-Za-z0-9_.+!-]{1,256}")
_BUILD: Final = re.compile(r"[A-Za-z0-9_.+]{1,256}")
_SUBDIR: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9_]+)?")
_FILE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+!-]{0,250}\.(?:conda|tar\.bz2)")
_CHANNEL_NAME: Final = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._-]{0,255}(?:/label/[A-Za-z0-9._-]{1,255})?"
)
_URL_TEXT: Final = re.compile(r"[a-z0-9+]+://[!-~]{1,2030}")
_INSTALLER: Final = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
_LICENSE: Final = re.compile(r"[ -~]{1,256}")
_PIP: Final = re.compile(r"pip(?:[0-9]+(?:\.[0-9]+)?)?")
_FINGERPRINT: Final = re.compile(r"[0-9a-f]{32}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_TOKEN: Final = re.compile(r"^/t/[^/]+")
_LEGACY_IPV4: Final = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}")
"""An IPv4 address as ``inet_aton`` reads it, in decimal, octal or hexadecimal parts."""
# What a command line must not keep: credentials in URLs, and the values of token options.
# Each pattern takes time linear in what it reads, and options are short.
_AUTHORITY_END: Final = re.compile(r"[/?#\s]")
_WORD: Final = re.compile(r"\S+")
_PASSWORD: Final = re.compile(r"[^:]*:(?![0-9]+$)")
"""The start of an authority that is ``user:password``, not ``host:port``."""
_LATE_USERINFO: Final = re.compile(r"@(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?(?=[/?#]|$)")
"""The ``@host`` that ends user information with a ``/``, ``?`` or ``#`` in its password:
a host name or a bracketed IPv6 address, and a port, before a path, a query, a fragment
or the end."""
_PATH_TOKEN: Final = re.compile(r"(://[^/?#\s]*/t/)[^/?#\s]+")
"""anaconda.org's token in a channel's path: ``https://conda.anaconda.org/t/<token>/…``."""
_HOST_TOKEN: Final = re.compile(
    r"(://(?:[^/?#@\s]*@)?(?:dl\.cloudsmith\.io|(?:[A-Za-z0-9-]+\.)*fury\.io)(?::[0-9]+)?/)"
    r"(?!(?:public|basic)/)[^/?#\s]+(?=/[^/?#\s])",
    re.IGNORECASE,
)
"""The token Cloudsmith and Gemfury take as an index URL's first path segment."""
_SECRET_PARAMETER: Final = re.compile(
    r"([?&;][A-Za-z0-9_.-]*(?:token|key|secret|password|passwd|auth|sig|signature)=)[^&;#\s]*",
    re.IGNORECASE,
)
_SECRET_OPTION: Final = re.compile(
    r"--?[A-Za-z0-9_-]*(?:token|password|passwd|secret|api-key|apikey)[A-Za-z0-9_-]*",
    re.IGNORECASE,
)
_MAX_OPTION: Final = 64
_NOTE: Final = re.compile(r"\[… [0-9]{1,10} more arguments\]")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_UNSAFE: Final = re.compile(r"[\x00-\x20\x7f]")
"""What no URL a lockfile keeps may hold: spaces, line breaks and other controls."""

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

STAND_IN: Final = """\
#!/bin/sh
# Written by the Sandbox Science executor: the environment's own @NAME@, for scripts
# that run the Python beside the package managers (docs/WORKER.md).
exec @REAL@ "$@"
"""


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
    uv changes the environment only through its pip interface (``uv pip install``). A dry
    run only reads, as the provisioning tool's resolve step does (CP-7), and runs at once:
    ``install --dry-run`` as the command's first words, where ``--dry-run`` can be no other
    option's value (``pip install --no-binary --dry-run x`` installs).
    """
    kind = family(tool)
    if kind is None:
        return False
    words = set(args)
    if kind == "uv" and "pip" not in words:
        return False
    lead = list(args[1:3] if kind == "uv" else args[:2])
    if lead == ["install", DRY_RUN]:
        return False
    return not words.isdisjoint(CHANGING[kind])


def tool_names() -> tuple[str, ...]:
    """The commands that get a wrapper when they are on ``PATH``."""
    major, minor = sys.version_info[:2]
    return ("pip", f"pip{major}", f"pip{major}.{minor}", "uv", "conda", "mamba", "micromamba")


def redact(argument: str) -> str:
    """An argument without credentials: a URL's user information, the token some indexes
    take in a URL's path, and the values of parameters that name a token, key, secret or
    password become ``***``. Only what records keep is redacted, never what runs."""
    argument = _WORD.sub(lambda word: _without_userinfo(word.group()), argument)
    argument = _PATH_TOKEN.sub(r"\1***", argument)
    argument = _HOST_TOKEN.sub(r"\1***", argument)
    argument = _SECRET_PARAMETER.sub(r"\1***", argument)
    option, equals, _ = argument.partition("=")
    if equals and _secret_option(option):
        return f"{option}=***"
    return argument


def _without_userinfo(word: str) -> str:
    """``word`` with the user information of each URL in it replaced by ``***``: all that
    comes between ``://`` and the last ``@`` before the URL's path. A password with a
    ``/``, ``?`` or ``#`` in it ends the authority early; then user information runs to the
    ``@host`` after it."""
    kept: list[str] = []
    start = 0
    first = True
    while (index := word.find("://", start)) != -1:
        begin = index + 3
        end = _AUTHORITY_END.search(word, begin)
        stop = end.start() if end else len(word)
        at = word.rfind("@", begin, stop)
        if at == -1 and first and end is not None and _PASSWORD.match(word, begin, stop):
            late = _LATE_USERINFO.search(word, stop)
            at = late.start() if late else -1
        first = False
        kept.append(word[start:begin])
        if at != -1:
            kept.append("***")
            start = at
        else:
            start = begin
    kept.append(word[start:])
    return "".join(kept)


def _secret_option(option: str) -> bool:
    return len(option) <= _MAX_OPTION and _SECRET_OPTION.fullmatch(option) is not None


def _cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters. A URL whose user information the cut may have
    parted from its ``@`` keeps only its scheme."""
    text = text[:limit]
    index = text.rfind("://")
    if index != -1 and _AUTHORITY_END.search(text, index + 3) is None:
        text = text[: min(index + 3, limit - 3)] + "***"
    return text


def bounded_command(argv: Sequence[str], length: int | None = None) -> list[str]:
    """A command line as records keep it: control characters replaced by ``?``,
    credentials redacted, at most 32 arguments of 512 characters and 4,096 in all, and a
    note for what is left out. What it returns, it returns unchanged. ``length`` is the
    command line's, when ``argv`` is only its start."""
    whole = len(argv) if length is None else max(length, len(argv))
    kept: list[str] = []
    total = 0
    secret_next = False
    for index, argument in enumerate(argv):
        if index >= MAX_ARGUMENTS or total >= MAX_COMMAND:
            rest = whole - index
            kept.append(
                argument
                if rest == 1 and _NOTE.fullmatch(argument)
                else f"[… {rest} more arguments]"
            )
            break
        head = _CONTROL.sub("?", argument[:MAX_COMMAND])  # no pattern reads more than this
        shown = "***" if secret_next else redact(head)
        secret_next = _secret_option(head)
        limit = min(MAX_ARGUMENT, MAX_COMMAND - total)
        if len(shown) > limit or len(head) < len(argument):
            shown = _cut(shown, limit)
        total += len(shown)
        kept.append(shown)
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
    keeps the scan cheap, and nor do ``conda-meta``'s ``history`` and ``pinned``, which
    record requests: an install that links nothing changes nothing (CP-16).
    """
    digest = hashlib.sha256()
    for place in places.all():
        try:
            with os.scandir(place) as scan:
                entries = sorted((entry.name, entry) for entry in scan)
        except OSError:
            continue
        for name, entry in entries:
            if not name.endswith((".json", ".dist-info", ".egg-info")):
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

    Conda packages are named by channel, subdir, file name and SHA-256; a channel on
    conda.anaconda.org by its name. Distributions pip or uv installed follow, by name and
    version, with the URL and hash of a direct-URL install; those a Conda package
    installed are left out, since the Conda entry stands for them. Every value matches
    the lockfile's patterns: one that does not is left out, and a record whose name,
    version or build cannot be held counts as skipped. The worker names the package
    cache's URLs by their channels and upstream URLs.
    """
    conda, owned, skipped = _conda_packages(places.conda_meta)
    pip: list[Message] = []
    for directory in places.site:
        found, left = _distributions(directory, "prefix", owned)
        pip += found
        skipped += left
    if places.user and places.user not in places.site:
        found, left = _distributions(places.user, "user", owned)
        pip += found
        skipped += left
    conda.sort(
        key=lambda item: (item["name"], item["version"], item["build"], item["subdir"] or "")
    )
    pip.sort(key=lambda item: (item["name"].lower(), item["version"], item["location"]))
    return {
        "format": LOCKFILE_FORMAT,
        "version": LOCKFILE_VERSION,
        "python": _matching(platform.python_version(), _VERSION),
        "platform": SUBDIRS.get(platform.machine().lower()),
        "conda": conda[:MAX_PACKAGES],
        "pip": pip[:MAX_PACKAGES],
        "skipped": skipped + max(0, len(conda) - MAX_PACKAGES) + max(0, len(pip) - MAX_PACKAGES),
    }


def _conda_packages(directory: str) -> tuple[list[Message], set[str], int]:
    """The lockfile's Conda entries, the pip metadata directories their packages own, and
    how many records were skipped."""
    try:
        with os.scandir(directory) as scan:
            names = sorted(entry.name for entry in scan if entry.name.endswith(".json"))
    except OSError:
        return [], set(), 0
    packages: list[Message] = []
    owned: set[str] = set()
    skipped = 0
    for name in names[:MAX_PACKAGES]:
        data = read_regular(os.path.join(directory, name), MAX_META_BYTES)
        try:
            record: object = json.loads(data) if data is not None else None
        except (ValueError, RecursionError):
            record = None
        if not isinstance(record, dict):
            skipped += 1
            continue
        fields = cast(Message, record)
        package = conda_package(fields)
        if package is None:
            skipped += 1
        else:
            packages.append(package)
        files = fields.get("files")
        for path in cast(list[object], files) if isinstance(files, list) else []:
            if isinstance(path, str) and (entry := _site_entry(path)) is not None:
                owned.add(entry)
    return packages, owned, skipped


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
    """A ``conda-meta`` record as a lockfile entry; None if its name, version or build
    cannot be held."""
    name, version = _matching(record.get("name"), _NAME), _matching(record.get("version"), _VERSION)
    build = _matching(record.get("build"), _BUILD) or _matching(record.get("build_string"), _BUILD)
    if name is None or version is None or build is None:
        return None
    url = record.get("url")
    file = record.get("fn")
    channel: object = None
    subdir: object = None
    if isinstance(url, str) and url.count("/") >= 3:
        base, _, last = url.strip().rpartition("/")
        channel, _, subdir = base.rpartition("/")
        file = file or last
    if not channel or not subdir:
        channel, subdir = record.get("channel"), record.get("subdir")
        if isinstance(channel, str) and isinstance(subdir, str) and subdir:
            # conda writes the channel with its subdir: …/conda-forge/linux-64.
            trimmed = channel.strip().rstrip("/")
            if trimmed.endswith("/" + subdir):
                channel = trimmed[: -len(subdir) - 1]
    return {
        "name": name,
        "version": version,
        "build": build,
        "channel": canonical_channel(channel) if isinstance(channel, str) and channel else None,
        "subdir": _matching(subdir, _SUBDIR),
        "file": _matching(file, _FILE),
        "sha256": _hex(record.get("sha256"), 64),
        "md5": _hex(record.get("md5"), 32),
        "license": _matching(record.get("license"), _LICENSE),
    }


def canonical_channel(channel: str, cache: str | None = None) -> str | None:
    """A Conda channel as a lockfile names it; None if it cannot be named safely.

    A channel name stays as it is. ``https://conda.anaconda.org/<channel>`` is
    ``<channel>``, and so is ``<cache>/conda/<channel>`` when ``cache`` is the package
    cache's URL (docs/PACKAGE-CACHE.md, "URL layout"); a channel on any other host keeps
    its URL, without user information, an anaconda.org token, query or fragment, and
    with its host in ASCII (IDNA). A URL that does not parse, or has another scheme,
    gives None.
    """
    value = channel.strip().rstrip("/")
    if _CHANNEL_NAME.fullmatch(value):
        return value
    parsed = _parse(value, CHANNEL_SCHEMES)
    if parsed is None:
        return None
    scheme, netloc, path = parsed
    path = _TOKEN.sub("", path).rstrip("/")
    if scheme == "https" and netloc == UPSTREAM_CONDA and _CHANNEL_NAME.fullmatch(path[1:]):
        return path[1:]
    if cache is not None and (base := _cache_base(cache)) is not None:
        prefix = f"{base}/conda/"
        whole = f"{scheme}://{netloc}{path}"
        if whole.startswith(prefix) and _CHANNEL_NAME.fullmatch(whole[len(prefix) :]):
            return whole[len(prefix) :]
    return _url_text(urllib.parse.urlunsplit((scheme, netloc, path, "", "")))


def canonical_url(url: str, cache: str | None = None) -> tuple[str, str | None] | None:
    """A direct-URL install's source as a lockfile keeps it, with the SHA-256 its URL
    names; None if it cannot be kept safely.

    The URL loses its user information, query and fragment, and its host is in ASCII
    (IDNA). When ``cache`` is the package cache's URL, the cache's PyPI files
    (``<cache>/pypi/files/<project>/<sha256>/<path>``) become their files.pythonhosted.org
    URL, with that SHA-256.
    """
    parsed = _parse(url.strip(), SOURCE_SCHEMES)
    if parsed is None:
        return None
    scheme, netloc, path = parsed
    if cache is not None and (base := _cache_base(cache)) is not None:
        prefix = f"{base}/pypi/files/"
        whole = f"{scheme}://{netloc}{path}"
        if whole.startswith(prefix):
            project, _, rest = whole[len(prefix) :].partition("/")
            sha256, _, file = rest.partition("/")
            if project and _SHA256.fullmatch(sha256) and file:
                upstream = _url_text(UPSTREAM_FILES + file)
                return (upstream, sha256) if upstream is not None else None
    text = _url_text(urllib.parse.urlunsplit((scheme, netloc, path, "", "")))
    return None if text is None else (text, None)


def _parse(value: str, schemes: frozenset[str]) -> tuple[str, str, str] | None:
    """A URL's scheme, ``host[:port]`` without a default port, and path; None if it does
    not parse, has another scheme, or names a host that has no ASCII form."""
    if _UNSAFE.search(value):
        return None  # spaces, line breaks and other controls
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        return None
    host = parts.hostname or ""
    if scheme == "file" or scheme.endswith("+file"):
        if host not in ("", "localhost"):
            return None
        return scheme, "", parts.path
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return scheme, _netloc(canonical_host(host), port, scheme), parts.path


def canonical_host(host: str) -> str:
    """A host as URLs compare it: lower case, without a trailing dot, an IPv6 address
    without brackets, and an IPv4 address in its one spelling (``0x7f.1``,
    ``2130706433`` and ``::ffff:127.0.0.1`` are ``127.0.0.1``)."""
    host = host.lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if _LEGACY_IPV4.fullmatch(host):
            with contextlib.suppress(OSError):
                return socket.inet_ntoa(socket.inet_aton(host))
        return host
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def url_host(value: str) -> str | None:
    """The host a URL names, as canonical_host spells it; None for no URL, or no host."""
    try:
        host = urllib.parse.urlsplit(value).hostname
    except ValueError:
        return None
    return canonical_host(host) if host else None


def _cache_base(cache: str) -> str | None:
    """The package cache's URL as canonical_channel compares URLs: no default port, no
    trailing slash."""
    parsed = _parse(cache.strip(), frozenset({"http", "https"}))
    if parsed is None:
        return None
    scheme, netloc, path = parsed
    return f"{scheme}://{netloc}{path.rstrip('/')}"


def _netloc(host: str, port: int | None, scheme: str = "") -> str:
    named = f"[{host}]" if ":" in host else host
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return named
    return f"{named}:{port}"


def _url_text(value: str) -> str | None:
    return value if len(value) <= 2048 and _URL_TEXT.fullmatch(value) else None


def _distributions(directory: str, location: str, owned: set[str]) -> tuple[list[Message], int]:
    """The lockfile's pip entries for one site-packages directory, and how many were
    skipped."""
    try:
        with os.scandir(directory) as scan:
            names = sorted(
                entry.name for entry in scan if entry.name.endswith((".dist-info", ".egg-info"))
            )
    except OSError:
        return [], 0
    found: list[Message] = []
    skipped = 0
    for name in names[:MAX_PACKAGES]:
        if name in owned:
            continue
        path = os.path.join(directory, name)
        directory_entry = os.path.isdir(path) and not os.path.islink(path)
        installer = _first_line(os.path.join(path, "INSTALLER")) if directory_entry else None
        if installer is not None and installer.lower() == "conda":
            continue
        described = _metadata(path, name, directory_entry)
        if described is None:
            skipped += 1
            continue
        project, version = described
        url, sha256 = _direct_url(path) if directory_entry else (None, None)
        found.append(
            {
                "name": project,
                "version": version,
                "installer": _matching(installer, _INSTALLER),
                "location": location,
                "url": url,
                "sha256": sha256,
            }
        )
    return found, skipped


def _metadata(path: str, name: str, directory: bool) -> tuple[str, str] | None:
    """A distribution's name and version, from its metadata or else its directory's name."""
    source = path
    if directory:
        source = os.path.join(path, "METADATA" if name.endswith(".dist-info") else "PKG-INFO")
    project = version = None
    data = read_regular(source, 64 * 1024)
    for line in data.decode("utf-8", "replace").splitlines() if data is not None else []:
        if not line.strip():
            break
        key, _, value = line.partition(":")
        if key.lower() == "name" and project is None:
            project = _matching(value, _NAME)
        elif key.lower() == "version" and version is None:
            version = _matching(value, _VERSION)
    if project is None or version is None:
        stem = name.rsplit(".", 1)[0]
        guessed, _, rest = stem.partition("-")
        project = project or _matching(guessed, _NAME)
        version = version or _matching(rest.split("-py", 1)[0], _VERSION)
    return (project, version) if project and version else None


def _direct_url(path: str) -> tuple[str | None, str | None]:
    """The URL and SHA-256 of a direct-URL install (PEP 610), canonical."""
    data = read_regular(os.path.join(path, "direct_url.json"), 1024 * 1024)
    try:
        decoded: object = json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        decoded = None
    fields = cast(Message, decoded) if isinstance(decoded, dict) else {}
    raw = fields.get("url")
    kept = canonical_url(raw) if isinstance(raw, str) else None
    archive = fields.get("archive_info")
    info = cast(Message, archive) if isinstance(archive, dict) else {}
    hashes = info.get("hashes")
    sha256 = _hex(cast(Message, hashes).get("sha256"), 64) if isinstance(hashes, dict) else None
    legacy = info.get("hash")
    if sha256 is None and isinstance(legacy, str) and legacy.startswith("sha256="):
        sha256 = _hex(legacy[len("sha256=") :], 64)
    return (kept[0] if kept is not None else None), sha256


def _matching(value: object, pattern: re.Pattern[str]) -> str | None:
    """``value``, stripped, if the whole of it matches ``pattern``."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if pattern.fullmatch(value) else None


def _hex(value: object, length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if len(value) == length and all(c in "0123456789abcdef" for c in value) else None


def _first_line(path: str) -> str | None:
    data = read_regular(path, 256)
    if data is None:
        return None
    return data.decode("utf-8", "replace").strip().split("\n", 1)[0] or None


def read_regular(path: str | Path, limit: int) -> bytes | None:
    """Up to ``limit`` bytes of a regular file; None for anything else, or more.

    The file is opened without following a symlink and without waiting, so research
    code cannot stall a read with a FIFO in its place, as the executor's own reads do.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1 << 20, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


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


def holder_pid(marker: str) -> int | None:
    """The pid in a HOLDER mark (``pid:start``); None if it is not one."""
    pid, _, start = marker.partition(":")
    return int(pid) if pid.isdigit() and start.isdigit() else None


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
    each such command, ``holder.json`` names the command holding the lock, and
    ``config.json`` says where the environment records its packages. ``prefix/`` is laid
    out like the environment's prefix, with the wrappers in its ``bin``.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = directory / "lock"
        self.journal = directory / "journal"
        self.holder = directory / "holder.json"
        self.config = directory / "config.json"
        self.mirror = directory / "prefix"
        self.bin = self.mirror / "bin"
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
        data = read_regular(self.holder, 65536)
        try:
            decoded: object = json.loads(data) if data is not None else None
        except (ValueError, RecursionError):
            return None
        return cast(Message, decoded) if isinstance(decoded, dict) else None

    # The journal.

    def record(self, entry: Message) -> None:
        """Add a package command's record, named so that records sort in the order they ended."""
        name = f"{time.time_ns():020d}-{os.getpid()}.json"
        _replace(self.journal / name, json.dumps(entry, separators=(",", ":")).encode())

    def read_records(self, limit: int = MAX_RECORDS) -> tuple[list[tuple[str, Message]], bool]:
        """Up to ``limit`` records, in the order their commands ended, with their file names,
        and whether that is all of them. They stay in the journal until removed."""
        try:
            with os.scandir(self.journal) as scan:
                names = sorted(entry.name for entry in scan if entry.name.endswith(".json"))
        except OSError:
            return [], True
        records: list[tuple[str, Message]] = []
        for name in names[:limit]:
            data = read_regular(self.journal / name, MAX_RECORD_BYTES)
            try:
                decoded: object = json.loads(data) if data is not None else None
            except (ValueError, RecursionError):
                decoded = None
            # One that is not a record is still taken, so that it cannot stop the rest.
            records.append((name, cast(Message, decoded) if isinstance(decoded, dict) else {}))
        return records, len(names) <= limit

    def remove_records(self, names: Iterable[str]) -> None:
        for name in names:
            with contextlib.suppress(OSError):
                (self.journal / name).unlink()

    def clear_journal(self) -> None:
        with contextlib.suppress(OSError), os.scandir(self.journal) as scan:
            for entry in list(scan):
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)

    # Configuration.

    def read_config(self) -> Config:
        data = read_regular(self.config, 1 << 20)
        if data is None:
            raise ValueError(f"no readable {self.config}")
        decoded: object = json.loads(data)
        fields = cast(Message, decoded) if isinstance(decoded, dict) else {}
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
    _mirror(state, Path(config.prefix))
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


def _mirror(state: State, prefix: Path) -> None:
    """Lay the prefix out around the wrappers, so that what scripts look for beside a
    package manager, or beside its Python, is there: ``$(dirname "$(command -v
    conda)")/../etc/profile.d/conda.sh``, ``$(dirname "$(command -v pip)")/python`` and
    ``$(dirname "$(command -v python)")/jupyter``. Every entry of the prefix but ``bin`` is
    a link, and so is every program in its ``bin`` but the package managers, which have
    wrappers, and the Pythons, which have stand-ins that run the prefix's own. A link
    whose target is gone goes too."""
    entries: list[str] = []
    with contextlib.suppress(OSError), os.scandir(prefix) as scan:
        entries = [entry.name for entry in scan if entry.name != "bin"]
    programs: list[str] = []
    with contextlib.suppress(OSError), os.scandir(prefix / "bin") as scan:
        skipped = {*tool_names(), *PYTHONS}
        programs = [entry.name for entry in scan if entry.name not in skipped]
    for directory in (state.mirror, state.bin):
        with contextlib.suppress(OSError), os.scandir(directory) as scan:
            for entry in list(scan):
                if entry.is_symlink() and not os.path.exists(entry.path):
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)
    for name in entries:
        _link(state.mirror / name, prefix / name)
    for name in programs:
        _link(state.bin / name, prefix / "bin" / name)
    for name in PYTHONS:
        real = prefix / "bin" / name
        if real.is_file() and os.access(real, os.X_OK):
            source = STAND_IN.replace("@NAME@", name).replace("@REAL@", shlex.quote(str(real)))
            _update(state.bin / name, source.encode(), 0o755)
        else:
            with contextlib.suppress(FileNotFoundError):
                (state.bin / name).unlink()


def _link(link: Path, target: Path) -> None:
    """Make ``link`` a symlink to ``target``, unless it is one already."""
    with contextlib.suppress(OSError):
        if os.readlink(link) == str(target):
            return
    temporary = link.with_name(f".tmp-{os.getpid()}-{link.name}")
    with contextlib.suppress(OSError):
        temporary.unlink()
    try:
        os.symlink(target, temporary)
        os.replace(temporary, link)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _update(target: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file unless it already has this content: a shell reading a wrapper as it
    runs keeps the file it opened."""
    if read_regular(target, len(data)) == data:
        with contextlib.suppress(OSError):
            if (target.stat().st_mode & 0o777) == mode:
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


def _marked(marker: str) -> list[tuple[int, int | None]]:
    """The processes that carry ``marker`` as their HOLDER, with their start times: what
    a package command started, unless it cleared its environment. This one aside."""
    needle = f"\0{HOLDER}={marker}\0".encode()
    me = os.getpid()
    found: list[tuple[int, int | None]] = []
    with contextlib.suppress(OSError), os.scandir("/proc") as scan:
        for entry in scan:
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                with open(f"/proc/{entry.name}/environ", "rb") as handle:
                    data = handle.read(1 << 20)
            except OSError:
                continue
            if needle in b"\0" + data:
                found.append((int(entry.name), process_start(int(entry.name))))
    return found


def end_marked(marker: str, grace: float = 0.0) -> int:
    """End the processes that carry ``marker``: after ``grace`` seconds for them to end on
    their own, with SIGKILL, in rounds that catch those forked meanwhile. Returns how many
    were killed."""
    deadline = time.monotonic() + grace
    found = _marked(marker)
    while found and time.monotonic() < deadline:
        time.sleep(0.05)
        found = _marked(marker)
    killed: dict[int, int] = {}
    observed: set[tuple[int, int]] = set()
    for _ in range(KILL_ROUNDS):
        observed.update((pid, start) for pid, start in found if start is not None)
        if not found:
            break
        for pid, start in found:
            if start is not None and process_start(pid) == start:  # still the one found
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
                    killed[pid] = start
        found = _marked(marker)
    observed.update((pid, start) for pid, start in found if start is not None)
    # SIGKILL returns before the target has exited. Its environment can already
    # disappear while /proc/stat still reports a running task, so rescanning marks
    # alone is not an exit barrier. Retain the signalled process identities until
    # they are gone or zombies, without following a reused PID.
    deadline = time.monotonic() + KILL_WAIT
    while any(_same_running(pid, start) for pid, start in observed):
        if time.monotonic() >= deadline:
            raise CleanupPendingError("a package process has not exited; retry cleanup")
        time.sleep(0.01)
    return len(killed)


class CleanupPendingError(RuntimeError):
    """Observed package processes still run; preserve the holder for another cleanup."""


def _same_running(pid: int, start: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        fields = data[data.rfind(b")") + 2 :].split()
        return int(fields[19]) == start and fields[0] not in (b"Z", b"X")
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, IndexError, ValueError):
        return True  # inability to inspect an observed process does not confirm exit


def end_strays(state: State) -> int:
    """End processes a previous package turn left running, including after wrapper death.

    Call this holding the lock. An active turn holds it exclusively, so even a live
    holder here has relinquished its turn (possibly while reporting failed cleanup).
    Its retained marker must be cleaned before another turn can replace it.
    """
    holder = state.holding()
    if holder is None:
        return 0
    pid, start = holder.get("pid"), holder.get("start")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(start, int)
        or isinstance(start, bool)
    ):
        return 0
    ended = end_marked(f"{pid}:{start}")
    with contextlib.suppress(OSError):
        state.holder.unlink()
    return ended


def baseline(state: State, places: Places) -> str:
    """The fingerprint as an executor starts: records left from before it are dropped,
    since the worker may have seen their changes another way."""
    state.clear_journal()
    return fingerprint(places)


@dataclass(slots=True)
class Observation:
    """What changed since the executor last looked."""

    records: list[tuple[str, Message]] = field(default_factory=list[tuple[str, Message]])
    """The journal's records with their file names, in the order their commands ended."""
    fingerprint: str | None = None
    """The packages' fingerprint now; None while a package command holds the lock, or
    records remain to read."""
    lockfile: Message | None = None
    """The lockfile for ``fingerprint``, when asked for and no record accounts for it."""
    busy: Message | None = None
    """What held the lock, when a package command did."""
    complete: bool = True
    """Whether every record was read; the rest wait for another look."""


def observe(
    state: State,
    places: Places,
    known: str,
    held: int | None = None,
    *,
    lockfile_wanted: bool = False,
) -> Observation:
    """Read the journal and the packages, unless a package command is changing them.

    ``held`` is a descriptor on which the caller holds the lock shared, and which this
    closes; otherwise this tries to take it. Under the lock, what a killed package command
    left running is ended first (end_strays), and no record can appear between
    reading the journal and reading the packages, so a change no record accounts for is
    one no package command made. The packages are read only once every record has been,
    so that a change is never reported before the records that come before it. The
    records stay in the journal: the caller removes each once the worker has it.
    """
    descriptor = held if held is not None else state.try_lock(exclusive=False)
    try:
        if descriptor is not None and (ended := end_strays(state)):
            sys.stderr.write(f"executor: ended {ended} processes a killed package command left\n")
        records, complete = state.read_records()
        if descriptor is None:
            return Observation(records, busy=state.holding() or {}, complete=complete)
        if not complete:
            return Observation(records, complete=False)
        now = fingerprint(places)
        usable = [record for _, record in records if _usable(record)]
        last = str(usable[-1]["after"]) if usable else known
        made = lockfile(places) if lockfile_wanted and now != last else None
        return Observation(records, now, made)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def usable(record: Message) -> bool:
    """Whether a journal record names the fingerprints before and after its command."""
    return _usable(record)


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
        try:
            ended = end_strays(self.state)
        except CleanupPendingError:
            os.close(descriptor)
            self.descriptor = None
            raise
        if ended:
            sys.stderr.write(
                f"sandbox-science: ended {ended} processes that a killed package command "
                "left running in this environment\n"
            )
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
        return True

    def end(self, code: int | None) -> None:
        """End what the command left running, record it, then give the lock up. A record
        that cannot be written is lost, never the command's exit status: the executor then
        sees an unmanaged change. The programs the command installed get their links beside
        the wrappers."""
        cleaned = False
        try:
            if ended := end_marked(_marker(), LEFTOVER_GRACE):
                sys.stderr.write(
                    f"sandbox-science: ended {ended} processes the package command left running\n"
                )
            cleaned = True
            with contextlib.suppress(OSError):
                _mirror(self.state, Path(self.config.prefix))
            after = fingerprint(self.config.places)
            entry: Message = {
                "version": RECORD_VERSION,
                "command": self.command,
                "via": self.via,
                "owner": os.environ.get(OWNER, "")[:256],
                "pid": os.getpid(),
                "invoked": self.invoked,
                "started": self.started,
                "ended": time.time(),
                "exit_code": code if code is not None and code >= 0 else None,
                "signal": -code if code is not None and code < 0 else None,
                "before": self.before,
                "after": after,
                "lockfile": lockfile(self.config.places) if after != self.before else None,
            }
            self.state.record(entry)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"sandbox-science: the package command was not recorded: {exc}\n")
        finally:
            if cleaned:
                with contextlib.suppress(OSError):
                    self.state.holder.unlink()
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None


def _death_signal() -> Any:
    """``prctl``, to have a command killed when its wrapper dies; None without libc."""
    try:
        import ctypes

        return ctypes.CDLL(None, use_errno=True).prctl
    except (ImportError, OSError, AttributeError):
        return None


def run(path: str, argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run a command to its end, passing on the signals this process gets; return its exit
    code, negative for the signal that ended it.

    The command gets this process's standard streams and other inheritable descriptors,
    signal mask and signal dispositions, apart from those Python itself ignores. It is
    killed if this process dies first (``PR_SET_PDEATHSIG``), as it would have been
    without a wrapper, so a wrapper killed with SIGKILL leaves no command running outside
    its turn. The lock's descriptor is not passed on: a process the command left running
    in the background would hold the environment's lock for as long as it ran.

    Off a terminal, the command gets a process group of its own, so that a signal sent to
    this process's group (a kernel interrupt is ``killpg``) reaches it once, from here;
    this process then passes on every signal it gets. On a terminal, the command stays in
    the foreground group, to read from the terminal, and a signal the kernel sent to the
    group (``SI_KERNEL``), as a terminal's are, reached it already. The child is forked, as
    multiprocessing forks, rather than spawned through ``clone3``, which a sandbox's
    seccomp filter may refuse.
    """
    import subprocess

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    waited = {*FORWARDED, signal.SIGCHLD}
    original = signal.pthread_sigmask(signal.SIG_BLOCK, waited)
    prctl = _death_signal()
    parent = os.getpid()
    terminal = os.isatty(0)

    def prepare_child() -> None:  # in the child, just before it runs the command
        signal.pthread_sigmask(signal.SIG_SETMASK, original)
        if not terminal:
            with contextlib.suppress(OSError):
                os.setpgid(0, 0)
        if prctl is not None:
            prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent:  # the wrapper died before the death signal was set
            os.kill(os.getpid(), signal.SIGKILL)

    try:
        # restore_signals (the default) resets the dispositions Python ignores.
        child = subprocess.Popen(  # noqa: S603 - the command as it was invoked
            list(argv), executable=path, env=dict(env), close_fds=False, preexec_fn=prepare_child
        )
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, original)
        raise
    pid = child.pid
    own_group = False
    with contextlib.suppress(OSError):
        own_group = os.getpgid(pid) == pid
    while True:
        info = signal.sigtimedwait(waited, 1.0)
        if info is not None and info.si_signo != signal.SIGCHLD:
            if own_group or info.si_code != SI_KERNEL:
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
    if number not in (signal.SIGKILL, signal.SIGSTOP):  # these keep their default action
        with contextlib.suppress(Exception):
            signal.signal(number, signal.SIG_DFL)
    with contextlib.suppress(Exception):
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
    except CleanupPendingError as exc:
        # Python ignores ordinary sitecustomize exceptions and would run the
        # requested installer anyway. Exit from both wrapper and module paths.
        try:
            sys.stderr.write(f"sandbox-science: package cleanup is incomplete: {exc}\n")
        finally:
            exit_as(1)


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
