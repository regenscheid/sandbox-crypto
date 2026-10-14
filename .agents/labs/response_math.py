# SPDX-License-Identifier: MIT
"""Marked disposable finding-prose browser fixture; local KaTeX, no model/worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from sandbox_science.orchestrator import mathchecks
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.mathcheck import MathChecker
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.projects import ProjectStore

PROJECT = "response-math-demo"
SESSION = "a"
MARKER = "synthetic-response-math-browser-v1\n"
MAIN = Author(
    kind="agent", session_id=SESSION, run_id="original-main", role="main", model="fixture"
)
CORRECTOR = MAIN.model_copy(update={"run_id": "correcting-main"})
BAD = r"$\definitelyUnknownCommand{x}$"
CASES = ("pending", "accepted", "removed", "unavailable", "unaddressed", "empty-macros")


def checked(raw: str) -> Path:
    root = Path(raw).resolve()
    if (
        not root.name.startswith("response-math-browser-")
        or (root / ".response-math-fixture").read_text() != MARKER
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


def edit(store: ReviewStore, source: dict, body: str) -> dict:
    api = ResponseBodies(store)
    current = api.get(PROJECT, SESSION, source["finding"], source["response_revision"])
    return api.edit(
        PROJECT,
        SESSION,
        {
            "finding": source["finding"],
            "response_revision": source["response_revision"],
            "expected": Findings(store).get(PROJECT, SESSION, source["finding"])["revision"],
            "base": current["revision"],
            "sha256": current["sha256"],
            "body": body,
            "operation_id": f"fixture-edit-{source['finding']}-{current['revision']}",
        },
        CORRECTOR,
    )


def confirmation(store: ReviewStore) -> str:
    review = store.dispatch(
        PROJECT,
        SESSION,
        "confirmation",
        author_run=MAIN.run_id or "fixture",
        owner="browser-fixture",
        alias="fixture-reviewer",
    )
    if review is None:
        raise ValueError("Expected one scientific confirmation target")
    after = 0
    while True:
        page = store.manifest(PROJECT, SESSION, review, after=after, limit=20)
        for item in page["evidence"]:
            store.read_evidence(PROJECT, SESSION, review, item["evidence"], owner="browser-fixture")
        if page["next_after"] is None:
            return review
        after = page["next_after"]


def confirm(store: ReviewStore, review: str, source: dict) -> dict:
    item = next(
        item
        for item in store.manifest(PROJECT, SESSION, review)["evidence"]
        if item["kind"] == "finding" and item["locator"].get("finding") == source["finding"]
    )
    return Findings(store).confirm(
        PROJECT,
        SESSION,
        review,
        "browser-fixture",
        {
            "finding": source["finding"],
            "expected": source["response_revision"],
            "decision": "confirm",
            "explanation": "Synthetic current-body scientific confirmation.",
            "anchors": [{"evidence": item["evidence"]}],
            "operation_id": f"fixture-confirm-{review}",
        },
    )


def audit(store: ReviewStore, metadata: dict) -> dict:
    api, bodies = Findings(store), ResponseBodies(store)
    return {
        "cases": {
            name: {
                **source,
                "parent": api.get(PROJECT, SESSION, source["finding"]),
                "current": bodies.get(
                    PROJECT, SESSION, source["finding"], source["response_revision"]
                ),
                "original": bodies.get(
                    PROJECT,
                    SESSION,
                    source["finding"],
                    source["response_revision"],
                    body_revision=1,
                ),
            }
            for name, source in metadata["cases"].items()
        },
        "findings": api.listing(PROJECT, SESSION)["findings"],
        "other_session": api.listing(PROJECT, "b")["findings"],
        "checks": MathJobs(store).listing(PROJECT),
        "confirmation_targets": store.confirmation_targets(PROJECT, SESSION),
    }


async def create(args: argparse.Namespace) -> None:
    checker = renderer(args)
    root = Path(tempfile.mkdtemp(prefix="response-math-browser-"))
    (root / ".response-math-fixture").write_text(MARKER)
    projects = ProjectStore(root)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    for session in (SESSION, "b"):
        projects.open_session(PROJECT, session, dedicated=False)
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        store = ReviewStore(documents)
        doc = documents.create(
            PROJECT,
            name="evidence",
            title="Synthetic finding evidence",
            text="---\nmacros:\n  '\\foo': 'x'\n---\n\nScientific evidence remains independent.\n",
            author=USER,
        ).document_id
        for name in CASES:
            store.observe(
                PROJECT,
                SESSION,
                f"fixture-{name}",
                "transcript",
                {"run": "fixture", "case": name},
                {"role": "assistant", "content": f"Claim for {name}."},
            )
        review = store.dispatch(
            PROJECT,
            SESSION,
            "terminal",
            author_run=MAIN.run_id or "fixture",
            owner="browser-fixture",
            alias="fixture-reviewer",
        )
        if review is None:
            raise ValueError("Fixture review was not dispatched")
        evidence = {
            item["locator"].get("case"): item["evidence"]
            for item in store.manifest(PROJECT, SESSION, review)["evidence"]
        }
        api, cases = Findings(store), {}
        for name in CASES:
            parent = api.create(
                PROJECT,
                SESSION,
                review,
                "browser-fixture",
                {
                    "severity": "fail",
                    "category": "contradicting_value",
                    "statement": f"Synthetic {name} parent. Reviewer $\\unknown{{excluded}}$.",
                    "anchors": [{"evidence": evidence[name]}],
                    "operation_id": f"fixture-parent-{name}",
                },
            )
            cases[name] = {"finding": parent["finding_id"]}
        store.finish(PROJECT, SESSION, review, "browser-fixture", "findings", "Synthetic parents.")
        for name, source in cases.items():
            payload = {
                "finding": source["finding"],
                "expected": 1,
                "action": "fix" if name == "accepted" else "rebut",
                "explanation": r"$\foo$" if name == "empty-macros" else BAD,
                "operation_id": f"fixture-response-{name}",
            }
            if name == "accepted":
                payload["revisions"] = [(doc, 1)]
            result = api.respond(PROJECT, SESSION, payload, MAIN)
            source["response_revision"] = result["revision"]
            source["original_parent"] = api.get(PROJECT, SESSION, source["finding"])
        await drain(store, checker)
        old = confirmation(store)
        edit(store, cases["accepted"], "$x^2$")
        await drain(store, checker)
        try:
            confirm(store, old, cases["accepted"])
        except DocumentError as exc:
            if "explanation changed" not in str(exc):
                raise
            stale = str(exc)
        else:
            raise ValueError("Old reviewer body pin was incorrectly accepted")
        store.finish(PROJECT, SESSION, old, "browser-fixture", "inconclusive", "Body changed.")
        edit(store, cases["removed"], "")
        await drain(store, checker)
        for body in (r"$\anotherUnknown{x}$", r"$\thirdUnknown{x}$"):
            edit(store, cases["unaddressed"], body)
            await drain(store, checker)
        edit(store, cases["unavailable"], "$x$")
        await drain(store, None)
        metadata = {
            "state_dir": str(root),
            "project": PROJECT,
            "session": SESSION,
            "document": doc,
            "cases": cases,
            "stale_confirmation": {"review": old, "refusal": stale},
            "renderer": checker.identity(),
            "scope": "Synthetic MAIN explanations; local KaTeX; no model/worker",
        }
        snapshot = audit(store, metadata)
        children = {
            f["anchors"][0]["locator"]["finding"]: f
            for f in snapshot["findings"]
            if f["origin"] == "math"
        }
        if len(children) != len(CASES) or set(children) != {c["finding"] for c in cases.values()}:
            raise ValueError("Expected exactly six original explanation math sources")
        for name, status in (
            ("pending", "open"),
            ("accepted", "confirmed fixed"),
            ("removed", "confirmed fixed"),
            ("unaddressed", "unaddressed"),
            ("unavailable", "open"),
            ("empty-macros", "open"),
        ):
            child = children[cases[name]["finding"]]
            if child["status"] != status or child["reflags"] != (2 if name == "unaddressed" else 0):
                raise ValueError(f"Fixture failed to establish {name}={status}")
        for name, source in snapshot["cases"].items():
            before, after = source["original_parent"], source["parent"]
            if (before["status"], before["revision"]) != (after["status"], after["revision"]):
                raise ValueError(f"Math correction changed scientific parent {name}")
        if snapshot["other_session"]:
            raise ValueError("Finding sources crossed the session boundary")
        (root / "fixture.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (root / "initial-evidence.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        print(json.dumps(metadata, indent=2))
    finally:
        documents.close()


async def change(args: argparse.Namespace) -> None:
    root = checked(args.state_dir)
    metadata = json.loads((root / "fixture.json").read_text())
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        store = ReviewStore(documents)
        if args.action == "main-fix":
            edit(store, metadata["cases"]["pending"], "$y^2$")
            await drain(store, renderer(args))
        elif args.action == "confirm":
            review = confirmation(store)
            result = confirm(store, review, metadata["cases"]["accepted"])
            if result["status"] != "confirmed fixed":
                raise ValueError("Fresh scientific confirmation did not succeed")
            store.finish(
                PROJECT,
                SESSION,
                review,
                "browser-fixture",
                "inconclusive",
                "One current-body scientific claim confirmed; other fixture findings remain unresolved.",
            )
            if store.run(PROJECT, SESSION, review)["state"] != "inconclusive":
                raise ValueError("Fixture incorrectly claims a complete review verdict")
        print(json.dumps(audit(store, metadata), indent=2))
    finally:
        documents.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "main-fix", "confirm", "audit"))
    parser.add_argument("--state-dir")
    parser.add_argument("--node")
    parser.add_argument("--launcher")
    parser.add_argument("--bundle")
    args = parser.parse_args()
    if args.action in ("create", "main-fix") and not all((args.node, args.launcher, args.bundle)):
        parser.error("Explicit absolute --node --launcher --bundle are required")
    if args.action != "create" and not args.state_dir:
        parser.error("Use the marked --state-dir")
    asyncio.run(create(args) if args.action == "create" else change(args))


if __name__ == "__main__":
    main()
