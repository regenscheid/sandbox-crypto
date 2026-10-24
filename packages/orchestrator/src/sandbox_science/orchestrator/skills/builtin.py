# SPDX-License-Identifier: MIT
"""The built-in skills (docs/specs/skills.md, SK-40): an ordinary Markdown pack in the
repository, ``skills/builtin/<name>/SKILL.md``, shipped and versioned with the harness.

They go through the same import, report and pin as any pack, as the source ``builtin``,
with the platform as their author. The import's ref names the harness version and the
snapshot, since the files have no commit of their own. Every project pins the version
that ships: a run's start imports the files again when they changed and moves the
project's pin to them, for that run and those after it (SK-5). An installed harness
reads them as its package data, ``builtin_pack/``, which the wheel takes from the
repository's directory; a checkout reads that directory. ``ROLES`` says which roles'
runs get each, as the tools it names are theirs.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import logging
import stat
from pathlib import Path
from typing import Final

from sandbox_science.orchestrator.documents.models import Author

from .packs import snapshot_sha256
from .sources import Fetched, PackFile

_log = logging.getLogger(__name__)

BUILTIN: Final = "builtin"
"""The built-in skills' source, a name no imported pack may take."""
PLATFORM: Final = Author(kind="platform", service="skills")
PACKAGED: Final = "builtin_pack"
"""Their directory in the installed package (pyproject.toml's ``force-include``)."""
ROLES: Final[dict[str, tuple[str, ...]]] = {
    "computation": ("main", "delegated"),
    "delegating-work": ("main", "delegated"),
    "environment-setup": ("main",),
    "figures": ("main",),
    "literature-search": ("main",),
    "pdf-reading": ("main",),
    "recording-results": ("main",),
    "scientific-writing": ("main", "delegated"),
    "writing-documents": ("main", "delegated"),
}
"""Each built-in skill, and the roles whose runs get it: those whose tools it names
(test_builtin_skills.py checks them). A skill not listed is a main run's."""
DELEGATING: Final = frozenset({"delegating-work"})
"""The built-in skills that teach delegating, which a run without the delegate tools, such
as a check or a task at the last level, does not get."""


def _root() -> Path:
    """The package data of an installed harness, or the checkout's directory."""
    packaged = Path(str(importlib.resources.files(__package__ or __name__) / PACKAGED))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[6] / "skills" / BUILTIN


ROOT: Path | None = _root()
"""Where the shipped pack is; ``None`` ships none on purpose, as the tests that need
none have it."""


def builtin_pack() -> Fetched | None:
    """The built-in skills as the harness ships them, or ``None`` when it ships none, which
    is logged as an error unless ``ROOT`` is ``None``: runs then keep the pin they had."""
    if ROOT is None:
        return None
    if not ROOT.is_dir():
        _log.error("the harness ships no built-in skills at %s; runs keep the pin they had", ROOT)
        return None
    files: list[PackFile] = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not path.is_symlink():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            files.append(PackFile(path.relative_to(ROOT).as_posix(), path.read_bytes(), executable))
    if not files:
        _log.error("the built-in skills at %s have no files; runs keep the pin they had", ROOT)
        return None
    try:
        version = importlib.metadata.version("sandbox-science-orchestrator")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    ref = f"{version}+{snapshot_sha256(tuple(files))[:12]}"
    return Fetched(BUILTIN, "sandbox-science", f"skills/{BUILTIN}", tuple(files), (), ref=ref)
