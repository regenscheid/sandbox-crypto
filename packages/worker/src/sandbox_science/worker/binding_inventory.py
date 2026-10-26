# SPDX-License-Identifier: MIT
"""Static package-owned variable inventory, shared by image builds and executors.

Only package metadata and owned program files are read. Unsupported binary/compiled
accesses remain coverage limitations; no installed module or helper is executed.
The guest payload uses only the standard library.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, cast

FORMAT: Final = "sandbox-binding-inventory-v1"
VERSION: Final = 1
MAX_METADATA: Final = 16 * 1024 * 1024
MAX_FILE: Final = 2 * 1024 * 1024
MAX_TOTAL: Final = 2 * 1024 * 1024 * 1024
MAX_FILES: Final = 300_000
MAX_ENTRIES: Final = 100_000
NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")
LITERAL: Final = re.compile(
    r"(?:getenv|secure_getenv|environ\.get|var)\s*\(\s*['\"]([A-Z][A-Z0-9_]{1,127})['\"]"
    r"|environ\s*\[\s*['\"]([A-Z][A-Z0-9_]{1,127})['\"]"
    r"|\$(?:\{)?([A-Z][A-Z0-9_]{1,127})"
)
SOURCES: Final = frozenset(
    {".py", ".pyx", ".pxd", ".sh", ".bash", ".c", ".h", ".cc", ".cpp", ".pl", ".rb"}
)


class InventoryError(ValueError):
    """The declared scan could not be completed; no bindings may be granted."""


def canonical(value: object) -> bytes:
    """Stable UTF-8 encoding, independent of directory enumeration and timestamps."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read(path: Path, limit: int = MAX_METADATA) -> bytes:
    """Bound every metadata/content read before materializing it."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise InventoryError(f"inventory input is not a bounded regular file: {path.name}")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise InventoryError(f"inventory input exceeds {limit} bytes: {path.name}")
    return data


def scoped_read(path: Path, root: Path) -> bytes:
    """Metadata never redirects inspection to credentials or other unowned files."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise InventoryError("package metadata escapes its installation root") from None
    return read(path)


def variable_reads(data: bytes, suffix: str) -> set[str]:
    """Literal variable accesses; binary strings are deliberately conservative."""
    if data.startswith(b"\x7fELF"):
        if b"getenv" not in data:
            return set()
        return {
            s.decode()
            for s in re.findall(rb"(?:^|\x00)([A-Z][A-Z0-9_]{1,127})(?=\x00)", data)
            if b"_" in s
        }
    text = data.decode("utf-8", "replace")
    return {next(v for v in match.groups() if v) for match in LITERAL.finditer(text)}


