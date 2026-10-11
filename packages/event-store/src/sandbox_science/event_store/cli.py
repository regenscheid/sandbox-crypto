# SPDX-License-Identifier: MIT
"""Command line: verify a store, take a checkpoint of it, export its records.

```
sandbox-science-events verify STORE [--checkpoint FILE ...]
sandbox-science-events checkpoint STORE [--checkpoint FILE ...] [--output FILE]
sandbox-science-events export STORE [--after SEQ] [--output FILE]
```

``verify`` exits with status 1 if the store is not intact or disagrees with a
checkpoint. ``checkpoint`` verifies the store first and writes nothing unless
it is intact, so a checkpoint never vouches for a broken chain. Keep
checkpoints where whoever can write the store cannot change them
(docs/EVENT-STORE.md). A checkpoint file holds one checkpoint, or one per
line, as an export of the journal that a timer logged them to does.
``export`` writes one JSON object per line.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO

from .errors import EventStoreError, RecordError
from .records import Checkpoint
from .store import StoreReader
from .verify import Verification, verify_store


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-events``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-events")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify", help="check the hash chain, and the store against checkpoints"
    )
    checkpoint = commands.add_parser(
        "checkpoint", help="verify the store, then write its head as a checkpoint"
    )
    for command in (verify, checkpoint):
        command.add_argument("store", type=Path, help="the store's database file")
        command.add_argument(
            "--checkpoint",
            dest="checkpoints",
            type=Path,
            action="append",
            default=[],
            metavar="FILE",
            help="checkpoints the store must agree with: one, or one per line (repeatable)",
        )
    checkpoint.add_argument("--output", type=Path, help="write here instead of to stdout")
    export = commands.add_parser("export", help="write the store's records as JSON lines")
    export.add_argument("store", type=Path, help="the store's database file")
    export.add_argument(
        "--after", type=int, default=0, metavar="SEQ", help="only records after this one"
    )
    export.add_argument("--output", type=Path, help="write here instead of to stdout")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "verify":
                return _verify(args.store, _checkpoints(args.checkpoints))
            case "checkpoint":
                return _checkpoint(args.store, _checkpoints(args.checkpoints), args.output)
            case _:
                return _export(args.store, args.after, args.output)
    except (EventStoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _verify(store: Path, checkpoints: Sequence[Checkpoint]) -> int:
    result = verify_store(store, checkpoints)
    _report(result, sys.stdout)
    if not result.ok:
        return 1
    agreed = f"; agrees with {len(checkpoints)} checkpoints" if checkpoints else ""
    print(f"ok: {result.records} records, head {result.head.seq} {result.head.hash}{agreed}")
    return 0


def _checkpoint(store: Path, checkpoints: Sequence[Checkpoint], output: Path | None) -> int:
    result = verify_store(store, checkpoints)
    if not result.ok:
        _report(result, sys.stderr)
        return 1
    with _output(output) as stream:
        stream.write(result.head.to_json().encode() + b"\n")
    return 0


def _export(store: Path, after: int, output: Path | None) -> int:
    with StoreReader(store) as reader, _output(output) as stream:
        try:
            for record in reader.records(after):
                stream.write(record.to_json().encode() + b"\n")
        except RecordError as exc:
            raise EventStoreError(
                f"record {exc.seq} is malformed ({exc}); verify the store"
            ) from exc
    return 0


def _report(result: Verification, stream: TextIO) -> None:
    for problem in result.problems:
        print(problem, file=stream)
    if result.truncated:
        print("(more problems not listed)", file=stream)
    if not result.ok:
        print(f"failed: {len(result.problems)} problems in {result.records} records", file=stream)


def _checkpoints(paths: Sequence[Path]) -> list[Checkpoint]:
    """The checkpoints in each file: one, in any JSON layout, or one per line.

    In a file of lines, such as checkpoints exported from the journal, lines that
    do not start with ``{`` are skipped, as the journal's own messages would be. A
    file must hold at least one checkpoint, so a damaged one is never read as none.
    """
    checkpoints: list[Checkpoint] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            checkpoints.append(Checkpoint.from_json(text))
            continue
        except EventStoreError as exc:
            lines = [(n, line) for n, line in enumerate(text.splitlines(), 1) if line[:1] == "{"]
            if not lines:
                raise EventStoreError(f"{path}: {exc}") from exc
        for number, line in lines:
            try:
                checkpoints.append(Checkpoint.from_json(line))
            except EventStoreError as exc:
                raise EventStoreError(f"{path}: line {number}: {exc}") from exc
    return checkpoints


@contextmanager
def _output(path: Path | None) -> Generator[BinaryIO, None, None]:
    """Standard output, or a file that appears only once it is completely written."""
    if path is None:
        sys.stdout.flush()
        yield sys.stdout.buffer
        sys.stdout.buffer.flush()
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            yield stream
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
