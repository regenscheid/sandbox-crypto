# SPDX-License-Identifier: MIT
"""Register a Cloudflare Access service token as an OpenShell provider, from the owner's
file (docs/MCP-CONNECTORS.md, "What the owner does"; the owner, 2026-10-15).

    python -m sandbox_science.launcher.access_provider ENV_FILE --profile PROFILE \\
        [--provider crypto-research-mcp] [--openshell openshell]

It is the owner-approved alternative to typing the token in, so that a gateway made
again, as each lab's is, gets the provider without the owner. ``sandbox-science up``
runs it in a process of its own when ``[mcp_proxy] provider_env_file`` is set and the
provider is missing, or with ``--refresh-provider``; the launcher never opens the file.
It:

1. refuses ENV_FILE unless it is a regular file the user owns, which its group and
   others may neither read nor write;
2. reads the two values from it;
3. imports the provider profile PROFILE, after ``lint``, if the gateway has no
   ``cloudflare-access`` profile yet;
4. creates the provider, or updates it with ``--wait`` if it exists.

The values are secrets. They reach one child only, ``openshell provider create`` or
``update``, through that child's environment: ``--credential KEY`` without ``=VALUE``
reads the variable ``KEY``. They are never in an argument, in this process's own
environment, in an error or in anything it prints. Nothing that child says is shown,
since it could echo them; the other OpenShell commands never get the values, and their
errors are shown.

The file is written by hand: ``KEY=VALUE`` lines, blank lines and ``#`` comments. A key
is header-style (``CF-Access-Client-Id``), with an optional ``:`` before the ``=``, or
env-style (``CF_ACCESS_CLIENT_ID``), in any case, with ``-`` and ``_`` alike, and may
follow ``export``. A value may have spaces around it and single or double quotes, and a
``#`` comment after it. Other keys are ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

PROVIDER: Final = "crypto-research-mcp"
TYPE: Final = "cloudflare-access"
"""The provider's type: its profile's ``id``."""
KEYS: Final = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
MAX_BYTES: Final = 64 * 1024
TIMEOUT: Final = 120.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COMMENT = re.compile(r"\s+#")


class RegistrationError(Exception):
    """Why the provider was not registered. Its message never holds a value."""


def read(path: Path) -> str:
    """The file's text, once it is shown to be the user's own and private."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError as exc:
        raise RegistrationError(f"cannot open {path}: {exc.strerror}") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RegistrationError(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise RegistrationError(f"{path} is not this user's: it must be")
        if info.st_mode & 0o077:
            mode = stat.S_IMODE(info.st_mode)
            raise RegistrationError(
                f"{path} has mode {mode:o}: its group or others may use it; chmod 600 it"
            )
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
    key = raw.strip()
    if key.startswith("export") and key[6:7].isspace():
        key = key[6:].strip()
    return key.removesuffix(":").strip().upper().replace("-", "_")


def _value(raw: str) -> str | None:
    """The value, unquoted; None if its quotes are wrong."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        after = value[end + 1 :].strip() if end > 0 else "unclosed"
        if after and not after.startswith("#"):
            return None
        return value[1:end].strip()
    return _COMMENT.split(value, maxsplit=1)[0].strip()


def parse(text: str) -> dict[str, str]:
    """The two values, by their environment variables."""
    found: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
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
        if _CONTROL.search(value):
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
            return subprocess.run(  # noqa: S603  the configured OpenShell CLI
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


def register(env_file: Path, profile: Path, *, provider: str, cli: str) -> str:
    """Register ``provider`` from ``env_file``; what was done."""
    values = parse(read(env_file))
    openshell = OpenShell(cli)
    if not _has_profile(openshell.json("provider", "profile", "list"), TYPE):
        openshell.checked("provider", "profile", "lint", "-f", str(profile))
        done = openshell.run("provider", "profile", "import", "-f", str(profile))
        # A profile the listing did not show may still be there.
        if done.returncode != 0 and "exist" not in done.stderr.lower():
            said = done.stderr.strip()[-500:]
            raise RegistrationError(f"openshell provider profile import failed: {said}")
    credentials = [option for key in KEYS for option in ("--credential", key)]
    if provider in provider_names(openshell.json("provider", "list")):
        openshell.registered(["provider", "update", provider, *credentials, "--wait"], values)
        return f"updated the OpenShell provider {provider} from {env_file}"
    openshell.registered(
        ["provider", "create", "--name", provider, "--type", TYPE, *credentials], values
    )
    return f"created the OpenShell provider {provider} from {env_file}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sandbox_science.launcher.access_provider",
        description="Register a Cloudflare Access service token as an OpenShell provider.",
    )
    parser.add_argument("env_file", type=Path, help="the owner's file with the token")
    parser.add_argument("--profile", type=Path, required=True, help="cloudflare-access.yaml")
    parser.add_argument("--provider", default=PROVIDER)
    parser.add_argument("--openshell", default="openshell", help="the OpenShell CLI")
    args = parser.parse_args(argv)
    try:
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