def generate(
    prefix: Path,
    sites: tuple[Path, ...],
    *,
    dpkg: Path = Path("/var/lib/dpkg"),
    base: Path | None = None,
) -> dict[str, Any]:
    """Generate deterministic provenance from installed ownership metadata.

    ``base`` is the read-only environment under a workspace layer (docs/IMAGES.md, "The
    environment outside the workspace"): its Conda records own the files, the layer's
    records of the same packages own none, and ``sites`` includes its site-packages.
    Large program files are explicitly outside this extractor's policy. Unexpected
    unreadability or a total bound refuses publication; names are never silently cut.
    """
    prefix = prefix.resolve()
    roots = (prefix,) if base is None else (base.resolve(), prefix)
    # A layer's interpreter names its sites through the /opt link to the workspace.
    sites = tuple(site.resolve() for site in sites)
    owned: dict[tuple[str, str], tuple[Path, str]] = {}
    packages: set[str] = set()
    origins: dict[str, dict[str, str]] = {}
    conda_metadata: set[Path] = set()
    notes: set[str] = {
        "literal accesses only; computed names/unsupported languages use maintained fallback rules",
        "ELF getenv users: underscore strings are candidates; bare/computed names need fallback",
        "program files only; documentation, translations and build headers excluded",
    }
    # Resolving a root for every owned file repeats filesystem walks tens of
    # thousands of times on a fresh executor. The root itself cannot change
    # during this scan; the package fingerprint is checked again before publish.
    resolved_roots: dict[Path, Path] = {}

    def within(candidate: Path) -> tuple[Path, str] | None:
        """The installation root ``candidate`` is in, and its path there."""
        for root in roots:
            try:
                return root, str(candidate.relative_to(root))
            except ValueError:
                continue
        return None

    def add(package: str, root: Path, relative: str) -> None:
        if len(relative) > 4096 or len(package) > 512 or any(ord(c) < 32 for c in package):
            raise InventoryError("invalid bounded package identity/path")
        if len(owned) >= MAX_FILES:
            raise InventoryError("inventory file count exceeds bound")
        # Documentation, translations and kernel build headers are not executed tool
        # inputs. Filter before resolving ownership lists with hundreds of thousands
        # of data files; retain only supported program sources/libraries/commands.
        if any(
            part in relative
            for part in ("/share/doc/", "/share/man/", "/share/locale/", "/src/", "/include/")
        ):
            return
        relative_path = Path(relative)
        if (
            relative_path.suffix not in SOURCES
            and ".so" not in relative_path.name
            and "/bin/" not in "/" + relative
            and "/sbin/" not in "/" + relative
        ):
            return
        candidate = root / relative_path
        try:
            if root not in resolved_roots:
                resolved_roots[root] = root.resolve()
            resolved = candidate.resolve()
            resolved.relative_to(resolved_roots[root])
        except (ValueError, OSError, RuntimeError):
            if base is not None and root == prefix and within(candidate.resolve()) is not None:
                return  # the layer's link to the base's file, which the base's record owns
            notes.add(f"{package}: external/symlink target outside package root")
            return
        owned[(package, relative)] = (resolved, relative)

    try:
        records = [
            (place, meta)
            for place in roots
            for meta in sorted((place / "conda-meta").glob("*.json"))
        ]
        for root, meta in records:
            metadata_bytes = scoped_read(meta, root)
            record = json.loads(metadata_bytes)
            package = f"conda:{record['name']}@{record['version']}:{record.get('build', '')}"
            if len(package) > 512 or any(ord(c) < 32 for c in package):
                raise InventoryError("invalid package identity")
            packages.add(package)
            origins[package] = {
                "source": "conda-meta/" + meta.name,
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            }
            files = record.get("files", [])
            if not isinstance(files, list):
                raise InventoryError("invalid Conda file list")
            for relative in cast(list[object], files):
                if not isinstance(relative, str):
                    raise InventoryError("invalid Conda file ownership")
                if relative.endswith(".dist-info/METADATA"):
                    conda_metadata.add((root / relative).resolve())
                add(package, root, relative)
        for site in sorted(set(sites)):
            for info in sorted(site.glob("*.dist-info")):
                metadata_bytes = scoped_read(info / "METADATA", site)
                metadata = metadata_bytes.decode("utf-8", "replace")
                name = re.search(r"^Name: (.+)$", metadata, re.M)
                version = re.search(r"^Version: (.+)$", metadata, re.M)
                if name is None or version is None:
                    raise InventoryError("distribution metadata lacks name/version")
                package = f"pip:{name[1].strip()}@{version[1].strip()}"
                if len(package) > 512 or any(ord(c) < 32 for c in package):
                    raise InventoryError("invalid package identity")
                packages.add(package)
                origins[package] = {
                    "source": info.name + "/METADATA",
                    "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                }
                if not (info / "RECORD").exists():
                    if (info / "METADATA").resolve() in conda_metadata:
                        notes.add(f"{package}: Conda ownership replaces absent pip RECORD")
                        continue
                    raise InventoryError("distribution lacks RECORD ownership")
                for row in csv.reader(scoped_read(info / "RECORD", site).decode().splitlines()):
                    if row:
                        candidate = Path(os.path.abspath(site / row[0]))
                        found = within(candidate)
                        if found is None:
                            # A user-site installation is scanned within that site only.
                            add(package, site, row[0])
                        else:
                            add(package, *found)
        for site in sorted(set(sites)):
            for info in sorted(site.glob("*.egg-info")):
                metadata_path = info / "PKG-INFO" if info.is_dir() else info
                metadata_bytes = scoped_read(metadata_path, site)
                metadata = metadata_bytes.decode("utf-8", "replace")
                name = re.search(r"^Name: (.+)$", metadata, re.M)
                version = re.search(r"^Version: (.+)$", metadata, re.M)
                if name is None or version is None:
                    raise InventoryError("egg metadata lacks name/version")
                package = f"egg:{name[1].strip()}@{version[1].strip()}"
                if len(package) > 512 or any(ord(c) < 32 for c in package):
                    raise InventoryError("invalid egg package identity")
                packages.add(package)
                origins[package] = {
                    "source": info.name + "/PKG-INFO",
                    "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                }
                installed = info / "installed-files.txt"
                if info.is_dir() and installed.exists():
                    for relative in scoped_read(installed, site).decode().splitlines():
                        candidate = Path(os.path.abspath(info / relative))
                        found = within(candidate)
                        if found is None:
                            raise InventoryError("egg ownership escapes installation prefix")
                        add(package, *found)
                else:
                    notes.add(
                        f"{package}: egg metadata lacks installed ownership; fallback rules apply"
                    )
        status_path = dpkg / "status"
        versions: dict[str, str] = {}
        if status_path.exists():
            for paragraph in (
                scoped_read(status_path, dpkg).decode("utf-8", "replace").split("\n\n")
            ):
                fields = dict(
                    line.split(": ", 1)
                    for line in paragraph.splitlines()
                    if ": " in line and not line.startswith(" ")
                )
                if "Package" in fields:
                    versions[fields["Package"]] = fields.get("Version", "unknown")
        for listing in sorted((dpkg / "info").glob("*.list")):
            name = listing.name.removesuffix(".list")
            package = f"dpkg:{name}@{versions.get(name.split(':')[0], 'unknown')}"
            if len(package) > 512 or any(ord(c) < 32 for c in package):
                raise InventoryError("invalid package identity")
            packages.add(package)
            metadata_bytes = scoped_read(listing, dpkg)
            origins[package] = {
                "source": "dpkg-info/" + listing.name,
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            }
            for absolute in metadata_bytes.decode().splitlines():
                # Never scan configuration or user files, even when a package owns them.
                if absolute.startswith(("/usr/", "/bin/", "/sbin/", "/lib/")):
                    if str(Path(absolute).resolve()).startswith(
                        ("/usr/", "/bin/", "/sbin/", "/lib/")
                    ):
                        add(package, Path("/"), absolute.lstrip("/"))
                    else:
                        notes.add(f"{package}: system symlink outside program roots")
        if len(packages) > 20_000:
            raise InventoryError("inventory package count exceeds bound")
        entries: list[dict[str, str]] = []
        seen_variables: set[tuple[str, str]] = set()
        commands: set[str] = set()
        command_sources: list[dict[str, str]] = []
        total = 0
        for (package, _), (path, relative) in sorted(owned.items()):
            try:
                mode = path.stat()
            except FileNotFoundError:
                notes.add(f"{package}: recorded files absent")
                continue
            if not stat.S_ISREG(mode.st_mode):
                continue
            executable = bool(mode.st_mode & 0o111) and "/bin/" in "/" + relative
            if executable:
                namespace = path.name.upper().replace("-", "_") + "_"
                commands.add(namespace)
                command_sources.append(
                    {
                        "namespace": namespace,
                        "package": package,
                        "source": relative,
                        "rule": "owned-command-v1",
                        "ownership_source": origins[package]["source"],
                        "sha256": origins[package]["sha256"],
                    }
                )
            if path.suffix not in SOURCES and not executable and ".so" not in path.name:
                continue
            if mode.st_size > MAX_FILE:
                notes.add(
                    f"{package}: program files above {MAX_FILE} bytes outside extraction policy"
                )
                continue
            total += mode.st_size
            if total > MAX_TOTAL:
                raise InventoryError("inventory total program bytes exceed bound")
            data = read(path, MAX_FILE)
            digest = hashlib.sha256(data).hexdigest()
            for variable in sorted(variable_reads(data, path.suffix)):
                if (package, variable) in seen_variables:
                    continue
                seen_variables.add((package, variable))
                entries.append(
                    {
                        "variable": variable,
                        "package": package,
                        "source": relative,
                        "sha256": digest,
                        "rule": "elf-string" if data.startswith(b"\x7fELF") else "literal-read-v1",
                    }
                )
                if len(entries) > MAX_ENTRIES:
                    raise InventoryError("inventory restriction count exceeds bound")
        families = [
            {
                "prefix": prefix,
                "package": package,
                "rule": "reviewed-package-family-v1",
                "source": origins[package]["source"],
                "sha256": origins[package]["sha256"],
            }
            for package in sorted(packages)
            for tool, prefixes in {
                "sage": ("SAGE",),
                "sage-conf": ("SAGE",),
                "python": ("PYTHON",),
                "gap": ("GAP_",),
                "gcc": ("GCC_",),
                "maxima": ("MAXIMA",),
                "jupyter-core": ("JUPYTER",),
            }.items()
            if package.split(":", 1)[1].split("@", 1)[0].lower() == tool
            for prefix in prefixes
        ]
        image = Path("/usr/share/sandbox-science/image.json")
        source_value: Any = (
            json.loads(scoped_read(image, image.parent)).get("sources", [])
            if image.exists()
            else list[object]()
        )
        if not isinstance(source_value, list) or len(cast(list[object], source_value)) > 512:
            raise InventoryError("invalid source-built package inventory")
        sources = cast(list[object], source_value)
        body: dict[str, Any] = {
            "format": FORMAT,
            "generator": VERSION,
            "extractor_sha256": hashlib.sha256(read(Path(__file__))).hexdigest(),
            "packages": sorted(packages),
            "package_metadata": origins,
            "source_packages": sources,
            "entries": entries,
            "namespaces": sorted(commands),
            "command_sources": command_sources,
            "families": families,
            "coverage": sorted(notes),
        }
        encoded = canonical(body)
        if len(encoded) > 64 * 1024 * 1024:
            raise InventoryError("inventory manifest exceeds 64 MiB bound")
        body["digest"] = hashlib.sha256(encoded).hexdigest()
        return body
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise InventoryError(
            f"static binding inventory unavailable: {type(error).__name__}: {str(error)[:160]}"
        ) from None


def forbidden(inventory: dict[str, Any], variable: str) -> bool:
    """Generated exact denials; maintained families are enforced independently."""
    return any(entry["variable"] == variable for entry in inventory["entries"])


def layered_base() -> Path | None:
    """The base environment under this interpreter's layer: a virtual environment over a
    Conda environment's interpreter (docs/IMAGES.md); None for any other interpreter."""
    if sys.prefix == sys.base_prefix or not os.path.isdir(
        os.path.join(sys.base_prefix, "conda-meta")
    ):
        return None
    return Path(sys.base_prefix)


if __name__ == "__main__":
    import sysconfig

    layered = layered_base()
    sites = (Path(sysconfig.get_path("purelib")),)
    if layered is not None:
        variables = {"base": sys.base_prefix, "platbase": sys.base_exec_prefix}
        sites += (Path(sysconfig.get_path("purelib", vars=variables)),)
    result = generate(Path(sys.prefix), sites, base=layered)
    Path(sys.argv[1]).write_bytes(canonical(result) + b"\n")


def system_fingerprint(dpkg: Path = Path("/var/lib/dpkg")) -> str:
    """Hash distribution metadata without reading environment values or running tools."""
    digest = hashlib.sha256()
    for path in [dpkg / "status", *sorted((dpkg / "info").glob("*.list"))]:
        if path.exists():
            digest.update(path.name.encode())
            digest.update(scoped_read(path, dpkg))
    return digest.hexdigest()


class Inventory:
    """Executor-local cache invalidated by actual package metadata, never guest JSON."""

    def __init__(self) -> None:
        self.fingerprint = ""
        self.manifest: dict[str, Any] | None = None
        self.variables: set[str] = set()
        self.families: tuple[str, ...] = ()

    def current(
        self,
        prefix: Path,
        sites: tuple[Path, ...],
        fingerprint: Callable[[], str],
        destination: Path,
        base: Path | None = None,
    ) -> dict[str, Any]:
        """Publish only a stable scan; a failed replacement invalidates the old cache."""
        before = fingerprint()
        if self.manifest is not None and before == self.fingerprint:
            return self.manifest
        self.manifest, self.variables, self.fingerprint, self.families = None, set(), "", ()
        manifest = generate(prefix, sites) if base is None else generate(prefix, sites, base=base)
        # Even an unsupported metadata layout must invalidate cached namespace grants.
        # Bind the observed metadata identity, not just extracted names, into the token.
        manifest.pop("digest")
        manifest["package_fingerprint"] = before
        manifest["digest"] = hashlib.sha256(canonical(manifest)).hexdigest()
        if fingerprint() != before:
            raise InventoryError("packages changed during inventory generation")
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = canonical(manifest)
        if len(encoded) > 64 * 1024 * 1024:
            raise InventoryError("inventory manifest exceeds 64 MiB bound")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(encoded + b"\n")
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.manifest = manifest
        self.variables = {entry["variable"] for entry in manifest["entries"]}
        self.families = tuple(entry["prefix"] for entry in manifest["families"])
        self.fingerprint = before
        return manifest

    def projection(self, names: list[str], epoch: int) -> dict[str, Any]:
        """Bounded candidate projection; full extraction provenance stays in the manifest."""
        if self.manifest is None:
            raise InventoryError("current binding inventory unavailable")
        examples: dict[str, dict[str, str]] = {}
        for entry in self.manifest["entries"]:
            examples.setdefault(entry["variable"], entry)
        provenance: list[dict[str, str]] = []
        for name in sorted(set(names)):
            matched = examples.get(name) or next(
                (entry for prefix, entry in examples.items() if prefix.startswith(name)), None
            )
            if matched is not None:
                provenance.append(
                    {
                        "name": name,
                        **{k: matched[k] for k in ("package", "source", "sha256", "rule")},
                    }
                )
                continue
            command = next(
                (
                    entry
                    for entry in self.manifest["command_sources"]
                    if entry["namespace"] in {name, name.split("_", 1)[0] + "_"}
                ),
                None,
            )
            family = next(
                (entry for entry in self.manifest["families"] if name.startswith(entry["prefix"])),
                None,
            )
            entry = family or command
            if entry is not None:
                provenance.append(
                    {
                        "name": name,
                        "package": entry["package"],
                        "source": entry.get("ownership_source", entry["source"]),
                        "sha256": entry["sha256"],
                        "rule": entry["rule"],
                    }
                )
        return {
            "state": "ready",
            "digest": self.manifest["digest"],
            "fingerprint": self.fingerprint,
            "generator": VERSION,
            "epoch": epoch,
            "provenance": provenance,
            "reserved": sorted(
                n for n in set(names) if n in self.variables or n.startswith(self.families)
            ),
            "namespaces": [
                n
                for n in names
                if any(v.startswith(n) for v in (*self.variables, *self.families))
                or n in self.manifest["namespaces"]
                or n.split("_", 1)[0] + "_" in self.manifest["namespaces"]
            ],
            "coverage": (
                "static literals and conservative ELF strings; computed/large/unsupported "
                "accesses use maintained fallback rules"
            ),
        }

    def require(self, names: set[str]) -> None:
        """No approval or previous staging bypasses a current generated restriction."""
        if self.manifest is None:
            raise InventoryError("current binding inventory unavailable")
        if reserved := sorted(
            n for n in names if n in self.variables or n.startswith(self.families)
        ):
            raise InventoryError(
                f"reserved tool variables: {', '.join(reserved[:20])}; "
                "remove bindings and explicitly restart affected kernels"
            )
