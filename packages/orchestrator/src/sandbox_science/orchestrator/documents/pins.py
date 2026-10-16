# SPDX-License-Identifier: MIT
"""References to recorded files in a revision, and their pins (figures spec, FG-6 to FG-11).

A file reference is a Markdown image whose address is a path, ``![Timing](plots/t.svg)``,
or the data path of a chart (``data.url`` in a ``vega-lite`` fence). When a revision is
saved, each reference is pinned to the recorded file's revision at that moment, and the
pins are kept beside the text, which keeps only the path (LD-2):

- ``shared/<path>`` names a file of the project's shared area; ``/sessions/<s>/<path>``,
  or ``sessions/<s>/<path>`` as the file tools and ``find_artifacts`` name it, a file of
  that session's workspace; any other relative path, a file of the text's session;
- an address with a scheme, a query or fragment, or a ``.`` or ``..`` segment is no file
  reference: as an image it stays a link that never loads;
- a reference whose block keeps its ID and whose path is unchanged keeps its pin, or the
  reason it has none, as it was resolved, in the session that resolved it, whoever saves
  the text; a new or changed one is resolved to the file's current revision, in the
  saver's session, which the pin records; a ``repin`` operation moves a pin to a named
  revision or the current one, of the file its reference was resolved to.

The parser reads only http, https and mailto addresses as links and images (LD-29), so a
path image is text to it; the references are found in each block's inline text by their
Markdown form, outside code, formulas and links. A reference that does not resolve keeps
its path, with the reason: no recorded file at that path, the file was deleted, its bytes
were not kept (RA-31), or its path needs a session the text does not have.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import unquote

from pydantic import ValidationError

from sandbox_science.contracts.artifact_inventory import ArtifactRevision, artifact_path

from .blocks import InlineText, Parsed, normalize_newlines, parse
from .errors import INVALID_REQUEST, DocumentError
from .identity import Mapped

MAX_REFERENCES: Final = 1000
"""References one revision pins; those beyond are not pinned, and a warning says so."""
MAX_CHART: Final = 64 * 1024
"""A chart's specification read for its data paths (FG-17)."""
CHART: Final = "vega-lite"

_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_ESCAPED: Final = re.compile(r"\\([!-/:-@\[-`{-~])")
_HTML: Final = re.compile(r"<!--.*?-->|<[A-Za-z/][^<>\n]*>", re.DOTALL)
"""An HTML comment, or a tag: an image written there is not one, as HTML would read it."""

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Reference:
    """One reference as the text writes it: its block, kind and path."""

    block: str
    kind: str
    """``image`` or ``chart_data``."""
    path: str

    def key(self, nth: int) -> tuple[str, str, str, int]:
        return (self.block, self.kind, self.path, nth)


def _images(inline: InlineText) -> Iterator[str]:
    """The addresses of the images in ``inline`` that the parser left as text."""
    content = inline.content
    html = [(m.start(), m.end()) for m in _HTML.finditer(content)]
    position = content.find("![")
    while position >= 0:
        escaped = len(content[:position]) - len(content[:position].rstrip("\\"))
        if (
            not escaped % 2
            and not any(low <= position < high for low, high, _ in inline.excluded)
            and not any(low <= position < high for low, high in html)
        ):
            found = _image_at(content, position)
            if found is not None:
                address, end = found
                yield address
                position = content.find("![", end)
                continue
        position = content.find("![", position + 2)


def _image_at(content: str, start: int) -> tuple[str, int] | None:
    """The address of an image written at ``start``, ``![alt](address "title")``, and
    where it ends; ``None`` when the text there is no image."""
    at, depth = start + 2, 1
    while at < len(content) and depth:
        char = content[at]
        if char == "\\":
            at += 2
            continue
        depth += {"[": 1, "]": -1}.get(char, 0)
        at += 1
    if depth or at >= len(content) or content[at] != "(":
        return None
    at += 1
    while at < len(content) and content[at] in " \t\n":
        at += 1
    if at < len(content) and content[at] == "<":
        close = content.find(">", at)
        if close < 0 or "\n" in content[at:close]:
            return None
        address, at = content[at + 1 : close], close + 1
    else:
        begin, parens = at, 0
        while at < len(content) and content[at] not in " \t\n" and ord(content[at]) > 31:
            char = content[at]
            if char == "\\":
                at += 2
                continue
            if char == "(":
                parens += 1
            elif char == ")":
                if not parens:
                    break
                parens -= 1
            at += 1
        address = content[begin:at]
    rest = content[at:]
    title = re.match(r"""\s*(?:"[^"]*"|'[^']*'|\([^()]*\))?\s*\)""", rest)
    if title is None or not address:
        return None
    return _ESCAPED.sub(r"\1", address), at + title.end()


