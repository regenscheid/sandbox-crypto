# SPDX-License-Identifier: MIT
"""Scoped review previews, authenticated decisions, revision guards and retries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

TOKEN = "review-fixture-synthetic-login-0123456789abcdef"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
pytestmark = pytest.mark.asyncio


@pytest.fixture
def review_data(state_dir: Path):
    documents = DocumentStore(store_path(state_dir))
    store = ReviewStore(documents)
    store.observe(
        "p1",
        "a",
        "claim",
        "transcript",
        {"run": "fixture"},
        {"content": "<script>untrusted()</script> There are 16."},
    )
    review = store.dispatch(
        "p1", "a", "terminal", author_run="fixture", owner="fixture", alias="scripted-reviewer"
    )
    assert review
    evidence = store.manifest("p1", "a", review)["evidence"][0]["evidence"]
    finding = Findings(store).create(
        "p1",
        "a",
        review,
        "fixture",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "Claim lacks its asserted output.",
            "anchors": [{"evidence": evidence}],
            "operation_id": "fixture",
        },
    )
    store.finish("p1", "a", review, "fixture", "findings", "A retained finding.")
    documents.close()
    return review, evidence, finding["finding_id"]


@pytest_asyncio.fixture
async def client(aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path):
    found = await aiohttp_client(create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN))
    assert (await found.get("/login", params={"token": TOKEN}, allow_redirects=False)).status == 303
    return found


async def test_pinned_scope_reads_and_origin_guard(client, review_data) -> None:
    review, evidence, finding = review_data
    base = "/api/v0/projects/p1/sessions/a"
    result = await client.get(base + "/reviews")
    assert result.status == 200
    assert (await result.json())["reviews"][0]["alias"] == "scripted-reviewer"
    result = await client.get(base + f"/reviews/{review}/evidence/{evidence}")
    assert result.status == 200
    assert "<script>" in (await result.json())["text"]
    result = await client.get(
        f"/api/v0/projects/p1/sessions/b/reviews/{review}/evidence/{evidence}"
    )
    assert result.status == 404
    body = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "Explicit user decision.",
        "operation_id": "dismiss",
    }
    assert (await client.post(base + f"/findings/{finding}", json=body)).status == 403
    assert (
        await client.post(
            base + f"/findings/{finding}", json=body, headers={"Origin": "https://attacker.invalid"}
        )
    ).status == 403
    origin = str(client.make_url("/")).rstrip("/")
    result = await client.post(base + f"/findings/{finding}", json=body, headers={"Origin": origin})
    assert result.status == 200
    assert (await result.json())["status"] == "withdrawn"
    replay = await client.post(base + f"/findings/{finding}", json=body, headers={"Origin": origin})
    assert (await replay.json())["replayed"]
    history = await (await client.get(base + f"/findings/{finding}")).json()
    assert history["history"][-1]["actor"] == "user:user"
    stale = await client.post(
        base + f"/findings/{finding}",
        json={**body, "action": "reopen", "operation_id": "stale"},
        headers={"Origin": origin},
    )
    assert stale.status == 409


@pytest.mark.parametrize(
    "extra",
    [
        {"author": {"kind": "user"}},
        {"role": "main"},
        {"expected": True},
        {"operation_id": "x" * 129},
    ],
)
async def test_finding_writes_reject_invented_identity_and_malformed_fields(
    client, review_data, extra
) -> None:
    _, _, finding = review_data
    body = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "User decision.",
        "operation_id": "bad",
        **extra,
    }
    result = await client.post(
        f"/api/v0/projects/p1/sessions/a/findings/{finding}",
        json=body,
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert result.status == 400


async def test_manual_request_is_queued_without_invented_wake(client, review_data) -> None:
    result = await client.post(
        "/api/v0/projects/p1/sessions/a/reviews",
        json={},
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert result.status == 200
    value = await result.json()
    assert value["queued"]
    assert "Idle sessions do not start" in value["detail"]


@pytest.mark.parametrize("source", ["document", "comment"])
async def test_mechanical_math_evidence_is_scoped_bounded_and_separate_from_review(
    client, state_dir: Path, source: str
) -> None:
    import hashlib

    from sandbox_science.orchestrator.documents import Author
    from sandbox_science.orchestrator.documents.mathjobs import MathJobs
    from sandbox_science.orchestrator.mathcheck import MathCheck

    documents = DocumentStore(store_path(state_dir))
    store = ReviewStore(documents)
    author = Author(
        kind="agent", session_id="a", run_id="math-fixture", role="main", model="scripted"
    )
    doc = documents.create(
        "p1",
        name="math",
        title="Math",
        text=r"$\unknown{x}$" + "\n" if source == "document" else "Context.",
        author=author,
        session_id="a",
    ).document_id
    entry: str | None = None
    if source == "comment":
        from sandbox_science.orchestrator.documents.annotations import Annotations

        entry = Annotations(documents).change(
            "p1",
            doc,
            {
                "action": "create",
                "anchor": {"revision": 1, "block": "b1"},
                "body": r"$\unknown{x}$",
                "operation_id": "comment-source",
            },
            author,
        )["entry_id"]

    class Scripted:
        def identity(self) -> dict[str, object]:
            return {"bundle_sha256": "a" * 64, "launcher_sha256": "b" * 64, "engine": "0.18.9"}

        async def check_many(self, formulas):
            return [
                MathCheck(
                    id=f.id,
                    status="rejected",
                    error="Synthetic parse error",
                    remedy="x",
                    input_sha256=hashlib.sha256(f.model_dump_json().encode()).hexdigest(),
                    bundle_sha256="a" * 64,
                    launcher_sha256="b" * 64,
                    engine="0.18.9",
                    runtime="22.12.0",
                )
                for f in formulas
            ]

    jobs = MathJobs(store)
    check = jobs.claim("p1", "fixture")
    assert check
    assert await jobs.step(check, "fixture", Scripted())
    if source == "comment":
        check = jobs.claim("p1", "fixture")
        assert check
        assert await jobs.step(check, "fixture", Scripted())
    documents.close()
    base = "/api/v0/projects/p1/sessions/a"
    response = await client.get(base + "/math-checks")
    assert response.status == 200
    assert (await response.json())["checks"][0]["check_id"] == check
    response = await client.get(base + f"/math-checks/{check}?span=m1")
    assert response.status == 200
    body = await response.json()
    assert body["check"]["document"] == doc
    if source == "comment":
        assert body["check"]["comment"]["entry_id"] == entry
    assert body["observations"][0]["owner"]["author"]["session_id"] == "a"
    finding = (await (await client.get(base + "/findings")).json())["findings"][0]
    assert finding["origin"] == "math"
    assert finding["review_id"] is None
    assert finding["source"]["check"] == check
    assert (await client.get(base + f"/math-checks/{check}?span=m1%27%20OR%201=1")).status == 400
    assert (await client.get(base + f"/math-checks/{check}?after=-1")).status == 400
    assert (await client.get(base + "/math-checks?after=not-a-math-id")).status == 400
    assert (
        await client.get(f"/api/v0/projects/other/sessions/a/math-checks/{check}")
    ).status == 404
    for suffix in ("/math-checks", f"/math-checks/{check}"):
        assert (await client.get("/api/v0/projects/p1/sessions/missing" + suffix)).status == 404
        assert (await client.get("/api/v0/projects/p1/sessions/" + suffix)).status == 404
    # A valid session may inspect project-wide USER/no-session evidence, without
    # acquiring another session's finding or changing its owner.
    assert (await client.get("/api/v0/projects/p1/sessions/b/math-checks")).status == 200
    assert (await client.get(f"/api/v0/projects/p1/sessions/b/math-checks/{check}")).status == 200
    assert (
        await client.get(f"/api/v0/projects/p1/sessions/b/findings/{finding['finding_id']}")
    ).status == 404


async def test_main_response_body_read_is_scoped_readonly_and_preserves_original(
    client, review_data, state_dir: Path
) -> None:
    from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
    from sandbox_science.orchestrator.documents import Author

    _, _, finding = review_data
    documents = DocumentStore(store_path(state_dir))
    store = ReviewStore(documents)
    actor = Author(kind="agent", session_id="a", run_id="original", role="main")
    Findings(store).respond(
        "p1",
        "a",
        {
            "finding": finding,
            "expected": 1,
            "action": "rebut",
            "explanation": r"Original $\unknown{x}$",
            "operation_id": "original",
        },
        actor,
    )
    current = ResponseBodies(store).get("p1", "a", finding, 2)
    ResponseBodies(store).edit(
        "p1",
        "a",
        {
            "finding": finding,
            "response_revision": 2,
            "expected": 2,
            "base": 1,
            "sha256": current["sha256"],
            "body": "$x$",
            "operation_id": "correct",
        },
        actor.model_copy(update={"run_id": "corrector"}),
    )
    documents.close()
    path = f"/api/v0/projects/p1/sessions/a/findings/{finding}/responses/2"
    result = await client.get(path)
    assert result.status == 200
    source = await result.json()
    assert source["revision"] == 2
    assert source["body"] == "$x$"
    assert source["original_author"]["run_id"] == "original"
    assert source["author"]["run_id"] == "corrector"
    original = await (await client.get(path + "?body_revision=1")).json()
    assert original["body"] == r"Original $\unknown{x}$"
    assert (await client.get(path.replace("sessions/a", "sessions/b"))).status == 404
    assert (await client.get(path.replace("projects/p1", "projects/other"))).status == 404
    assert (await client.get(path + "?body_revision=-1")).status == 400
    assert (
        await client.post(
            path,
            json={},
            headers={
                "Origin": str(client.make_url("/")).rstrip("/"),
            },
        )
    ).status == 405


async def test_explanation_check_evidence_and_queue_never_cross_session(
    client, review_data, state_dir: Path
) -> None:
    from sandbox_science.orchestrator.documents import Author
    from sandbox_science.orchestrator.documents.mathjobs import MathJobs

    _, _, finding = review_data
    documents = DocumentStore(store_path(state_dir))
    store = ReviewStore(documents)
    Findings(store).respond(
        "p1",
        "a",
        {
            "finding": finding,
            "expected": 1,
            "action": "rebut",
            "explanation": r"Private $\unknown{x}$",
            "operation_id": "private",
        },
        Author(kind="agent", session_id="a", run_id="main", role="main"),
    )
    check = MathJobs(store).claim("p1", "owner")
    assert check
    assert await MathJobs(store).step(check, "owner", None)
    documents.close()
    own = "/api/v0/projects/p1/sessions/a"
    other = "/api/v0/projects/p1/sessions/b"
    assert (await client.get(own + f"/math-checks/{check}")).status == 200
    assert (await client.get(other + f"/math-checks/{check}")).status == 404
    assert (await client.get(other + f"/math-checks?after={check}")).status == 404
    listed = await (await client.get(other + "/math-checks")).json()
    assert listed["checks"] == []
    assert listed["queue"] == []
