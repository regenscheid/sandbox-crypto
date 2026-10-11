# SPDX-License-Identifier: MIT
"""Resolving a provisioning request, and deciding each resolved file (CP-6, CP-7, CP-9).

Pure functions, for ``provisioner.py``: the commands an operation runs in the target
environment's sandbox, the parsing of their output, the policy decision on each resolved
file, the AGPL check, and the comparison of lockfiles.

The commands are package managers run through the environment's wrappers, so an install
takes the environment's turn and becomes a managed revision (CP-4, CP-11); a dry run
(``--dry-run``) only reads, and runs at once. Specs reach them as arguments, never through
a shell's parsing: each command is ``sh -c 'exec … "$@"'`` with the specs after it.

What a dry run reports comes from the sandbox, whose code can forge it. The decision on
each file is therefore a forecast of the policy middleware's, made with the same library;
the middleware's decision on each download is what holds (docs/WORKER.md, "Provisioning").
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from sandbox_science.contracts import Lockfile, PackageChange, ResolvedFile
from sandbox_science.contracts.provisioning import MAX_FILES, Stage
from sandbox_science.policy import (
    Action,
    ClassificationError,
    Decision,
    HardFloor,
    LoadedPolicy,
    PackageOperation,
    PoolCeiling,
    classify_package_request,
    evaluate,
)

from . import provisioning

CONDA_DRY_RUN: Final = (
    'exec micromamba install --dry-run --json -y -p "${CONDA_PREFIX:?is not set}" "$@"'
)
CONDA_INSTALL: Final = (
    "MAMBA_PINNED_PACKAGES=$1; export MAMBA_PINNED_PACKAGES; shift; "
    'exec micromamba install --json -y -p "${CONDA_PREFIX:?is not set}" "$@"'
)
"""micromamba installs the request's specs, pinned to exactly the decided files: each by
channel, subdir, name, version and build, in ``pinned_packages`` (its first argument),
so the solve links those builds and nothing else they need, and the environment's history
records the request, not the pins. micromamba 2.9, the images' (MICROMAMBA_VERSION in
images/Containerfile), reads the pins from MAMBA_PINNED_PACKAGES, separated by commas."""
PIP_DRY_RUN: Final = 'exec pip install --dry-run --quiet --only-binary :all: --report - "$@"'
"""Wheels only: pip resolves from the index's metadata and never builds a source
distribution, which runs its code (CP-7)."""
PIP_INSTALL: Final = (
    'f=$(mktemp) || exit 1; printf "%s\\n" "$@" > "$f"; '
    'pip install --quiet --no-deps --only-binary :all: --require-hashes -r "$f"; s=$?; '
    'rm -f "$f"; exit $s'
)
"""pip installs exactly the resolved files: each pinned, with its SHA-256 (hash-checking
mode), which pip takes only from a requirements file."""
DEFAULT_CHANNEL: Final = "conda-forge"
"""The channel a Conda spec without one names: the only one the sandbox's condarc lists."""
MAX_REPORT_TEXT: Final = 64 * 1024
"""Characters of a license text read for the AGPL check."""
_AGPL: Final = re.compile(r"agpl|affero", re.IGNORECASE)
_SPEC_NAME: Final = re.compile(
    r"(?:(?P<channel>[a-z0-9][a-z0-9._-]*)(?:/[a-z0-9_-]+)?::)?(?P<name>[a-z0-9_][a-z0-9._+-]*)"
)
_PIP_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_CHANNEL: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""A channel's name as the package cache's paths hold it (docs/PACKAGE-CACHE.md)."""
_PIN_PART: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+!-]{0,255}")
_LICENSE_TEXT: Final = re.compile(r"[ -~]{1,256}")
_SOLVE_WORDS: Final = (
    "could not solve",
    "nothing provides",
    "unsatisfiable",
    "resolutionimpossible",
    "no matching distribution",
    "conflict",
)


class ResolveError(Exception):
    """A dry run's output cannot be used: it failed, or names what the worker cannot hold."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """A resolved package file, and its path at the package cache, where the sandbox will
    download it and the policy middleware decide it."""

    file: ResolvedFile
    path: str


@dataclass(frozen=True, slots=True)
class Decided:
    """The decisions on a resolved set: each file with the policy's action."""

    files: tuple[ResolvedFile, ...]
    paths: tuple[str, ...]
    decisions: tuple[Decision, ...]

    def with_action(self, action: Action) -> list[int]:
        return [index for index, decision in enumerate(self.decisions) if decision.action is action]


