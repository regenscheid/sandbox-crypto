# SPDX-License-Identifier: MIT
"""Document-only D82: durable ownership, exact mechanical evidence and closing bounds."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from sandbox_science.orchestrator import mathchecks
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.mathcheck import Formula, MathCheck
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs, MathLimits
from sandbox_science.orchestrator.mathchecks.notices import MathNotices
from sandbox_science.orchestrator.projects import ProjectStore

MAIN = Author(kind="agent", session_id="s", run_id="r1", role="main", model="scripted")
OTHER = MAIN.model_copy(update={"session_id": "other", "run_id": "r2"})
BAD = r"$\unknown{x}$"


class ScriptedChecker:
    def __init__(
        self,
        status: Literal["accepted", "rejected", "unavailable"] = "rejected",
        error: str = "Unknown command",
    ) -> None:
        self.status: Literal["accepted", "rejected", "unavailable"] = status
        self.error = error
        self.calls: list[Formula] = []

    def identity(self) -> dict[str, object]:
        return {"bundle_sha256": "a" * 64, "launcher_sha256": "b" * 64, "engine": "0.18.9"}

    async def check_many(self, formulas: list[Formula]) -> list[MathCheck]:
        self.calls.extend(formulas)
        return [
            MathCheck(
                id=f.id,
                status=self.status,
                error=self.error if self.status != "accepted" else None,
                remedy="x" if self.status == "rejected" else None,
                input_sha256=hashlib.sha256(f.model_dump_json().encode()).hexdigest(),
                bundle_sha256="a" * 64,
                launcher_sha256="b" * 64,
                engine="0.18.9",
                runtime="22.0.0",
            )
            for f in formulas
        ]


@pytest.fixture
def stores(tmp_path: Path):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    docs = DocumentStore(store_path(tmp_path))
    mathchecks.subscribe(docs)
    clock = [1000.0]
    reviews = ReviewStore(docs, clock=lambda: clock[0])
    yield docs, reviews, clock
    docs.close()


def create(
    docs: DocumentStore, author: Author = MAIN, text: str = BAD + "\n\nOther paragraph.\n"
) -> str:
    return docs.create(
        "p", name="notes", title="Notes", text=text, author=author, session_id=author.session_id
    ).document_id


async def drain(reviews: ReviewStore, checker: ScriptedChecker | None = None) -> None:
    jobs = MathJobs(reviews)
    for _ in range(200):
        check = jobs.claim("p", "owner")
        if check is None:
            return
        while not await jobs.step(check, "owner", checker):
            pass
    pytest.fail("math queue did not drain")


def get(reviews: ReviewStore, session: str = "s"):
    return Findings(reviews).listing("p", session)["findings"]


def patch(docs: DocumentStore, doc: str, tex: str, op: str, author: Author = MAIN) -> None:
    mapped = docs.mapped("p", doc)
    result = docs.patch(
        "p",
        doc,
        base=docs.document("p", doc).head,
        author=author,
        reason="Edit formula",
        operation_id=op,
        operations=[{"op": "replace_math", "math": mapped.span_ids[0], "tex": tex}],
    )
    assert result.outcome == "applied"


@pytest.mark.asyncio
async def test_agent_origin_and_user_exclusion(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    (found,) = get(reviews)
    assert (found["origin"], found["category"], found["severity"], found["review_id"]) == (
        "math",
        "math",
        "warn",
        None,
    )
    assert found["source"]["kind"] == "math"
    assert FindingNotices(reviews).pending("p", "s", "r1", set()) == []
    assert FindingNotices(reviews).counts("p", "s") == {}
    assert reviews.confirmation_targets("p", "s") == []
    notices = MathNotices(reviews).pending("p", "s", "r1", set())
    assert len(notices) == 1
    docs.edit_source(
        "p",
        doc,
        base=1,
        text=BAD + "\n\nUnrelated USER change.\n",
        author=USER,
        reason="Other block",
        operation_id="user-other",
    )
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert get(reviews)[0]["reflags"] == 0
    assert MathNotices(reviews).pending("p", "s", "r1", set())
    patch(docs, doc, r"\unknown{y}", "user-takes-over", USER)
    await drain(reviews, checker)
    assert get(reviews)[0]["status"] == "open"
    assert get(reviews)[0]["reflags"] == 0
    assert MathNotices(reviews).pending("p", "s", "r1", set()) == []
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT excluded FROM math_finding_state").fetchone()[0]


@pytest.mark.asyncio
async def test_unknown_user_and_reviewer_have_no_author_findings(stores) -> None:
    docs, reviews, _ = stores
    create(docs, USER)
    await drain(reviews, ScriptedChecker())
    assert get(reviews) == []
    # Real checks are retained even though there is no eligible recipient.
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT rejected FROM math_jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_two_failing_patches_reflag_but_unrelated_revisions_do_not(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    patch(docs, doc, r"\unknown{y}", "try-one")
    await drain(reviews, ScriptedChecker(error="New error"))
    (found,) = get(reviews)
    assert found["reflags"] == 1
    assert found["status"] == "open"
    assert "New error" in found["statement"]
    docs.edit_source(
        "p",
        doc,
        base=2,
        text=r"$\unknown{y}$" + "\n\nUnrelated again.\n",
        author=USER,
        reason="Other",
        operation_id="other",
    )
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 1
    patch(docs, doc, r"\unknown{z}", "try-two")
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 2
    assert get(reviews)[0]["status"] == "unaddressed"
    revision = get(reviews)[0]["revision"]
    for ordinal in (3, 4):
        patch(docs, doc, rf"\unknown{{attempt{ordinal}}}", f"try-{ordinal}")
        await drain(reviews, ScriptedChecker(error=f"Exact error {ordinal}"))
        current = get(reviews)[0]
        assert current["reflags"] == 2
        assert current["status"] == "unaddressed"
        assert current["revision"] == revision + ordinal - 2
        assert f"Exact error {ordinal}" in current["statement"]
        assert current["source"]["checked_revision"] == docs.document("p", doc).head


@pytest.mark.asyncio
async def test_accept_remove_and_unavailable_are_distinct(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    patch(docs, doc, "x", "repair", USER)
    await drain(reviews, None)
    assert get(reviews)[0]["status"] == "open"
    docs.edit_source(
        "p",
        doc,
        base=2,
        text="$x$\n\nOther revised.\n",
        author=USER,
        reason="Other",
        operation_id="rev3",
    )
    await drain(reviews, ScriptedChecker("accepted"))
    (found,) = get(reviews)
    assert found["status"] == "confirmed fixed"
    assert (
        Findings(reviews).get("p", "s", found["finding_id"])["history"][-1]["payload"]["action"]
        == "mechanical_accept"
    )
    patch(docs, doc, r"\unknown{x}", "again")
    await drain(reviews, ScriptedChecker())
    docs.edit_source(
        "p",
        doc,
        base=4,
        text="Other revised.\n",
        author=USER,
        reason="Remove formula",
        operation_id="remove",
    )
    await drain(reviews, ScriptedChecker())
    current = get(reviews)[0]
    assert current["status"] == "confirmed fixed"
    assert (
        Findings(reviews).get("p", "s", current["finding_id"])["history"][-1]["payload"]["action"]
        == "mechanical_removal"
    )


@pytest.mark.asyncio
async def test_old_revision_owner_and_stale_result_cannot_relabel_or_close(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    jobs = MathJobs(reviews)
    first = jobs.claim("p", "owner")
    assert first
    patch(docs, doc, r"\unknown{user}", "later-user", USER)
    # R1 is checked after USER R2: retained proof must still name the R1 agent.
    assert await jobs.step(first, "owner", ScriptedChecker("accepted"))
    with docs.control.read() as tx:
        db = tx.sql
        proof = json.loads(db.execute("SELECT payload FROM math_observations").fetchone()[0])
        assert proof["owner"]["author"]["session_id"] == "s"
        assert proof["owner"]["revision"] == 1
    assert get(reviews) == []
    await drain(reviews, ScriptedChecker())
    assert get(reviews) == []


@pytest.mark.asyncio
async def test_eligible_owner_switch_routes_access_and_notice_to_new_session(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    old = get(reviews)[0]
    patch(docs, doc, r"\unknown{other}", "other-agent", OTHER)
    await drain(reviews, ScriptedChecker(error="Other-session error"))
    assert get(reviews) == []
    (current,) = get(reviews, "other")
    assert current["finding_id"] == old["finding_id"]
    assert current["revision"] > old["revision"]
    assert "Other-session error" in current["statement"]
    assert MathNotices(reviews).pending("p", "s", "r1", set()) == []
    assert MathNotices(reviews).pending("p", "other", "r2", set())
    with pytest.raises(DocumentError):
        Findings(reviews).get("p", "s", old["finding_id"])


@pytest.mark.asyncio
async def test_late_accepted_check_cannot_close_newer_rejected_finding(stores) -> None:
    import asyncio

    docs, reviews, clock = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    patch(docs, doc, "x", "potential-repair")
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "old")
    assert check
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(ScriptedChecker):
        async def check_many(self, formulas):
            entered.set()
            await release.wait()
            return await super().check_many(formulas)

    old = asyncio.create_task(jobs.step(check, "old", Paused("accepted")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        patch(docs, doc, r"\unknown{newest}", "newer-rejection")
        clock[0] += 121
        assert jobs.claim("p", "new") == check
        assert await jobs.step(check, "new", ScriptedChecker("accepted"))
        await drain(reviews, ScriptedChecker(error="Newest rejection"))
        before = get(reviews)[0]
        assert before["status"] == "open"
        assert before["source"]["checked_revision"] == 3
    finally:
        release.set()
        with pytest.raises(DocumentError, match="ownership"):
            await old
    assert get(reviews)[0] == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("history_per_step", -1),
        ("history_per_step", 3),
        ("active", True),
        ("jobs", 0),
        ("observations", 1.1),
    ],
)
def test_limits_are_strict_and_bounded(field, value) -> None:
    with pytest.raises(ValueError, match="bounded math limit"):
        MathLimits(**{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [None, "unavailable"])
async def test_unchanged_revision_recovery_keeps_immutable_attempts(stores, initial) -> None:
    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, None if initial is None else ScriptedChecker(initial))
    assert get(reviews) == []
    jobs = MathJobs(reviews)
    jobs.retry_unavailable("p", ScriptedChecker().identity())
    await drain(reviews, ScriptedChecker())
    assert len(get(reviews)) == 1
    with docs.control.read() as tx:
        db = tx.sql
        rows = db.execute(
            "SELECT revision,unavailable,rejected FROM math_jobs ORDER BY rowid"
        ).fetchall()
        assert rows == [(1, 1, 0), (1, 0, 1)]
        assert db.execute("SELECT count(*) FROM math_observations").fetchone()[0] == 2
    # Recovery did not make a new source revision or reflag a duplicate check.
    assert docs.document("p", "notes", session_id="s").head == 1
    jobs.retry_unavailable("p", ScriptedChecker().identity())
    assert jobs.claim("p", "owner") is None


@pytest.mark.asyncio
async def test_effective_macro_change_rechecks_unchanged_agent_block(stores) -> None:
    docs, reviews, _ = stores
    original = "---\nmacros:\n  \\F: \\mathbb{F}\n---\n" + BAD + "\n"
    doc = create(docs, text=original)
    first = ScriptedChecker()
    await drain(reviews, first)
    docs.edit_source(
        "p",
        doc,
        base=1,
        text=original.replace("\\mathbb{F}", "\\unknown{F}"),
        author=USER,
        reason="Change macro",
        operation_id="macros",
    )
    second = ScriptedChecker()
    await drain(reviews, second)
    assert first.calls[0].macros != second.calls[0].macros
    assert get(reviews)[0]["reflags"] == 0
    assert get(reviews)[0]["source"]["owner_revision"] == 1


@pytest.mark.asyncio
async def test_config_change_retries_accepted_current_revision(stores) -> None:
    docs, reviews, _ = stores
    create(docs)
    accepted = ScriptedChecker("accepted")
    await drain(reviews, accepted)

    class Changed(ScriptedChecker):
        def identity(self):
            return {**super().identity(), "node": "changed-operator-config"}

    changed = Changed()
    MathJobs(reviews).retry_unavailable("p", changed.identity())
    await drain(reviews, changed)
    assert len(get(reviews)) == 1


@pytest.mark.asyncio
async def test_historical_owner_is_resumable_and_does_not_guess_latest_author(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    for revision in range(1, 8):
        docs.edit_source(
            "p",
            doc,
            base=revision,
            text=BAD + f"\n\nOther {revision}.\n",
            author=USER,
            reason="Other block",
            operation_id=f"other-{revision}",
        )
    MathJobs(reviews)
    # Check previous revisions first; only the current one can change findings.
    await drain(reviews, ScriptedChecker())
    (found,) = get(reviews)
    assert found["source"]["owner_revision"] == 1
    with docs.control.read() as tx:
        db = tx.sql
        proof = db.execute(
            "SELECT revision,author FROM math_job_owners WHERE check_id=?",
            (found["math_check_id"],),
        ).fetchone()
        assert proof[0] == 1
        assert json.loads(proof[1])["kind"] == "agent"
        assert db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] == 8


@pytest.mark.asyncio
async def test_lease_expiry_refuses_old_owner_and_preserves_exact_attempt(stores) -> None:
    docs, reviews, clock = stores
    create(docs)
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "old")
    assert check
    clock[0] += 121
    assert jobs.claim("p", "new") == check
    with pytest.raises(DocumentError, match="ownership"):
        await jobs.step(check, "old", ScriptedChecker())
    assert await jobs.step(check, "new", ScriptedChecker())
    assert len(get(reviews)) == 1


@pytest.mark.asyncio
async def test_logical_quota_holds_checks_without_vetoing_edits(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs)
    jobs = MathJobs(reviews, limits=MathLimits(observations=1))
    check = jobs.claim("p", "quota")
    assert check
    assert not await jobs.step(check, "quota", ScriptedChecker())
    docs.edit_source(
        "p",
        doc,
        base=1,
        text=BAD + "\n\nStill editable.\n",
        author=USER,
        reason="Other",
        operation_id="edit-quota",
    )
    with docs.control.read() as tx:
        db = tx.sql
        row = db.execute("SELECT target,completed,blocked FROM math_sources").fetchone()
        assert row[:2] == (2, 0)
        assert "quota" in row[2]
        assert db.execute("SELECT count(*) FROM math_observations").fetchone()[0] == 0


def test_save_hook_fault_rolls_source_and_event_back(stores) -> None:
    docs, _, _ = stores
    doc = create(docs)

    def broken(*args):
        raise RuntimeError("synthetic atomic math scheduling fault")

    docs.control.subscribe("document.revision_created", broken)
    with pytest.raises(RuntimeError, match="scheduling"):
        docs.edit_source(
            "p", doc, base=1, text="New text.\n", author=USER, reason="Edit", operation_id="fault"
        )
    assert docs.document("p", doc).head == 1
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT target FROM math_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_math_commit_fault_rolls_observation_finding_and_cursor_back(stores) -> None:
    docs, reviews, _ = stores
    create(docs)
    with docs.control.write() as tx:
        db = tx.sql
        db.execute(
            "CREATE TRIGGER synthetic_math_fault BEFORE INSERT ON math_notices "
            "BEGIN SELECT RAISE(ABORT,'synthetic fault'); END"
        )
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "owner")
    assert check
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="synthetic fault"):
        await jobs.step(check, "owner", ScriptedChecker())
    assert get(reviews) == []
    assert jobs.job(check)["cursor"] == 0
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM math_observations").fetchone()[0] == 0
    with docs.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER synthetic_math_fault")
    assert await jobs.step(check, "owner", ScriptedChecker())


@pytest.mark.asyncio
async def test_a_job_is_checked_when_written_and_bound_to_its_source(stores) -> None:
    docs, reviews, _ = stores
    create(docs)
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "owner")
    assert check
    for column, value in (("reason", "x" * 5000), ("lease_until", float("inf"))):
        refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
        with refused, docs.control.write() as tx:
            tx.sql.execute(
                f"UPDATE math_jobs SET {column}=? WHERE check_id=?",
                (value, check),
            )
    with docs.control.write() as tx:
        db = tx.sql
        db.execute("UPDATE math_jobs SET source_sha256=? WHERE check_id=?", ("b" * 64, check))
    checker = ScriptedChecker()
    with pytest.raises(DocumentError, match="digest"):
        await jobs.step(check, "owner", checker)
    assert checker.calls == []
    assert get(reviews) == []


@pytest.mark.asyncio
async def test_model_math_authority_denied_user_cas_unchanged(stores) -> None:
    from pydantic import ValidationError

    from sandbox_science.orchestrator.consistency.models import FindingInput

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    found = get(reviews)[0]
    raw = {
        "finding": found["finding_id"],
        "expected": 1,
        "action": "confirm",
        "explanation": "Explicit USER confirmation.",
        "operation_id": "decision",
    }
    with pytest.raises(DocumentError):
        Findings(reviews).respond("p", "s", raw, MAIN)
    result = Findings(reviews).respond("p", "s", raw, USER)
    assert result["status"] == "confirmed fixed"
    assert result["revision"] == 2
    assert Findings(reviews).respond("p", "s", raw, USER)["replayed"]
    with pytest.raises(DocumentError, match="changed"):
        Findings(reviews).respond("p", "s", {**raw, "operation_id": "stale"}, USER)
    with pytest.raises(ValidationError):
        FindingInput.model_validate(
            {
                "severity": "warn",
                "category": "math",
                "statement": "Invented",
                "anchors": [],
                "operation_id": "model-math",
            }
        )


@pytest.mark.asyncio
async def test_oversized_formula_is_unavailable_without_renderer_and_rest_continue(stores) -> None:
    docs, reviews, _ = stores
    create(docs, text="$" + "x" * 21000 + "$\n\n" + BAD + "\n")
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert len(checker.calls) == 1
    assert len(get(reviews)) == 1
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT spans,rejected,unavailable FROM math_jobs").fetchone() == (
            2,
            1,
            1,
        )


@pytest.mark.asyncio
async def test_unavailable_latest_attempt_blocks_old_notice_and_bound(stores) -> None:
    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    notices = MathNotices(reviews)
    first = notices.pending("p", "s", "run", set())[0]
    jobs = MathJobs(reviews)
    jobs.retry_unavailable("p", {**ScriptedChecker().identity(), "changed": True})
    check = jobs.claim("p", "owner")
    assert check
    assert notices.pending("p", "s", "run", set()) == []
    await jobs.step(check, "owner", ScriptedChecker("unavailable"))
    notices.bound("p", "s", "run", {first["notice_id"]})
    assert get(reviews)[0]["status"] == "open"
    assert notices.pending("p", "s", "run", set()) == []


@pytest.mark.asyncio
async def test_configuration_drift_during_await_and_mixed_runtime_are_unavailable(stores) -> None:
    docs, reviews, _ = stores
    create(docs, text=BAD + "\n\n" + BAD + "\n")

    class Drift(ScriptedChecker):
        async def check_many(self, formulas):
            result = await super().check_many(formulas)
            self.identity = lambda: {"changed": True}
            return result

    await drain(reviews, Drift())
    assert get(reviews) == []
    jobs = MathJobs(reviews)
    jobs.retry_unavailable("p")

    class Runtime(ScriptedChecker):
        async def check_many(self, formulas):
            result = await super().check_many(formulas)
            result[1] = result[1].model_copy(update={"runtime": "23.0.0"})
            return result

    await drain(reviews, Runtime())
    assert get(reviews) == []
    with docs.control.read() as tx:
        db = tx.sql
        assert (
            db.execute(
                "SELECT count(*) FROM math_observations WHERE status!='unavailable'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_closing_only_delivery_success_and_final_turn_pending(stores) -> None:
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathController
    from sandbox_science.orchestrator.model import ScriptedModel
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        final = MathController(reviews, "p", "s", "final", checker=ScriptedChecker())
        transcript = await run_task(
            ScriptedModel.from_json([{"text": "Final answer."}]),
            Toolbox(client, context, math_checks=final),
            "Continue",
            run_id="final",
            max_turns=1,
        )
        assert sorted(transcript.delivered_ids("math")) == []
        assert not any(
            "Mechanical math findings" in str(m.get("content")) for m in transcript.messages
        )
        assert MathNotices(reviews).pending("p", "s", "next", set())
        MathNotices(reviews).release("p", "s", "next")
        controller = MathController(reviews, "p", "s", "saved", checker=ScriptedChecker())
        transcript = await run_task(
            ScriptedModel.from_json(
                [{"text": "Before closing."}, {"text": "I have read the warning."}]
            ),
            Toolbox(client, context, math_checks=controller),
            "Continue",
            run_id="saved",
            max_turns=3,
        )
    assert transcript.turns == 2
    assert len(sorted(transcript.delivered_ids("math"))) == 1
    assert get(reviews)[0]["status"] == "open"
    assert get(reviews)[0]["reflags"] == 0
    # Delivered is not acknowledged before a durable save.
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT delivered_run FROM math_notices").fetchone()[0] is None
        assert db.execute("SELECT run FROM math_ignored").fetchone()[0] == "saved"


@pytest.mark.asyncio
async def test_failed_model_after_closing_notice_releases_it_unacknowledged(stores) -> None:
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathController
    from sandbox_science.orchestrator.model import ModelError, ModelTurn
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())

    class Failed:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("synthetic failed closing model")
            return ModelTurn("Before closing.")

    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    controller = MathController(reviews, "p", "s", "failed", checker=ScriptedChecker())
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            Failed(),
            Toolbox(client, context, math_checks=controller),
            "Continue",
            run_id="failed",
            max_turns=3,
        )
    assert (transcript.stopped, transcript.failure, sorted(transcript.delivered_ids("math"))) == (
        "error",
        "model",
        [],
    )
    assert controller.delivered == set()
    assert MathNotices(reviews).pending("p", "s", "next", set())


@pytest.mark.asyncio
async def test_actual_commit_thread_owned_through_repeated_cancellation(
    stores, monkeypatch
) -> None:
    import asyncio
    import threading

    from sandbox_science.orchestrator.mathchecks.controller import MathController

    docs, reviews, _ = stores
    create(docs)
    controller = MathController(reviews, "p", "s", "cancel", checker=ScriptedChecker())
    entered = threading.Event()
    release = threading.Event()
    original = controller.jobs.commit

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(controller.jobs, "commit", blocked)

    async def host():
        async with controller.active():
            controller.safe_point()
            await asyncio.Event().wait()

    task = asyncio.create_task(host())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT lease_until FROM math_jobs").fetchone()[0] > 1000
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(get(reviews)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("math_source", ["document", "comment"])
async def test_terminal_review_and_math_share_one_closing_model_handoff(
    stores, monkeypatch, math_source
) -> None:
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.consistency.models import ReviewConfig
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathController
    from sandbox_science.orchestrator.model import ModelTurn
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    if math_source == "comment":
        from .test_comment_math import comment

        comment(docs, create(docs, USER, "Context."))
    else:
        create(docs)
    await drain(reviews, ScriptedChecker())
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    controller = ReviewController(reviews, context, run_id="joint", model=None, alias=None)
    invoked = False

    async def terminal(messages, answer):
        nonlocal invoked
        if not invoked:
            invoked = True
            reviews.observe(
                "p",
                "s",
                "synthetic-claim",
                "transcript",
                {"run": "joint"},
                {"content": "There are 16."},
            )
            # Explicit reviewer fixture grant, independent of automatic scheduler.
            reviews.configure("p", "s", ReviewConfig())
            review = reviews.dispatch(
                "p", "s", "terminal", author_run="joint", owner="fixture", alias="scripted"
            )
            assert review
            evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
            Findings(reviews).create(
                "p",
                "s",
                review,
                "fixture",
                {
                    "severity": "fail",
                    "category": "contradicting_value",
                    "statement": "The count contradicts pinned evidence.",
                    "anchors": [{"evidence": evidence}],
                    "operation_id": "joint-fail",
                },
            )
            reviews.finish("p", "s", review, "fixture", "findings", "Synthetic failure.")
            reviews.configure("p", "s", ReviewConfig(enabled=False))
        return {"counts": FindingNotices(reviews).counts("p", "s")}

    monkeypatch.setattr(controller, "terminal", terminal)

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            content = "\n".join(str(m.get("content")) for m in messages)
            if self.calls == 1:
                assert "Mechanical math findings" not in content
            if self.calls == 2:
                assert "Mechanical math findings" in content
                assert "Consistency findings from an independent reviewer" in content
            return ModelTurn("Synthetic final answer.")

    model = Model()
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            model,
            Toolbox(
                client,
                context,
                reviews=controller,
                math_checks=MathController(reviews, "p", "s", "joint", checker=ScriptedChecker()),
            ),
            "Continue",
            run_id="joint",
            max_turns=4,
        )
    assert model.calls == 2
    assert len(sorted(transcript.delivered_ids("reviews"))) == 1
    assert len(sorted(transcript.delivered_ids("math"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "cleanup"])
async def test_math_notice_ack_only_after_durable_run_save(stores, monkeypatch, failure) -> None:
    import argparse
    from datetime import UTC, datetime

    from sandbox_science.contracts import ErrorKind, ExecutionContext
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathChecks, MathController
    from sandbox_science.orchestrator.model import ScriptedModel
    from sandbox_science.orchestrator.projects import ProjectError
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    controller = MathController(reviews, "p", "s", "saved")
    hooks = [MathChecks(controller)]
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            ScriptedModel.from_json([{"text": "Before closing."}, {"text": "Warning read."}]),
            Toolbox(client, context, math_checks=controller),
            "Continue",
            run_id="saved",
            max_turns=3,
            hooks=hooks,
        )
    assert len(sorted(transcript.delivered_ids("math"))) == 1
    assert get(reviews)[0]["status"] == "open"
    now = datetime.now(UTC)
    fields = {
        "run_id": "saved",
        "context": context.model_dump(mode="json"),
        "task": "Synthetic",
        "started_at": now,
        "ended_at": now,
        "stopped": "error" if failure else "answered",
        "failure": failure,
        "final_text": None if failure else "Read warning.",
        "messages": transcript.messages,
        "tools": [],
        "delivered": {"math": sorted(transcript.delivered_ids("math"))},
    }
    original = ProjectStore.save_run

    def failed(*args):
        raise ProjectError(ErrorKind.INTERNAL, "Synthetic durable save failure")

    monkeypatch.setattr(ProjectStore, "save_run", failed)
    args = argparse.Namespace(state_dir=docs.path.parent, transcript=None)
    assert (
        await save(
            fields,
            0,
            hooks,
            projects=ProjectStore(args.state_dir),
            transcript=args.transcript,
            error=print,
        )
        == 1
    )
    assert get(reviews)[0]["status"] == "open"
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT delivered_run FROM math_notices").fetchone()[0] is None
    monkeypatch.setattr(ProjectStore, "save_run", original)
    assert (
        await save(
            fields,
            0,
            hooks,
            projects=ProjectStore(args.state_dir),
            transcript=args.transcript,
            error=print,
        )
        == 0
    )
    assert get(reviews)[0]["status"] == ("open" if failure else "unaddressed")
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT delivered_run FROM math_notices").fetchone()[0] == "saved"
    assert ProjectStore(docs.path.parent).run(
        "p", "s", ProjectStore(docs.path.parent).runs("p", "s")[0].name
    ).delivered["math"] == sorted(transcript.delivered_ids("math"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "user", "accepted", "unavailable", "pending"])
async def test_staged_bound_rebinds_after_failed_save_and_rechecks_current_evidence(
    stores, change: str
) -> None:
    docs, reviews, clock = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    notices = MathNotices(reviews)
    notice = notices.pending("p", "s", "old-save-failed", set())[0]
    ids = {notice["notice_id"]}
    notices.bound("p", "s", "old-save-failed", ids)
    assert get(reviews)[0]["status"] == "open"
    clock[0] += 121
    assert notices.pending("p", "s", "new-saved", set())[0]["notice_id"] in ids
    notices.bound("p", "s", "new-saved", ids)
    notices.acknowledge("p", "s", "old-save-failed", list(ids))
    assert get(reviews)[0]["status"] == "open"
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT run FROM math_ignored").fetchone()[0] == "new-saved"
        assert db.execute("SELECT delivered_run FROM math_notices").fetchone()[0] is None
    expected = "unaddressed"
    if change == "user":
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": notice["finding"],
                "expected": notice["revision"],
                "action": "dismiss",
                "explanation": "Concurrent exact USER decision.",
                "operation_id": "concurrent-user",
            },
            USER,
        )
        expected = "withdrawn"
    elif change != "none":
        patch(docs, doc, "x" if change == "accepted" else r"\newUnknown{x}", "concurrent-source")
        if change != "pending":
            await drain(reviews, ScriptedChecker("accepted") if change == "accepted" else None)
        expected = "confirmed fixed" if change == "accepted" else "open"
    # Trusted save-success callback: staged run AND notice claim must both match;
    # current source/USER/evidence is fenced again inside this transaction.
    notices.acknowledge("p", "s", "new-saved", list(ids))
    assert get(reviews)[0]["status"] == expected
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT delivered_run FROM math_notices").fetchone()[0] == "new-saved"


@pytest.mark.asyncio
async def test_same_head_bounded_retry_remains_pending_and_visible(stores) -> None:
    from sandbox_science.orchestrator.mathchecks.controller import MathController

    docs, reviews, _ = stores
    create(docs, text="\n\n".join(f"$x_{i}$" for i in range(300)))
    await drain(reviews, None)
    controller = MathController(reviews, "p", "s", "retry", checker=ScriptedChecker("accepted"))
    async with controller.active():
        result = await controller.closing()
        assert result["pending"]
        listing = controller.jobs.listing("p")
        assert listing["queue"][0]["running"]
        assert listing["queue"][0]["completed"] == listing["queue"][0]["target"] == 1
        assert listing["checks"][0]["cursor"] == 256


@pytest.mark.asyncio
async def test_closing_bound_thread_owned_through_repeated_cancellation(
    stores, monkeypatch
) -> None:
    import asyncio
    import threading

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathController
    from sandbox_science.orchestrator.model import ScriptedModel
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    controller = MathController(reviews, "p", "s", "cancel-bound", checker=ScriptedChecker())
    entered = threading.Event()
    release = threading.Event()
    original = controller.notice_store.bound

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(controller.notice_store, "bound", blocked)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        task = asyncio.create_task(
            run_task(
                ScriptedModel.from_json([{"text": "Before closing."}, {"text": "Warning read."}]),
                Toolbox(client, context, math_checks=controller),
                "Continue",
                run_id="cancel-bound",
                max_turns=3,
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert get(reviews)[0]["status"] == "open"


@pytest.mark.asyncio
async def test_completed_scientific_bound_survives_later_math_failure(stores, monkeypatch):
    import argparse
    from datetime import UTC, datetime

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.consistency.models import ReviewConfig
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.mathchecks.controller import MathController
    from sandbox_science.orchestrator.model import ScriptedModel
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    reviews.observe("p", "s", "science", "transcript", {}, {"role": "assistant", "content": "16"})
    review = reviews.dispatch("p", "s", "terminal", author_run="old", owner="owner", alias=None)
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    scientific = Findings(reviews).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "15 versus 16",
            "anchors": [{"evidence": evidence}],
            "operation_id": "science-finding",
        },
    )
    reviews.finish("p", "s", review, "owner", "findings", "Contradiction")
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(docs.path.parent).get("p").default_environment_id,
    )
    mechanical = MathController(reviews, "p", "s", "partial", checker=ScriptedChecker())
    bound = mechanical.notice_store.bound

    def failed(*args):
        bound(*args)
        raise RuntimeError("test-secret-failed-math-close")

    monkeypatch.setattr(mechanical.notice_store, "bound", failed)
    async with WorkerClient("http://127.0.0.1:1", "synthetic") as client:
        toolbox = Toolbox(
            client,
            context,
            reviews=ReviewController(reviews, context, run_id="partial", model=None, alias=None),
            math_checks=mechanical,
        )
        hooks = toolbox.turn_hooks()
        transcript = await run_task(
            ScriptedModel.from_json([{"text": "First response"}, {"text": "Closing response"}]),
            toolbox,
            "Continue",
            run_id="partial",
            max_turns=2,
            hooks=hooks,
        )
    assert (transcript.failure, transcript.final_text) == ("harness", None)
    assert Findings(reviews).get("p", "s", scientific["finding_id"])["status"] == "unaddressed"
    math = next(f for f in get(reviews) if f["origin"] == "math")
    assert math["status"] == "open"
    now = datetime.now(UTC)
    fields = {
        "run_id": "partial",
        "context": context.model_dump(mode="json"),
        "task": "Continue",
        "started_at": now,
        "ended_at": now,
        "stopped": "error",
        "failure": transcript.failure,
        "messages": transcript.messages,
        "tools": transcript.tool_records,
        "delivered": {source: sorted(ids) for source, ids in transcript.delivered.items()},
    }
    args = argparse.Namespace(state_dir=docs.path.parent, transcript=None)
    assert (
        await save(
            fields,
            1,
            hooks,
            projects=ProjectStore(args.state_dir),
            transcript=args.transcript,
            error=print,
        )
        == 1
    )
    assert Findings(reviews).get("p", "s", math["finding_id"])["status"] == "open"
    assert Findings(reviews).get("p", "s", scientific["finding_id"])["status"] == "unaddressed"


@pytest.mark.asyncio
async def test_the_math_checks_keep_jobs_observations_and_findings(tmp_path, monkeypatch):
    from sandbox_science.orchestrator.control import migrations

    with monkeypatch.context() as old:  # a store from before version 6
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:4])
        old.setattr(migrations, "VERSION", migrations.BASELINE + 3)
        projects = ProjectStore(tmp_path)
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        projects.open_session("p", "s", dedicated=False)
        projects.close()
        docs = DocumentStore(store_path(tmp_path))
        reviews = ReviewStore(docs, clock=lambda: 1000.0)
        create(docs)
        await drain(reviews, ScriptedChecker())
        jobs = MathJobs(reviews)
        before = (jobs.listing("p"), [jobs.job(c["check_id"]) for c in jobs.listing("p")["checks"]])
        findings = get(reviews)
        docs.close()
    docs = DocumentStore(store_path(tmp_path))
    docs.initialize()  # the writer brings the store to the current version
    try:
        reviews = ReviewStore(docs, clock=lambda: 1000.0)
        jobs = MathJobs(reviews)
        after = (jobs.listing("p"), [jobs.job(c["check_id"]) for c in jobs.listing("p")["checks"]])
        assert after == before
        assert get(reviews) == findings
    finally:
        docs.close()
