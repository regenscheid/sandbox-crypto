# SPDX-License-Identifier: MIT
"""Create only the fixed empty state directories required by trusted service Pods."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

LAYOUTS = {
    "app": ("control",),
    "pool": ("worker", "middleware"),
    "runtime-manager": ("runtime",),
}
"""Each Pod's fixed state directories; the runtime manager's is its own PVC (KRB-9)."""


def initialize(root: Path, component: str, *, package_cache: bool = False) -> None:
    """Initialize a mounted volume without following links or modifying existing state."""
    if component not in LAYOUTS or (package_cache and component != "pool"):
        raise ValueError("unsupported service state layout")
    directories = LAYOUTS[component]
    if package_cache:
        directories += ("package-cache",)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root, flags)
    try:
        for name in directories:
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            else:
                os.fsync(descriptor)
            # Existing regular directories are retained exactly. Symlinks, files
            # and other collisions must not redirect initialization or be reset.
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(child)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--component", choices=tuple(LAYOUTS), required=True)
    parser.add_argument("--package-cache", action="store_true")
    args = parser.parse_args()
    try:
        initialize(args.root, args.component, package_cache=args.package_cache)
    except (OSError, ValueError):
        print(
            "service state initialization failed; check mounted volume and fixed directories",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