# Commands.


def conda_dry_run(specs: Sequence[str]) -> tuple[str, ...]:
    return ("sh", "-c", CONDA_DRY_RUN, "sh", *specs)


def conda_install(specs: Sequence[str], files: Iterable[ResolvedFile]) -> tuple[str, ...]:
    """The install of the request's specs, pinned to the decided files (``CONDA_INSTALL``).
    A file the pins leave out would be refused at its download if the policy escalates
    it, and is reported if the policy allows it (``unplanned``)."""
    return ("sh", "-c", CONDA_INSTALL, "sh", conda_pins(files), *specs)


def conda_pins(files: Iterable[ResolvedFile]) -> str:
    """Each decided Conda file as an exact spec, ``channel/subdir::name==version=build``,
    separated by commas, which none of the parts can hold."""
    pins: list[str] = []
    for file in files:
        parts = (file.channel or "", file.subdir or "", file.name, file.version, file.build or "")
        if not (
            _CHANNEL.fullmatch(parts[0])
            and _PIN_PART.fullmatch(parts[1])
            and all(_PIN_PART.fullmatch(part) for part in parts[2:])
        ):
            raise ValueError(
                f"cannot pin {file.file}: it lacks a channel at the cache, a subdir or a build"
            )
        pins.append(f"{parts[0]}/{parts[1]}::{parts[2]}=={parts[3]}={parts[4]}")
    return ",".join(pins)


def pip_dry_run(specs: Sequence[str]) -> tuple[str, ...]:
    return ("sh", "-c", PIP_DRY_RUN, "sh", *specs)


def pip_install(files: Iterable[ResolvedFile]) -> tuple[str, ...]:
    lines = [f"{file.name}=={file.version} --hash=sha256:{file.sha256}" for file in files]
    return ("sh", "-c", PIP_INSTALL, "sh", *lines)


def spec_name(spec: str) -> tuple[str | None, str]:
    """A Conda spec's channel (None for the default) and package name."""
    match = _SPEC_NAME.match(spec)
    if match is None:
        raise ValueError(f"not a Conda spec: {spec!r}")
    return match["channel"], match["name"]


def pip_name(spec: str) -> str:
    """A pip spec's project name, normalized (PEP 503)."""
    match = _PIP_NAME.match(spec)
    if match is None:
        raise ValueError(f"not a pip requirement: {spec!r}")
    return normalize_pypi(match.group())


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# Dry runs.


def _document(stdout: str, what: str) -> dict[str, Any]:
    """The JSON object a dry run printed: the whole output, or failing that the first object
    in it, as micromamba may print a line before its JSON."""
    text = stdout.strip()
    try:
        value: object = json.loads(text)
    except (ValueError, RecursionError):
        start = text.find("{")
        try:
            value = json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else None
        except (ValueError, RecursionError):
            value = None
    if not isinstance(value, dict):
        raise ResolveError(f"{what} printed no JSON object")
    return cast(dict[str, Any], value)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _license(value: object) -> str | None:
    """A declared license as records keep it: printable ASCII, at most 256 characters."""
    text = _text(value)
    if text is None:
        return None
    first = text.splitlines()[0].strip()
    return first if _LICENSE_TEXT.fullmatch(first) else None


