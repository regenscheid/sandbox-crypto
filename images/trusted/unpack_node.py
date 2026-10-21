# SPDX-License-Identifier: MIT
"""Build-only extraction of the checksum-pinned Node archive with Python's data filter."""

import argparse
import tarfile
import tempfile
from pathlib import Path


def unpack(archive: Path, destination: Path) -> None:
    """Expose the archive's one directory only after safe, complete extraction."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("Node destination must not exist")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".node-unpack-") as working:
        root = Path(working)
        with tarfile.open(archive, mode="r:xz") as stream:
            stream.extractall(root, filter="data")
        entries = list(root.iterdir())
        if len(entries) != 1 or entries[0].is_symlink() or not entries[0].is_dir():
            raise ValueError("Node archive must contain exactly one literal directory")
        entries[0].rename(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    unpack(args.archive, args.destination)


if __name__ == "__main__":
    main()
