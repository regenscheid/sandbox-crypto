# SPDX-License-Identifier: MIT
"""The executor's file searches, ``glob`` and ``grep``, run as a helper process of their own.

The executor (executor.py) opens the directory to search through no symlink, as the
other file operations open theirs, and passes its descriptor to this file, run with
``--helper``: the request is JSON on stdin and the reply JSON on stdout. The helper walks
below that descriptor, opening one entry at a time relative to its directory, and never
follows a symlink. It depends only on the standard library.

Every search is bounded: the entries it walks, its depth, the bytes it reads, its
results, the size of its reply and its time. Past one of them it stops and says which.
A pattern is the agent's own, so a regular expression can take any time on one line:
the executor ends a helper that runs past its time, whatever it is doing.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import Any, Final, cast

Message = dict[str, Any]

MAX_SCAN_ENTRIES: Final = 20_000
"""Entries, of every kind, one search looks at."""
MAX_DEPTH: Final = 128
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
"""Larger files are left out of a grep, and counted."""
MAX_SCANNED_BYTES: Final = 64 * 1024 * 1024
"""Bytes one grep reads in all."""
MAX_REPLY_BYTES: Final = 256 * 1024
"""Bytes of paths and lines one reply carries."""
MAX_LINE_CHARS: Final = 400
MAX_GLOB_RESULTS: Final = 200
MAX_GREP_RESULTS: Final = 500
MAX_CONTEXT: Final = 10
MAX_PATTERN: Final = 1000
MAX_SKIPPED: Final = 16
MAX_REQUEST: Final = 64 * 1024
DEFAULT_SECONDS: Final = 20.0
SKIPPED: Final = frozenset({".sandbox-science-prefix", ".executor"})
"""Directories a walk does not enter: the research prefix and the executor's state. A
search whose path is inside one of them searches it."""
MODES: Final = ("files", "count", "content")
DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SearchError(Exception):
    """A refusal, with one of the tools spec's error kinds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def printable(name: str) -> str:
    """A path as one line of text: bytes that are not UTF-8 become U+FFFD, and line
    breaks and other control characters are escaped."""
    text = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


# Patterns.