def is_agpl(*texts: str | None) -> bool:
    """Whether any of a package's license texts is, or names, the GNU Affero GPL (CP-9)."""
    return any(text is not None and _AGPL.search(text[:MAX_REPORT_TEXT]) for text in texts)


def _cache_path(url: str, cache: str | None) -> str | None:
    """A URL's path at the package cache, if the URL is the cache's; None otherwise."""
    base = provisioning.canonical_url(cache, None) if cache is not None else None
    parsed = provisioning.canonical_url(url, None)
    if base is None or parsed is None:
        return None
    prefix = base[0].rstrip("/")
    if not parsed[0].startswith(prefix + "/"):
        return None
    return parsed[0][len(prefix) :]


def _file(fields: Mapping[str, object], what: str) -> ResolvedFile:
    try:
        return ResolvedFile.model_validate({**fields, "action": "allow"})
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ResolveError(f"{what} names a package the worker cannot hold ({where})") from None


def parse_conda_dry_run(stdout: str, cache: str | None) -> list[Resolved]:
    """The files micromamba's JSON dry run would link (``actions.LINK``), by channel name.

    The sandbox reports them, and the permission request grants their paths while people
    read their names, so the two must agree: a file at the package cache is
    ``/conda/<channel>/<subdir>/<file>``, with the entry's own subdir and file, and every
    file is named ``<name>-<version>-<build>``. A file whose channel is not reached
    through the package cache keeps its URL as its channel; the decision refuses it,
    since the policy middleware would never see it.
    """
    document = _document(stdout, "the Conda dry run")
    if document.get("success") is False:
        raise ResolveError("the Conda dry run did not succeed")
    actions = document.get("actions")
    links = cast(dict[str, Any], actions).get("LINK") if isinstance(actions, dict) else None
    entries = cast(list[object], links) if isinstance(links, list) else []
    _bounded(len(entries), "the Conda dry run")
    resolved: list[Resolved] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResolveError("the Conda dry run's LINK holds something other than packages")
        fields = cast(dict[str, Any], entry)
        name, version = _text(fields.get("name")), _text(fields.get("version"))
        build = _text(fields.get("build")) or _text(fields.get("build_string"))
        url, file, subdir = (
            _text(fields.get("url")),
            _text(fields.get("fn")),
            _text(fields.get("subdir")),
        )
        channel: str | None = None
        path = ""
        at_cache = _cache_path(url, cache) if url is not None else None
        if at_cache is not None:
            parts = at_cache.split("/")
            if len(parts) != 5 or parts[1] != "conda" or not _CHANNEL.fullmatch(parts[2]):
                raise ResolveError(
                    f"the Conda dry run would fetch {name!r} from {at_cache!r} at the package "
                    "cache, which is no Conda package's path"
                )
            if (subdir or parts[3], file or parts[4]) != (parts[3], parts[4]):
                raise ResolveError(
                    f"the Conda dry run names another file for {name!r} than its URL"
                )
            channel, subdir, file, path = parts[2], parts[3], parts[4], at_cache
        elif url is not None and url.count("/") >= 3:
            # Downloaded from anywhere but the cache, the file would meet no decision.
            base, _, last = url.rpartition("/")
            prefix, _, url_subdir = base.rpartition("/")
            file, subdir = file or last, subdir or url_subdir
            channel = provisioning.canonical_channel(prefix, cache)
        elif isinstance(fields.get("channel"), str):
            raw = str(fields["channel"]).strip().rstrip("/")
            if subdir and raw.endswith("/" + subdir):
                raw = raw[: -len(subdir) - 1]
            channel = provisioning.canonical_channel(raw, cache)
            if channel is not None and _CHANNEL.fullmatch(channel):
                # No URL: the sandbox fetches it from its channel, through the cache.
                path = f"/conda/{channel}/{subdir}/{file}"
        if channel is None:
            raise ResolveError(f"the Conda dry run names no usable channel for {name!r}")
        if file not in {f"{name}-{version}-{build}{suffix}" for suffix in (".conda", ".tar.bz2")}:
            raise ResolveError(
                f"the Conda dry run names the file {file!r} for {name!r} {version!r} {build!r}, "
                "which is another package's"
            )
        record = _file(
            {
                "ecosystem": "conda",
                "name": name,
                "version": version,
                "build": build,
                "channel": channel,
                "subdir": subdir,
                "file": file,
                "sha256": (_text(fields.get("sha256")) or "").lower() or None,
                "license": _license(fields.get("license")),
                "agpl": is_agpl(_text(fields.get("license"))),
            },
            "the Conda dry run",
        )
        resolved.append(Resolved(record, path))
    return resolved


