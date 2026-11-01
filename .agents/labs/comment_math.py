# SPDX-License-Identifier: MIT
"""Marked disposable comment-math browser fixture; local renderer, no model/worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from sandbox_science.orchestrator import mathchecks
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.mathcheck import MathChecker
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.projects import ProjectStore

PROJECT = "comment-math-demo"
MARKER = "synthetic-comment-math-browser-v1\n"
MAIN = Author(kind="agent", session_id="a", run_id="fixture-main", role="main", model="fixture")
DELEGATED = MAIN.model_copy(update={"run_id": "fixture-delegated", "role": "delegated"})
BAD = r"$\definitelyUnknownCommand{x}$"


def checked(raw: str) -> Path:
    root = Path(raw).resolve()
    if (
        not root.name.startswith("comment-math-browser-")
        or (root / ".comment-math-fixture").read_text() != MARKER
    ):
        raise ValueError("Use only state created by this synthetic fixture")
    return root


def renderer(args: argparse.Namespace) -> MathChecker:
    return MathChecker(Path(args.node), Path(args.launcher), Path(args.bundle))


async def drain(store: ReviewStore, checker: MathChecker | None) -> None:
    jobs = MathJobs(store)
    for _ in range(200):
        check = jobs.claim(PROJECT, "browser-fixture")
        if check is None:
            return
        for _ in range(200):
            if await jobs.step(check, "browser-fixture", checker):
                break
        else:
            raise ValueError("Fixture check did not complete")
    raise ValueError("Fixture queue did not drain")


def edit(documents: DocumentStore, doc: str, entry: str, body: str, actor: Author) -> dict:
    api = Annotations(documents)
    current = api.body(PROJECT, doc, entry)
    return api.change(
        PROJECT,
        doc,
        {
            "action": "edit_body",
            "thread": current["thread"],
            "entry": entry,
            "expected": current["expected"],
            "base": current["revision"],
            "sha256": current["sha256"],
            "body": body,
            "operation_id": f"fixture-edit-{entry}-{current['revision']}",
        },
        actor,
    )


def audit(documents: DocumentStore, store: ReviewStore, metadata: dict) -> dict:
    api = Annotations(documents)
    return {
        "cases": {
            name: {
                **source,
                "current": api.body(PROJECT, metadata["document"], source["entry"]),
                "thread": api.read_thread(PROJECT, metadata["document"], source["thread"]),
            }
            for name, source in metadata["cases"].items()
        },
        "findings": Findings(store).listing(PROJECT, "a")["findings"],
        "checks": MathJobs(store).listing(PROJECT),
    }


async def create(args: argparse.Namespace) -> None:
    checker = renderer(args)
    root = Path(tempfile.mkdtemp(prefix="comment-math-browser-"))
    (root / ".comment-math-fixture").write_text(MARKER)
    projects = ProjectStore(root)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    for session in ("a", "b"):
        projects.open_session(PROJECT, session, dedicated=False)
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        store, api = ReviewStore(documents), Annotations(documents)
        text = "---\nmacros:\n  '\\foo': 'x'\n---\n\nOriginal anchor context.\n"
        doc = documents.create(
            PROJECT, name="comments", title="Comment math fixture", text=text, author=USER
        ).document_id
        block = documents.mapped(PROJECT, doc).block_ids[-1]
        cases = {}
        for name in (
            "agent-opening",
            "user-owned",
            "accepted",
            "removed",
            "unavailable",
            "unaddressed",
            "macro-context",
            "parser-held",
        ):
            body = (
                r"$\foo$"
                if name == "macro-context"
                else "> " * 40 + BAD
                if name == "parser-held"
                else BAD
            )
            value = api.change(
                PROJECT,
                doc,
                {
                    "action": "create",
                    "anchor": {"revision": 1, "block": block},
                    "body": body,
                    "operation_id": f"fixture-{name}",
                },
                USER if name == "user-owned" else MAIN,
            )
            cases[name] = {"entry": value["entry_id"], "thread": value["thread_id"]}
        reply = api.change(
            PROJECT,
            doc,
            {
                "action": "reply",
                "thread": cases["user-owned"]["thread"],
                "expected": 1,
                "body": BAD,
                "operation_id": "fixture-delegated-reply",
            },
            DELEGATED,
        )
        cases["delegated-reply"] = {"entry": reply["entry_id"], "thread": reply["thread_id"]}
        await drain(store, checker)
        edit(documents, doc, cases["accepted"]["entry"], "$x^2$", MAIN)
        edit(documents, doc, cases["removed"]["entry"], "", MAIN)
        edit(documents, doc, cases["unaddressed"]["entry"], r"$\secondUnknown{x}$", MAIN)
        edit(documents, doc, cases["user-owned"]["entry"], "USER changed only own opening.", USER)
        await drain(store, checker)
        reply_after_user = next(
            f
            for f in Findings(store).listing(PROJECT, "a")["findings"]
            if f["anchors"][0]["locator"].get("comment") == cases["delegated-reply"]["entry"]
        )
        if reply_after_user["reflags"] != 0:
            raise ValueError("Unrelated USER opening edit reflagged delegated reply")
        edit(documents, doc, cases["unaddressed"]["entry"], r"$\thirdUnknown{x}$", MAIN)
        await drain(store, checker)
        documents.edit_source(
            PROJECT,
            doc,
            base=1,
            text=text.replace("'x'", "'\\missingMacro{x}'").replace(
                "Original anchor context.", "USER changed the anchor context."
            ),
            author=USER,
            reason="Synthetic macro/context update",
            operation_id="fixture-macros",
        )
        await drain(store, checker)
        edit(documents, doc, cases["unavailable"]["entry"], r"$\unavailableUnknown{x}$", MAIN)
        await drain(store, None)
        metadata = {
            "state_dir": str(root),
            "project": PROJECT,
            "session": "a",
            "document": doc,
            "cases": cases,
            "renderer": checker.identity(),
            "scope": "Synthetic comments/replies; local KaTeX; no model/worker/service",
        }
        snapshot = audit(documents, store, metadata)
        by_entry = {f["anchors"][0]["locator"].get("comment"): f for f in snapshot["findings"]}
        for name, status in (
            ("accepted", "fixed"),
            ("removed", "fixed"),
            ("unaddressed", "open"),
            ("unavailable", "open"),
        ):
            if by_entry[cases[name]["entry"]]["status"] != status:
                raise ValueError(f"Fixture failed to establish {name}={status}")
        if cases["user-owned"]["entry"] in by_entry or cases["parser-held"]["entry"] in by_entry:
            raise ValueError("Excluded/held source incorrectly received a finding")
        for name in ("agent-opening", "delegated-reply"):
            if by_entry[cases[name]["entry"]]["reflags"] != 0:
                raise ValueError(f"Dependency-only update consumed {name}'s failed-patch budget")
        if by_entry[cases["unaddressed"]["entry"]]["reflags"] != 2:
            raise ValueError("Two failed body edits did not consume exactly two reflags")
        metadata["before_macro_reply_assessment"] = reply_after_user
        (root / "fixture.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (root / "initial-evidence.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        print(json.dumps(metadata, indent=2))
    finally:
        documents.close()


async def change(args: argparse.Namespace) -> None:
    root = checked(args.state_dir)  # Marker before opening a writer.
    metadata = json.loads((root / "fixture.json").read_text())
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        store = ReviewStore(documents)
        if args.action != "audit":
            case = "delegated-reply" if args.action == "main-fix" else "user-owned"
            current = Annotations(documents).body(
                PROJECT, metadata["document"], metadata["cases"][case]["entry"]
            )
            edit(
                documents,
                metadata["document"],
                current["entry_id"],
                "$x^2$"
                if args.action == "main-fix"
                else f"Concurrent USER body revision {current['revision'] + 1}.",
                MAIN if args.action == "main-fix" else USER,
            )
            await drain(store, renderer(args))
        print(json.dumps(audit(documents, store, metadata), indent=2))
    finally:
        documents.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "main-fix", "concurrent-user-edit", "audit"))
    parser.add_argument("--state-dir")
    parser.add_argument("--node")
    parser.add_argument("--launcher")
    parser.add_argument("--bundle")
    args = parser.parse_args()
    if args.action != "audit" and not all((args.node, args.launcher, args.bundle)):
        parser.error("Explicit absolute --node --launcher --bundle are required")
    if args.action != "create" and not args.state_dir:
        parser.error("Use the marked --state-dir")
    asyncio.run(create(args) if args.action == "create" else change(args))


if __name__ == "__main__":
    main()
