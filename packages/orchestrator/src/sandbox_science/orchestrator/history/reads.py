# SPDX-License-Identifier: MIT
"""The read register (docs/specs/context-management.md, CM-12): how much of each source
a session read, observed from its tool records, never the agent's statement.

A source is a PDF (by its SHA-256), a file the session read or a sample's saved file (by
its SHA-256), an ePrint paper, a fetched web page (by its URL), a NIST publication, or a
paper a connector names. Each tool result that read one is kept as a ``Read`` beside its
archived message (``Archive.append``), with what this read covered:

- ``pdf_pages``: the pages returned, of the PDF's page count; a page cut short does not
  count as read. ``pdf_search`` reads lines, not pages: ``partial``;
- ``read_file``: the byte range it showed, of the file's size; ``whole`` when it showed
  every byte, as a delegated run's whole read is counted (PD-18);
- a shell or cell call whose output was sampled: the saved file, ``preview_only``;
- a connector tool: what its descriptor says (``descriptors``), ``unknown`` without one.

``merged`` puts a source's reads together, as of the latest: ``whole`` when one read was
whole or their pages, byte ranges or character ranges together cover it; otherwise the
best of ``partial`` (with what they covered), ``abstract_only``, ``preview_only`` and
``unknown``. ``Register`` reads them for a session or a reference (LG-15), with the share
of reads no descriptor could place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlStore

Json = dict[str, Any]

EXTENTS: Final = ("whole", "partial", "abstract_only", "preview_only", "unknown")
"""A read's extents, from the most of a source to what says nothing of it."""
KINDS: Final = ("pdf", "file", "eprint", "web", "nist", "paper", "connector")
"""The kinds of source."""
CONTENT_KEYED: Final = frozenset({"pdf", "file"})
"""The kinds whose key is their content's digest: every read of one reads the same."""
PDF_TOOLS: Final = ("pdf_pages", "pdf_search")
MAX_DETAIL: Final = 16384
"""The largest detail a read keeps, as JSON; a larger one keeps its extent alone."""


@dataclass(frozen=True)
class Read:
    """What one tool result read of one source."""

    kind: str
    source: str
    """The source's key in its kind: a SHA-256, a paper's ID, a page's URL."""
    extent: str
    detail: Json = field(default_factory=dict[str, Any])
    """What it covered: ``pages`` with ``of``, byte ``ranges`` with ``size``, character
    ``chars`` with ``end``, or ``passages``."""
    label: str | None = None
    """How a reader knows the source: its path, title or URL."""


def reads_of(
    tool: str,
    arguments: str,
    record: Mapping[str, Any],
    text: str,
    saved: Mapping[str, str] = {},
) -> list[Read]:
    """The sources one tool call read, from its record and its result as the model got
    it; none for a call that failed or read none."""
    if "error" in record:
        return []
    try:
        given: object = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        given = {}
    args = cast(Json, given) if isinstance(given, dict) else {}
    found: list[Read] = []
    if tool in PDF_TOOLS and isinstance(record.get("pdf"), dict):
        found += _pdf(tool, cast(Json, record["pdf"]))
    elif isinstance(record.get("read"), dict) and "sha256" in record["read"]:
        found += _file(cast(Json, record["read"]))
    elif "connector" in record:
        from .descriptors import descriptor  # which reads this module's Read

        describe = descriptor(tool)
        placed = describe(args, text) if describe is not None else None
        if placed is None:
            identity = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
            placed = [Read("connector", f"{tool}:{identity[:16]}", "unknown", label=tool)]
        found += placed
    cut = record.get("cut")
    if isinstance(cut, dict):
        found = [_cut(read, cast(Json, cut)) for read in found]
    listed = cast(Json, record.get("recorded_files") or {}).get("files")
    recorded = {
        str(cast(Json, f).get("path")).removeprefix("./"): cast(Json, f)
        for f in cast(list[object], listed if isinstance(listed, list) else [])
        if isinstance(f, dict)
    }
    for path, content in saved.items():  # a sample's saved file, which it previews
        # By the revision its call recorded, as read_file will report it; else by its text.
        revision = recorded.get(path.removeprefix("./")) or {}
        digest = revision.get("sha256")
        size = revision.get("size")
        if not isinstance(digest, str):
            digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
            size = len(content.encode("utf-8", "replace"))
        found.append(Read("file", digest, "preview_only", {"size": size}, label=path))
    return found