def parse_pip_report(stdout: str, cache: str | None) -> list[Resolved]:
    """The distributions pip's installation report (``--report``) would install.

    Each must come from the package cache's PyPI files, with its SHA-256: the record names
    it by its files.pythonhosted.org URL (CP-18), and the install pins it by its hash. Its
    path at the cache must name its project and that SHA-256, since the permission
    request grants the path while people read the name.
    """
    document = _document(stdout, "pip's dry run")
    items = document.get("install")
    entries = cast(list[object], items) if isinstance(items, list) else []
    _bounded(len(entries), "pip's dry run")
    resolved: list[Resolved] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResolveError("pip's report holds something other than distributions")
        fields = cast(dict[str, Any], entry)
        metadata = fields.get("metadata")
        meta = cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}
        info = fields.get("download_info")
        download = cast(dict[str, Any], info) if isinstance(info, dict) else {}
        url = _text(download.get("url"))
        name = _text(meta.get("name"))
        if url is None or fields.get("is_direct") is True:
            raise ResolveError(f"pip would install {name!r} from a URL of its own")
        archive = download.get("archive_info")
        hashes = cast(dict[str, Any], archive).get("hashes") if isinstance(archive, dict) else None
        sha256 = (
            _text(cast(dict[str, Any], hashes).get("sha256")) if isinstance(hashes, dict) else None
        )
        path = _cache_path(url, cache)
        upstream = provisioning.canonical_url(url, cache)
        classifiers = meta.get("classifier")
        listed = (
            [c for c in cast(list[object], classifiers) if isinstance(c, str)]
            if isinstance(classifiers, list)
            else []
        )
        licenses = [c.rsplit("::", 1)[-1].strip() for c in listed if c.startswith("License ::")]
        expression = _text(meta.get("license_expression"))
        text = _text(meta.get("license"))
        record = _file(
            {
                "ecosystem": "pypi",
                "name": name,
                "version": _text(meta.get("version")),
                "file": urllib.parse.unquote(url.rsplit("/", 1)[-1]),
                "url": upstream[0] if upstream is not None else None,
                "sha256": (sha256 or (upstream[1] if upstream is not None else None) or "").lower()
                or None,
                "license": _license(expression)
                or _license(text)
                or _license(licenses[0] if licenses else None),
                "agpl": is_agpl(expression, text, *licenses),
            },
            "pip's dry run",
        )
        if record.sha256 is None:
            raise ResolveError(f"pip's report gives no SHA-256 for {record.name}")
        if path is not None:
            parts = path.split("/")
            if (
                len(parts) < 7
                or parts[1:3] != ["pypi", "files"]
                or parts[3] != normalize_pypi(record.name)
                or parts[4] != record.sha256
                or urllib.parse.unquote(parts[-1]) != record.file
            ):
                raise ResolveError(
                    f"pip would fetch {record.name} {record.version} from {path!r} at the "
                    "package cache, which is not that distribution's path"
                )
        resolved.append(Resolved(record, path or ""))
    return resolved


def _bounded(count: int, what: str) -> None:
    if count > MAX_FILES:
        raise ResolveError(
            f"{what} would install {count} package files, more than the {MAX_FILES} one "
            "operation may"
        )


# Decisions.