def fence_body(text: str) -> str:
    """A fenced block's text without its opening and closing fences."""
    lines = text.split("\n")
    return "\n".join(
        lines[1:-1] if len(lines) > 1 and lines[-1].lstrip()[:3] in ("```", "~~~") else lines[1:]
    )


def chart_data(text: str) -> Iterator[str]:
    """The data paths a ``vega-lite`` fence names: every ``data`` with a ``url``."""
    body = fence_body(text)
    if len(body) > MAX_CHART:
        return
    try:
        spec = json.loads(body)
    except (ValueError, RecursionError):
        return
    stack: list[object] = [spec]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            data = fields.get("data")
            if isinstance(data, dict):
                url = cast(dict[str, object], data).get("url")
                if isinstance(url, str):
                    yield url
            stack.extend(reversed(list(fields.values())))
        elif isinstance(value, list):
            stack.extend(reversed(cast(list[object], value)))


def references(mapped: Mapped) -> list[Reference]:
    """The file references of a revision, in document order, by block."""
    return _found(mapped.parsed, mapped.block_ids)


def text_references(text: str, field: str) -> list[Reference]:
    """The file references of a text that has no blocks of its own, such as a ledger
    statement or a chat message, all under ``field`` (FG-7). A text the parser refuses,
    which the GUI shows as written, has none."""
    try:
        parsed = parse(normalize_newlines(text), documents=False)
    except DocumentError:
        return []
    return _found(parsed, [field] * len(parsed.blocks))


def _found(parsed: Parsed, ids: Sequence[str]) -> list[Reference]:
    found: list[Reference] = []
    for block in parsed.blocks:
        block_id = ids[block.index]
        for inline in block.inlines:
            found.extend(Reference(block_id, "image", path) for path in _images(inline))
        info = str(block.attrs.get("info") or "").split(" ", 1)[0]
        if block.kind == "code" and info == CHART:
            found.extend(Reference(block_id, "chart_data", path) for path in chart_data(block.text))
    return found


def project_path(path: str, session: str | None) -> str | None:
    """The artifact path a reference names; ``None`` when it is no file reference, and
    ``""`` when it names a file of the text's session and the text has none."""
    if _SCHEME.match(path) or path.startswith("//") or "?" in path or "#" in path:
        return None
    path = unquote(path)
    if path.startswith("/sessions/"):
        path = path[1:]
    elif path.startswith("/"):
        return None
    try:
        artifact_path(path)
    except ValueError:
        return None
    if path.startswith(("shared/", "sessions/")):
        return path
    return "" if session is None else f"sessions/{session}/{path}"


def _resolve(db: sqlite3.Connection, project: str, path: str, revision: int | None) -> Json:
    row = db.execute(
        "SELECT o.id, r.record FROM artifact_objects o JOIN artifact_revisions r "
        "ON r.project = o.project AND r.artifact = o.id AND r.number = coalesce(?, o.head) "
        "WHERE o.project = ? AND o.path = ?",
        (revision, project, path),
    ).fetchone()
    if row is None:
        return {"unresolved": "no_revision" if revision is not None else "no_file"}
    try:
        record = ArtifactRevision.model_validate_json(row[1])
    except ValidationError:
        return {"unresolved": "no_file"}
    if record.deleted:
        return {"unresolved": "deleted"}
    if not record.content_retained:
        return {"unresolved": "content_not_kept"}
    reference = record.reference
    return {
        "artifact": reference.artifact_id,
        "revision": reference.revision,
        "sha256": reference.sha256,
        "size": reference.size,
        "media_type": reference.media_type,
    }


