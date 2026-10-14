# SPDX-License-Identifier: MIT
"""Fail when an installed package's license is outside the tier 1 allowlist.

docs/LICENSING.md defines the tiers. Run inside the project environment, as CI
does: ``uv run python scripts/check_licenses.py``, which checks every installed
Python distribution.

``python scripts/check_licenses.py --npm web/workspace/package-lock.json`` checks a
web app's npm packages instead, from the lockfile ``npm ci`` installs exactly (version
3, whose ``packages`` section is the only one npm reads). Each package must declare an
allowed license, come from the npm registry with a SHA-512 integrity hash, and, unless
it is optional, need no install script, since installs run with ``--ignore-scripts``.
With ``--installed``, after ``npm ci``, each installed package's own ``package.json``
must also name the license the lockfile does: the lockfile's license fields are not
covered by the integrity hashes, the packages' are. It needs only the standard library.

One exception to "from the registry": a stub kept in the app's own ``stubs/`` directory,
which an npm override puts in place of a package the app must not install (a native
binary it never runs). Its lockfile entry is a link to that directory; the directory's
own entry must declare an allowed license and have no dependencies or install script.
Its code is in the repository, so it is reviewed like the rest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import Any, cast

ALLOWED = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "PostgreSQL",
        "Unlicense",
    }
)
_ALLOWED_FOLDED = frozenset(name.casefold() for name in ALLOWED)

# Short values of the legacy ``License`` metadata field, mapped to SPDX identifiers.
LICENSE_FIELD = {
    "MIT": "MIT",
    "MIT License": "MIT",
    "BSD": "BSD-3-Clause",
    "BSD License": "BSD-3-Clause",
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache-2.0": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "ISC": "ISC",
}

CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}


def license_of(dist: Distribution) -> str | None:
    """Return the distribution's license as an SPDX expression, if it can be determined."""
    metadata = dist.metadata
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    field = (metadata.get("License") or "").strip()
    if field in LICENSE_FIELD:
        return LICENSE_FIELD[field]
    if field in ALLOWED:
        return field  # already an SPDX identifier, as psutil writes it
    for classifier in metadata.get_all("Classifier") or []:
        if classifier in CLASSIFIERS:
            return CLASSIFIERS[classifier]
    return None


_SPDX_WORD = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+:-]+)")


class _Expression:
    """An SPDX license expression, read with its precedence: ``WITH`` binds tightest, then
    ``AND``, then ``OR``, and parentheses group. Anything else makes it unreadable."""

    def __init__(self, text: str) -> None:
        self.words: list[str] = []
        position = 0
        while position < len(text):
            match = _SPDX_WORD.match(text, position)
            if match is None:
                if text[position:].strip():
                    raise ValueError(f"unreadable license expression {text!r}")
                break
            self.words.append(match.group(1))
            position = match.end()
        self.at = 0

    def allowed(self) -> bool:
        result = self._any()
        if self.at != len(self.words):
            raise ValueError("unreadable license expression")
        return result

    def _peek(self) -> str | None:
        return self.words[self.at] if self.at < len(self.words) else None

    def _take(self) -> str:
        word = self._peek()
        if word is None:
            raise ValueError("the license expression ends too early")
        self.at += 1
        return word

    def _operator(self, name: str) -> bool:
        word = self._peek()
        if word is not None and word.upper() == name:
            self.at += 1
            return True
        return False

    def _any(self) -> bool:
        # Every alternative is read, so that a malformed one is not skipped.
        results = [self._all()]
        while self._operator("OR"):
            results.append(self._all())
        return any(results)

    def _all(self) -> bool:
        results = [self._one()]
        while self._operator("AND"):
            results.append(self._one())
        return all(results)

    def _one(self) -> bool:
        word = self._take()
        if word == "(":
            result = self._any()
            if self._take() != ")":
                raise ValueError("unbalanced parentheses")
            return result
        if word == ")" or word.upper() in ("AND", "OR", "WITH"):
            raise ValueError(f"unexpected {word!r}")
        if self._operator("WITH"):
            # An exception only adds permissions, so the license decides.
            exception = self._take()
            if exception in ("(", ")"):
                raise ValueError(f"unexpected {exception!r}")
        return word.removesuffix("+").casefold() in _ALLOWED_FOLDED


def is_allowed(expression: str) -> bool:
    """Whether an SPDX license expression lets the package be used in tier 1. An
    expression that cannot be read is not allowed."""
    try:
        return _Expression(expression).allowed()
    except ValueError:
        return False


NPM_REGISTRY = "https://registry.npmjs.org/"
STUBS = "stubs/"


def _stub(path: str, entry: dict[str, Any]) -> bool:
    """Whether a lockfile entry is the app's own stub package, under ``stubs/``."""
    parts = path.split("/")
    return (
        path.startswith(STUBS)
        and len(parts) == 2
        and parts[1] not in ("", ".", "..")
        and not any(key in entry for key in ("dependencies", "optionalDependencies"))
    )


