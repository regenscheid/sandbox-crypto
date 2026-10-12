# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Create disposable browser acceptance data, or simulate its concurrent agent edit.

Usage: python .agents/labs/document_editing.py create|agent|record STATE_DIRECTORY
Only ``create`` accepts a new directory; ``agent`` requires its fixture marker.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore

PROJECT = "editing-demo"
AGENT = Author(kind="agent", session_id="session-a", run_id="run-demo", role="main")
TEXT = """# Research notes

Original equation $x = 1$.

::: lemma
A nested statement $y$.
:::

| Input | Result |
| --- | --- |
| 1 | 1 |

Untouched conclusion.
"""


def main() -> None:
    command, directory = sys.argv[1:]
    state = Path(directory)
    marker = state / "browser-fixture.json"
    if command == "create":
        if state.exists():
            raise SystemExit("Choose a fresh disposable directory.")
        projects = ProjectStore(state)
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        projects.open_session(PROJECT, "session-a", dedicated=False)
        store = DocumentStore(store_path(state))
        document = store.create(
            PROJECT, name="notes", title="Research notes", text=TEXT, author=USER
        ).document_id
        paragraph = store.mapped(PROJECT, document).block_ids[1]
        store.patch(
            PROJECT,
            document,
            base=1,
            operations=[
                {"op": "replace", "block": paragraph, "markdown": "Current agent equation $x = 2$."}
            ],
            author=AGENT,
            reason="Agent updates equation",
            operation_id="current-equation",
        )
        retained = store.patch(
            PROJECT,
            document,
            base=1,
            operations=[
                {"op": "replace", "block": paragraph, "markdown": "Retained proposal $x = 3$."}
            ],
            author=AGENT,
            reason="Proposal conflicts with current equation",
            operation_id="retained-proposal",
        )
        workbook = store.create(
            PROJECT,
            name="workbook",
            title="Workbook notes",
            text="Workbook $z$.\n",
            author=AGENT,
            session_id="session-a",
        ).document_id
        record = {
            "fixture": "disposable-document-editing",
            "project": PROJECT,
            "document": document,
            "paragraph": paragraph,
            "patch": retained.patch_id,
            "workbook": workbook,
        }
        marker.write_text(json.dumps(record, indent=2) + "\n")
        store.close()
        print(json.dumps(record))
    elif command == "agent":
        record = json.loads(marker.read_text())
        assert record["fixture"] == "disposable-document-editing"
        assert record["project"] == PROJECT
        store = DocumentStore(store_path(state))
        result = store.patch(
            PROJECT,
            record["document"],
            base=store.document(PROJECT, record["document"]).head,
            operations=[
                {
                    "op": "replace",
                    "block": record["paragraph"],
                    "markdown": "Concurrent agent equation $x = 4$.",
                }
            ],
            author=AGENT,
            reason="Concurrent browser acceptance edit",
            operation_id=f"concurrent-{time.time_ns()}",
        )
        assert result.outcome == "applied", result
        store.close()
        print(result.model_dump_json())
    elif command == "record":
        record = json.loads(marker.read_text())
        assert record["fixture"] == "disposable-document-editing"
        store = DocumentStore(store_path(state), readonly=True)
        evidence = {
            "fixture": record,
            "documents": [item.model_dump(mode="json") for item in store.documents(PROJECT)],
            "history": store.history(PROJECT, record["document"]),
            "current": store.revision(PROJECT, record["document"]).model_dump(mode="json"),
            "events": [item.model_dump(mode="json") for item in store.events(PROJECT)],
        }
        store.close()
        print(json.dumps(evidence, indent=2))
    else:
        raise SystemExit("Use create, agent or record.")


if __name__ == "__main__":
    main()