def pin(
    db: sqlite3.Connection,
    project: str,
    session: str | None,
    written: Sequence[Reference],
    previous: Sequence[Json] = (),
    moves: Mapping[tuple[str, str], int | None] | None = None,
) -> tuple[list[Json], list[str]]:
    """The pins of the references ``written``, and the blocks whose pins ``moves`` moved.

    ``previous`` holds the pins of the revision it was made from; ``moves`` maps a block
    and a path to the revision a ``repin`` names, or ``None`` for the current one."""
    moves = moves or {}
    kept: dict[tuple[str, str, str, int], Json] = {}
    seen: Counter[tuple[str, str, str]] = Counter()
    for entry in previous:
        reference = Reference(str(entry["block"]), str(entry["kind"]), str(entry["path"]))
        nth = seen[(reference.block, reference.kind, reference.path)]
        seen[(reference.block, reference.kind, reference.path)] += 1
        kept[reference.key(nth)] = entry
    pins: list[Json] = []
    moved: list[str] = []
    seen.clear()
    for reference in written[:MAX_REFERENCES]:
        nth = seen[(reference.block, reference.kind, reference.path)]
        seen[(reference.block, reference.kind, reference.path)] += 1
        move = (reference.block, reference.path)
        old = kept.get(reference.key(nth))
        if old is not None and move not in moves:
            # The same reference in the same place keeps its pin, or its reason, as it was
            # resolved, in the session that resolved it, whoever saves now (FG-7, FG-11).
            pins.append(old)
            continue
        at = session if old is None else old.get("session")
        target = old.get("artifact_path") if old is not None else None
        if not target:
            target = project_path(reference.path, at if old is not None else session)
        if target is None:
            continue
        entry: Json = {"block": reference.block, "kind": reference.kind, "path": reference.path}
        entry["session"] = at
        if not target:
            if move in moves:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{reference.path} in block {reference.block} names no session's file: "
                    "write shared/… or /sessions/<s>/…",
                )
            pins.append({**entry, "unresolved": "needs_session"})
            continue
        entry["artifact_path"] = target
        if move in moves:
            found = {**entry, **_resolve(db, project, target, moves[move])}
            if "unresolved" in found:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{reference.path} in block {reference.block} cannot be pinned there: "
                    f"{str(found['unresolved']).replace('_', ' ')}",
                )
            if old is None or found.get("revision") != old.get("revision"):
                moved.append(reference.block)
            pins.append(found)
        else:
            pins.append({**entry, **_resolve(db, project, target, None)})
    named = {(str(entry["block"]), str(entry["path"])) for entry in pins}
    for block, path in moves:
        if (block, path) not in named:
            raise DocumentError(
                INVALID_REQUEST, f"block {block} holds no reference to a recorded file at {path}"
            )
    return pins, moved


_REASONS: Final = {
    "no_file": "no recorded file at that path",
    "deleted": "the file was deleted",
    "content_not_kept": "its bytes were not kept",
    "needs_session": "a relative path needs a session; write shared/… or /sessions/<s>/…",
}


def unresolved_warnings(pins: Sequence[Json], blocks: set[str]) -> list[Json]:
    """A warning for each reference in ``blocks`` that does not resolve (FG-9)."""
    return [
        {
            "kind": "unresolved_reference",
            "block": entry["block"],
            "path": entry["path"],
            "message": f"{entry['path']} is not pinned: {_REASONS[entry['unresolved']]}",
        }
        for entry in pins
        if "unresolved" in entry and entry["block"] in blocks
    ]


def newer(db: sqlite3.Connection, project: str, pins: Sequence[Json]) -> list[Json]:
    """``pins`` with each pinned file's current revision, as ``latest``, when it is newer."""
    shown: list[Json] = []
    for entry in pins:
        if "artifact" in entry:
            row = db.execute(
                "SELECT head FROM artifact_objects WHERE project = ? AND id = ?",
                (project, entry["artifact"]),
            ).fetchone()
            if row is not None and row[0] > entry["revision"]:
                entry = {**entry, "latest": row[0]}
        shown.append(entry)
    return shown