def _cut(read: Read, cut: Json) -> Read:
    """A read whose result the toolbox cut before the model saw it: never ``whole``,
    and none of its pages or ranges count toward the whole, since the model saw only the
    head and tail of what the tool returned."""
    extent = "partial" if read.extent == "whole" else read.extent
    kept = {k: v for k, v in read.detail.items() if k in ("document", "final", "query")}
    detail: Json = {**kept, "cut": {"length": cut.get("length"), "shown": cut.get("shown")}}
    return Read(read.kind, read.source, extent, detail, label=read.label)


def _pdf(tool: str, result: Json) -> list[Read]:
    source = cast(Json, result.get("source") or {})
    sha256, path = source.get("sha256"), str(source.get("path") or "")[:1024] or None
    if not isinstance(sha256, str) or len(sha256) != 64:
        return []
    if tool == "pdf_search":
        hits = cast(list[object], result.get("hits") or [])
        return [Read("pdf", sha256, "partial", {"hits": len(hits)}, label=path)]
    pages = [
        cast(Json, p) for p in cast(list[object], result.get("pages") or []) if isinstance(p, dict)
    ]
    read = sorted(
        index
        for page in pages
        if type(index := page.get("index")) is int and not page.get("truncated")
    )
    count = result.get("page_count")
    detail: Json = {"pages": read, "of": count if type(count) is int else None}
    return [Read("pdf", sha256, _covered(detail), detail, label=path)]


def _file(read: Json) -> list[Read]:
    sha256, size = read.get("sha256"), read.get("size")
    offset, shown = read.get("offset"), read.get("shown")
    if not isinstance(sha256, str) or type(offset) is not int or type(shown) is not int:
        return []
    if shown <= 0:
        return []  # binary, or not shown: nothing of it was read
    detail: Json = {
        "ranges": [[offset, offset + shown]],
        "size": size if type(size) is int else None,
    }
    extent = "whole" if read.get("complete") is True else _covered(detail)
    return [Read("file", sha256, extent, detail, label=str(read.get("path") or "")[:1024] or None)]


def merged(reads: Iterable[Read]) -> Read | None:
    """A source's reads put together, in order: the last label, and its extent and
    detail as all of them covered it."""
    found = list(reads)
    if not found:
        return None
    pages: set[int] = set()
    ranges: list[list[int]] = []
    chars: list[list[int]] = []
    whole, end, of, size = False, False, None, None
    for read in found:
        whole = whole or read.extent == "whole"
        detail = read.detail
        pages.update(p for p in cast(list[Any], detail.get("pages") or []) if type(p) is int)
        ranges += cast(list[list[int]], detail.get("ranges") or [])
        chars += cast(list[list[int]], detail.get("chars") or [])
        end = end or detail.get("end") is True
        of = detail.get("of") if type(detail.get("of")) is int else of
        size = detail.get("size") if type(detail.get("size")) is int else size
    last = found[-1]
    label = next((r.label for r in reversed(found) if r.label), None)
    detail: Json = {}
    if pages:
        detail.update(pages=sorted(pages), of=of)
    if ranges:
        detail.update(ranges=_union(ranges), size=size)
    if chars:
        detail.update(chars=_union(chars), end=end)
    extent = "whole" if whole else _covered(detail) if detail else None
    if extent is None or extent == "unknown":
        extent = min((r.extent for r in found), key=EXTENTS.index)
    return Read(last.kind, last.source, extent, detail, label=label)


