# SPDX-License-Identifier: MIT
"""Register a Cloudflare Access service token as an OpenShell provider, from the owner's
file (docs/MCP-CONNECTORS.md, "What the owner does"; the owner, 2026-10-15).

    python -I -m sandbox_science.launcher.access_provider ENV_FILE --profile PROFILE \\
        --openshell /absolute/path/to/openshell [--provider crypto-research-mcp]

It is the owner-approved alternative to typing the token in, so that a gateway made
again, as each lab's is, gets the provider without the owner. With ``--from-environment``
instead of ENV_FILE (and ``--profile`` optional), it reads the two values from its own
environment, where the Kubernetes bundle's runtime manager Pod puts them from the Secret
its ``[mcp_direct] provider_secret`` names, in an init container of its own
(docs/KUBERNETES-PREP.md); the rest is the same. ``sandbox-science up``
runs it in a process of its own when ``[mcp_direct] provider_env_file`` is set and the
provider is missing, or with ``--refresh-provider``; the launcher never opens the file.
It:

1. refuses to run with a variable that the tests' stand-ins record calls with
   (``FAKE_*``), or one that loads code into a program (``LD_*``);
2. refuses an OpenShell CLI that is not an absolute path to a regular file the user or
   root owns, which its group and others may not write, outside every git checkout
   but in an installed Homebrew package (Homebrew's prefix is its own checkout)
   (this repository and its worktrees among them);
3. refuses ENV_FILE unless it is a regular file, not a symbolic link, with one link,
   that the user owns and its group and others may neither read nor write, in a
   directory the user owns that its group and others may not write;
4. reads the two values from it;
5. imports the provider profile PROFILE, after ``lint``, if the gateway has no
   ``cloudflare-access`` profile yet;
6. creates the provider, or updates it with ``--wait`` if it exists.

The values are secrets. They reach one child only, ``openshell provider create`` or
``update``, through that child's environment (``--credential KEY`` without ``=VALUE``
reads the variable ``KEY``; OpenShell 0.1.5 takes a credential no other way but on its
command line), and from it OpenShell's own encrypted store. While that child runs, the
user's other processes can read its environment in ``/proc/<pid>/environ``. The values
are never in an argument, in this process's own environment (but as Kubernetes gives
them with ``--from-environment``), in an error or in anything it prints. Nothing that
child says is shown, since it could echo them; the other OpenShell commands never get the
values, and their errors are shown. Neither this
process nor its children write a core file, and on Linux this process cannot be traced
or read through ``/proc`` by the user's other processes.

The file is written by hand: ``KEY=VALUE`` lines, blank lines and ``#`` comments. A key
is header-style (``CF-Access-Client-Id``), with an optional ``:`` before the ``=``, or
env-style (``CF_ACCESS_CLIENT_ID``), in any case, with ``-`` and ``_`` alike, and may
follow ``export``. A value may have spaces around it and single or double quotes, and a
``#`` comment after it. Other keys are ignored.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import resource
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

PROVIDER: Final = "crypto-research-mcp"
TYPE: Final = "cloudflare-access"
"""The provider's type: its profile's ``id``."""
KEYS: Final = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
MAX_BYTES: Final = 64 * 1024
TIMEOUT: Final = 120.0
REFUSED_VARIABLES: Final = ("FAKE_", "LD_")
"""Variables the helper will not run with: the tests' stand-ins record calls through
``FAKE_*``, and ``LD_*`` loads code into the child that gets the values."""
PR_SET_DUMPABLE: Final = 4
_COMMENT = re.compile(r"[ \t]+#")
BLANK: Final = " \t"
"""The spaces a line or value may have around it; any other is a control character."""


class RegistrationError(Exception):
    """Why the provider was not registered. Its message never holds a value."""


def harden() -> None:
    """No core file from this process or its children; on Linux, no tracing of this
    process and no reading it through ``/proc`` by the user's other processes."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform == "linux":
        prctl = cast(Callable[..., int], ctypes.CDLL(None, use_errno=True).prctl)
        if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise RegistrationError("cannot make this process undumpable")