def npm_problems(lockfile: dict[str, Any]) -> tuple[int, list[str]]:
    """The packages an npm lockfile (version 3) installs, and what is wrong with them."""
    if lockfile.get("lockfileVersion") != 3:
        return 0, ["the lockfile is not version 3; regenerate it with npm 9 or later"]
    if "dependencies" in lockfile:
        return 0, ["the lockfile has an old `dependencies` section, which npm may read instead"]
    packages = lockfile.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        return 0, ["the lockfile has no `packages` section with the app itself"]
    problems: list[str] = []
    count = 0
    for path, entry in sorted(cast(dict[str, dict[str, Any]], packages).items()):
        if not path:
            continue  # the web app itself
        count += 1
        label = _label(path, entry)
        resolved = entry.get("resolved")
        if entry.get("link"):
            # Only a link to one of the app's own stubs; the stub's entry is checked itself.
            target = packages.get(str(resolved))
            if target is None or not _stub(str(resolved), target):
                problems.append(f"{label}: comes from {resolved}, not the npm registry")
            continue
        stub = _stub(path, entry)
        if not stub and not str(resolved).startswith(NPM_REGISTRY):
            problems.append(f"{label}: comes from {resolved}, not the npm registry")
        if not stub and not str(entry.get("integrity", "")).startswith("sha512-"):
            problems.append(f"{label}: has no SHA-512 integrity hash")
        expression = entry.get("license")
        if not isinstance(expression, str) or not expression.strip():
            problems.append(f"{label}: license not declared")
        elif not is_allowed(expression):
            problems.append(f"{label}: {expression} is not allowed in tier 1")
        if entry.get("hasInstallScript") and not entry.get("optional"):
            problems.append(f"{label}: needs an install script, which --ignore-scripts skips")
    return count, problems


def installed_problems(lockfile: dict[str, Any], root: Path) -> tuple[int, list[str]]:
    """Packages installed under ``root`` whose own name, version or license differs from
    the lockfile's. Optional packages for other platforms are not installed."""
    packages = cast(dict[str, dict[str, Any]], lockfile.get("packages") or {})
    problems: list[str] = []
    count = 0
    for path, entry in sorted(packages.items()):
        if entry.get("link"):
            # npm links the stub's directory in; the stub's own entry is checked below.
            if path and (root / path).resolve() != (root / str(entry.get("resolved"))).resolve():
                problems.append(f"{_label(path, entry)}: is not linked to {entry.get('resolved')}")
            continue
        manifest = root / path / "package.json"
        if not path or not manifest.is_file():
            if path and not entry.get("optional"):
                problems.append(f"{_label(path, entry)}: is not installed; run npm ci first")
            continue
        count += 1
        found = json.loads(manifest.read_text(encoding="utf-8"))
        declared = found.get("license")
        if isinstance(declared, dict):  # an old form: {"type": "MIT", "url": ...}
            declared = cast(dict[str, Any], declared).get("type")
        for name, value in (("license", declared), ("version", found.get("version"))):
            if value != entry.get(name):
                problems.append(
                    f"{_label(path, entry)}: its package.json gives {name} {value!r}, the "
                    f"lockfile {entry.get(name)!r}"
                )
    return count, problems


def _label(path: str, entry: dict[str, Any]) -> str:
    return f"{path.rpartition('node_modules/')[2]}@{entry.get('version')}"


def python_problems() -> tuple[int, list[str]]:
    """The installed Python distributions, and what is wrong with their licenses."""
    problems: list[str] = []
    found = sorted(distributions(), key=lambda dist: dist.metadata["Name"].lower())
    for dist in found:
        name = dist.metadata["Name"]
        expression = license_of(dist)
        if expression is None:
            problems.append(f"{name}: license not declared in metadata")
        elif not is_allowed(expression):
            problems.append(f"{name}: {expression} is not allowed in tier 1")
    return len(found), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check licenses against the tier 1 allowlist.")
    parser.add_argument(
        "--npm", type=Path, metavar="LOCKFILE", help="check an npm lockfile's packages instead"
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help="with --npm: also check the installed packages' own package.json files",
    )
    args = parser.parse_args(argv)
    if args.npm is None:
        if args.installed:
            parser.error("--installed needs --npm")
        count, problems = python_problems()
        checked = f"all {count} installed distributions have"
    else:
        lockfile = json.loads(args.npm.read_text(encoding="utf-8"))
        count, problems = npm_problems(lockfile)
        checked = f"all {count} packages in {args.npm} come from the registry and have"
        if args.installed and not problems:
            installed, mismatches = installed_problems(lockfile, args.npm.parent)
            problems += mismatches
            checked = f"all {count} packages in {args.npm} ({installed} installed) come from "
            checked += "the registry and have"
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"{checked} tier 1 licenses")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
