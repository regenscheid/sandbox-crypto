# SPDX-License-Identifier: MIT
"""Disposable consistency UI fixture. Scripted models only; no worker client."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from aiohttp import web

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

PROJECT = "review-demo"
MARKER = "synthetic-consistency-browser\n"
AUTHOR = Author(
    kind="agent", session_id="a", run_id="fixture-author", role="main", model="scripted"
)
SOURCE = "# Count\n\nThe computation returned $16$.\n"
CORRECTED = "# Count\n\nThe computation returned $15$.\n"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def checked(raw: str) -> Path:
    root = Path(raw).resolve()
    if (
        not root.name.startswith("consistency-browser-")
        or (root / ".review-fixture").read_text() != MARKER
    ):
        raise ValueError("Use only state created by this synthetic fixture")
    return root


def open_store(root: Path) -> tuple[DocumentStore, ReviewStore]:
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    return documents, ReviewStore(documents)


async def create() -> None:
    root = Path(tempfile.mkdtemp(prefix="consistency-browser-"))
    (root / ".review-fixture").write_text(MARKER)
    projects = ProjectStore(root)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    for session in ("a", "b"):
        projects.open_session(PROJECT, session, dedicated=False)
    documents, reviews = open_store(root)
    doc = documents.create(
        PROJECT,
        name="count",
        title="Finite field count",
        text=SOURCE,
        author=AUTHOR,
        session_id="a",
    ).document_id
    reviews.ingest_documents(PROJECT, "a")
    reviews.observe(
        PROJECT,
        "a",
        "fixture-execution",
        "execution",
        {"execution": "fixture-execution", "attempt": "fixture-attempt"},
        {"stdout": "nonzero_ok = 15\n", "stderr": ""},
        effects=True,
    )
    review = reviews.dispatch(
        PROJECT,
        "a",
        "terminal",
        author_run="fixture-author",
        owner="fixture-reviewer",
        alias="scripted-reviewer",
    )
    require(review is not None, "The synthetic review did not dispatch")
    items = reviews.manifest(PROJECT, "a", review)["evidence"]
    document = next(item for item in items if item["kind"] == "document")
    execution = next(item for item in items if item["kind"] == "execution")
    block = documents.mapped(PROJECT, doc, 1).block_ids[1]
    turns = [
        {"tool_calls": [{"name": "review_manifest", "arguments": {}}]},
        {
            "tool_calls": [
                {"name": "review_read", "arguments": {"evidence": item["evidence"]}}
                for item in items
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "review_finding",
                    "arguments": {
                        "severity": "fail",
                        "category": "contradicting_value",
                        "statement": (
                            "The document says $16$, while the pinned computation records $15$."
                        ),
                        "anchors": [
                            {"evidence": document["evidence"], "block": block},
                            {
                                "evidence": execution["evidence"],
                                "stream": "stdout",
                                "first_line": 1,
                                "last_line": 1,
                            },
                        ],
                        "remedy": "Check the computation and correct the document's count.",
                        "operation_id": "fixture-count",
                    },
                }
            ]
        },
        {
            "text": json.dumps(
                {
                    "outcome": "findings",
                    "summary": "One contradiction in the pinned count.",
                    "cap_reached": False,
                }
            )
        },
    ]
    await ReviewerRunner(
        reviews, PROJECT, "a", review, "fixture-reviewer", ScriptedModel.from_json(turns)
    ).run()
    require(
        reviews.run(PROJECT, "a", review)["state"] == "findings",
        "Expected the seeded contradiction",
    )
    finding = Findings(reviews).listing(PROJECT, "a")["findings"][0]["finding_id"]
    reviews.observe(
        PROJECT,
        "a",
        "fixture-later-text",
        "transcript",
        {"run": "fixture-later", "message": 1},
        {"role": "assistant", "content": "A later check still needs a reviewer."},
    )
    unavailable = reviews.dispatch(
        PROJECT,
        "a",
        "checkpoint",
        author_run="fixture-later",
        owner="fixture-unavailable",
        alias=None,
    )
    require(unavailable is not None, "The unavailable fixture did not dispatch")
    await ReviewerRunner(reviews, PROJECT, "a", unavailable, "fixture-unavailable", None).run()
    result = {
        "state": str(root),
        "project": PROJECT,
        "document": doc,
        "finding": finding,
        "review": review,
        "unavailable_review": unavailable,
        "url_fragment": f"#/projects/{PROJECT}/sessions/a",
    }
    (root / "fixture.json").write_text(json.dumps(result))
    print(json.dumps(result, indent=2))
    documents.close()


def metadata(root: Path) -> dict[str, str]:
    return json.loads((root / "fixture.json").read_text())


def fix(root: Path) -> None:
    documents, _ = open_store(root)
    doc = metadata(root)["document"]
    mapped = documents.mapped(PROJECT, doc)
    result = documents.patch(
        PROJECT,
        doc,
        base=1,
        operations=[{"op": "replace_math", "math": mapped.span_ids[0], "tex": "15"}],
        reason="Synthetic count correction",
        author=AUTHOR,
        operation_id="fixture-fix",
    )
    require(result.revision == 2, "Expected the synthetic document revision 2")
    print(result.model_dump_json())
    documents.close()


async def confirm(root: Path) -> None:
    documents, reviews = open_store(root)
    with sqlite3.connect(documents.path) as db:
        window = db.execute(
            "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
            (PROJECT, "a"),
        ).fetchone()
    require(window == (2, 3, 1), "Expected the seeded pending author-work retry")
    finding = Findings(reviews).get(PROJECT, "a", metadata(root)["finding"])
    require(finding["status"] == "claimed fixed", "Claim the seeded fix before confirming")
    doc = metadata(root)["document"]
    require(documents.revision(PROJECT, doc, 1).text == SOURCE, "Seeded source changed")
    require(
        documents.revision(PROJECT, doc, 2).text == CORRECTED, "The claimed correction is not 15"
    )
    pins = finding["history"][-1]["payload"]["revisions"]
    require(
        any(pin["document"] == doc and pin["revision"] == 2 for pin in pins),
        "Claimed fix did not pin the seeded corrected revision",
    )
    review = reviews.dispatch(
        PROJECT,
        "a",
        "confirmation",
        author_run="fixture-author",
        owner="fixture-confirmation",
        alias="scripted-reviewer",
    )
    require(review is not None, "The synthetic review did not dispatch")
    items = reviews.manifest(PROJECT, "a", review)["evidence"]
    fixed = next(
        item
        for item in items
        if item["kind"] == "document"
        and item["locator"].get("document") == doc
        and item["locator"].get("revision") == 2
    )
    turns = [
        {"tool_calls": [{"name": "review_manifest", "arguments": {}}]},
        {
            "tool_calls": [
                {"name": "review_read", "arguments": {"evidence": item["evidence"]}}
                for item in items
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "review_checked",
                    "arguments": {
                        "statement": "The corrected revision's count 15 matches the original pinned execution.",
                        "evidence": [item["evidence"] for item in items],
                    },
                },
                {
                    "name": "review_decide",
                    "arguments": {
                        "finding": finding["finding_id"],
                        "expected": finding["revision"],
                        "decision": "confirm",
                        "explanation": (
                            "The pinned revision now states 15, matching "
                            "the original pinned execution."
                        ),
                        "anchors": [{"evidence": fixed["evidence"]}],
                        "operation_id": "fixture-confirm",
                    },
                },
            ]
        },
        {
            "text": json.dumps(
                {
                    "outcome": "passed",
                    "summary": "The changed count agrees with the pinned execution.",
                    "cap_reached": False,
                }
            )
        },
    ]
    await ReviewerRunner(
        reviews, PROJECT, "a", review, "fixture-confirmation", ScriptedModel.from_json(turns)
    ).run()
    result = Findings(reviews).get(PROJECT, "a", finding["finding_id"])
    require(result["status"] == "confirmed fixed", "The scripted confirmation did not finish")
    run = reviews.run(PROJECT, "a", review)
    require(run["state"] == "passed", "Complete checked confirmation did not pass")
    with sqlite3.connect(documents.path) as db:
        remaining = db.execute(
            "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
            (PROJECT, "a"),
        ).fetchone()
    require(remaining == window, "Confirmation consumed unrelated author work")
    print(
        json.dumps(
            {
                "confirmation_review": review,
                "status": result["status"],
                "revision": result["revision"],
                "review_state": run["state"],
                "author_window_unchanged": list(remaining),
            }
        )
    )
    documents.close()


def reopen(root: Path) -> None:
    documents, reviews = open_store(root)
    finding = Findings(reviews).get(PROJECT, "a", metadata(root)["finding"])
    result = Findings(reviews).respond(
        PROJECT,
        "a",
        {
            "finding": finding["finding_id"],
            "expected": finding["revision"],
            "action": "reopen",
            "explanation": "Synthetic concurrent user reopens for browser draft retention.",
            "operation_id": "fixture-reopen",
        },
        USER,
    )
    print(json.dumps(result))
    documents.close()


def record(root: Path) -> None:
    documents, reviews = open_store(root)
    result = {
        "fixture": {k: v for k, v in metadata(root).items() if k != "state"},
        "document_head": documents.document(PROJECT, metadata(root)["document"]).head,
        "reviews": reviews.listing(PROJECT, "a"),
        "finding": Findings(reviews).get(PROJECT, "a", metadata(root)["finding"]),
        "provider_network_calls": 0,
        "worker_calls": 0,
    }
    with sqlite3.connect(documents.path) as db:
        window = db.execute(
            "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
            (PROJECT, "a"),
        ).fetchone()
        result["author_work_window"] = dict(
            zip(("watermark", "retry_end", "failures"), window, strict=True)
        )
        result["audit"] = [
            {"review": row[0], "kind": row[1], "payload": json.loads(row[2])}
            for row in db.execute(
                "SELECT review_id,kind,payload FROM review_records ORDER BY rowid"
            )
        ]
    print(json.dumps(result, indent=2))
    documents.close()


def serve(root: Path, static: str, port: int, delay_review: str | None) -> None:
    app = create_app(
        ProjectStore(root), Path(static), login_token="test-secret-consistency-browser-" + "r" * 32
    )
    if delay_review:

        @web.middleware
        async def delay(request: web.Request, handler):
            if f"/reviews/{delay_review}/" in request.path and (
                request.path.endswith("/records") or "/evidence/" in request.path
            ):
                await asyncio.sleep(2)
            return await handler(request)

        app.middlewares.append(delay)
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "fix", "confirm", "reopen", "record", "serve"])
    parser.add_argument("--state")
    parser.add_argument("--static")
    parser.add_argument("--port", type=int, default=35997)
    parser.add_argument(
        "--delay-review",
        help="Synthetic two-second evidence/audit delay for selection-race acceptance",
    )
    args = parser.parse_args()
    if args.action == "create":
        asyncio.run(create())
        return
    root = checked(args.state or "")
    if args.action == "confirm":
        asyncio.run(confirm(root))
    elif args.action == "serve":
        serve(root, args.static, args.port, args.delay_review)
    else:
        {"fix": fix, "reopen": reopen, "record": record}[args.action](root)


if __name__ == "__main__":
    main()