def decide(
    resolved: Sequence[Resolved], policy: LoadedPolicy, ceiling: PoolCeiling, floor: HardFloor
) -> Decided:
    """Each file with the action the policy middleware will decide for its download (CP-6).

    The file's path at the package cache is classified and evaluated with the policy
    library, as the middleware does it. A file not fetched through the cache, which the
    middleware would never see, is denied, and so is a path that is not the package file
    the record names (the parsers make sure of it; this keeps it so).
    """
    files: list[ResolvedFile] = []
    decisions: list[Decision] = []
    for item in resolved:
        if not item.path:
            decision = Decision(
                action=Action.DENY,
                layer="policy",
                reason=f"{item.file.name} would not come through the package cache",
                rule=f"packages.{item.file.ecosystem}",
                policy_digest=policy.digest,
            )
        else:
            try:
                operation = classify_package_request("GET", item.path)
                if not _names(operation, item.file):
                    raise ClassificationError(
                        f"{item.path!r} is not the package file of {item.file.name} "
                        f"{item.file.version}"
                    )
                decision = evaluate(operation, policy, ceiling=ceiling, floor=floor)
            except ClassificationError as exc:
                decision = Decision(
                    action=Action.DENY,
                    layer="classification",
                    reason=str(exc),
                    policy_digest=policy.digest,
                )
        decisions.append(decision)
        files.append(
            item.file.model_copy(update={"action": decision.action.value, "rule": decision.rule})
        )
    return Decided(tuple(files), tuple(item.path for item in resolved), tuple(decisions))


def _names(operation: PackageOperation, file: ResolvedFile) -> bool:
    """Whether a classified path is the download of ``file``: a package file of its
    ecosystem, with its name and, for Conda, its channel."""
    if operation.kind != "artifact" or operation.ecosystem != file.ecosystem:
        return False
    if file.ecosystem == "conda":
        return (operation.channel, operation.name) == (file.channel, file.name)
    return operation.name == normalize_pypi(file.name)


def index_refusal(
    conda: Sequence[str],
    pip: Sequence[str],
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
) -> Decision | None:
    """The policy's refusal of the index reads a dry run needs, if it refuses one: a
    channel the policy does not list (CP-F4), or an ecosystem it denies."""
    paths: list[str] = []
    for spec in conda:
        channel, _ = spec_name(spec)
        paths.append(f"/conda/{channel or DEFAULT_CHANNEL}/noarch/repodata.json")
    paths += [f"/pypi/simple/{pip_name(spec)}/" for spec in pip]
    for path in dict.fromkeys(paths):
        try:
            decision = evaluate(
                classify_package_request("GET", path), policy, ceiling=ceiling, floor=floor
            )
        except ClassificationError as exc:
            return Decision(
                action=Action.DENY,
                layer="classification",
                reason=str(exc),
                policy_digest=policy.digest,
            )
        if decision.action is Action.DENY:
            return decision
    return None


def agpl_refused(files: Iterable[ResolvedFile], policy: LoadedPolicy) -> list[ResolvedFile]:
    """The AGPL-licensed files the project does not explicitly allow (CP-9)."""
    allowed = policy.policy.packages.agpl_exceptions
    refused: list[ResolvedFile] = []
    for file in files:
        if not file.agpl:
            continue
        if file.ecosystem == "conda" and file.name in allowed.conda:
            continue
        if file.ecosystem == "pypi" and normalize_pypi(file.name) in allowed.pypi:
            continue
        refused.append(file)
    return refused


# After an install.


def failure_stage(text: str, changed: bool) -> Stage:
    """Where a failed install stopped, from its output and whether the packages changed.

    A post-link script says so in its error; a change means linking had begun; an
    unchanged environment failed before linking, most often downloading, unless the
    output tells of a solve that failed.
    """
    lowered = text.lower()
    if "post-link" in lowered or "post_link" in lowered or "postlink" in lowered:
        return "post-link"
    if changed:
        return "link"
    refused = "403" in lowered or "approval_required" in lowered
    if not refused and any(word in lowered for word in _SOLVE_WORDS):
        return "resolve"
    return "download"