def _segment(segment: str) -> str:
    """One segment of a glob as a regular expression that never crosses a ``/``."""
    out: list[str] = []
    i = 0
    while i < len(segment):
        c = segment[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = segment.find("]", i + 2 if segment[i + 1 : i + 2] in ("!", "]") else i + 1)
            if end < 0:
                out.append(re.escape(c))
            else:
                inner = segment[i + 1 : end]
                negated = inner.startswith("!")
                inner = inner[1:] if negated else inner
                body = "".join("\\" + ch if ch in "\\^[]" else ch for ch in inner)
                out.append(f"[{'^/' if negated else ''}{body}]" if body else "[^/]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def check_glob(pattern: object) -> str:
    """The contracts' ``validate_glob``."""
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise SearchError("invalid_request", "pattern must be non-empty text")
    if len(pattern) > MAX_PATTERN or pattern.startswith("/"):
        raise SearchError("invalid_request", f"invalid pattern {pattern!r}")
    if any(segment in ("", ".", "..") for segment in pattern.split("/")):
        raise SearchError(
            "invalid_request", f"pattern {pattern!r} has an empty, '.' or '..' segment"
        )
    return pattern


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A glob as a regular expression over paths: ``*``, ``?`` and ``[…]`` within one
    segment, and a segment ``**`` for any number of directories, none included."""
    parts = check_glob(pattern).split("/")
    out: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
        else:
            out.append(_segment(part) + ("" if last else "/"))
    return re.compile("".join(out), re.DOTALL)


def glob_depth(pattern: str) -> int | None:
    """How many directory levels a glob can reach, or None for any with ``**``."""
    parts = pattern.split("/")
    return None if "**" in parts else len(parts)


def file_filter(pattern: str | None) -> Callable[[str, str], bool]:
    """Whether a file is searched, by its path below the search's directory and its name:
    a glob with a ``/`` matches the path, one without matches the name at any depth."""
    if pattern is None:
        return lambda relative, name: True
    regex = glob_regex(pattern)
    if "/" in pattern:
        return lambda relative, name: regex.fullmatch(relative) is not None
    return lambda relative, name: regex.fullmatch(name) is not None


# The walk.


@dataclass
class Scan:
    """What one search has looked at, and why it stopped early, if it did."""

    deadline: float
    scanned: int = 0
    scanned_files: int = 0
    scanned_bytes: int = 0
    reply_bytes: int = 0
    unreadable: int = 0
    stopped: str | None = None
    skipped: set[str] = field(default_factory=lambda: set[str]())

    def over(self) -> bool:
        if self.stopped is None:
            if self.scanned >= MAX_SCAN_ENTRIES:
                self.stopped = "entries"
            elif time.monotonic() > self.deadline:
                self.stopped = "time"
        return self.stopped is not None


def _names(directory: int, scan: Scan) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except OSError:
        scan.unreadable += 1
        return []


Entry = tuple[str, int, str, os.stat_result]


def entries(top: int, scan: Scan, depth: int | None = None) -> Generator[Entry]:
    """Each entry below the directory ``top`` (consumed), depth first by name: its path
    below ``top``, its directory's descriptor, its name and its status, never following a
    symlink. ``depth`` levels at most, when given."""
    stack: list[tuple[int, str, Iterator[str]]] = [(top, "", iter(_names(top, scan)))]
    try:
        while stack:
            current, prefix, names = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                os.close(current)
                continue
            if scan.over():
                return
            scan.scanned += 1
            try:
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError:
                scan.unreadable += 1
                continue
            relative = prefix + name
            directory = stat.S_ISDIR(info.st_mode)
            if directory and name in SKIPPED:
                scan.skipped.add(printable(relative))
                continue
            yield relative, current, name, info
            if not directory or (depth is not None and len(stack) >= depth):
                continue
            if len(stack) >= MAX_DEPTH:
                scan.stopped = scan.stopped or "depth"
                continue
            try:
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
            except OSError:
                scan.unreadable += 1
                continue
            stack.append((child, relative + "/", iter(_names(child, scan))))
    finally:
        for current, _, _ in stack:
            os.close(current)


def _kind(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _joined(prefix: str, relative: str) -> str:
    return printable(f"{prefix}/{relative}" if prefix else relative)


def glob(top: int, request: Message, scan: Scan) -> Message:
    """The entries below ``top`` (consumed) whose paths match, newest first."""
    pattern = check_glob(request.get("pattern"))
    limit = _integer(request.get("limit", MAX_GLOB_RESULTS), 1, MAX_GLOB_RESULTS, "limit")
    prefix = str(request.get("prefix", ""))
    regex = glob_regex(pattern)
    found: list[tuple[int, str, os.stat_result]] = []
    for relative, _, _, info in entries(top, scan, glob_depth(pattern)):
        if regex.fullmatch(relative) is not None:
            found.append((-info.st_mtime_ns, relative, info))
    found.sort(key=lambda item: (item[0], item[1]))
    listed: list[Message] = []
    for _, relative, info in found[:limit]:
        path = _joined(prefix, relative)
        scan.reply_bytes += len(path.encode()) + 96
        if scan.reply_bytes > MAX_REPLY_BYTES:
            scan.stopped = scan.stopped or "reply"
            break
        listed.append(
            {"path": path, "type": _kind(info), "size": info.st_size, "modified": info.st_mtime}
        )
    if len(found) > limit and scan.stopped is None:
        scan.stopped = "results"
    return {
        "entries": listed,
        "truncated": len(listed) < len(found) or scan.stopped not in (None, "results"),
        "stopped": scan.stopped,
        "scanned": scan.scanned,
        "skipped": sorted(scan.skipped)[:MAX_SKIPPED],
    }


# grep.


def _shown(text: str) -> str:
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + "…[cut]"


@dataclass
class Found:
    """A grep's results so far, in its mode."""

    mode: str
    limit: int
    files: list[str] = field(default_factory=lambda: list[str]())
    counts: list[Message] = field(default_factory=lambda: list[Message]())
    lines: list[str] = field(default_factory=lambda: list[str]())
    results: int = 0
    binary: int = 0
    large: int = 0
    full: bool = False

    def room(self, scan: Scan, text: str) -> bool:
        """Whether the reply has room for ``text``; when not, the search stops."""
        scan.reply_bytes += len(text.encode()) + 8
        if scan.reply_bytes > MAX_REPLY_BYTES:
            scan.stopped = scan.stopped or "reply"
            return False
        return True

    def take(self, scan: Scan) -> bool:
        """Count one more result, or stop the search when it would pass the limit."""
        if self.results >= self.limit:
            scan.stopped = scan.stopped or "results"
            self.full = True
            return False
        self.results += 1
        return True


def _lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _search_file(
    path: str, lines: list[str], regex: re.Pattern[str], context: int, found: Found, scan: Scan
) -> None:
    matched: list[int] = []
    for number, line in enumerate(lines):
        if number % 1024 == 0 and scan.over():
            return
        if regex.search(line) is not None:
            matched.append(number)
            if found.mode == "files":
                break
    if not matched:
        return
    if found.mode == "files":
        if found.take(scan) and found.room(scan, path):
            found.files.append(path)
        return
    if found.mode == "count":
        if found.take(scan) and found.room(scan, path):
            found.counts.append({"path": path, "count": len(matched)})
        return
    matching = set(matched)
    shown_to = -1
    for number in matched:
        if not found.take(scan):
            return
        start = max(number - context, shown_to + 1)
        if found.lines and start > shown_to + 1 and context:
            found.lines.append("--")
        for other in range(start, min(number + context, len(lines) - 1) + 1):
            if other <= shown_to:
                continue
            mark = ":" if other in matching else "-"
            text = f"{path}{mark}{other + 1}{mark} {_shown(lines[other])}"
            if not found.room(scan, text):
                return
            found.lines.append(text)
            shown_to = other


def _read(parent: int, name: str, size: int) -> bytes | None:
    """A regular file's bytes, read through no symlink; None when it cannot be read."""
    try:
        descriptor = os.open(name, FILE_FLAGS, dir_fd=parent)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read(min(size, MAX_FILE_BYTES) + 1)


def grep(
    top: int, single: tuple[str, os.stat_result] | None, request: Message, scan: Scan
) -> Message:
    """Search the files below the directory ``top`` (consumed), or, with ``single``, the
    file of that name in it."""
    pattern = request.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN:
        raise SearchError("invalid_request", "pattern must be a regular expression")
    mode = request.get("mode", "files")
    if mode not in MODES:
        raise SearchError("invalid_request", f"mode must be one of {', '.join(MODES)}")
    context = _integer(request.get("context_lines", 0), 0, MAX_CONTEXT, "context_lines")
    limit = _integer(request.get("max_results", 100), 1, MAX_GREP_RESULTS, "max_results")
    raw_glob = request.get("glob")
    wanted = file_filter(None if raw_glob is None else check_glob(raw_glob))
    flags = re.IGNORECASE if request.get("case_insensitive") is True else 0
    try:
        regex = re.compile(pattern, flags)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        raise SearchError("invalid_request", f"pattern is not a regular expression: {exc}") from exc
    prefix = str(request.get("prefix", ""))
    found = Found(str(mode), limit)
    walk: Generator[Entry] | None = None
    candidates: Iterator[Entry]
    if single is not None:
        name, info = single
        candidates = iter([(name, top, name, info)])
        prefix = prefix.rpartition("/")[0]
    else:
        candidates = walk = entries(top, scan)
    try:
        for relative, parent, name, info in candidates:
            if not stat.S_ISREG(info.st_mode) or not wanted(relative, name):
                continue
            if info.st_size > MAX_FILE_BYTES:
                found.large += 1
                continue
            if scan.scanned_bytes + info.st_size > MAX_SCANNED_BYTES:
                scan.stopped = scan.stopped or "bytes"
                break
            data = _read(parent, name, info.st_size)
            if data is None:
                scan.unreadable += 1
                continue
            scan.scanned_files += 1
            scan.scanned_bytes += len(data)
            if len(data) > MAX_FILE_BYTES:
                found.large += 1
                continue
            if b"\x00" in data:
                found.binary += 1
                continue
            _search_file(_joined(prefix, relative), _lines(data), regex, context, found, scan)
            if scan.stopped is not None:
                break
    finally:
        if walk is not None:
            walk.close()
        else:
            os.close(top)
    return {
        "files": found.files,
        "counts": found.counts,
        "lines": found.lines,
        "truncated": scan.stopped is not None,
        "stopped": scan.stopped,
        "scanned_files": scan.scanned_files,
        "scanned_bytes": scan.scanned_bytes,
        "skipped_binary": found.binary,
        "skipped_large": found.large,
        "skipped_unreadable": scan.unreadable,
        "skipped": sorted(scan.skipped)[:MAX_SKIPPED],
    }


def _integer(value: object, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise SearchError("invalid_request", f"{name} must be a whole number from {low} to {high}")
    return value


def run(request: Message) -> Message:
    """One search, on the descriptor and name the executor passed."""
    op, descriptor, name = request.get("op"), request.get("fd"), request.get("name")
    if op not in ("glob", "grep") or type(descriptor) is not int:
        raise SearchError("invalid_request", "unknown search")
    seconds = request.get("seconds", DEFAULT_SECONDS)
    if type(seconds) not in (int, float) or not 0 < cast(float, seconds) <= 600:
        raise SearchError("invalid_request", "invalid search time")
    scan = Scan(time.monotonic() + cast(float, seconds))
    path = printable(str(request.get("prefix", "")) or ".")
    if name is None:
        top = descriptor
    else:
        if not isinstance(name, str):
            os.close(descriptor)
            raise SearchError("invalid_request", "invalid name")
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            os.close(descriptor)
            raise SearchError("not_found", f"no file or directory {path!r}") from None
        if stat.S_ISREG(info.st_mode) and op == "grep":
            return grep(descriptor, (name, info), request, scan)
        try:
            if stat.S_ISLNK(info.st_mode):
                raise SearchError("invalid_request", f"path {path!r} goes through a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise SearchError("invalid_request", f"{path!r} is not a directory")
            top = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
        finally:
            os.close(descriptor)
    if op == "glob":
        return glob(top, request, scan)
    return grep(top, None, request, scan)


def main() -> int:
    if sys.argv[1:] != ["--helper"]:
        print("usage: sandbox_search.py --helper < request.json", file=sys.stderr)
        return 2
    try:
        raw: object = json.loads(sys.stdin.buffer.read(MAX_REQUEST + 1)[:MAX_REQUEST])
        if not isinstance(raw, dict):
            raise SearchError("invalid_request", "the request is not an object")
        reply: Message = {"ok": True, "result": run(cast(Message, raw))}
    except SearchError as error:
        reply = {"ok": False, "error": {"kind": error.kind, "message": error.message[:500]}}
    except (ValueError, RecursionError) as exc:
        reply = {"ok": False, "error": {"kind": "invalid_request", "message": str(exc)[:500]}}
    except OSError as exc:
        reply = {"ok": False, "error": {"kind": "invalid_request", "message": str(exc)[:500]}}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
