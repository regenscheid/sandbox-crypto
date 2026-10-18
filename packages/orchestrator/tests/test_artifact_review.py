# SPDX-License-Identifier: MIT
"""Pinned raw evidence, durable ingestion, scoped findings and hostile SQL boundaries."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import sys
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ArtifactRef, ExecutionContext, WriteFileRequest
from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.consistency import artifacts as consumer_module
from sandbox_science.orchestrator.consistency.artifacts import (
    TEXT_BYTES,
    ArtifactConsumer,
    ingest_artifacts,
)
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.runner import SPECS, ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import Author, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.model import ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

TOKEN = "test-secret-artifact-review-0123456789"


@pytest_asyncio.fixture
async def worker_url(tmp_path: Path):
    root = tmp_path / "worker"
    root.mkdir()
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    await manager.start()
    try:
        async with TestServer(create_app(manager, TOKEN)) as server:
            yield str(server.make_url("/")).rstrip("/")
    finally:
        await manager.close()
        store.close()


@pytest.fixture
def state(tmp_path: Path):
    made = build(tmp_path)
    yield made
    made[0].documents.close()


def build(tmp_path: Path):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    projects.create("foreign", "minimal", "sha256:" + "a" * 64)
    projects.open_session("foreign", "s", dedicated=False)
    documents = DocumentStore(store_path(tmp_path))
    inventory = ArtifactInventory(documents)
    reviews = ReviewStore(documents, artifacts=inventory)
    context = ExecutionContext(
        project_id="p", session_id="s", environment_id=session.environment_id
    )
    source = ArtifactPublicationSource(
        context=context,
        run_id="author",
        tool_call="publish-call",
        worker_endpoint_sha256="a" * 64,
    )
    return reviews, inventory, source


def publish(
    state, operation="op1", data=b"count = 16\n", media="text/plain", path="result.txt", source=None
):
    _reviews, inventory, default = state
    source = source or default
    worker = ArtifactRef(
        artifact_id=f"worker-{operation}",
        project_id=source.context.project_id,
        source_path=path,
        media_type=media,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=datetime.now(UTC),
        produced_by="unproven-hint",
    )
    inventory.begin(source, operation, path, media)
    inventory.pin(source, operation, path, media, worker)
    receipt = inventory.finish(source, operation, path, media, worker, data)
    assert receipt.revision
    return receipt.revision


def dispatch(reviews, kind="terminal"):
    review = reviews.dispatch("p", "s", kind, author_run="author", owner="owner", alias="scripted")
    assert review
    return review


def items(reviews, review):
    result, after = [], 0
    while True:
        page = reviews.manifest("p", "s", review, limit=20, after=after)
        result.extend(page["evidence"])
        if page["next_after"] is None:
            return result
        after = page["next_after"]


def finding(reviews, review, evidence):
    return Findings(reviews).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "Count is wrong.",
            "anchors": [{"evidence": evidence}],
            "operation_id": "raise-count",
        },
    )


def fix(reviews, found, ref, operation="fix-count"):
    return Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": found["finding_id"],
            "expected": found["revision"],
            "action": "fix",
            "explanation": "Changed the exact retained artifact.",
            "artifact_revisions": [ref.model_dump(mode="json")],
            "operation_id": operation,
        },
        Author(kind="agent", session_id="s", run_id="author", role="main"),
    )


def decide(reviews, review, found, evidence):
    return Findings(reviews).confirm(
        "p",
        "s",
        review,
        "owner",
        {
            "finding": found["finding_id"],
            "expected": found["revision"],
            "decision": "confirm",
            "explanation": "Read the original and changed source.",
            "anchors": [{"evidence": evidence}],
            "operation_id": "confirm-count",
        },
    )


@pytest.mark.parametrize(
    "raw", [b"", b'quotes" \\ slash\r\n', "\ufeff\u03bb = 2\n".encode(), b"x" * TEXT_BYTES]
)
def test_exact_raw_text_copy_and_empty_exposure(state, raw: bytes) -> None:
    reviews, _, _ = state
    ref = publish(state, data=raw)
    assert ingest_artifacts(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    evidence = items(reviews, review)[0]
    slices, start = [], 0
    while True:
        read = reviews.read_evidence(
            "p", "s", review, evidence["evidence"], start=start, owner="owner"
        )
        slices.append(read["text"])
        assert read["source_sha256"] == ref.sha256
        assert read["availability"] == "text"
        assert read["complete"]
        if read["next_start"] is None:
            break
        start = read["next_start"]
    assert "".join(slices).encode() == raw
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_reads").fetchone()[0] >= 1
    assert (
        reviews.evidence("p", "s", review, evidence["evidence"])["payload"]["source"]["producer"]
        == "unknown"
    )
    for session in ("other",):
        with pytest.raises(DocumentError):
            reviews.read_evidence("p", session, review, evidence["evidence"])
    with pytest.raises(DocumentError):
        reviews.read_evidence("foreign", "s", review, evidence["evidence"])


@pytest.mark.parametrize(
    ("data", "media", "availability"),
    [
        (b"x" * (TEXT_BYTES + 1), "text/plain", "oversize"),
        (b"\xff", "text/plain", "non_utf8"),
        (b"\x00", "text/plain", "binary"),
        (b"<script>alert(1)</script>", "text/html", "binary"),
        (b"safe bytes", "application/octet-stream", "binary"),
        (b"text", "text/plain; charset=latin1", "binary"),
    ],
    ids=["oversize", "non-utf8", "nul", "html", "binary-media", "charset"],
)
def test_unavailable_content_explicitly_prevents_pass(state, data, media, availability) -> None:
    reviews, _, _ = state
    publish(state, data=data, media=media)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    evidence = items(reviews, review)[0]
    read = reviews.read_evidence("p", "s", review, evidence["evidence"], owner="owner")
    assert read["availability"] == availability
    assert not read["complete"]
    assert reviews.evidence("p", "s", review, evidence["evidence"])["payload"]["text"] is None
    reviews.finish("p", "s", review, "owner", "passed", "Everything passed.")
    assert reviews.run("p", "s", review)["state"] == "inconclusive"


def test_reproduction_does_not_count_and_restart_does_not_need_callback(state) -> None:
    reviews, _inventory, _ = state
    one = publish(state)
    publish(state, "op2")
    publish(state, "op3", b"changed")
    publish(state, "op4", b"again")
    reviews.configure("p", "s", ReviewConfig(min_interval_seconds=0))
    path = reviews.documents.path
    reviews.documents.close()
    reopened = DocumentStore(path)
    try:
        recovered = ReviewStore(reopened)  # inventories are discovered from canonical schema
        result = ingest_artifacts(recovered, "p", "s")
        assert result["complete"]
        assert result["cursor"] == 4
        assert recovered.due("p", "s")
        assert ingest_artifacts(recovered, "p", "s") == result
        with reopened.control.read() as tx:
            db = tx.sql
            assert db.execute("SELECT count(*),sum(artifacts) FROM review_feed").fetchone() == (
                4,
                3,
            )
        review = dispatch(recovered)
        assert items(recovered, review)[0]["locator"]["artifact"] == one.model_dump(mode="json")
        assert recovered.run_effects("p", "s", "author")
    finally:
        reopened.close()


def test_source_cursor_and_feed_rollback_atomically(state, monkeypatch) -> None:
    reviews, _, _ = state
    publish(state)
    original = reviews.record_observation

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before cursor commit")

    monkeypatch.setattr(reviews, "record_observation", fail)
    with pytest.raises(RuntimeError):
        ingest_artifacts(reviews, "p", "s")
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_feed").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM feed_cursors").fetchone()[0] == 0
    monkeypatch.setattr(reviews, "record_observation", original)
    assert ingest_artifacts(reviews, "p", "s")["complete"]


def test_prefix_bounds_and_concurrent_ingestors(state) -> None:
    reviews, _, _ = state
    for number in range(115):
        publish(state, f"op{number}", str(number).encode())
    first = ingest_artifacts(reviews, "p", "s")
    assert first == {"cursor": 10, "target": 115, "complete": False, "enabled": True}
    with ThreadPoolExecutor(max_workers=2) as threads:
        list(threads.map(lambda _: ingest_artifacts(reviews, "p", "s", drain=True), range(2)))
    assert ingest_artifacts(reviews, "p", "s", drain=True)["complete"]
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*),sum(artifacts) FROM review_feed").fetchone() == (
            115,
            115,
        )


def test_unknown_and_known_worker_pins_observed_once_then_recorded(state) -> None:
    reviews, inventory, source = state
    inventory.begin(source, "unknown", "missing.txt", "text/plain")
    worker = ArtifactRef(
        artifact_id="worker-held",
        project_id="p",
        source_path="held.txt",
        size=4,
        sha256=hashlib.sha256(b"held").hexdigest(),
        media_type="text/plain",
        created_at=datetime.now(UTC),
    )
    inventory.begin(source, "known", "held.txt", "text/plain")
    inventory.pin(source, "known", "held.txt", "text/plain", worker)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    availability = [
        reviews.evidence("p", "s", review, i["evidence"])["payload"]["availability"]
        for i in items(reviews, review)
    ]
    assert set(availability) == {"unknown_remote_outcome", "known_worker_snapshot"}
    reviews.finish("p", "s", review, "owner", "inconclusive", "Uncertain publication.")
    inventory.hold(source, "known", "held.txt", "text/plain", "later transfer problem")
    ingest_artifacts(reviews, "p", "s")
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_feed").fetchone()[0] == 2
    inventory.finish(source, "known", "held.txt", "text/plain", worker, b"held")
    ingest_artifacts(reviews, "p", "s")
    fresh = dispatch(reviews)
    # The earlier inconclusive span remains open even though this new revision is known.
    assert not reviews.run("p", "s", fresh)["coverage_complete"]
    # The new revision is reviewed alongside both unresolved earlier observations.
    assert len(items(reviews, fresh)) == 3


@pytest.mark.parametrize("changed", [b"fixed = 15\n", b""])
def test_claim_confirmation_pins_exact_sources_and_empty(state, changed) -> None:
    reviews, _, _ = state
    old = publish(state)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    original = items(reviews, review)[0]["evidence"]
    found = finding(reviews, review, original)
    reviews.finish("p", "s", review, "owner", "findings", "Wrong count.")
    new = publish(state, "op2", changed)
    ingest_artifacts(reviews, "p", "s")
    claimed = fix(reviews, found, new)
    assert fix(reviews, found, new)["replayed"]
    confirm = dispatch(reviews, "confirmation")
    evidence = items(reviews, confirm)
    with pytest.raises(DocumentError, match="read"):
        decide(reviews, confirm, claimed, original)
    for item in evidence:
        reviews.read_evidence("p", "s", confirm, item["evidence"], owner="owner")
    new_item = next(i for i in evidence if i["locator"].get("artifact", {}).get("revision") == 2)
    result = decide(reviews, confirm, claimed, new_item["evidence"])
    assert result["status"] == "confirmed fixed"
    assert reviews.read_evidence("p", "s", confirm, original)["text"] == "count = 16\n"
    assert old.sha256 != new.sha256


def test_confirmation_requires_full_source_exposure_and_cannot_confirm_binary(state) -> None:
    reviews, _, _ = state
    publish(state, data=b"a" * 20000)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    original = items(reviews, review)[0]["evidence"]
    found = finding(reviews, review, original)
    reviews.finish("p", "s", review, "owner", "findings", "Needs correction.")
    new = publish(state, "op2", b"b" * 20000)
    ingest_artifacts(reviews, "p", "s")
    claimed = fix(reviews, found, new)
    confirm = dispatch(reviews, "confirmation")
    evidence = items(reviews, confirm)
    for item in evidence:
        reviews.read_evidence("p", "s", confirm, item["evidence"], count=100, owner="owner")
    with pytest.raises(DocumentError, match="read"):
        decide(reviews, confirm, claimed, original)
    reviews.finish("p", "s", confirm, "owner", "inconclusive", "Not fully read.")
    # A newly claimed oversized revision is metadata-only, even with an exact pin.
    binary = publish(state, "op3", b"b" * (TEXT_BYTES + 1))
    ingest_artifacts(reviews, "p", "s")
    again = fix(reviews, claimed, binary, "new-claim")
    confirm = dispatch(reviews, "confirmation")
    for item in items(reviews, confirm):
        start = 0
        while True:
            read = reviews.read_evidence(
                "p", "s", confirm, item["evidence"], start=start, owner="owner"
            )
            if read["next_start"] is None:
                break
            start = read["next_start"]
    with pytest.raises(DocumentError, match="artifact"):
        decide(reviews, confirm, again, original)


def test_fix_refs_need_relevant_same_session_exact_grants(state) -> None:
    reviews, _, source = state
    publish(state)
    unrelated = publish(state, "unrelated", path="elsewhere.txt")
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    first = next(
        i
        for i in items(reviews, review)
        if i["locator"]["artifact"]["artifact_id"] != unrelated.artifact_id
    )
    found = finding(reviews, review, first["evidence"])
    with pytest.raises(DocumentError, match="unrelated"):
        fix(reviews, found, unrelated)
    foreign_source = source.model_copy(
        update={"context": source.context.model_copy(update={"session_id": "other"})}
    )
    foreign = publish(state, "foreign", path="private.txt", source=foreign_source)
    ingest_artifacts(reviews, "p", "s")
    with pytest.raises(DocumentError, match="session evidence"):
        reviews.artifact_pin("p", "s", foreign)
    with pytest.raises(DocumentError):
        reviews.artifact_pin("foreign", "s", unrelated)
    with pytest.raises(DocumentError, match="differs"):
        reviews.artifact_pin("p", "s", unrelated.model_copy(update={"sha256": "b" * 64}))


def test_supersession_is_not_confirmation_and_mixed_anchor_is_not_replaced(state) -> None:
    reviews, _, _ = state
    publish(state)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    old = items(reviews, review)[0]["evidence"]
    found = finding(reviews, review, old)
    reviews.finish("p", "s", review, "owner", "findings", "Wrong count.")
    new = publish(state, "op2", b"new result")
    ingest_artifacts(reviews, "p", "s")
    Findings(reviews).supersede("p", "s")
    superseded = Findings(reviews).get("p", "s", found["finding_id"])
    assert superseded["status"] == "superseded"
    assert superseded["history"][-1]["payload"]["artifact_replacements"] == [
        new.model_dump(mode="json")
    ]
    next_review = dispatch(reviews)
    assert any(
        i["locator"].get("artifact", {}).get("revision") == 2 for i in items(reviews, next_review)
    )


def test_corrupt_giant_blob_is_clipped_before_python_and_never_copied(state) -> None:
    reviews, inventory, _ = state
    ref = publish(state, data=b"small")
    with reviews.documents.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER artifact_revisions_no_update")
        db.execute("UPDATE artifact_revisions SET data=zeroblob(20971520)")
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="integrity"):
            inventory.content("p", ref, max_bytes=TEXT_BYTES)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    item = items(reviews, review)[0]
    assert not item["complete"]
    assert (
        reviews.evidence("p", "s", review, item["evidence"])["payload"]["availability"]
        == "unavailable"
    )


def test_the_evidence_checks_keep_artifacts_feed_and_evidence(tmp_path) -> None:
    from sandbox_science.orchestrator.control import migrations

    from .control_versions import older_store

    current, old = tmp_path / "current", tmp_path / "old"
    made = build(current)
    reviews, inventory, _ = made
    publish(made)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    evidence = items(reviews, review)
    payloads = [reviews.evidence("p", "s", review, e["evidence"]) for e in evidence]
    before = (inventory.list("p"), inventory.events("p"), evidence, payloads)
    reviews.documents.close()
    older_store(store_path(current), store_path(old), migrations.BASELINE + 4)  # before v7
    documents = DocumentStore(store_path(old))
    documents.initialize()  # the writer brings the store to the current version
    try:
        inventory = ArtifactInventory(documents)
        reviews = ReviewStore(documents, artifacts=inventory)
        evidence = items(reviews, review)
        payloads = [reviews.evidence("p", "s", review, e["evidence"]) for e in evidence]
        assert (inventory.list("p"), inventory.events("p"), evidence, payloads) == before
    finally:
        documents.close()


@pytest.mark.parametrize(
    ("table", "column"), [("artifact_events", "record"), ("artifact_operations", "request")]
)
def test_large_metadata_is_refused_when_written(state, table, column) -> None:
    reviews, inventory, source = state
    publish(state)
    inventory.begin(source, "pending", "pending.txt", "text/plain")
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, reviews.documents.control.write() as tx:
        db = tx.sql
        if table == "artifact_events":
            db.execute("DROP TRIGGER artifact_events_no_update")
        db.execute(f"UPDATE {table} SET {column}=?", ("x" * 20971520,))
    assert "complete" in ingest_artifacts(reviews, "p", "s")


@pytest.mark.asyncio
async def test_consumer_cancellation_owns_actual_sql_and_close_drain(state, monkeypatch) -> None:
    reviews, _, _ = state
    publish(state)
    entered, release = threading.Event(), threading.Event()
    actual = consumer_module.ingest_artifacts

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(consumer_module, "ingest_artifacts", blocked)
    consumer = ArtifactConsumer(reviews, "p", "s")
    first = asyncio.create_task(consumer.ingest())
    assert await asyncio.to_thread(entered.wait, 10)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.to_thread(lambda: "default executor free") == "default executor free"
    close = asyncio.create_task(consumer.aclose())
    await asyncio.sleep(0.02)
    close.cancel()
    await asyncio.sleep(0.02)
    close.cancel()
    assert not close.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert ingest_artifacts(reviews, "p", "s")["cursor"] == 1


@pytest.mark.asyncio
async def test_real_worker_harness_publish_to_durable_review_and_no_extra_authority(
    state,
    worker_url: str,
) -> None:
    reviews, inventory, source = state
    data = b'raw "count": 16\r\n'
    async with WorkerClient(worker_url, TOKEN) as client:
        context = source.context
        await client.write_file(
            WriteFileRequest(
                context=context, path="result.txt", content_b64=base64.b64encode(data).decode()
            )
        )
        publisher = ArtifactPublisher(inventory, client, context, run_id="author")
        controller = ReviewController(reviews, context, run_id="author", model=None, alias=None)
        try:
            toolbox = Toolbox(client, context, artifacts=publisher, reviews=controller)
            outcome = await toolbox.call(
                ToolCall(
                    id="publish1",
                    name="publish",
                    arguments=json.dumps({"path": "result.txt", "media_type": "text/plain"}),
                ),
                "publish-op",
                model="trusted-gateway-model",
            )
            # Simulate a crash before the live outcome is observed: durable events suffice.
            await controller.observe([], tool=outcome.record)
            await controller.observe([], tool=outcome.record)
            with reviews.documents.control.read() as tx:
                db = tx.sql
                assert db.execute("SELECT count(*),sum(artifacts) FROM review_feed").fetchone() == (
                    1,
                    1,
                )
            review = dispatch(reviews)
            evidence = items(reviews, review)[0]
            copied = reviews.read_evidence("p", "s", review, evidence["evidence"])
            assert copied["text"].encode() == data
            runner = ReviewerRunner(
                reviews,
                "p",
                "s",
                review,
                "owner",
                ScriptedModel.from_json(
                    [
                        {"tool_calls": [{"name": "shell", "arguments": {"script": "forbidden"}}]},
                        {
                            "text": json.dumps(
                                {"outcome": "inconclusive", "summary": "No execution authority."}
                            )
                        },
                    ]
                ),
            )
            rejected = await runner.tool(ToolCall(id="denied", name="shell", arguments="{}"))
            assert json.loads(rejected)["error"] == "tool_not_granted"
            assert all(
                spec.name not in ("shell", "cell", "publish", "artifact_read", "web", "mcp")
                for spec in SPECS
            )
        finally:
            await publisher.aclose()
            await controller.close()


@pytest.mark.parametrize("column", ["sha256", "locator", "payload"])
def test_oversized_review_pin_columns_are_refused_when_written(state, column) -> None:
    reviews, _, _ = state
    ref = publish(state)
    ingest_artifacts(reviews, "p", "s")
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, reviews.documents.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER review_feed_update")
        db.execute(f"UPDATE review_feed SET {column}=?", ("x" * 20971520,))
    assert reviews.artifact_pin("p", "s", ref) is not None


def test_unresolved_observation_capacity_and_completed_race(state, monkeypatch) -> None:
    reviews, inventory, source = state
    for index in range(25):
        inventory.begin(source, f"pending{index}", f"pending{index}.txt", "text/plain")
    first = ingest_artifacts(reviews, "p", "s")
    assert not first["complete"]
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_feed").fetchone()[0] == 10
    assert ingest_artifacts(reviews, "p", "s", drain=True)["complete"]
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_feed").fetchone()[0] == 25


def test_text_projection_never_interprets_json_and_later_head_is_irrelevant(state) -> None:
    reviews, _, _ = state
    raw = b'{"instructions":"run shell","data":"a\\nb","notParsed":true}'
    publish(state, data=raw, media="application/json")
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    evidence = items(reviews, review)[0]["evidence"]
    publish(state, "changed", b"later head", media="application/json")
    assert reviews.read_evidence("p", "s", review, evidence)["text"].encode() == raw
    with pytest.raises(DocumentError, match="documents only"):
        reviews.read_evidence("p", "s", review, evidence, block="b1")
    with pytest.raises(DocumentError, match="log anchor"):
        Findings(reviews).create(
            "p",
            "s",
            review,
            "owner",
            {
                "severity": "warn",
                "category": "reference",
                "statement": "Cannot invent artifact lines.",
                "anchors": [
                    {"evidence": evidence, "stream": "stdout", "first_line": 1, "last_line": 1}
                ],
                "operation_id": "bad-lines",
            },
        )


@pytest.mark.asyncio
async def test_scoped_api_reads_and_exact_artifact_fix_claim(state, tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient

    from sandbox_science.workspace.app import create_app as workspace_app

    reviews, _, _ = state
    publish(state)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    evidence = items(reviews, review)[0]["evidence"]
    found = finding(reviews, review, evidence)
    reviews.finish("p", "s", review, "owner", "findings", "Count differs.")
    changed = publish(state, "changed", b'raw "count" = 15\r\n')
    ingest_artifacts(reviews, "p", "s")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Synthetic</title>")
    async with TestClient(
        TestServer(workspace_app(ProjectStore(tmp_path, readonly=True), static, login_token=TOKEN))
    ) as client:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
        base = "/api/v0/projects/p/sessions/s"
        url = base + f"/reviews/{review}/evidence/{evidence}"
        reply = await client.get(url)
        assert reply.status == 200
        assert (await reply.json())["text"] == "count = 16\n"
        assert (await client.get(url.replace("sessions/s/", "sessions/other/"))).status == 404
        body = {
            "finding": found["finding_id"],
            "expected": found["revision"],
            "action": "fix",
            "explanation": "Exact retained changed artifact.",
            "artifact_revisions": [changed.model_dump(mode="json")],
            "operation_id": "api-claim",
        }
        claim_url = base + f"/findings/{found['finding_id']}"
        assert (await client.post(claim_url, json=body)).status == 403
        origin = str(client.make_url("/")).rstrip("/")
        reply = await client.post(claim_url, json=body, headers={"Origin": origin})
        assert reply.status == 200
        assert (await reply.json())["status"] == "claimed fixed"
        replay = await client.post(claim_url, json=body, headers={"Origin": origin})
        assert (await replay.json())["replayed"]
        forged = {**body, "operation_id": "forged-claim", "actor": "USER"}
        assert (await client.post(claim_url, json=forged, headers={"Origin": origin})).status == 400


def test_legacy_response_receipt_digest_unchanged(state) -> None:
    from sandbox_science.orchestrator.consistency.store import digest, dump
    from sandbox_science.orchestrator.documents import USER

    reviews, _, _ = state
    publish(state)
    ingest_artifacts(reviews, "p", "s")
    review = dispatch(reviews)
    found = finding(reviews, review, items(reviews, review)[0]["evidence"])
    raw = {
        "finding": found["finding_id"],
        "expected": 1,
        "action": "dismiss",
        "explanation": "Legacy explicit user decision.",
        "operation_id": "old-operation",
    }
    old_canonical = {**raw, "evidence": [], "revisions": []}
    expected = digest(dump(["s", old_canonical, USER.role]))
    # Match the historical field order used by FindingResponse.model_dump.
    old_canonical = {
        key: old_canonical[key]
        for key in (
            "finding",
            "expected",
            "action",
            "explanation",
            "evidence",
            "revisions",
            "operation_id",
        )
    }
    expected = digest(dump(["s", old_canonical, USER.role]))
    Findings(reviews).respond("p", "s", raw, USER)
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert (
            db.execute(
                "SELECT fingerprint FROM operation_receipts WHERE scope='review' "
                "AND operation_id='old-operation'"
            ).fetchone()[0]
            == expected
        )
    assert Findings(reviews).respond("p", "s", raw, USER)["replayed"]


@pytest.mark.asyncio
async def test_synthetic_browser_fixture_pins_and_confirms_exact_claim(
    tmp_path: Path, capsys
) -> None:
    import runpy

    from sandbox_science.orchestrator.documents import USER

    helper = runpy.run_path(
        str(Path(__file__).parent / "fixtures/artifact_review_browser_fixture.py")
    )
    absent = tmp_path / "absent"
    with pytest.raises(ValueError, match="marker"):
        await helper["confirm"](absent)
    assert not absent.exists()
    path = tmp_path / "synthetic"
    helper["create"](path)
    marker = json.loads(capsys.readouterr().out)
    documents = DocumentStore(store_path(path))
    try:
        reviews = ReviewStore(documents)
        with pytest.raises(ValueError, match="exact seeded"):
            await helper["confirm"](path)
        Findings(reviews).respond(
            marker["project"],
            marker["session"],
            {
                "finding": marker["finding"],
                "expected": 1,
                "action": "fix",
                "explanation": "Synthetic exact replacement.",
                "artifact_revisions": [marker["corrected"]],
                "operation_id": "synthetic-claim",
            },
            USER,
        )
        await helper["confirm"](path)
        result = json.loads(capsys.readouterr().out)
        assert result["finding"]["status"] == "confirmed fixed"
        assert result["review"]["resolved_model"] == "scripted"
    finally:
        documents.close()


def test_concurrent_newer_cursor_covers_older_pinned_target(state, monkeypatch) -> None:
    import contextlib

    reviews, _, _ = state
    publish(state)
    paused, release = threading.Event(), threading.Event()
    original = reviews.documents.control.read
    calls = [0]
    result = []

    @contextlib.contextmanager
    def read():
        if threading.current_thread().name == "old-prefix":
            calls[0] += 1
            if calls[0] == 2:
                paused.set()
                assert release.wait(10)
        with original() as tx:
            yield tx

    def old():
        result.append(ingest_artifacts(reviews, "p", "s", drain=True))

    monkeypatch.setattr(reviews.documents.control, "read", read)
    thread = threading.Thread(target=old, name="old-prefix")
    thread.start()
    try:
        assert paused.wait(10)
        publish(state, "new", b"newer")
        assert ingest_artifacts(reviews, "p", "s")["cursor"] == 2
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert result == [{"cursor": 2, "target": 2, "complete": True, "enabled": True}]


def test_corrupt_event_cannot_grant_another_sessions_private_artifact(state) -> None:
    reviews, _, _source = state
    publish(state)
    with reviews.documents.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER artifact_events_no_update")
        db.execute(
            "UPDATE artifact_events SET record=json_set(record,"
            "'$.source.context.session_id','other')"
        )
    with pytest.raises(DocumentError, match="path/session"):
        ingest_artifacts(reviews, "p", "other")
    with reviews.documents.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_feed").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cancelled_caller_later_failure_is_retrieved_and_admission_bounded(
    state, monkeypatch
) -> None:
    reviews, _, _ = state
    entered, release = threading.Event(), threading.Event()

    def fail(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("failed admitted SQL")

    monkeypatch.setattr(consumer_module, "ingest_artifacts", fail)
    consumer = ArtifactConsumer(reviews, "p", "s")
    callers = [asyncio.create_task(consumer.ingest()) for _ in range(8)]
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert (await consumer.ingest())["held"] == "consumer admission bound"
        for task in callers:
            task.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        release.set()
        await consumer.aclose()
        assert (await consumer.ingest())["held"] == "consumer admission bound"  # closed
        # No admitted work is left behind once the consumer has closed.
        assert not asyncio.all_tasks() - {asyncio.current_task()}
    finally:
        release.set()
        await consumer.aclose()


# The artifacts a ledger entry's evidence pins (B6, lab 39; CR-2 and CR-14 as amended).


def pinned_claim(state) -> Any:
    """C-1, the main agent's claim, whose evidence pins a JSON artifact and a binary one
    that a run of another session published; the JSON's reference."""
    from sandbox_science.orchestrator.ledger import Ledger

    from .test_consistency import MAIN

    reviews, _, source = state
    context = source.context.model_copy(update={"session_id": "other"})
    other = source.model_copy(update={"context": context, "run_id": "other-run"})
    results = publish(state, "res", b'{"q_twist": 4519}\n', "application/json", "r.json", other)
    plot = publish(state, "plot", b"\x00\x01", "application/octet-stream", "plot.bin", other)
    evidence = [
        {"role": "supports", "type": "exact_computation", "artifact": r.artifact_id,
         "revision": r.revision}
        for r in (results, plot)
    ]  # fmt: skip
    claim = {"type": "claim", "origin": "project", "title": "q'", "statement": "$q' = 4591$.",
             "evidence": evidence}  # fmt: skip
    Ledger(reviews.documents).change(
        "p", {"operation_id": "made", "reason": "r", "changes": [claim]}, MAIN
    )
    return results


