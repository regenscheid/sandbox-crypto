# SPDX-License-Identifier: MIT
"""The built-in skills (docs/specs/skills.md, SK-40): an ordinary Markdown pack in the
repository, ``skills/builtin/<name>/SKILL.md``, shipped and versioned with the harness.

They go through the same import, report and pin as any pack, as the source ``builtin``,
with the platform as their author. The import's ref names the harness version and the
snapshot, since the files have no commit of their own. Every project pins the version
that ships: a run's start imports the files again when they changed and moves the
project's pin to them, for that run and those after it (SK-5).
"""

from __future__ import annotations

import importlib.metadata
import stat
from pathlib import Path
from typing import Final

from sandbox_science.orchestrator.documents.models import Author

from .packs import snapshot_sha256
from .sources import Fetched, PackFile

BUILTIN: Final = "builtin"
"""The built-in skills' source, a name no imported pack may take."""
PLATFORM: Final = Author(kind="platform", service="skills")
ROOT: Path = Path(__file__).resolve().parents[6] / "skills" / BUILTIN
"""Where the repository keeps them."""


def builtin_pack() -> Fetched | None:
    """The built-in skills as the harness ships them, or ``None`` when it ships none."""
    if not ROOT.is_dir():
        return None
    files: list[PackFile] = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not path.is_symlink():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            files.append(PackFile(path.relative_to(ROOT).as_posix(), path.read_bytes(), executable))
    if not files:
        return None
    try:
        version = importlib.metadata.version("sandbox-science-orchestrator")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    ref = f"{version}+{snapshot_sha256(tuple(files))[:12]}"
    return Fetched(BUILTIN, "sandbox-science", f"skills/{BUILTIN}", tuple(files), (), ref=ref)
