# SPDX-License-Identifier: MIT
"""The control-store table lists in the docs match the tables the stores create.

Rule: ``docs/CONSISTENCY-REVIEW.md`` lists the reviewer's tables (``review_*``, and the
math checks of finding explanations), and ``docs/LIVING-DOCUMENTS.md`` lists every other
table of the control store. Each doc's list is the first column of its first Markdown
table headed ``| Table | Holds |``; a cell may name several tables in backticks. The
tables are read from ``sqlite_master`` after a new store is opened through its public
constructor, so a table added in a migration, or one removed by it, fails here and names
the doc to update.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from sandbox_science.orchestrator.documents import DocumentStore

DOCS = Path(__file__).resolve().parents[3] / "docs"


def _listed(doc: str) -> set[str]:
    lines = (DOCS / doc).read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(r"\|\s*Table\s*\|\s*Holds\s*\|", line)),
        None,
    )
    assert start is not None, f"docs/{doc} has no '| Table | Holds |' table"
    names: set[str] = set()
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        names.update(re.findall(r"`([a-z0-9_]+)`", line.split("|")[1]))
    return names


def _tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        # A virtual table's own tables, such as FTS5's, are SQLite's: its type is shadow.
        rows = db.execute(
            "SELECT name FROM pragma_table_list WHERE schema = 'main' "
            "AND type IN ('table', 'virtual') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        db.close()
    return {name for (name,) in rows}


def _check(doc: str, created: set[str]) -> None:
    listed = _listed(doc)
    difference = {
        "created but not listed": sorted(created - listed),
        "listed but not created": sorted(listed - created),
    }
    assert difference == {"created but not listed": [], "listed but not created": []}, (
        f"update the table list in docs/{doc}: {difference}"
    )


def test_control_store_tables_match_the_docs(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    documents = DocumentStore(path)
    try:
        documents.initialize()
        tables = _tables(path)
    finally:
        documents.close()
    review_tables = _listed("CONSISTENCY-REVIEW.md")
    _check("CONSISTENCY-REVIEW.md", tables & review_tables)
    _check("LIVING-DOCUMENTS.md", tables - review_tables)