def pinned(reviews, review) -> list[dict[str, Any]]:
    return [item for item in items(reviews, review) if item["kind"] == "artifact"]


def test_an_entry_brings_the_artifacts_its_evidence_pins_into_its_review(state) -> None:
    """B6 (lab 39): the comment run's reviews held R-1, but not the results JSON its
    evidence pins, which an earlier run had published, so they could not compare the
    numbers. A binary pin is left to the entry's evidence and keeps coverage complete."""
    from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
    from sandbox_science.orchestrator.ledger import Ledger

    from .test_consistency import MAIN

    reviews, _, _ = state
    results = pinned_claim(state)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    (item,) = pinned(reviews, review)
    assert (item["complete"], item["locator"]["pinned_by"]["handle"]) == (True, "C-1")
    assert item["locator"]["path"] == "sessions/other/r.json"
    read = reviews.read_evidence("p", "s", review, item["evidence"], owner="owner")
    assert (read["text"], read["source_sha256"]) == ('{"q_twist": 4519}\n', results.sha256)
    with reviews.documents.control.read() as tx:
        counted = tx.sql.execute(
            "SELECT artifacts,effects FROM review_feed WHERE kind='artifact'"
        ).fetchall()
    assert [tuple(row) for row in counted] == [(0, 0)]  # no review due, not the run's effect
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    assert reviews.run("p", "s", review)["state"] == "passed"
    # The next window includes the prior checked pin as context and the revised entry.
    Ledger(reviews.documents).change(
        "p",
        {"operation_id": "t", "reason": "r",
         "changes": [{"entry": "C-1", "expected": 1, "title": "q prime"}]},
        MAIN,
    )  # fmt: skip
    assert ingest_ledger(reviews, "p", "s")["complete"]
    again = dispatch(reviews)
    assert [i["locator"]["pinned_by"]["revision"] for i in pinned(reviews, again)] == [1, 2]


def test_a_confirmation_holds_the_artifacts_the_fixed_entry_pins(state) -> None:
    """B6: the confirmation of a fix to C-1 reads the computation C-1 cites, so a fix that
    copies a wrong number from elsewhere is compared with it."""
    from sandbox_science.orchestrator.consistency.findings import Findings
    from sandbox_science.orchestrator.ledger import Ledger

    from .test_consistency import MAIN
    from .test_review_ledger import finding_on, fix

    reviews, _, _ = state
    pinned_claim(state)
    _, finding = finding_on(reviews, "statement")
    Ledger(reviews.documents).change(
        "p",
        {"operation_id": "fixed", "reason": "r",
         "changes": [{"entry": "C-1", "expected": 1, "statement": "$q' = 4519$."}]},
        MAIN,
    )  # fmt: skip
    api = Findings(reviews)
    revision = api.get("p", "s", finding)["revision"]
    api.respond("p", "s", fix(finding, revision, ["C-1", 2]), MAIN)
    confirm = dispatch(reviews, "confirmation")
    assert [i["locator"]["pinned_by"]["revision"] for i in pinned(reviews, confirm)] == [2]