def check_environment(environment: Mapping[str, str]) -> None:
    for name in sorted(environment):
        if name.startswith(REFUSED_VARIABLES):
            raise RegistrationError(f"refusing to run with {name} set")


def _is_repository(folder: Path) -> bool:
    """Whether ``folder/.git`` is a repository's own: a ``.git`` directory that holds
    ``HEAD``, or a worktree's or submodule's ``.git`` file, which begins ``gitdir:``.

    An empty ``.git`` directory (babbage's home has one) is not a checkout.
    """
    git = folder / ".git"
    try:
        if git.is_dir():
            return (git / "HEAD").is_file()
        if git.is_file():
            with git.open("rb") as handle:
                return handle.read(len(b"gitdir:")) == b"gitdir:"
    except OSError:
        return False
    return False


def _in_checkout(path: Path) -> Path | None:
    """The git checkout that holds ``path``, if one does.

    Homebrew's prefix on Apple Silicon, ``/opt/homebrew``, is Homebrew's own git
    checkout, and its installed packages are in its ``Cellar``, which that checkout
    ignores: a program there is installed, not checked out.
    """
    for folder in path.parents:
        if _is_repository(folder):
            homebrew = (folder / "Library/Homebrew/brew.sh").is_file()
            return None if homebrew and path.is_relative_to(folder / "Cellar") else folder
    return None


