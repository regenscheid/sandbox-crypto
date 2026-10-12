# SPDX-License-Identifier: MIT
"""Marked disposable document-math fixture. Local renderer only; no model or worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.documents.mathjobs import MathJobs
from sandbox_science.orchestrator.mathcheck import MathChecker
from sandbox_science.orchestrator.projects import ProjectStore

PROJECT = "math-demo"
MARKER = "synthetic-document-math-browser-v1\n"
AUTHOR = Author(kind="agent", session_id="a", run_id="fixture-author", role="main", model="fixture")
BAD = r"$\definitelyUnknownCommand{x}$"


def checked(raw: str) -> Path:
    root = Path(raw).resolve()
    if (
        not root.name.startswith("document-math-browser-")
        or (root / ".math-fixture").read_text() != MARKER
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


def patch(documents: DocumentStore, doc: str, tex: str, op: str) -> None:
    mapped = documents.mapped(PROJECT, doc)
    result = documents.patch(
        PROJECT,
        doc,
        base=documents.document(PROJECT, doc).head,
        author=AUTHOR,
        reason="Synthetic browser formula change",
        operation_id=op,
        operations=[{"op": "replace_math", "math": mapped.span_ids[0], "tex": tex}],
    )
    if result.outcome != "applied":
        raise ValueError("Fixture formula change did not apply")


async def create(args: argparse.Namespace) -> None:
    checker = renderer(args)
    root = Path(tempfile.mkdtemp(prefix="document-math-browser-"))
    (root / ".math-fixture").write_text(MARKER)
    projects = ProjectStore(root)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    for session in ("a", "b"):
        projects.open_session(PROJECT, session, dedicated=False)
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    try:
        store = ReviewStore(documents)
        cases = {}
        for name in (
            "agent-invalid",
            "user-invalid",
            "preserved-owner",
            "accepted",
            "removed",
            "unavailable",
            "unaddressed",
        ):
            author = USER if name == "user-invalid" else AUTHOR
            cases[name] = documents.create(
                PROJECT,
                name=name,
                title=name.replace("-", " "),
                text=BAD + "\n\nOriginal other paragraph.\n",
                author=author,
                session_id="a" if author == AUTHOR else None,
            ).document_id
        await drain(store, checker)
        documents.edit_source(
            PROJECT,
            cases["preserved-owner"],
            base=1,
            text=BAD + "\n\nUSER changed only this other paragraph.\n",
            author=USER,
            reason="Synthetic unrelated USER edit",
            operation_id="preserve-owner",
        )
        patch(documents, cases["accepted"], "x^2", "accepted")
        documents.edit_source(
            PROJECT,
            cases["removed"],
            base=1,
            text="Formula removed.\n",
            author=USER,
            reason="Synthetic formula removal",
            operation_id="removed",
        )
        patch(documents, cases["unaddressed"], r"\otherUnknown{x}", "reflag-1")
        await drain(store, checker)
        patch(documents, cases["unaddressed"], r"\thirdUnknown{x}", "reflag-2")
        await drain(store, checker)
        patch(documents, cases["unavailable"], r"\missingRenderer{x}", "unavailable")
        await drain(store, None)
        findings = Findings(store).listing(PROJECT, "a")["findings"]
        by_doc = {value["anchors"][0]["locator"]["document"]: value for value in findings}
        expected = {
            "accepted": "confirmed fixed",
            "removed": "confirmed fixed",
            "unaddressed": "unaddressed",
        }
        for name, status in expected.items():
            if by_doc[cases[name]]["status"] != status:
                raise ValueError(f"Fixture {name} did not establish {status}; verify renderer")
        if cases["user-invalid"] in by_doc:
            raise ValueError("USER text incorrectly received an author finding")
        result = {
            "state_dir": str(root),
            "project": PROJECT,
            "session": "a",
            "renderer": checker.identity(),
            "documents": cases,
            "findings": {
                name: by_doc[doc]["finding_id"] for name, doc in cases.items() if doc in by_doc
            },
            "checks": MathJobs(store).listing(PROJECT),
            "scope": "Synthetic documents; actual local KaTeX; no model/worker/service calls",
        }
        (root / "fixture.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        documents.close()


async def change(args: argparse.Namespace) -> None:
    root = checked(args.state_dir)  # Marker before opening any writer.
    metadata = json.loads((root / "fixture.json").read_text())
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    try:
        store = ReviewStore(documents)
        doc = metadata["documents"][args.case]
        if args.action == "reopen":
            finding = metadata["findings"][args.case]
            current = Findings(store).get(PROJECT, "a", finding)
            result = Findings(store).respond(
                PROJECT,
                "a",
                {
                    "finding": finding,
                    "expected": current["revision"],
                    "action": "reopen",
                    "explanation": "Concurrent synthetic USER decision; preserve browser draft.",
                    "operation_id": f"fixture-reopen-{current['revision']}",
                },
                USER,
            )
        else:
            patch(
                documents,
                doc,
                "x" if args.action == "fix" else r"\stillUnknown{x}",
                f"fixture-{args.action}-{documents.document(PROJECT, doc).head}",
            )
            await drain(store, None if args.action == "unavailable" else renderer(args))
            result = Findings(store).get(PROJECT, "a", metadata["findings"][args.case])
        print(json.dumps(result, indent=2))
    finally:
        documents.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "fix", "unavailable", "reopen"))
    parser.add_argument("--state-dir")
    parser.add_argument("--case", default="agent-invalid")
    parser.add_argument("--node")
    parser.add_argument("--launcher")
    parser.add_argument("--bundle")
    args = parser.parse_args()
    if args.action in ("create", "fix") and not all((args.node, args.launcher, args.bundle)):
        parser.error("create/fix require explicit absolute --node --launcher --bundle")
    if args.action != "create" and not args.state_dir:
        parser.error("mutations require the marked --state-dir")
    asyncio.run(create(args) if args.action == "create" else change(args))


if __name__ == "__main__":
    main()
