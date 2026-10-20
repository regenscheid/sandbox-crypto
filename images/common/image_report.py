# SPDX-License-Identifier: MIT
"""Checks and records for a base profile image, run inside the image while it is built.

``kernels`` fails unless the interpreter's Jupyter installation finds each named kernelspec.
``record`` writes the image's record to ``/usr/share/sandbox-science``: what it is built from,
every package in it and their licenses. It fails when the licenses differ from the ones the
recipe declares (``images/<profile>/licenses.txt``, which also becomes the image's
``org.opencontainers.image.licenses`` label), and, under ``--policy permissive``, when a package
is neither permissive nor one of the base distribution's runtime libraries (docs/LICENSING.md,
tier 2). docs/IMAGES.md describes the record.

The script uses only the standard library and the image's own Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

PREFIX = Path(sys.prefix)
RECORD_DIR = Path("/usr/share/sandbox-science")

PERMISSIVE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "LicenseRef-Public-Domain",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
        "TCL",
        "Unlicense",
        "X11",
        "Zlib",
        "blessing",
        "bzip2-1.0.6",
    }
)
"""Licenses the published image may carry without an exception (docs/LICENSING.md, tiers 1-2)."""

BASE_DISTRIBUTION = {
    "keyutils": "LGPL runtime library of Kerberos, which ZeroMQ links",
    "ld_impl_linux-64": "GNU ld; conda-forge's Python requires it for building extensions",
    "ld_impl_linux-aarch64": "GNU ld; conda-forge's Python requires it for building extensions",
    "libgcc": "GCC runtime library, under the GCC Runtime Library Exception",
    "libgcc-ng": "GCC runtime library, under the GCC Runtime Library Exception",
    "libgomp": "GCC runtime library, under the GCC Runtime Library Exception",
    "libstdcxx": "GCC runtime library, under the GCC Runtime Library Exception",
    "libstdcxx-ng": "GCC runtime library, under the GCC Runtime Library Exception",
    "readline": "linked only by Python's optional readline module, which conda-forge builds",
}
"""Runtime libraries of conda-forge's Python that are not permissive, with the reason each is
accepted as part of the base distribution (docs/LICENSING.md, tier 2)."""

UBUNTU = "LicenseRef-Ubuntu-archive"
"""Stands for the Ubuntu packages, whose licenses are in /usr/share/doc/*/copyright."""

ALIASES = {"BSD": "LicenseRef-BSD", "LGPL": "LicenseRef-LGPL", "GPLv2+": "GPL-2.0-or-later"}
"""Names some packages give that are not SPDX identifiers."""

_TOKEN = re.compile(r"\(|\)|[^\s()]+")
_ID = re.compile(r"[A-Za-z0-9.+-]+")
_OPERATORS = frozenset({"AND", "OR", "WITH"})


class ReportError(Exception):
    """The image does not meet its recipe's declarations."""


def tokens(expression: str) -> list[str]:
    return _TOKEN.findall(expression)


def is_spdx(parts: list[str]) -> bool:
    """Whether ``parts`` follow the SPDX expression grammar: identifiers joined by AND and OR,
    each optionally WITH an exception, in parentheses as needed."""
    position = 0

    def term() -> bool:
        nonlocal position
        if position < len(parts) and parts[position] == "(":
            position += 1
            if not expression() or position >= len(parts) or parts[position] != ")":
                return False
            position += 1
            return True
        if position >= len(parts) or not identifier(parts[position]):
            return False
        position += 1
        if position < len(parts) and parts[position] == "WITH":
            if position + 1 >= len(parts) or not identifier(parts[position + 1]):
                return False
            position += 2
        return True

    def expression() -> bool:
        nonlocal position
        if not term():
            return False
        while position < len(parts) and parts[position] in ("AND", "OR"):
            position += 1
            if not term():
                return False
        return True

    def identifier(part: str) -> bool:
        return part not in _OPERATORS and part not in "()" and bool(_ID.fullmatch(part))

    return expression() and position == len(parts)


def normalize(expression: str | None) -> str:
    """The license as an SPDX expression; anything else becomes a ``LicenseRef``."""
    parts = [
        part.upper() if part.upper() in _OPERATORS else ALIASES.get(part, part)
        for part in tokens(expression or "")
    ]
    if not parts:
        return "NOASSERTION"
    if is_spdx(parts):
        return " ".join(parts).replace("( ", "(").replace(" )", ")")
    text = " ".join((expression or "").split())
    return "LicenseRef-" + re.sub(r"[^A-Za-z0-9.-]+", "-", text).strip("-")


def permissive(expression: str) -> bool:
    """Evaluate ``expression``: OR needs one permissive side, AND needs both; WITH keeps the
    license's own status."""
    parts = tokens(expression)
    position = 0

    def peek() -> str | None:
        return parts[position] if position < len(parts) else None

    def take() -> str:
        nonlocal position
        position += 1
        return parts[position - 1]

    def disjunction() -> bool:
        result = conjunction()
        while peek() == "OR":
            take()
            right = conjunction()
            result = result or right
        return result

    def conjunction() -> bool:
        result = factor()
        while peek() == "AND":
            take()
            right = factor()
            result = result and right
        return result

    def factor() -> bool:
        part = take()
        if part == "(":
            result = disjunction()
            if take() != ")":
                raise ValueError(expression)
            return result
        if peek() == "WITH":
            take()
            take()
        return part in PERMISSIVE

    try:
        result = disjunction()
    except IndexError:
        return False
    return result and position == len(parts)


def label(expressions: list[str]) -> str:
    """One SPDX expression for the image's ``org.opencontainers.image.licenses`` label."""
    parts = [f"({item})" if len(tokens(item)) > 1 else item for item in expressions]
    return " AND ".join(parts)


def conda_records() -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in sorted((PREFIX / "conda-meta").glob("*.json"))]


def conda_packages(records: list[dict[str, object]]) -> list[dict[str, str]]:
    return [
        {
            "name": str(record["name"]),
            "version": str(record["version"]),
            "build": str(record.get("build", "")),
            "url": str(record.get("url", "")),
            "sha256": str(record.get("sha256", "")),
            "license": normalize(str(record.get("license") or "")),
        }
        for record in records
    ]


def pip_packages(
    records: list[dict[str, object]], licenses: dict[str, str]
) -> list[dict[str, str]]:
    """Python distributions in the prefix that no Conda package owns: installed with pip.

    ``licenses`` gives the license of a distribution whose metadata names none (G6K's).
    """
    owned: set[str] = set()
    for record in records:
        files = record.get("files")
        if isinstance(files, list):
            owned.update(str(item) for item in files)
    packages: list[dict[str, str]] = []
    for info in sorted(PREFIX.glob("lib/python3*/site-packages/*.dist-info")):
        if str((info / "METADATA").relative_to(PREFIX)) in owned:
            continue
        dist = metadata.PathDistribution(info)
        name = dist.metadata["Name"]
        found = (
            licenses.get(name.lower())
            or dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
        )
        packages.append(
            {
                "name": name,
                "version": dist.version,
                "installer": (dist.read_text("INSTALLER") or "").strip() or "unknown",
                "license": normalize(found),
            }
        )
    return packages


def apt_packages() -> list[dict[str, str]]:
    output = subprocess.run(
        [
            "/usr/bin/dpkg-query",
            "--show",
            "--showformat",
            "${Package}\t${Version}\t${Architecture}\n",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = [line.split("\t") for line in output.splitlines() if line]
    return [{"name": n, "version": v, "architecture": a, "license": UBUNTU} for n, v, a in rows]


def license_set(conda: list[dict[str, str]], pip: list[dict[str, str]]) -> list[str]:
    return sorted({item["license"] for item in [*conda, *pip]} | {UBUNTU})


def check_policy(conda: list[dict[str, str]], pip: list[dict[str, str]]) -> list[str]:
    """Packages a permissive image may not contain."""
    problems: list[str] = []
    for item in [*conda, *pip]:
        if permissive(item["license"]) or item["name"] in BASE_DISTRIBUTION:
            continue
        problems.append(f"{item['name']} {item['version']}: {item['license']}")
    return problems


def read_licenses(path: Path) -> list[str]:
    """A recipe's licenses.txt: one SPDX expression per line; ``#`` starts a comment."""
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return sorted({line for line in lines if line})


def command_kernels(names: list[str]) -> None:
    from jupyter_client.kernelspec import KernelSpecManager

    specs: dict[str, str] = KernelSpecManager().find_kernel_specs()
    missing = [name for name in names if name not in specs]
    for name, directory in sorted(specs.items()):
        print(f"kernelspec {name}: {directory}")
    if missing:
        raise ReportError(f"kernelspecs not found: {', '.join(missing)}")


def command_record(args: argparse.Namespace) -> None:
    sources: dict[str, dict[str, str]] = {}
    for item in args.source:
        name, version, license_, url, flags = item.split("|", 4)
        sources[name.lower()] = {
            "name": name,
            "version": version,
            "license": license_,
            "url": url,
            "build_flags": flags,
        }
    records = conda_records()
    conda = conda_packages(records)
    pip = pip_packages(records, {key: value["license"] for key, value in sources.items()})
    apt = apt_packages()
    licenses = license_set(conda, pip)
    lockfile = Path(args.lockfile)
    record = {
        "profile": args.profile,
        "revision": args.revision,
        "source": args.source_url,
        "base_image": args.base_image,
        "ubuntu_snapshot": args.ubuntu_snapshot,
        "micromamba": args.micromamba,
        "conda_prefix": str(PREFIX),
        "conda_lockfile_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "sources": sorted(sources.values(), key=lambda item: item["name"]),
        "licenses": licenses,
        "packages": {"conda": conda, "pip": pip, "apt": apt},
    }
    problems: list[str] = []
    research = args.profile in ("minimal", "crypto-base")
    workspace_prefix = Path("/sandbox/.sandbox-science-prefix")
    home = Path("/sandbox")
    if research:
        if (
            PREFIX.resolve() != workspace_prefix
            or workspace_prefix.is_symlink()
            or not Path("/opt/sandbox-science").is_symlink()
        ):
            problems.append("research prefix must live under /sandbox with its /opt symlink")
        if set(home.iterdir()) != {workspace_prefix}:
            problems.append("research home contains files outside its package prefix")
        if (workspace_prefix / ".sandbox-science-prefix-id").exists():
            problems.append("image must not seed a workspace prefix identity")
        record["workspace_prefix_protocol"] = 1
    # An extracted package cache would duplicate the retained base package bytes.
    for directory in (PREFIX / "pkgs",) if research else (home, PREFIX / "pkgs"):
        if directory.is_dir() and any(directory.iterdir()):
            problems.append(f"{directory} is not empty")
    if args.policy == "permissive":
        problems += [f"not permissive: {item}" for item in check_policy(conda, pip)]
    if args.expect_licenses != label(licenses):
        problems.append(
            f"the licenses differ from images/{args.profile}/licenses.txt; its lines should be:\n"
            + "\n".join(licenses)
        )
    if problems:
        raise ReportError("\n".join(problems))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "image.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    (out / "conda-lock.yml").write_bytes(lockfile.read_bytes())
    print(f"recorded {len(conda)} conda, {len(pip)} pip and {len(apt)} apt packages in {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks and records for a base profile image.")
    commands = parser.add_subparsers(dest="command", required=True)
    label_command = commands.add_parser("label", help="print a licenses.txt as one SPDX label")
    label_command.add_argument("file", type=Path)
    kernels = commands.add_parser("kernels", help="require these kernelspecs")
    kernels.add_argument("names", nargs="+")
    record = commands.add_parser("record", help="write the image's record")
    record.add_argument("--profile", required=True)
    record.add_argument("--revision", required=True)
    record.add_argument("--source-url", required=True)
    record.add_argument("--base-image", required=True)
    record.add_argument("--ubuntu-snapshot", required=True)
    record.add_argument("--micromamba", required=True)
    record.add_argument("--lockfile", required=True)
    record.add_argument("--expect-licenses", required=True)
    record.add_argument("--policy", choices=["permissive", "report"], default="report")
    record.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME|VERSION|LICENSE|URL|FLAGS",
        help="a package built from source: its commit, license, repository and build flags",
    )
    record.add_argument("--out", default=str(RECORD_DIR))
    args = parser.parse_args(argv)
    try:
        if args.command == "label":
            print(label(read_licenses(args.file)))
        elif args.command == "kernels":
            command_kernels(args.names)
        else:
            command_record(args)
    except ReportError as error:
        print(f"image check failed:\n{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
