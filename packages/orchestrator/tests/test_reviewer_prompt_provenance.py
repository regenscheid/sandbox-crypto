# SPDX-License-Identifier: MIT
"""Reserved reviewer pins match actual input without changing review authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandbox_science.orchestrator.consistency import prompt_bundle
from sandbox_science.orchestrator.consistency import runner as running
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.prompt_bundle import (
    MAX_PROVENANCE_BYTES,
    assemble_reviewer,
    encode_provenance,
)
from sandbox_science.orchestrator.consistency.rubric import RUBRIC, RUBRIC_VERSION
from sandbox_science.orchestrator.consistency.runner import SPECS, ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.model import (
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.projects import ProjectStore


@pytest.fixture
def store(tmp_path):
    projects = ProjectStore(tmp_path)
    for project in ("p", "q"):
        projects.create(project, "minimal", "sha256:" + "a" * 64)
        for session in ("s", "other"):
            projects.open_session(project, session, dedicated=False)
    documents = DocumentStore(store_path(tmp_path))
    reviews = ReviewStore(documents, clock=lambda: 1000)
    reviews.observe("p", "s", "m1", "transcript", {"run": "main"}, {"content": "A claim."})
    review = reviews.dispatch("p", "s", "terminal", author_run="main", owner="owner", alias="alias")
    assert review is not None
    yield reviews, documents, review
    documents.close()


def bundle():
    return assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS)


def reserve(reviews, review, **kwargs):
    return reviews.reserve_call("p", "s", review, 100, owner="owner", **kwargs)


def finish():
    return ModelTurn(
        '{"outcome":"passed","summary":"Checked.","cap_reached":false}', model="actual"
    )


def projection(specs):
    return [
        {"name": s.name, "description": s.description, "parameters": s.parameters} for s in specs
    ]


def test_exact_historical_text_ordered_schema_pins_and_owned_inputs():
    original = bundle()
    pin = original.provenance
    assert pin.rubric_text == RUBRIC
    assert pin.rubric_version == RUBRIC_VERSION
    assert pin.rubric_bytes == len(RUBRIC.encode())
    assert pin.rubric_sha256 == hashlib.sha256(RUBRIC.encode()).hexdigest()
    assert pin.tool_names == tuple(s.name for s in SPECS)
    assert pin.tool_count == 6
    assert len(encode_provenance(pin).encode()) <= MAX_PROVENANCE_BYTES
    assert assemble_reviewer(RUBRIC + "\n", RUBRIC_VERSION, SPECS).provenance != pin
    assert assemble_reviewer(RUBRIC, RUBRIC_VERSION, tuple(reversed(SPECS))).provenance != pin
    copied = tuple(
        ToolSpec(s.name, s.description, dict(reversed(s.parameters.items()))) for s in SPECS
    )
    reordered = assemble_reviewer(RUBRIC, RUBRIC_VERSION, copied)
    assert reordered.provenance == pin
    array = ToolSpec("read", "Read", {"required": ["a", "b"]})
    reverse = ToolSpec("read", "Read", {"required": ["b", "a"]})
    assert (
        assemble_reviewer(RUBRIC, RUBRIC_VERSION, [array]).provenance
        != assemble_reviewer(RUBRIC, RUBRIC_VERSION, [reverse]).provenance
    )


@pytest.mark.parametrize(
    "change",
    [
        {"role": "main"},
        {"format_version": True},
        {"rubric_version": "bad version"},
        {"rubric_text": "changed"},
        {"rubric_bytes": False},
        {"rubric_sha256": "0" * 64},
        {"tool_names": ("shell",)},
        {"tool_count": True},
        {"schema_bytes": -1},
        {"schemas_sha256": []},
        {"bundle_sha256": "0" * 64},
    ],
)
def test_unvalidated_copied_pin_never_charges_reservation(store, change):
    reviews, _documents, review = store
    with pytest.raises(ValueError, match="reviewer"):
        reserve(reviews, review, prompt_provenance=bundle().provenance.model_copy(update=change))
    assert reviews.run("p", "s", review)["calls"] == 0


def test_unrepresentable_installed_input_and_copied_overflow_refuse_before_encoding(monkeypatch):
    for rubric in ("x" * 65537, "\ud800", "\0" * 10000):
        with pytest.raises(ValueError, match="reviewer"):
            assemble_reviewer(rubric, RUBRIC_VERSION, SPECS)
    invalid = bundle().provenance.model_copy(update={"rubric_text": "x" * (20 * 1024 * 1024)})

    def unexpected(value):
        pytest.fail("oversized copied rubric reached encoding")

    monkeypatch.setattr(prompt_bundle, "_json", unexpected)
    with pytest.raises(ValueError, match="rubric"):
        encode_provenance(invalid)


@pytest.mark.asyncio
async def test_actual_multi_turn_input_is_reserved_before_each_call(store):
    reviews, documents, review = store
    expected = bundle().provenance
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    unchanged = json.dumps(projection(SPECS))

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert messages[0] == {"role": "system", "content": RUBRIC}
            assert json.dumps(projection(tools)) == unchanged
            assert json.loads(messages[1]["content"]) == {
                "review": review,
                "instruction": "Read review_manifest first. Check only this pinned snapshot.",
            }
            with sqlite3.connect(documents.path) as db:
                call, answered = db.execute(
                    "SELECT seq,answered FROM review_calls ORDER BY seq DESC LIMIT 1"
                ).fetchone()
            assert answered == 0
            assert reviews.run("p", "s", review)["calls"] == self.calls
            assert reviews.call_provenance("p", "s", review, call).provenance == expected
            if self.calls == 1:
                return ModelTurn(None, (ToolCall("m", "review_manifest", "{}"),), model="actual")
            if self.calls == 2:
                return ModelTurn(
                    None,
                    (
                        ToolCall("r", "review_read", json.dumps({"evidence": evidence})),
                        ToolCall(
                            "c",
                            "review_checked",
                            json.dumps({"statement": "Read.", "evidence": [evidence]}),
                        ),
                    ),
                    model="actual",
                    input_tokens=2,
                    output_tokens=3,
                )
            return finish()

    model = Model()
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    result = reviews.run("p", "s", review)
    assert result["state"] == "passed"
    assert result["calls"] == 3
    assert result["alias"] == "alias"
    assert result["resolved_model"] == "actual"
    assert (result["input_tokens"], result["output_tokens"]) == (2, 3)
    assert json.dumps(projection(SPECS)) == unchanged


def test_atomic_pin_counter_rollback_owner_and_concurrent_budget(store):
    reviews, documents, review = store
    pin = bundle().provenance
    with documents.control.write() as tx:
        db = tx.sql
        db.execute(
            "CREATE TRIGGER fail_counter BEFORE UPDATE OF calls ON review_runs "
            "BEGIN SELECT RAISE(ABORT, 'synthetic counter failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic counter"):
        reserve(reviews, review, prompt_provenance=pin)
    with documents.control.write() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM review_calls").fetchone() == (0,)
        assert db.execute(
            "SELECT calls,estimated_tokens FROM review_runs WHERE review_id=?", (review,)
        ).fetchone() == (0, 0)
        db.execute("DROP TRIGGER fail_counter")
        config = ReviewConfig(calls_per_hour=3).model_dump_json()
        db.execute("UPDATE review_runs SET config=? WHERE review_id=?", (config, review))
    with pytest.raises(DocumentError, match="owns a live grant"):
        reviews.reserve_call("p", "s", review, 100, owner="stale", prompt_provenance=pin)
    with ThreadPoolExecutor(max_workers=8) as pool:
        calls = list(pool.map(lambda _: reserve(reviews, review, prompt_provenance=pin), range(20)))
    assert len([c for c in calls if c is not None]) == 3
    assert reviews.run("p", "s", review)["calls"] == 3


@pytest.mark.parametrize("call", [False, 0, 2**63])
def test_exact_reader_refuses_invalid_reservation_ids(store, call):
    reviews, _documents, review = store
    with pytest.raises(DocumentError, match="reservation ID"):
        reviews.call_provenance("p", "s", review, call)


def test_historical_text_usage_idempotence_immutable_pin_and_foreign_scope(store):
    reviews, documents, review = store
    historical = assemble_reviewer("Earlier original text ∀", RUBRIC_VERSION, SPECS).provenance
    call = reserve(reviews, review, prompt_provenance=historical)
    assert call is not None
    for _ in range(2):
        reviews.call_usage(
            "p", "s", review, call, model="actual", input_tokens=2, output_tokens=3, owner="owner"
        )
    assert reviews.run("p", "s", review)["input_tokens"] == 2
    assert ReviewStore(documents).call_provenance("p", "s", review, call).provenance == historical
    for project, session in (("q", "s"), ("p", "other")):
        assert reviews.call_provenance(project, session, review, call).status == "absent"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), documents.control.write() as tx:
        tx.sql.execute("UPDATE review_calls SET prompt_provenance=NULL WHERE seq=?", (call,))


@pytest.mark.parametrize("kind", ["oversized", "utf8", "invalid", "digest", "version"])
def test_corrupt_retained_metadata_refuses_before_large_python_allocation(store, kind):
    reviews, documents, review = store
    call = reserve(reviews, review, prompt_provenance=bundle().provenance)
    assert call is not None
    values = {
        "oversized": "x" * (20 * 1024 * 1024),
        "utf8": b"\xff",
        "invalid": "{",
        "digest": encode_provenance(bundle().provenance).replace('"reviewer"', '"main"', 1),
        "version": encode_provenance(assemble_reviewer(RUBRIC, "old-rubric", SPECS).provenance),
    }
    with documents.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER review_calls_prompt_immutable")
        sql = (
            "UPDATE review_calls SET prompt_provenance=CAST(? AS TEXT) WHERE seq=?"
            if kind == "utf8"
            else "UPDATE review_calls SET prompt_provenance=? WHERE seq=?"
        )
        db.execute(sql, (values[kind], call))
    tracemalloc.start()
    try:
        result = reviews.call_provenance("p", "s", review, call)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.status == "corrupt"
    assert result.provenance is None
    assert peak < 256 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "timeout", "cancel", "renew", "usage"])
async def test_failed_attempt_keeps_exact_charge_pin_and_unknown_model(store, monkeypatch, failure):
    reviews, documents, review = store
    entered = asyncio.Event()
    budgets: list[asyncio.Timeout] = []
    if failure == "timeout":
        # The review's own time budget ends while its model call runs. A 20 ms budget
        # could end first, under load, before the call was reserved, and then there was
        # no call to read: so the model's call ends the real budget itself.
        timeout = asyncio.timeout

        def budget(delay: float | None) -> asyncio.Timeout:
            found = timeout(delay)
            if delay == ReviewConfig().time_seconds:
                budgets.append(found)
            return found

        monkeypatch.setattr(asyncio, "timeout", budget)

    class Model:
        async def complete(self, messages, tools):
            entered.set()
            if failure == "model":
                raise ModelError("test-secret-reviewer-not-a-credential")
            if failure == "timeout":
                (review_budget,) = budgets
                review_budget.reschedule(asyncio.get_running_loop().time())
            if failure in {"timeout", "cancel"}:
                await asyncio.Event().wait()
            return finish()

    def refuse(*args, **kwargs):
        raise DocumentError("conflict", "synthetic accounting failure")

    if failure in {"renew", "usage"}:
        monkeypatch.setattr(reviews, failure if failure == "renew" else "call_usage", refuse)
    task = asyncio.create_task(ReviewerRunner(reviews, "p", "s", review, "owner", Model()).run())
    # Bounds against a hang only: the run's own steps take what the machine gives them.
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 30)
    with documents.control.read() as tx:
        db = tx.sql
        call, answered = db.execute("SELECT seq,answered FROM review_calls").fetchone()
    assert answered == 0
    pin = reviews.call_provenance("p", "s", review, call).provenance
    assert pin == bundle().provenance
    assert "test-secret" not in encode_provenance(pin)
    result = reviews.run("p", "s", review)
    assert result["calls"] == 1
    assert result["resolved_model"] is None
    assert result["state"] == {"cancel": "dropped", "timeout": "capped"}.get(failure, "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["reserve_call", "call_usage", "finish"])
async def test_accounting_threads_drain_repeated_cancellation_before_owner_close(
    store, monkeypatch, seam
):
    reviews, documents, review = store
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original, close = getattr(reviews, seam), documents.close

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(reviews, seam, blocked)

    def closing():
        close()
        closed.set()

    monkeypatch.setattr(documents, "close", closing)

    async def host():
        try:
            await ReviewerRunner(
                reviews, "p", "s", review, "owner", ScriptedModel([finish()])
            ).run()
        finally:
            documents.close()

    task = asyncio.create_task(host())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done()
        assert not closed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert closed.is_set()
    # Inspect committed state without reopening the closed borrowed owner.
    with sqlite3.connect(documents.path) as db:
        answered = db.execute("SELECT answered FROM review_calls").fetchone()[0]
        state, calls = db.execute(
            "SELECT state,calls FROM review_runs WHERE review_id=?", (review,)
        ).fetchone()
    assert calls == 1
    assert answered == (0 if seam == "reserve_call" else 1)
    assert state == ("inconclusive" if seam == "finish" else "dropped")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["version", "schema", "no_model"])
async def test_invalid_installed_capability_or_missing_model_never_reserves(
    store, monkeypatch, invalid
):
    reviews, _documents, review = store
    model = ScriptedModel([finish()])
    if invalid == "version":
        monkeypatch.setattr(running, "RUBRIC_VERSION", "different-version")
    if invalid == "schema":
        monkeypatch.setattr(running, "SPECS", (ToolSpec("shell", "Invalid", {"value": object()}),))
    await ReviewerRunner(
        reviews, "p", "s", review, "owner", None if invalid == "no_model" else model
    ).run()
    assert model.requests == []
    assert reviews.run("p", "s", review)["calls"] == 0
