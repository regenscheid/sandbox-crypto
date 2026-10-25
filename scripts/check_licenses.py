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

``--image trusted-app`` (or ``trusted-worker``) compares ``images/<profile>/licenses.txt``
with the Python distributions the image installs, without building it. The build
(``images/common/image_report.py``) joins that file into the image's license label and fails
when it differs from the licenses of the image's Conda and pip packages. This check
reproduces the pip part: it exports the locked requirements as ``images/build_trusted.sh``
does (``uv export --locked --no-dev --no-emit-workspace``, the profile's packages from
``images/revision.sh``, ``--extra s3``), reads each wheel's license as the build does
(``license_of``, then the build's ``normalize``) and compares the set with the file. It
reads a distribution from the synced environment when its version is the locked one and
otherwise downloads the locked wheel (hash-checked) into a temporary directory. It fails
for a license the wheels carry that the file lacks, and warns about a file entry no wheel
carries. What it cannot compute it says so: the Conda bootstrap's licenses (the image's
``conda-meta``), the Ubuntu packages (always ``LicenseRef-Ubuntu-archive``), and Node and
the OpenShell CLI (in the image record, not in the label's set).

One exception to "from the registry": a stub kept in the app's own ``stubs/`` directory,
which an npm override puts in place of a package the app must not install (a native
binary it never runs). The app's ``package.json`` must name the package in
``overrides`` and depend on it as ``file:stubs/<name>``; its lockfile entry is exactly a
link to that directory, whose own entry gives only a name, a version and an allowed
license. With ``--installed``, the directory, reached through no symbolic link, holds
nothing but a ``package.json`` with those fields and a description: no dependencies,
scripts, executables or entry points.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
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
_STUB_PATH = re.compile(r"stubs/[a-z0-9][a-z0-9._-]*")
_STUB_ENTRY = frozenset({"name", "version", "license"})
_STUB_MANIFEST = frozenset({"name", "version", "license", "description", "private"})


def _stubs(packages: dict[str, dict[str, Any]], app: dict[str, Any] | None) -> dict[str, str]:
    """The stubs the app overrides packages with: each link's path to its stub's path.
    A link qualifies only when the app's ``package.json`` (``app``) overrides that package
    and depends on it as the stub, and both lockfile entries have exactly a stub's fields."""
    found: dict[str, str] = {}
    if app is None:
        return found
    overrides = app.get("overrides")
    dependencies = app.get("dependencies")
    if not isinstance(overrides, dict) or not isinstance(dependencies, dict):
        return found
    for path, entry in packages.items():
        name = path.removeprefix("node_modules/")
        stub = entry.get("resolved")
        if (
            not path.startswith("node_modules/")
            or "node_modules/" in name
            or set(entry) != {"resolved", "link"}
            or entry.get("link") is not True
            or not isinstance(stub, str)
            or not _STUB_PATH.fullmatch(stub)
        ):
            continue
        target = packages.get(stub)
        if (
            target is None
            or not set(target) <= _STUB_ENTRY
            or target.get("name") != name
            or dependencies.get(name) != f"file:{stub}"
            or overrides.get(name) not in (f"${name}", f"file:{stub}")
        ):
            continue
        found[path] = stub
    return found


def npm_problems(
    lockfile: dict[str, Any], app: dict[str, Any] | None = None
) -> tuple[int, list[str]]:
    """The packages an npm lockfile (version 3) installs, and what is wrong with them.
    ``app`` is the app's own ``package.json``; without it, no stub is admitted."""
    if lockfile.get("lockfileVersion") != 3:
        return 0, ["the lockfile is not version 3; regenerate it with npm 9 or later"]
    if "dependencies" in lockfile:
        return 0, ["the lockfile has an old `dependencies` section, which npm may read instead"]
    packages = lockfile.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        return 0, ["the lockfile has no `packages` section with the app itself"]
    entries = cast(dict[str, dict[str, Any]], packages)
    links = _stubs(entries, app)
    stubs = set(links.values())
    problems: list[str] = []
    count = 0
    for path, entry in sorted(entries.items()):
        if not path:
            continue  # the web app itself
        count += 1
        label = _label(path, entry)
        resolved = entry.get("resolved")
        if path in links:
            continue  # a link to one of the app's stubs, whose entry is checked itself
        stub = path in stubs
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
        if _STUB_PATH.fullmatch(path):
            problems += _stub_problems(root, path, entry)
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


def _stub_problems(root: Path, path: str, entry: dict[str, Any]) -> list[str]:
    """What is wrong with an installed stub: it must be a real directory in the app, with
    nothing but a ``package.json`` that names the package and declares nothing to run."""
    label = _label(path, entry)
    directory = root / path
    if any(part.is_symlink() for part in (root / "stubs", directory, directory / "package.json")):
        return [f"{label}: is reached through a symbolic link"]
    if not directory.is_dir() or directory.resolve() != root.resolve() / path:
        return [f"{label}: is not a directory in the app"]
    contents = sorted(child.name for child in directory.iterdir())
    if contents != ["package.json"]:
        return [f"{label}: holds {contents}, not only a package.json"]
    found = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    if not isinstance(found, dict):
        return [f"{label}: its package.json is not an object"]
    fields = cast(dict[str, Any], found)
    problems: list[str] = []
    extra = sorted(set(fields) - _STUB_MANIFEST)
    if extra:
        problems.append(f"{label}: its package.json declares {extra}, which a stub may not")
    if fields.get("name") != entry.get("name"):
        problems.append(f"{label}: its package.json names {fields.get('name')!r}")
    return problems


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


IMAGE_PROFILES = ("trusted-app", "trusted-worker")
UBUNTU = "LicenseRef-Ubuntu-archive"
"""The label always carries this one, for the Ubuntu packages (image_report.UBUNTU)."""


class ImageLicenseError(Exception):
    """The locked dependencies of an image cannot be read."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _image_report(root: Path) -> Any:
    """The build's own ``image_report.py``, so the file's format and the license
    normalization are the build's, not a copy of them."""
    path = root / "images" / "common" / "image_report.py"
    spec = importlib.util.spec_from_file_location("image_report", path)
    if spec is None or spec.loader is None:
        raise ImageLicenseError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_workspace_packages(root: Path, profile: str) -> list[str]:
    """The repository's packages the profile installs, from ``images/revision.sh``'s
    ``recipe_packages``, which the build reads too."""
    text = (root / "images" / "revision.sh").read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(profile)}\) packages=\(([^)]*)\)", text, re.MULTILINE)
    if match is None:
        raise ImageLicenseError(f"images/revision.sh names no packages for {profile}")
    return match.group(1).split()


