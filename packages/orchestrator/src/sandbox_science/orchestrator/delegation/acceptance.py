# SPDX-License-Identifier: MIT
"""What a parent read of a returned task's files before it accepted it (plans-and-
delegation spec, PD-46).

An acceptance does not wait for reads: it records, for each of the task's result files
and each deliverable its result names by path, whether the accepting run read it whole
at the revision the result names, after the task's latest return. The read register
(docs/specs/context-management.md, CM-12) says so, from the runs' tool records, never
the agent's statement: a file is keyed by its SHA-256, so a read of the handed-back path,
of the per-return copy under ``.handback/returned/<n>/`` or of the path the result names
counts alike, and its reads together are whole when one showed every byte, or their byte
ranges, or a PDF's pages, cover it (``merged``), as a delegated run's whole read of a
check's proof is counted (PD-18). Only the reads of the accepting run and of the task's
parent run count, made at or after the task's return.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, cast

from pydantic import JsonValue

from sandbox_science.orchestrator.context.history.reads import Read, merged

from .outcome import asked_of

READS_BYTES: Final = 65536
"""The most an acceptance's account of its reads takes, as UTF-8 JSON: files past it are
left out from the end, and counted."""
Json = dict[str, Any]


def _files(files: Mapping[str, JsonValue] | None) -> tuple[list[Json], list[Json]]:
    """A delegation's pinned result files and its other recorded revisions, as its
    outcome keeps them (RA-36, RA-38)."""
    found = files or {}
    results = [
        cast(Json, r) for r in cast(list[object], found.get("results") or []) if isinstance(r, dict)
    ]
    recorded = [
        cast(Json, r) for r in cast(list[object], found.get("files") or []) if isinstance(r, dict)
    ]
    return results, recorded


def _revision_of(path: str, revision: object, known: Iterable[Json]) -> Json | None:
    """The recorded revision of ``path`` a deliverable names: the one numbered
    ``revision``, or without a number the latest recorded."""
    named = path.removeprefix("./")
    same = [
        r
        for r in known
        if str(r.get("path") or "").removeprefix("./") == named and r.get("deleted") is not True
    ]
    if isinstance(revision, int):
        return next((r for r in same if r.get("revision") == revision), None)
    return same[-1] if same else None


def accounted_files(
    files: Mapping[str, JsonValue] | None,
    brief: Mapping[str, JsonValue],
    result: Mapping[str, JsonValue] | None,
) -> list[Json]:
    """The files an acceptance accounts for, in order: each result file, then each
    deliverable of the brief its result names by path that is not one of them, each with
    its path, revision and SHA-256 as the task's calls recorded it (``None`` when they
    recorded none) and the deliverables it stands for."""
    results, recorded = _files(files)
    found: list[Json] = [
        {"path": r.get("path"), "revision": r.get("revision"), "sha256": r.get("sha256")}
        for r in results
    ]
    standing: dict[int, list[str]] = {}
    """The deliverables each of ``found`` stands for, by its place."""
    kinds = {asked.id: asked.kind for asked in asked_of(brief)}
    given = cast(list[object], (result or {}).get("deliverables") or [])
    for item in (cast(Json, i) for i in given if isinstance(i, dict)):
        identity, path = str(item.get("id")), item.get("path")
        if kinds.get(identity) not in ("artifact", "file") or not isinstance(path, str):
            continue
        revision = _revision_of(path, item.get("revision"), [*results, *recorded])
        number = revision.get("revision") if revision is not None else item.get("revision")
        named = path.removeprefix("./")
        place = next(
            (
                n
                for n, f in enumerate(found)
                if str(f["path"]).removeprefix("./") == named and f["revision"] == number
            ),
            None,
        )
        if place is None:
            place = len(found)
            found.append(
                {
                    "path": path,
                    "revision": number,
                    "sha256": revision.get("sha256") if revision is not None else None,
                }
            )
        standing.setdefault(place, []).append(identity)
    for place, identities in standing.items():
        found[place]["deliverables"] = identities
    return found


def _whole(
    db: sqlite3.Connection,
    project: str,
    session: str,
    runs: Sequence[str],
    digests: Sequence[str],
    since: datetime,
) -> set[str]:
    """The SHA-256s among ``digests`` that the reads of ``runs`` in the session, archived
    at or after ``since``, showed whole together: the register's kinds keyed by a file's
    digest, a file read with ``read_file`` and a PDF read by its pages."""
    if not digests or not runs:
        return set()
    rows = db.execute(
        "SELECT s.kind, s.source, s.extent, s.detail, a.created FROM source_reads s "
        "JOIN archive_messages a ON a.project = s.project AND a.message_id = s.message_id "
        "WHERE s.project = ? AND s.session = ? AND s.kind IN ('file', 'pdf') "
        "AND s.source IN (SELECT value FROM json_each(?)) "
        "AND a.run IN (SELECT value FROM json_each(?)) ORDER BY s.rowid",
        (project, session, json.dumps(list(digests)), json.dumps(list(runs))),
    ).fetchall()
    reads: dict[tuple[str, str], list[Read]] = {}
    for kind, source, extent, detail, created in rows:
        try:
            at = datetime.fromisoformat(str(created))
        except ValueError:
            continue
        if at < since:
            continue
        reads.setdefault((str(kind), str(source)), []).append(
            Read(str(kind), str(source), str(extent), cast(Json, json.loads(detail)))
        )
    return {
        source
        for (_, source), found in reads.items()
        if (read := merged(found)) is not None and read.extent == "whole"
    }


def acceptance_reads(
    db: sqlite3.Connection,
    project: str,
    session: str,
    *,
    files: Mapping[str, JsonValue] | None,
    brief: Mapping[str, JsonValue],
    result: Mapping[str, JsonValue] | None,
    returned: datetime,
    runs: Sequence[str],
) -> dict[str, JsonValue]:
    """What an acceptance records of its reads, read in the open transaction ``db``:
    ``files``, each accounted file with whether ``runs`` read it whole after
    ``returned`` (``read``); ``unread``, how many they did not; and ``cut``, how many
    were left out to stay within ``READS_BYTES``. A file whose revision the task's calls
    did not record is never shown read."""
    found = accounted_files(files, brief, result)
    digests = sorted({f["sha256"] for f in found if isinstance(f.get("sha256"), str)})
    read = _whole(db, project, session, runs, digests, returned)
    listed: list[JsonValue] = []
    for item in found:
        shown: Json = {"path": item["path"], "revision": item["revision"]}
        if item.get("deliverables"):
            shown["deliverables"] = item["deliverables"]
        shown["read"] = isinstance(item.get("sha256"), str) and item["sha256"] in read
        listed.append(cast(JsonValue, shown))
    cut = 0
    while listed and len(json.dumps(listed).encode()) > READS_BYTES - 64:
        listed.pop()
        cut += 1
    unread = sum(1 for item in listed if not cast(Json, item)["read"])
    account: dict[str, JsonValue] = {"files": listed, "unread": unread}
    if cut:
        account["cut"] = cut
    return account


def unread_text(reads: Mapping[str, JsonValue] | None) -> str | None:
    """``accepted unread: …``, naming the files an acceptance's runs did not read whole,
    each with its revision; ``None`` when they read every one."""
    listed = cast(list[object], (reads or {}).get("files") or [])
    named = [
        f"{item.get('path')} (revision {item.get('revision')})"
        if item.get("revision") is not None
        else str(item.get("path"))
        for item in (cast(Json, i) for i in listed if isinstance(i, dict))
        if item.get("read") is not True
    ]
    cut = (reads or {}).get("cut")
    if isinstance(cut, int) and cut:
        named.append(f"{cut} more not listed")
    return f"accepted unread: {', '.join(named)}" if named else None
