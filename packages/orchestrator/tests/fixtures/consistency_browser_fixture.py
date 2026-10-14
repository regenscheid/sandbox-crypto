# SPDX-License-Identifier: MIT
"""Synthetic browser acceptance state; no worker, credential or real model requests.

create STATE requires a new directory. confirm STATE checks a browser-created fix claim
with ScriptedModel only, reading the pinned original and changed evidence first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

MARKER = ".synthetic-consistency-fixture.json"
REPORT = (
    "# Synthetic GF(17) count\n\nThe scripted nonzero count was 16.\n\n"
    "This fixture makes no claim about a real experiment.\n"
)


def create(path: Path) -> None:
    if path.exists():
        raise ValueError("create requires a new synthetic state directory")
    projects = ProjectStore(path)
    project, _ = projects.create("review-demo", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("review-demo", "main", dedicated=False)
    author = Author(
        kind="agent",
        session_id="main",
        run_id="synthetic-author",
        role="main",
        model="scripted",
        alias="scripted-main",
    )
    documents = DocumentStore(store_path(path), producer="synthetic-fixture")
    try:
        doc = documents.create(
            "review-demo",
            name="count-report",
            title="Synthetic count report",
            text=REPORT,
            author=author,
            session_id="main",
        ).document_id
        store = ReviewStore(documents)
        store.ingest_documents("review-demo", "main", drain=True)
        store.observe(
            "review-demo",
            "main",
            "synthetic-output",
            "execution",
            {"execution": "exe-synthetic", "attempt": "att-synthetic", "run": "synthetic-author"},
            {
                "stdout": "nonzero_ok = 15\n",
                "stderr": "",
                "fixture": "scripted synthetic output; no computation ran",
            },
            effects=True,
        )
        review = store.dispatch(
            "review-demo",
            "main",
            "terminal",
            author_run="synthetic-author",
            owner="fixture",
            alias="scripted-reviewer",
        )
        assert review is not None
        page = store.manifest("review-demo", "main", review, limit=20)
        doc_evidence = next(item for item in page["evidence"] if item["kind"] == "document")
        log = next(item for item in page["evidence"] if item["kind"] == "execution")
        block = next(
            block
            for block, parsed in zip(
                documents.mapped("review-demo", doc).block_ids,
                documents.mapped("review-demo", doc).parsed.blocks,
                strict=True,
            )
            if "count was" in parsed.text
        )
        anchors = [
            {"evidence": doc_evidence["evidence"], "block": block},
            {"evidence": log["evidence"], "stream": "stdout", "first_line": 1, "last_line": 1},
        ]
        fail = Findings(store).create(
            "review-demo",
            "main",
            review,
            "fixture",
            {
                "severity": "fail",
                "category": "contradicting_value",
                "statement": (
                    "The report says **16**, while the pinned scripted "
                    "output says `nonzero_ok = 15`. For "
                    "$\\mathbb{F}_{17}$ this is a fixture contradiction,"
                    " not a real measurement."
                ),
                "anchors": anchors,
                "remedy": (
                    "Correct the report to **15**, then claim the exact changed document revision."
                ),
                "operation_id": "synthetic-fail",
            },
        )
        Findings(store).create(
            "review-demo",
            "main",
            review,
            "fixture",
            {
                "severity": "warn",
                "category": "evidence_strength",
                "statement": (
                    "Synthetic data do not establish a general proof. "
                    "Keep that qualification in the report."
                ),
                "anchors": [anchors[0]],
                "operation_id": "synthetic-warning",
            },
        )
        Findings(store).create(
            "review-demo",
            "main",
            review,
            "fixture",
            {
                "severity": "note",
                "category": "missing_action",
                "statement": (
                    "The immutable shell artifact inventory is unavailable in this fixture."
                ),
                "assessment": "missing_evidence",
                "anchors": [anchors[1]],
                "operation_id": "synthetic-missing",
            },
        )
        store.finish(
            "review-demo",
            "main",
            review,
            "fixture",
            "findings",
            (
                "Synthetic contradiction: report16 versus "
                "recorded15; missing inventory remains "
                "inconclusive."
            ),
        )
        now = datetime.now(UTC)
        projects.save_run(
            RunRecord(
                run_id="synthetic-author",
                context=ExecutionContext(
                    project_id=project.project_id,
                    session_id=session.session_id,
                    environment_id=session.environment_id,
                ),
                task="Synthetic review GUI fixture; no real model or computation request.",
                started_at=now,
                ended_at=now,
                stopped="answered",
                final_text="The scripted report says 16.",
                messages=[
                    {"role": "user", "content": "Synthetic fixture"},
                    {"role": "assistant", "content": "The scripted report says 16."},
                ],
                tools=[],
                summaries={
                    "reviews": {
                        "outcome": "findings",
                        "reviews": [review],
                        "counts": {"fail:open": 1, "warn:open": 1, "note:inconclusive": 1},
                    }
                },
            )
        )
        marker = {
            "fixture": "synthetic-consistency-1",
            "project": "review-demo",
            "session": "main",
            "document": doc,
            "block": block,
            "review": review,
            "finding": fail["finding_id"],
            "url": "/#/projects/review-demo/sessions/main",
        }
        (path / MARKER).write_text(json.dumps(marker))
        print(json.dumps(marker))
    finally:
        documents.close()


async def confirm(path: Path) -> None:
    marker_path = path / MARKER
    if not marker_path.is_file() or marker_path.stat().st_size > 4096:
        raise ValueError("confirm requires state created by this synthetic fixture")
    marker = json.loads(marker_path.read_text())
    if (
        not isinstance(marker, dict)
        or marker.get("fixture") != "synthetic-consistency-1"
        or marker.get("project") != "review-demo"
        or marker.get("session") != "main"
    ):
        raise ValueError("invalid synthetic fixture marker")
    documents = DocumentStore(store_path(path), producer="synthetic-fixture")
    try:
        store = ReviewStore(documents)
        targets = store.confirmation_targets("review-demo", "main")
        target = next((item for item in targets if item[0] == marker["finding"]), None)
        if target is None:
            raise ValueError("first claim a fix with exact changed revisions in the browser")
        finding, revision = target
        found = Findings(store).get("review-demo", "main", finding)
        if (
            found["review_id"] != marker["review"]
            or found["category"] != "contradicting_value"
            or found["status"] != "claimed fixed"
            or documents.revision("review-demo", marker["document"], 1).text != REPORT
        ):
            raise ValueError("the target is not the seeded synthetic contradiction")
        revisions = found["history"][-1]["payload"].get("revisions", [])
        fixed = [
            item
            for item in revisions
            if item["document"] == marker["document"] and item["revision"] > 1
        ]
        if len(fixed) != 1:
            raise ValueError("claim exactly one changed revision of the seeded report")
        corrected = documents.revision("review-demo", marker["document"], fixed[0]["revision"])
        if corrected.info.sha256 != fixed[0]["sha256"] or corrected.text != REPORT.replace(
            "count was 16.", "count was 15."
        ):
            raise ValueError("the exact claimed revision must correct the synthetic count to 15")
        review = store.dispatch(
            "review-demo",
            "main",
            "confirmation",
            author_run="synthetic-confirmation",
            owner="fixture",
            alias="scripted-reviewer",
        )
        assert review is not None
        items = []
        after = 0
        while True:
            page = store.manifest("review-demo", "main", review, limit=20, after=after)
            items.extend(page["evidence"])
            if page["next_after"] is None:
                break
            after = page["next_after"]
        calls = [
            {"name": "review_read", "arguments": {"evidence": item["evidence"]}} for item in items
        ]
        turns: list[dict[str, Any]] = [
            {"tool_calls": calls[start : start + 16]} for start in range(0, len(calls), 16)
        ]
        document = next(
            item
            for item in items
            if item["kind"] == "document"
            and all(
                item["locator"].get(key) == fixed[0][key]
                for key in ("document", "revision", "sha256")
            )
        )
        turns.append(
            {
                "tool_calls": [
                    {
                        "name": "review_decide",
                        "arguments": {
                            "finding": finding,
                            "expected": revision,
                            "decision": "confirm",
                            "explanation": (
                                "Synthetic scripted acceptance: the exact changed "
                                "revision agrees with the pinned15 output."
                            ),
                            "anchors": [{"evidence": document["evidence"]}],
                            "operation_id": "synthetic-confirm",
                        },
                    }
                ]
            }
        )
        turns.append(
            {
                "text": json.dumps(
                    {
                        "outcome": "findings",
                        "summary": (
                            "The synthetic claimed fix was confirmed; other "
                            "findings retain their own states."
                        ),
                        "cap_reached": False,
                    }
                )
            }
        )
        await ReviewerRunner(
            store, "review-demo", "main", review, "fixture", ScriptedModel.from_json(turns)
        ).run()
        print(
            json.dumps(
                {
                    "review": store.run("review-demo", "main", review),
                    "finding": Findings(store).get("review-demo", "main", finding),
                },
                ensure_ascii=False,
            )
        )
    finally:
        documents.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "confirm"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        create(args.state)
    else:
        asyncio.run(confirm(args.state))


if __name__ == "__main__":
    main()