def package_changes(before: Lockfile | None, after: Lockfile) -> list[PackageChange]:
    """How the packages differ between two lockfiles, by name: added, removed or changed."""
    changes: list[PackageChange] = []
    old = {p.name: p for p in before.conda} if before is not None else {}
    new = {p.name: p for p in after.conda}
    for name in sorted(old.keys() | new.keys()):
        a, b = old.get(name), new.get(name)
        if a is not None and b is not None and (a.version, a.build) == (b.version, b.build):
            continue
        changes.append(
            PackageChange(
                ecosystem="conda",
                name=name,
                before=a.version if a else None,
                after=b.version if b else None,
                build_before=a.build if a else None,
                build_after=b.build if b else None,
            )
        )
    old_pip = (
        {normalize_pypi(d.name): d for d in before.pip if d.location == "prefix"} if before else {}
    )
    new_pip = {normalize_pypi(d.name): d for d in after.pip if d.location == "prefix"}
    for name in sorted(old_pip.keys() | new_pip.keys()):
        a, b = old_pip.get(name), new_pip.get(name)
        if a is not None and b is not None and a.version == b.version:
            continue
        either = b if b is not None else a
        changes.append(
            PackageChange(
                ecosystem="pypi",
                name=either.name if either is not None else name,
                before=a.version if a else None,
                after=b.version if b else None,
            )
        )
    return changes


def unplanned(
    changes: Iterable[PackageChange],
    files: Iterable[ResolvedFile],
    lockfile: Lockfile | None = None,
) -> list[str]:
    """Packages an install added or changed that the decided set does not hold: by name and
    version, and for Conda by build and, from ``lockfile``, channel too, since micromamba
    2.9 keeps a pin to its name, version and build but not to its channel."""
    decided = list(files)
    conda = {f.name: f for f in decided if f.ecosystem == "conda"}
    pip = {(normalize_pypi(f.name), f.version) for f in decided if f.ecosystem == "pypi"}
    channels = {p.name: p.channel for p in lockfile.conda} if lockfile is not None else {}
    found: list[str] = []
    for change in changes:
        if change.after is None:
            continue
        if change.ecosystem == "pypi":
            if (normalize_pypi(change.name), change.after) not in pip:
                found.append(f"pypi:{change.name} {change.after}")
            continue
        file = conda.get(change.name)
        same = file is not None and file.version == change.after
        if same and file is not None and file.build is not None:
            same = file.build == change.build_after
        channel = channels.get(change.name)
        if same and file is not None and file.channel is not None and channel is not None:
            same = file.channel == channel
        if not same:
            found.append(f"conda:{change.name} {change.after}")
    return found


Kind = Literal["conda", "pypi"]


def requested_names(conda: Sequence[str], pip: Sequence[str]) -> list[tuple[Kind, str]]:
    """The packages a request names, for their version checks."""
    names: list[tuple[Kind, str]] = []
    for spec in conda:
        names.append(("conda", spec_name(spec)[1]))
    for spec in pip:
        names.append(("pypi", pip_name(spec)))
    return list(dict.fromkeys(names))


def installed_version(lockfile: Lockfile, kind: Kind, name: str) -> str | None:
    """The version a lockfile lists for a package, as ``version build`` for Conda."""
    if kind == "conda":
        for package in lockfile.conda:
            if package.name == name:
                return f"{package.version} {package.build}"
        return None
    for distribution in lockfile.pip:
        if normalize_pypi(distribution.name) == name:
            return distribution.version
    # A distribution a Conda package installed is the Conda package's, in the lockfile.
    for package in lockfile.conda:
        if normalize_pypi(package.name) == name:
            return f"{package.version} (the Conda package {package.name})"
    return None
