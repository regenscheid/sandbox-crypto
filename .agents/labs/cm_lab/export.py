# SPDX-License-Identifier: MIT
"""An arm's session as one JSON bundle: what ``score`` reads.

The bundle holds the session's archived messages in order (with the files saved behind
a sampled tool result), its main runs' records as the session keeps them, the project's
ledger with every revision of every entry, and the read register's sources. It is read
from a copy of the arm's store, never the store a run is writing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.control.store import store_path
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.history.archive import Archive
from sandbox_science.orchestrator.history.reads import Register, shown
from sandbox_science.orchestrator.ledger.store import Ledger
from sandbox_science.orchestrator.projects import ProjectStore

Json = dict[str, Any]
REVISION: Final = {
    "title", "status", "statement", "fields", "links", "evidence", "anchors", "flags", "revision"
}  # fmt: skip
"""What an entry's revision keeps, as it read then."""
RECORD: Final = {
    "run_id", "task", "started_at", "ended_at", "stopped", "answer", "final_text", "usage",
    "summaries", "alias",
}  # fmt: skip
"""What a run's record keeps that the scores read."""


def rows(archive: Archive, project: str, session: str) -> list[Json]:
    """The session's archived messages, in order."""
    found: list[Json] = []
    start = 1
    while page := archive.messages(project, session, start=start):
        for row in page:
            entry = row.entry
            item: Json = {
                "position": row.position,
                "message_id": row.message_id,
                "run": row.run,
                "role": entry.role,
                "kind": entry.kind,
                "content": entry.content,
                "source": entry.source,
                "call_id": entry.call_id,
                "tool_calls": [
                    {
                        "id": call.get("id"),
                        "name": (call.get("function") or {}).get("name"),
                        "arguments": (call.get("function") or {}).get("arguments"),
                    }
                    for call in entry.tool_calls
                ],
                "native": entry.native.decode() if entry.kind == "fold" and entry.native else None,
            }
            if entry.files:
                item["saved"] = {
                    saved.path: saved.text for saved in archive.files(project, row.message_id)
                }
            found.append(item)
        start = page[-1].position + 1
    return found


def ledger(documents: DocumentStore, project: str) -> list[Json]:
    """Every entry, each revision as it read then."""
    book = Ledger(documents)
    found: list[Json] = []
    after: str | None = None
    while True:
        page = book.entries(project, after=after, limit=200)
        for summary in page.entries:
            revisions = []
            for revision in book.history(project, summary.handle):
                view = book.entry(project, summary.handle, revision.revision)
                revisions.append(
                    {
                        **view.model_dump(mode="json", include=REVISION),
                        "created": revision.created,
                        "reason": revision.reason,
                    }
                )
            found.append(
                {
                    "handle": summary.handle,
                    "entry_id": summary.entry_id,
                    "type": summary.type,
                    "kind": summary.kind,
                    "revisions": revisions,
                }
            )
        if page.next_after is None:
            return found
        after = page.next_after


def export(state: Path, meta: Json) -> Json:
    """The bundle of ``meta``'s project and session, from the store in ``state``."""
    project, session = meta["project"], meta["session"]
    documents = DocumentStore(store_path(state), producer="cm-lab", readonly=True)
    projects = ProjectStore(state, readonly=True)
    control = ControlStore(store_path(state), readonly=True)
    try:
        runs = [
            projects.run(project, session, entry.name).model_dump(mode="json", include=RECORD)
            | {"name": entry.name}
            for entry in projects.runs(project, session)
            if entry.role == "main"
        ]
        return {
            "meta": meta,
            "rows": rows(Archive(documents), project, session),
            "runs": runs,
            "ledger": ledger(documents, project),
            "reads": [shown(read) for read in Register(control).sources(project, session=session)],
        }
    finally:
        control.close()
        projects.close()
        documents.close()