def check_cli(cli: str) -> str:
    """The OpenShell CLI's real path, once it is shown to be the user's or root's own."""
    if not os.path.isabs(cli):
        raise RegistrationError(f"the OpenShell CLI {cli} is not an absolute path")
    real = Path(os.path.realpath(cli))
    if (checkout := _in_checkout(real)) is not None:
        raise RegistrationError(f"the OpenShell CLI {real} is in the git checkout {checkout}")
    try:
        info = real.stat()
    except OSError as exc:
        raise RegistrationError(f"cannot use the OpenShell CLI {cli}: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise RegistrationError(f"the OpenShell CLI {real} is not a regular file")
    if info.st_uid not in (os.getuid(), 0):
        raise RegistrationError(f"the OpenShell CLI {real} is neither this user's nor root's")
    if info.st_mode & 0o022:
        raise RegistrationError(f"the OpenShell CLI {real} may be changed by others")
    return str(real)


def _private(info: os.stat_result, what: str, *, others: int) -> None:
    if info.st_uid != os.getuid():
        raise RegistrationError(f"{what} is not this user's: it must be")
    if info.st_mode & others:
        mode = stat.S_IMODE(info.st_mode)
        raise RegistrationError(f"{what} has mode {mode:o}: its group or others may use it")


def _unopened(path: Path, error: OSError) -> RegistrationError:
    if os.path.islink(path):
        return RegistrationError(f"{path} is a symbolic link: name what it links to")
    return RegistrationError(f"cannot open {path}: {error.strerror}")


def read(path: Path) -> str:
    """The file's text, once it is shown to be the user's own and private."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        folder = os.open(path.parent, flags | os.O_DIRECTORY)
    except OSError as exc:
        raise _unopened(path.parent, exc) from None
    try:
        _private(os.fstat(folder), str(path.parent), others=0o022)
        try:
            descriptor = os.open(path.name, flags | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=folder)
        except OSError as exc:
            raise _unopened(path, exc) from None
    finally:
        os.close(folder)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RegistrationError(f"{path} is not a regular file")
        if info.st_nlink != 1:
            raise RegistrationError(f"{path} has {info.st_nlink} links: it must have one")
        _private(info, str(path), others=0o077)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, 8192):
            size += len(chunk)
            if size > MAX_BYTES:
                raise RegistrationError(f"{path} is over {MAX_BYTES} bytes")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise RegistrationError(f"{path} is not UTF-8 text") from None


def _key(raw: str) -> str:
    key = raw.strip(BLANK)
    if key.startswith("export") and key[6:7] in (" ", "\t"):
        key = key[6:].strip(BLANK)
    return key.removesuffix(":").strip(BLANK).upper().replace("-", "_")


def _value(raw: str) -> str | None:
    """The value, unquoted; None if its quotes are wrong."""
    value = raw.strip(BLANK)
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        after = value[end + 1 :].strip(BLANK) if end > 0 else "unclosed"
        if after and not after.startswith("#"):
            return None
        return value[1:end].strip(BLANK)
    return _COMMENT.split(value, maxsplit=1)[0].strip(BLANK)


def parse(text: str) -> dict[str, str]:
    """The two values, by their environment variables. Lines end at ``\\n`` alone, so
    that every other line or page break is a character the values are checked for."""
    found: dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.removesuffix("\r").strip(BLANK)
        if not stripped or stripped.startswith("#"):
            continue
        key, equals, rest = stripped.partition("=")
        if not equals:
            raise RegistrationError(f"line {number} is not KEY=VALUE")
        name = _key(key)
        if name not in KEYS:
            continue
        if name in found:
            raise RegistrationError(f"{name} is set twice")
        value = _value(rest)
        if value is None:
            raise RegistrationError(f"line {number}, {name}: its quotes do not match")
        if not value.isprintable():
            raise RegistrationError(f"line {number}, {name}: its value has a control character")
        found[name] = value
    missing = [name for name in KEYS if not found.get(name)]
    if missing:
        raise RegistrationError("no " + " and no ".join(missing))
    return found


def provider_names(listing: object) -> set[str]:
    """The providers in ``openshell provider list -o json``."""
    found = cast(dict[str, object], listing) if isinstance(listing, dict) else {}
    providers = found.get("providers")
    return {
        str(cast(dict[str, object], item).get("name"))
        for item in (cast(list[object], providers) if isinstance(providers, list) else [])
        if isinstance(item, dict)
    }


def _has_profile(listing: object, profile: str) -> bool:
    """Whether ``openshell provider profile list -o json`` names ``profile`` anywhere."""
    if isinstance(listing, dict):
        found = cast(dict[str, object], listing)
        if profile in (found.get("id"), found.get("name")):
            return True
        return any(_has_profile(item, profile) for item in found.values())
    if isinstance(listing, list):
        return any(_has_profile(item, profile) for item in cast(list[object], listing))
    return False


class OpenShell:
    def __init__(self, cli: str) -> None:
        self.cli = cli
        # No child but the one that registers the values ever has them.
        self.env = {key: value for key, value in os.environ.items() if key not in KEYS}

    def _call(
        self, args: Sequence[str], env: Mapping[str, str], *, shown: bool
    ) -> subprocess.CompletedProcess[str]:
        """Run OpenShell; what it says is kept only if ``shown``."""
        kept = subprocess.PIPE if shown else subprocess.DEVNULL
        what = "openshell " + " ".join(args[:3])
        try:
            return subprocess.run(  # noqa: S603  the checked OpenShell CLI
                [self.cli, *args], env=dict(env), stdin=subprocess.DEVNULL, stdout=kept,
                stderr=kept, text=True, timeout=TIMEOUT, check=False,
            )  # fmt: skip
        except OSError as exc:
            raise RegistrationError(f"cannot run {self.cli}: {exc.strerror}") from None
        except subprocess.TimeoutExpired:
            raise RegistrationError(f"{what} took over {TIMEOUT:g} s") from None

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a command that gets no values: what it says may be shown."""
        return self._call(args, self.env, shown=True)

    def checked(self, *args: str) -> str:
        done = self.run(*args)
        if done.returncode != 0:
            said = done.stderr.strip()[-500:]
            raise RegistrationError(f"openshell {' '.join(args[:3])} failed: {said}")
        return done.stdout

    def json(self, *args: str) -> object:
        try:
            return json.loads(self.checked(*args, "-o", "json"))
        except ValueError:
            raise RegistrationError(f"openshell {' '.join(args)} did not answer JSON") from None

    def registered(self, args: Sequence[str], values: Mapping[str, str]) -> None:
        """Run the one command that gets the values, in its environment alone. Nothing
        it says is kept or shown."""
        done = self._call(args, {**self.env, **values}, shown=False)
        if done.returncode != 0:
            raise RegistrationError(
                f"openshell provider {args[1]} failed, with status {done.returncode}; what it "
                "said is not shown, since it could hold the values"
            )


def from_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The two values from the process's environment, where the bundle puts a Kubernetes
    Secret's two keys (made with ``kubectl create secret generic … --from-env-file``, each
    given by ``secretKeyRef``), checked as a file's are."""
    found: dict[str, str] = {}
    for name in KEYS:
        value = environment.get(name, "").strip(BLANK)
        if value and not value.isprintable():
            raise RegistrationError(f"{name}: its value has a control character")
        if value:
            found[name] = value
    missing = [name for name in KEYS if name not in found]
    if missing:
        raise RegistrationError("no " + " and no ".join(missing))
    return found