def _covered(detail: Json) -> str:
    """``whole`` when a read's pages, bytes or characters cover their source, else
    ``partial``."""
    pages, of = cast(list[int], detail.get("pages") or []), detail.get("of")
    if pages and type(of) is int and of > 0 and set(range(1, of + 1)) <= set(pages):
        return "whole"
    ranges, size = cast(list[list[int]], detail.get("ranges") or []), detail.get("size")
    if ranges and type(size) is int and _reaches(ranges, size):
        return "whole"
    chars = cast(list[list[int]], detail.get("chars") or [])
    if chars and detail.get("end") is True and _reaches(chars, max(e for _, e in chars)):
        return "whole"
    return "partial"


def _union(ranges: Sequence[Sequence[int]]) -> list[list[int]]:
    joined: list[list[int]] = []
    for start, end in sorted((int(s), int(e)) for s, e in ranges):
        if joined and start <= joined[-1][1]:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, end])
    return joined


def _reaches(ranges: Sequence[Sequence[int]], size: int) -> bool:
    """Whether the ranges, together, cover ``[0, size)``."""
    joined = _union(ranges)
    return bool(joined) and joined[0][0] == 0 and joined[0][1] >= size


def encoded(read: Read) -> str:
    """A read's detail as the register keeps it, within ``MAX_DETAIL``."""
    text = json.dumps(read.detail, separators=(",", ":"), sort_keys=True)
    return text if len(text) <= MAX_DETAIL else "{}"


class Register:
    """The read register in one control store, which its owner closes."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def sources(
        self, project: str, *, session: str | None = None, keys: Iterable[tuple[str, str]] = ()
    ) -> list[Read]:
        """The project's sources, or a session's, or those of ``keys`` (kind, source),
        each as its reads put together, in the order first read."""
        wanted = list(dict.fromkeys(keys))
        query = "SELECT kind, source, extent, detail, label FROM source_reads WHERE project = :p"
        params: dict[str, object] = {"p": project, "keys": json.dumps([list(k) for k in wanted])}
        if session is not None:
            query += " AND session = :session"
            params["session"] = session
        if wanted:
            # A fetched page counts under the URL asked for and the one it ended at.
            query += (
                " AND ((kind, source) IN (SELECT json_extract(value, '$[0]'), "
                "json_extract(value, '$[1]') FROM json_each(:keys)) OR (kind = 'web' AND "
                "json_extract(detail, '$.final') IN (SELECT json_extract(value, '$[1]') "
                "FROM json_each(:keys) WHERE json_extract(value, '$[0]') = 'web')))"
            )
        with self.control.read() as tx:
            rows = tx.sql.execute(query + " ORDER BY rowid", params).fetchall()
        grouped: dict[tuple[str, str], list[Read]] = {}
        for kind, source, extent, detail, label in rows:
            grouped.setdefault((kind, source), []).append(
                Read(kind, source, extent, cast(Json, json.loads(detail)), label=label)
            )
        return [m for reads in grouped.values() if (m := merged(reads)) is not None]

    def of_messages(self, project: str, messages: Sequence[str]) -> list[tuple[str, Read]]:
        """What each of ``messages`` read, in order: its archived message's ID, and each
        source it read (a fold's markers, CM-18)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT message_id, kind, source, extent, detail, label FROM source_reads "
                "WHERE project = ? AND message_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY rowid",
                (project, json.dumps(list(messages))),
            ).fetchall()
        return [
            (message, Read(kind, source, extent, cast(Json, json.loads(detail)), label=label))
            for message, kind, source, extent, detail, label in rows
        ]

    def completed(
        self, project: str, kind: str, source: str, *, after: str | None = None
    ) -> str | None:
        """The archive message of the read, after the reads of the message ``after``,
        that made a source ``whole``, its reads put together in order; ``None`` while
        none did (CM-21a). A PDF's or a file's key is its digest, so all its reads count;
        another source's content may change, so only its reads after ``after`` do."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT rowid, message_id, extent, detail FROM source_reads WHERE project = ? "
                "AND ((kind = ? AND source = ?) OR (kind = 'web' AND ? = 'web' AND "
                "json_extract(detail, '$.final') = ?)) ORDER BY rowid",
                (project, kind, source, kind, source),
            ).fetchall()
            boundary = 0
            if after is not None:
                found = tx.sql.execute(
                    "SELECT max(rowid) FROM source_reads WHERE project = ? AND message_id = ?",
                    (project, after),
                ).fetchone()
                boundary = int(found[0]) if found and found[0] is not None else 0
        same = kind in CONTENT_KEYED
        reads: list[Read] = []
        for rowid, message, extent, detail in rows:
            if rowid <= boundary and not same:
                continue
            reads.append(Read(kind, source, extent, cast(Json, json.loads(detail))))
            whole = (found := merged(reads)) is not None and found.extent == "whole"
            if whole and rowid > boundary:
                return str(message)
        return None

    def unplaced(self, project: str, session: str) -> Json:
        """How many of a session's reads were connector reads no descriptor placed, of
        all its reads (CM-12a)."""
        with self.control.read() as tx:
            total, unknown = tx.sql.execute(
                "SELECT count(*), count(*) FILTER (WHERE extent = 'unknown') FROM source_reads "
                "WHERE project = ? AND session = ?",
                (project, session),
            ).fetchone()
        return {"reads": total, "unknown": unknown, "share": unknown / total if total else 0.0}


def shown(read: Read) -> Json:
    """A source's extent as a reader is shown it."""
    found: Json = {"kind": read.kind, "source": read.label or read.source, "extent": read.extent}
    if read.detail:
        covered = dict(read.detail)
        if "pages" in covered:  # pages as spans, such as "1-3, 7"
            covered["pages"] = spans(cast(list[int], covered["pages"]))
        found["covered"] = covered
    if read.kind == "eprint":
        found["versions"] = "any: the reads of all its versions count together"
    return found