def image_bootstrap_names(root: Path) -> frozenset[str]:
    """Names of the Conda packages the bootstrap installs (``orchestrator/conda-lock.yml``).
    A Python distribution of the same name may have its license from the Conda record."""
    text = (root / "images" / "orchestrator" / "conda-lock.yml").read_text(encoding="utf-8")
    return frozenset(
        _canonical(name) for name in re.findall(r"^- name: (\S+)$", text, re.MULTILINE)
    )


def image_python_environment(root: Path) -> dict[str, str]:
    """Environment markers of the image's interpreter (Python from the bootstrap's
    ``environment.yml``, on Linux), for the markers ``uv export`` leaves in the requirements."""
    text = (root / "images" / "orchestrator" / "environment.yml").read_text(encoding="utf-8")
    match = re.search(r"^\s*- python (\d+\.\d+)\.\*", text, re.MULTILINE)
    if match is None:
        raise ImageLicenseError("images/orchestrator/environment.yml pins no Python")
    return {
        "python_version": match.group(1),
        "python_full_version": f"{match.group(1)}.0",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }


def image_requirements(root: Path, profile: str) -> str:
    """The hashed requirements ``images/build_trusted.sh`` exports from ``uv.lock``."""
    uv = shutil.which("uv")
    if uv is None:
        raise ImageLicenseError("uv is not on PATH")
    command = [uv, "--directory", str(root), "export", "--locked", "--no-dev"]
    command += ["--no-emit-workspace", "--no-header", "--no-annotate"]
    for package in image_workspace_packages(root, profile):
        command += ["--package", f"sandbox-science-{package}"]
    command += ["--extra", "s3"]
    done = subprocess.run(  # noqa: S603 -- uv from PATH, arguments from the repository
        command, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise ImageLicenseError(f"uv export failed:\n{done.stderr.strip()}")
    return done.stdout


def parse_requirements(text: str, environment: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Canonical name -> (version, the requirement's lines, hashes included) for each
    requirement that applies in the image. A requirement is ``name==version``, an optional
    ``; marker``, and its hash lines, which continue it with a trailing backslash."""
    found: dict[str, tuple[str, str]] = {}
    block: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.strip():
            block.append(line)
            if line.rstrip().endswith("\\"):
                continue
        if not block:
            continue
        lines, block = block, []
        head = lines[0].rstrip().removesuffix("\\").strip()
        requirement, _, marker = head.partition(";")
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==(\S+)", requirement.strip())
        if match is None:
            raise ImageLicenseError(f"unexpected line in the exported requirements: {head!r}")
        if marker.strip() and not _marker_applies(marker.strip(), environment):
            continue
        found[_canonical(match.group(1))] = (match.group(2), "\n".join(lines) + "\n")
    return found


def _marker_applies(marker: str, environment: dict[str, str]) -> bool:
    try:
        from packaging.markers import Marker
    except ImportError as error:
        raise ImageLicenseError(
            "the requirements carry environment markers, which need the `packaging` module"
        ) from error
    # Both architectures the image is published for count; neither changes a license.
    return any(
        Marker(marker).evaluate({**environment, "platform_machine": machine})
        for machine in ("x86_64", "aarch64")
    )


def _locked_licenses(
    requirements: dict[str, tuple[str, str]], workspace: list[str]
) -> dict[str, str | None]:
    """Distribution name -> license (``license_of``) of every distribution the image installs
    with pip: the locked wheels and the repository's own packages. A wheel is read from the
    synced environment when that has the locked version, else fetched by hash into a
    temporary directory."""
    licenses: dict[str, str | None] = {}
    missing: dict[str, tuple[str, str]] = {}
    for canonical, (version, block) in requirements.items():
        try:
            dist = metadata.distribution(canonical)
        except metadata.PackageNotFoundError:
            missing[canonical] = (version, block)
            continue
        if dist.version != version:
            missing[canonical] = (version, block)
            continue
        licenses[dist.metadata["Name"]] = license_of(dist)
    if missing:
        uv = shutil.which("uv")
        if uv is None:
            raise ImageLicenseError("uv is not on PATH")
        with tempfile.TemporaryDirectory(prefix="image-licenses-") as directory:
            target = Path(directory) / "site"
            file = Path(directory) / "requirements.txt"
            file.write_text("".join(block for _, block in missing.values()), encoding="utf-8")
            install = [uv, "pip", "install", "--python", sys.executable, "--no-deps", "--no-cache"]
            install += ["--only-binary", ":all:", "--require-hashes", "--target", str(target)]
            install += ["-r", str(file)]
            done = subprocess.run(  # noqa: S603 -- uv from PATH, hash-pinned locked wheels
                install,
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode != 0:
                raise ImageLicenseError(
                    f"cannot fetch the locked wheels {', '.join(sorted(missing))} (they are not "
                    f"installed here at the locked version; try `uv sync --locked --all-extras`):\n"
                    f"{done.stderr.strip()}"
                )
            for dist in distributions(path=[str(target)]):
                licenses[dist.metadata["Name"]] = license_of(dist)
    for package in workspace:
        name = f"sandbox-science-{package}"
        try:
            licenses[name] = license_of(metadata.distribution(name))
        except metadata.PackageNotFoundError as error:
            raise ImageLicenseError(f"{name} is not installed; run `uv sync --locked`") from error
    return licenses


def compare_image_licenses(
    carriers: dict[str, list[str]], bootstrap: frozenset[str], entries: list[str]
) -> tuple[list[str], list[str]]:
    """What is wrong with a licenses.txt (its ``entries``) given ``carriers``, the license
    each pip-installed distribution would give the label (license -> distribution names):
    problems, and warnings.

    A license the distributions carry that the file lacks is a problem, since the build
    would fail. When only distributions that the Conda bootstrap also installs carry it, it
    is a warning: the image may take their license from the Conda record instead. An entry
    that no distribution carries is a warning, since the Conda packages or the Ubuntu
    packages may give it, which this check cannot see."""
    problems: list[str] = []
    warnings: list[str] = []
    present = set(entries)
    for license_, names in sorted(carriers.items()):
        if license_ in present:
            continue
        who = ", ".join(sorted(names))
        if all(_canonical(name) in bootstrap for name in names):
            warnings.append(
                f"{license_!r} is carried only by {who}, which the Conda bootstrap also installs; "
                "the image may take their license from the Conda record, so it cannot be told "
                "here whether the file needs this entry"
            )
        else:
            problems.append(f"{license_!r} (carried by {who}) is missing from the file")
    stale = sorted(present - set(carriers) - {UBUNTU})
    if stale:
        warnings.append(
            f"{len(stale)} entries in the file are carried by no Python distribution: "
            + "; ".join(repr(entry) for entry in stale)
            + ". They are right if a Conda package, the base distribution or a binary gives "
            "them, which this check cannot see"
        )
    return problems, warnings


def image_problems(root: Path, profile: str) -> tuple[str, list[str], list[str]]:
    """Check ``images/<profile>/licenses.txt`` against the image's locked Python
    distributions: a summary line, problems and warnings."""
    report = _image_report(root)
    workspace = image_workspace_packages(root, profile)
    environment = image_python_environment(root)
    requirements = parse_requirements(image_requirements(root, profile), environment)
    licenses = _locked_licenses(requirements, workspace)
    problems: list[str] = []
    carriers: dict[str, list[str]] = {}
    for name, found in sorted(licenses.items(), key=lambda item: item[0].lower()):
        if found is None:
            problems.append(f"{name}: license not declared in metadata (the build fails on it)")
        elif not is_allowed(found):
            problems.append(f"{name}: {found} is not allowed in tier 1 (the build fails on it)")
        else:
            carriers.setdefault(report.normalize(found), []).append(name)
    entries = report.read_licenses(root / "images" / profile / "licenses.txt")
    more_problems, warnings = compare_image_licenses(carriers, image_bootstrap_names(root), entries)
    summary = (
        f"{profile}: {len(requirements)} locked wheels and {len(workspace)} repository packages "
        f"carry {len(carriers)} distinct licenses; images/{profile}/licenses.txt has {len(entries)}"
    )
    return summary, problems + more_problems, warnings


IMAGE_NOT_CHECKED = (
    "not checked here: the Conda bootstrap's packages (their licenses are in the image's "
    "conda-meta), the Ubuntu packages (the label always adds LicenseRef-Ubuntu-archive) and "
    "Node and the OpenShell CLI (in the image record, not in the label's set)"
)


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
    parser.add_argument(
        "--image",
        choices=IMAGE_PROFILES,
        help="compare images/<profile>/licenses.txt with the image's locked Python packages",
    )
    args = parser.parse_args(argv)
    if args.image is not None:
        if args.npm is not None or args.installed:
            parser.error("--image cannot be combined with --npm or --installed")
        try:
            summary, problems, warnings = image_problems(_repository_root(), args.image)
        except ImageLicenseError as error:
            print(f"{args.image}: {error}", file=sys.stderr)
            return 1
        print(summary)
        print(IMAGE_NOT_CHECKED)
        for warning in warnings:
            print(f"warning: {warning}")
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            print(f"{args.image}: the file lists every license the locked wheels carry")
        return 1 if problems else 0
    if args.npm is None:
        if args.installed:
            parser.error("--installed needs --npm")
        count, problems = python_problems()
        checked = f"all {count} installed distributions have"
    else:
        lockfile = json.loads(args.npm.read_text(encoding="utf-8"))
        manifest = args.npm.parent / "package.json"
        app = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
        count, problems = npm_problems(lockfile, app)
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
