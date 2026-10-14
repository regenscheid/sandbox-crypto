# SPDX-License-Identifier: MIT
"""Keep raw lab evidence and other bulky files out of git.

Lab reports stay in git; their raw evidence (JSON records, screenshots, archives) is kept
outside it and named in the reports by SHA-256 (.agents/CONVENTIONS.md). This check fails
when a tracked file is raw evidence under `.agents/docs`, or larger than MAX_BYTES.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Final

MAX_BYTES: Final = 1024 * 1024
EVIDENCE_SUFFIXES: Final = frozenset(
    {".json", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".gz", ".tar", ".log"}
)
EVIDENCE_ALLOWED: Final = frozenset({".agents/docs/INVENTORY.json"})
"""Tracked files under `.agents/docs` that look like evidence but are not."""
EVIDENCE_ALLOWED_DIRS: Final = (".agents/docs/current/examples/", ".agents/docs/history/")


def problems(listing: list[tuple[str, int]]) -> list[str]:
    """What is wrong with the tracked files ``listing`` (path, size)."""
    found: list[str] = []
    for path, size in listing:
        suffix = PurePosixPath(path).suffix.lower()
        if (
            path.startswith(".agents/docs/")
            and suffix in EVIDENCE_SUFFIXES
            and path not in EVIDENCE_ALLOWED
            and not path.startswith(EVIDENCE_ALLOWED_DIRS)
        ):
            found.append(f"{path}: raw evidence belongs outside git; name it by SHA-256")
        elif size > MAX_BYTES:
            found.append(f"{path}: {size} bytes, over the {MAX_BYTES}-byte limit")
    return found


def tracked() -> list[tuple[str, int]]:
    """Every tracked file with its size in the index."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is not on PATH")
    out = subprocess.run(  # noqa: S603 -- fixed arguments, git resolved on PATH
        [git, "ls-files", "-z", "--format=%(objectsize)\t%(path)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    listing: list[tuple[str, int]] = []
    for entry in filter(None, out.split("\0")):
        size, path = entry.split("\t", 1)
        listing.append((path, int(size)))
    return listing


def main() -> int:
    found = problems(tracked())
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