def spans(pages: Sequence[int]) -> str:
    """Page numbers as spans: ``[1, 2, 3, 7]`` is ``1-3, 7``."""
    joined = _union([[p, p + 1] for p in pages])
    return ", ".join(f"{s}" if e == s + 1 else f"{s}-{e - 1}" for s, e in joined)


def reference_reads(control: ControlStore, project: str, fields: Mapping[str, Any]) -> list[Json]:
    """How far the project's sessions read a ``reference`` entry's sources (LG-15, as
    CM-12 amends it): its PDF, by its SHA-256 or its artifact revision's, and its ePrint,
    URL, NIST, arXiv and DOI identifiers, each as the register put its reads together."""
    from .descriptors import nist_key, paper_key, url_key  # which read this module's Read

    identifiers = cast(Json, fields.get("identifiers") or {})
    keys: list[tuple[str, str]] = []
    if isinstance(eprint := identifiers.get("eprint"), str):
        keys.append(("eprint", eprint.strip()))
    if isinstance(url := identifiers.get("url"), str):
        keys.append(("web", url_key(url)))
    if isinstance(nist := identifiers.get("nist"), str):
        keys.append(("nist", nist_key(nist)))
    for scheme in ("arxiv", "doi"):
        if isinstance(value := identifiers.get(scheme), str):
            keys.append(("paper", paper_key(f"{scheme}:{value}")))
    pdf = cast(Json, fields.get("pdf") or {})
    sha256 = pdf.get("sha256")
    if not isinstance(sha256, str) and isinstance(pdf.get("artifact_id"), str):
        with control.read() as tx:
            found = tx.sql.execute(
                "SELECT json_extract(record, '$.sha256') FROM artifact_revisions "
                "WHERE project = ? AND artifact = ? AND number = ?",
                (project, pdf["artifact_id"], pdf.get("revision")),
            ).fetchone()
        sha256 = found[0] if found is not None else None
    if isinstance(sha256, str):
        keys.append(("pdf", sha256))
    if not keys:
        return []
    return [shown(read) for read in Register(control).sources(project, keys=keys)]
