# SPDX-License-Identifier: MIT
"""Disposable pinned-artifact GUI/API acceptance; no worker or real model requests.

create STATE requires a new directory. The marker contains an exact corrected artifact
ref for the authenticated finding API. confirm STATE re-checks that exact fix using only
ScriptedModel and immutable control evidence. This proves plumbing, not model judgment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.contracts import ArtifactRef, ExecutionContext
from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.consistency.artifacts import ingest_artifacts
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

MARKER = ".synthetic-artifact-review.json"
PROJECT, SESSION = "artifact-demo", "main"
ORIGINAL = b'count = 16\r\nraw "quote" and \\slash\n'
CORRECTED = ORIGINAL.replace(b"16", b"15")


def create(path: Path) -> None:
    """Seed only new disposable state, with deliberately synthetic retained bytes."""
    if path.exists():
        raise ValueError("create requires a new synthetic state directory")
    projects = ProjectStore(path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    session = projects.open_session(PROJECT, SESSION, dedicated=False)
    other = projects.open_session(PROJECT, "other", dedicated=False)
    projects.create("foreign-artifact-demo", "minimal", "sha256:" + "b" * 64)
    projects.open_session("foreign-artifact-demo", SESSION, dedicated=False)
    context = ExecutionContext(
        project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
    )
    source = ArtifactPublicationSource(
        context=context,
        run_id="synthetic-author",
        tool_call="synthetic-publication",
        alias="scripted",
        model="scripted",
        worker_endpoint_sha256="0" * 64,
    )
    documents = DocumentStore(store_path(path))
    try:
        inventory = ArtifactInventory(documents)
        reviews = ReviewStore(documents, artifacts=inventory)
        refs = []
        review, evidence, finding = None, None, None
        for op, data in (("original", ORIGINAL), ("corrected", CORRECTED)):
            worker = ArtifactRef(
                artifact_id="synthetic-worker-" + op,
                project_id=PROJECT,
                source_path="count.txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                media_type="text/plain",
                created_at=datetime.now(UTC),
            )
            inventory.begin(source, op, "count.txt", "text/plain")
            inventory.pin(source, op, "count.txt", "text/plain", worker)
            receipt = inventory.finish(source, op, "count.txt", "text/plain", worker, data)
            assert receipt.revision is not None
            refs.append(receipt.revision)
            if op == "original":
                ingest_artifacts(reviews, PROJECT, SESSION)
                review = reviews.dispatch(
                    PROJECT,
                    SESSION,
                    "terminal",
                    author_run="synthetic-author",
                    owner="fixture",
                    alias="scripted-reviewer",
                )
                assert review is not None
                evidence = reviews.manifest(PROJECT, SESSION, review)["evidence"][0]["evidence"]
                finding = Findings(reviews).create(
                    PROJECT,
                    SESSION,
                    review,
                    "fixture",
                    {
                        "severity": "fail",
                        "category": "contradicting_value",
                        "statement": "Synthetic count16 must be corrected to15. No experiment ran.",
                        "anchors": [{"evidence": evidence}],
                        "operation_id": "fixture-finding",
                    },
                )
                reviews.finish(
                    PROJECT, SESSION, review, "fixture", "findings", "Synthetic count mismatch."
                )
        ingest_artifacts(reviews, PROJECT, SESSION)
        assert review is not None
        assert evidence is not None
        assert finding is not None
        for op, data, media in (
            ("empty", b"", "text/plain"),
            ("binary", b"\x00\xff", "application/octet-stream"),
            ("non-utf8", b"\xff", "text/plain"),
        ):
            worker = ArtifactRef(
                artifact_id="synthetic-worker-" + op,
                project_id=PROJECT,
                source_path=op + ".txt",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                media_type=media,
                created_at=datetime.now(UTC),
            )
            inventory.begin(source, op, worker.source_path, media)
            inventory.pin(source, op, worker.source_path, media, worker)
            inventory.finish(source, op, worker.source_path, media, worker, data)
        ingest_artifacts(reviews, PROJECT, SESSION)
        availability_review = reviews.dispatch(
            PROJECT,
            SESSION,
            "terminal",
            author_run="synthetic-author",
            owner="fixture",
            alias="scripted-reviewer",
        )
        assert availability_review is not None
        availability_items = reviews.manifest(PROJECT, SESSION, availability_review, limit=20)[
            "evidence"
        ]
        empty_item = next(
            i
            for i in availability_items
            if i["kind"] == "artifact" and i["locator"]["artifact"]["size"] == 0
        )
        empty_read = reviews.read_evidence(
            PROJECT, SESSION, availability_review, empty_item["evidence"], owner="fixture"
        )
        assert empty_read["text"] == ""
        assert empty_read["locator"]["empty_source"]
        reviews.finish(
            PROJECT,
            SESSION,
            availability_review,
            "fixture",
            "inconclusive",
            "Synthetic binary/non-UTF8 content remains incomplete.",
        )
        other_source = source.model_copy(
            update={
                "context": context.model_copy(
                    update={
                        "session_id": "other",
                        "environment_id": other.environment_id,
                    }
                )
            }
        )
        worker = ArtifactRef(
            artifact_id="synthetic-other-private",
            project_id=PROJECT,
            source_path="private.txt",
            sha256=hashlib.sha256(b"private").hexdigest(),
            size=7,
            media_type="text/plain",
            created_at=datetime.now(UTC),
        )
        inventory.begin(other_source, "private", worker.source_path, worker.media_type)
        inventory.pin(other_source, "private", worker.source_path, worker.media_type, worker)
        inventory.finish(
            other_source, "private", worker.source_path, worker.media_type, worker, b"private"
        )
        ingest_artifacts(reviews, PROJECT, "other", drain=True)
        other_review = reviews.dispatch(
            PROJECT,
            "other",
            "terminal",
            author_run="synthetic-other",
            owner="fixture",
            alias="scripted-reviewer",
        )
        assert other_review is not None
        other_evidence = reviews.manifest(PROJECT, "other", other_review)["evidence"][0]["evidence"]
        reviews.finish(
            PROJECT, "other", other_review, "fixture", "inconclusive", "Synthetic private evidence."
        )
        now = datetime.now(UTC)
        projects.save_run(
            RunRecord(
                run_id="synthetic-author",
                context=context,
                task="Synthetic pinned artifact acceptance.",
                started_at=now,
                ended_at=now,
                stopped="answered",
                final_text="Synthetic count16.",
                messages=[{"role": "assistant", "content": "Synthetic count16."}],
                tools=[],
                summaries={
                    "reviews": {"outcome": "findings", "reviews": [review, availability_review]}
                },
            )
        )
        marker = {
            "fixture": "synthetic-artifact-review-1",
            "project": PROJECT,
            "session": SESSION,
            "review": review,
            "evidence": evidence,
            "finding": finding["finding_id"],
            "availability_review": availability_review,
            "empty_evidence": empty_item["evidence"],
            "other_session": "other",
            "other_review": other_review,
            "other_evidence": other_evidence,
            "foreign_project": "foreign-artifact-demo",
            "original": refs[0].model_dump(mode="json"),
            "corrected": refs[1].model_dump(mode="json"),
            "url": f"/#/projects/{PROJECT}/sessions/{SESSION}",
        }
        (path / MARKER).write_text(json.dumps(marker))
        print(json.dumps(marker))
    finally:
        documents.close()


async def confirm(path: Path) -> None:
    """Refuse unrelated state or claims; read the exact granted original and changed sources."""
    marker_path = path / MARKER
    if not marker_path.is_file() or marker_path.stat().st_size > 4096:
        raise ValueError("confirm requires this synthetic fixture's marker")
    marker = json.loads(marker_path.read_text())
    if (marker.get("fixture"), marker.get("project"), marker.get("session")) != (
        "synthetic-artifact-review-1",
        PROJECT,
        SESSION,
    ):
        raise ValueError("invalid synthetic fixture marker")
    documents = DocumentStore(store_path(path))
    try:
        inventory = ArtifactInventory(documents)
        reviews = ReviewStore(documents, artifacts=inventory)
        found = Findings(reviews).get(PROJECT, SESSION, marker["finding"])
        queued = found.get("recheck") or {}
        if (
            found["status"] != "open"
            or queued.get("cause") != "fix"
            or found["history"][-1]["payload"].get("artifact_revisions") != [marker["corrected"]]
        ):
            raise ValueError(
                "claim only the exact seeded corrected artifact through the finding API"
            )
        from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef

        if (
            inventory.content(PROJECT, ArtifactRevisionRef.model_validate(marker["original"]))
            != ORIGINAL
        ):
            raise ValueError("original seeded bytes differ")
        if (
            inventory.content(PROJECT, ArtifactRevisionRef.model_validate(marker["corrected"]))
            != CORRECTED
        ):
            raise ValueError("corrected seeded bytes differ")
        review = reviews.dispatch(
            PROJECT,
            SESSION,
            "confirmation",
            author_run="synthetic-confirmation",
            owner="fixture",
            alias="scripted-reviewer",
        )
        assert review is not None
        items = reviews.manifest(PROJECT, SESSION, review, limit=20)["evidence"]
        changed = next(i for i in items if i["locator"].get("artifact") == marker["corrected"])
        calls = [{"name": "review_read", "arguments": {"evidence": i["evidence"]}} for i in items]
        turns = [
            {"tool_calls": calls},
            {
                "tool_calls": [
                    {
                        "name": "review_decide",
                        "arguments": {
                            "finding": marker["finding"],
                            "expected": found["revision"],
                            "decision": "resolved",
                            "explanation": "Scripted fixture: exact16 to15; synthetic only.",
                            "anchors": [{"evidence": changed["evidence"]}],
                            "operation_id": "fixture-confirm",
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "review_checked",
                        "arguments": {
                            "statement": "Synthetic exact corrected count agrees with fixture15.",
                            "evidence": [changed["evidence"], marker["evidence"]],
                        },
                    }
                ]
            },
            {"text": json.dumps({"outcome": "passed", "summary": "Synthetic correction checked."})},
        ]
        await ReviewerRunner(
            reviews, PROJECT, SESSION, review, "fixture", ScriptedModel.from_json(turns)
        ).run()
        ended = reviews.run(PROJECT, SESSION, review)
        final = Findings(reviews).get(PROJECT, SESSION, marker["finding"])
        assert ended["state"] == "passed"
        assert final["status"] == "fixed"
        with documents.control.read() as tx:
            db = tx.sql
            audited = {
                row[0]
                for row in db.execute(
                    "SELECT evidence_id FROM review_reads WHERE review_id=?",
                    (review,),
                )
            }
        assert {marker["evidence"], changed["evidence"]} <= audited
        print(json.dumps({"review": ended, "finding": final, "audited": sorted(audited)}))
    finally:
        documents.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "confirm"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        create(args.state)
    else:
        asyncio.run(confirm(args.state))
