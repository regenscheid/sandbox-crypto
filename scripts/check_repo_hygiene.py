# SPDX-License-Identifier: MIT
"""Keep raw lab evidence and other bulky files out of git, and the docs' links whole.

Lab reports stay in git; their raw evidence (JSON records, screenshots, archives) is kept
outside it and named in the reports by SHA-256 (.agents/CONVENTIONS.md). This check fails
when a tracked file is raw evidence under `.agents/docs`, or larger than MAX_BYTES.

It also fails when a relative link in a tracked Markdown file names a file or a heading
that does not exist, and when a page in `docs/` is missing from the index, docs/README.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Final

MAX_BYTES: Final = 1024 * 1024
EVIDENCE_SUFFIXES: Final = frozenset(
    {".json", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".gz", ".tar", ".log"}
)
EVIDENCE_ALLOWED: Final = frozenset({".agents/docs/INVENTORY.json"})
"""Tracked files under `.agents/docs` that look like evidence but are not."""
EVIDENCE_ALLOWED_DIRS: Final = (".agents/docs/current/examples/", ".agents/docs/history/")

LINK: Final = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING: Final = re.compile(r"#{1,6}\s+(.*?)\s*#*\s*$")
SCHEME: Final = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
DOCS_INDEX: Final = "docs/README.md"


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


def anchors(text: str) -> set[str]:
    """The anchors GitHub gives a Markdown text's headings."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        if match := HEADING.match(line):
            heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1).strip().lower())
            heading = re.sub(r"[`*~]", "", heading)
            slug = "".join(c for c in heading if c.isalnum() or c in " -_").replace(" ", "-")
            seen = counts.get(slug, 0)
            counts[slug] = seen + 1
            found.add(slug if seen == 0 else f"{slug}-{seen}")
        found.update(re.findall(r'<a (?:name|id)="([^"]+)"', line))
    return found


def _normalized(path: PurePosixPath) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
        elif part != ".":
            parts.append(part)
    return str(PurePosixPath(*parts)) if parts else "."


def link_problems(pages: dict[str, str], exists: set[str]) -> list[str]:
    """Relative links in the Markdown ``pages`` (path to text) to paths not in ``exists``,
    or to headings their target lacks. Links inside code are not links."""
    found: list[str] = []
    for path, text in pages.items():
        prose = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.DOTALL))
        for target in LINK.findall(prose):
            if SCHEME.match(target):
                continue
            name, _, fragment = target.partition("#")
            key = _normalized(PurePosixPath(path).parent / name) if name else path
            if key not in exists:
                found.append(f"{path}: the link {target} names no tracked file")
            elif fragment and key in pages and fragment not in anchors(pages[key]):
                found.append(f"{path}: the link {target} names no heading")
    return found


def index_problems(pages: dict[str, str]) -> list[str]:
    """Pages directly in docs/ that the index does not link to."""
    index = pages.get(DOCS_INDEX)
    if index is None:
        return [f"{DOCS_INDEX} is missing"]
    linked = {target.partition("#")[0] for target in LINK.findall(index)}
    return [
        f"{path}: not listed in {DOCS_INDEX}"
        for path in sorted(pages)
        if str(PurePosixPath(path).parent) == "docs"
        and path != DOCS_INDEX
        and PurePosixPath(path).name not in linked
    ]


def docs_problems(listing: list[tuple[str, int]]) -> list[str]:
    """Broken relative links and unindexed pages among the tracked files ``listing``."""
    exists = {path for path, _ in listing}
    exists |= {str(parent) for path in exists for parent in PurePosixPath(path).parents}
    pages = {
        path: Path(path).read_text(encoding="utf-8")
        for path, _ in listing
        if path.endswith(".md") and Path(path).is_file()
    }
    return link_problems(pages, exists) + index_problems(pages)


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
    listing = tracked()
    found = problems(listing) + docs_problems(listing)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