def register(env_file: Path | None, profile: Path | None, *, provider: str, cli: str) -> str:
    """Register ``provider`` from ``env_file``, or with none from the process's environment
    (``from_environment``); what was done. Without ``profile``, the gateway must have the
    provider's profile already."""
    check_environment(os.environ)
    openshell = OpenShell(check_cli(cli))
    values = parse(read(env_file)) if env_file is not None else from_environment(os.environ)
    source = str(env_file) if env_file is not None else "the environment"
    if not _has_profile(openshell.json("provider", "profile", "list"), TYPE):
        if profile is None:
            raise RegistrationError(
                f"the gateway has no {TYPE} provider profile: import it once with "
                "openshell provider profile import -f "
                "examples/openshell/profiles/cloudflare-access.yaml"
            )
        openshell.checked("provider", "profile", "lint", "-f", str(profile))
        done = openshell.run("provider", "profile", "import", "-f", str(profile))
        # A profile the listing did not show may still be there.
        if done.returncode != 0 and "exist" not in done.stderr.lower():
            said = done.stderr.strip()[-500:]
            raise RegistrationError(f"openshell provider profile import failed: {said}")
    credentials = [option for key in KEYS for option in ("--credential", key)]
    if provider in provider_names(openshell.json("provider", "list")):
        openshell.registered(["provider", "update", provider, *credentials, "--wait"], values)
        return f"updated the OpenShell provider {provider} from {source}"
    openshell.registered(
        ["provider", "create", "--name", provider, "--type", TYPE, *credentials], values
    )
    return f"created the OpenShell provider {provider} from {source}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -I -m sandbox_science.launcher.access_provider",
        description="Register a Cloudflare Access service token as an OpenShell provider.",
    )
    parser.add_argument("env_file", type=Path, nargs="?", help="the owner's file with the token")
    parser.add_argument(
        "--from-environment",
        action="store_true",
        help="read CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET from the environment "
        "instead of a file (a Kubernetes Secret's two keys)",
    )
    parser.add_argument("--profile", type=Path, help="cloudflare-access.yaml; needed with a file")
    parser.add_argument("--openshell", required=True, help="the OpenShell CLI's absolute path")
    parser.add_argument("--provider", default=PROVIDER)
    args = parser.parse_args(argv)
    if (args.env_file is None) != args.from_environment:
        parser.error("give the owner's file or --from-environment, not both or neither")
    if args.env_file is not None and args.profile is None:
        parser.error("--profile is needed with the owner's file")
    try:
        harden()
        said = register(args.env_file, args.profile, provider=args.provider, cli=args.openshell)
    except RegistrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__} while registering the provider", file=sys.stderr)
        return 1
    print(said)
    return 0


if __name__ == "__main__":
    sys.exit(main())
