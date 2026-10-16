# SPDX-License-Identifier: MIT
"""Figure checks of the current revisions of documents and ledger statements, as the
run service drains them (docs/specs/figures.md, FG-19 to FG-21; FG-F7, FG-F9). The
checker here is a stand-in; ``test_figurecheck`` runs the real one."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import figurechecks
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path
from sandbox_science.orchestrator.figurecheck import Chart, FigureCheck, FigureChecker, Problem
from sandbox_science.orchestrator.figurechecks import FigureChecks
from sandbox_science.orchestrator.harness import Harness, Role, Stores
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.worker_client import WorkerClient

from .ledger_support import AnchoredLedger as Ledger
from .test_artifact_inventory import SOURCE, record

AGENT = Author(kind="agent", session_id="s", run_id="r1", role="main", model="scripted")
IN_S = SOURCE.model_copy(
    update={"context": ExecutionContext(project_id="p", session_id="s", environment_id="env1")}
)
CSV = b"bits,ms\n64,3\n128,9\n"


def chart(y: str = "ms", url: str = "data/t.csv") -> str:
    spec = {
        "data": {"url": url},
        "mark": "point",
        "encoding": {"x": {"field": "bits"}, "y": {"field": y}},
    }
    return "```vega-lite\n" + json.dumps(spec) + "\n```"


class Checker:
    """Accepts a chart whose data hold its y field; counts what it is given."""

    def __init__(self) -> None:
        self.seen: list[Chart] = []

    def version(self) -> str:
        return "fake-1"

    async def check_many(self, charts: list[Chart]) -> list[FigureCheck]:
        self.seen.extend(charts)
        results: list[FigureCheck] = []
        for item in charts:
            field = json.loads(item.source)["encoding"]["y"]["field"]
            if any(field in text.split("\n", 1)[0] for text in item.files.values()):
                results.append(FigureCheck(id=item.id, status="accepted"))
            else:
                problem = Problem(kind="field", message=f"no {field}", line=1, column=60)
                results.append(FigureCheck(id=item.id, status="rejected", errors=[problem]))
        return results


@pytest.fixture
def documents(tmp_path: Path) -> Iterator[DocumentStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    store = DocumentStore(store_path(tmp_path), producer="harness")
    # A harness's stores subscribe the checks to every save.
    Stores(projects, store, JobState(tmp_path, control=store.control), RunState(projects.control))
    yield store
    store.close()
    projects.close()


def results(store: DocumentStore) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
    with store.control.read() as tx:
        rows = tx.sql.execute("SELECT place,figure,status,result FROM figure_checks").fetchall()
    return {(place, figure): (status, json.loads(result)) for place, figure, status, result in rows}


def pending(store: DocumentStore) -> int:
    with store.control.read() as tx:
        return tx.sql.execute("SELECT count(*) FROM figure_sources").fetchone()[0]


def checks(store: DocumentStore, checker: Checker | None) -> FigureChecks:
    return FigureChecks(store, cast(FigureChecker, checker))


async def test_a_saved_documents_figures_are_checked_at_its_current_revision(
    documents: DocumentStore,
) -> None:
    inventory = ArtifactInventory(documents)
    record(inventory, "op1", CSV, source=IN_S, path="data/t.csv")
    record(inventory, "op2", b"%PDF-1", source=IN_S, path="plots/t.bin")
    text = f"# T\n\n{chart()}\n\n![T](plots/t.bin) ![M](plots/missing.svg)\n\nWords.\n"
    made = documents.create("p", name="n", title="T", text=text, author=AGENT, session_id="s")
    assert pending(documents) == 1
    checker = Checker()
    assert await checks(documents, checker).drain() == 1
    assert pending(documents) == 0
    (sent,) = checker.seen
    assert sent.files == {"data/t.csv": CSV.decode()}  # the pinned bytes, by the chart's path
    found = results(documents)
    mapped = documents.mapped("p", made.document_id)
    fence, images = mapped.block_ids[1], mapped.block_ids[2]
    assert found[(fence, "chart:0")][0] == "accepted"
    status, file = found[(images, "file:0")]
    assert status == "accepted"
    assert file["notes"][0]["kind"] == "link"  # a type the GUI shows as a link (FG-19)
    status, missing = found[(images, "file:1")]
    assert status == "rejected"
    assert missing["errors"][0]["message"].startswith("plots/missing.svg is not pinned")

    # Another block's change keeps the chart's result without checking it again; a
    # changed fence is checked, and its errors kept with their line and column.
    words = mapped.block_ids[3]
    documents.patch("p", made.document_id, base=1, author=AGENT, operation_id="o2",
                    reason="r", operations=[{"op": "replace_text", "block": words,
                                             "quote": "Words", "with": "More words"}])  # fmt: skip
    await checks(documents, checker).drain()
    assert len(checker.seen) == 1
    documents.patch("p", made.document_id, base=2, author=AGENT, operation_id="o3",
                    reason="r", operations=[{"op": "replace", "block": fence,
                                             "markdown": chart("msec")}])  # fmt: skip
    await checks(documents, checker).drain()
    assert len(checker.seen) == 2
    (key,) = [key for key in results(documents) if key[1] == "chart:0"]
    status, result = results(documents)[key]
    assert (status, result["errors"]) == (
        "rejected",
        [{"kind": "field", "message": "no msec", "line": 1, "column": 60}],
    )


async def test_without_a_checker_charts_are_unavailable_and_checked_later(
    documents: DocumentStore,
) -> None:
    """FG-F9: no result is accepted or rejected without the checker, and none is kept."""
    record(ArtifactInventory(documents), "op1", CSV, source=IN_S, path="data/t.csv")
    made = documents.create("p", name="n", title="T", text=f"# T\n\n{chart()}\n",
                            author=AGENT, session_id="s")  # fmt: skip
    await checks(documents, None).drain()
    ((status, result),) = results(documents).values()
    assert (status, result["reason"]) == ("unavailable", "no figure checker is configured")
    checker = Checker()
    title = documents.mapped("p", made.document_id).block_ids[0]
    documents.patch("p", made.document_id, base=1, author=AGENT, operation_id="o2",
                    reason="r", operations=[{"op": "replace_text", "block": title,
                                             "quote": "T", "with": "Timing"}])  # fmt: skip
    await checks(documents, checker).drain()
    assert len(checker.seen) == 1
    assert [status for status, _ in results(documents).values()] == ["accepted"]


async def test_inline_data_and_unresolved_data_are_rejected_before_any_check(
    documents: DocumentStore,
) -> None:
    """A data path that does not resolve is an error of the chart (FG-19)."""
    documents.create("p", name="n", title="T", text=f"# T\n\n{chart(url='data/none.csv')}\n",
                     author=AGENT, session_id="s")  # fmt: skip
    checker = Checker()
    await checks(documents, checker).drain()
    assert checker.seen == []
    ((status, result),) = results(documents).values()
    assert status == "rejected"
    assert result["errors"][0]["kind"] == "unresolved_reference"


async def test_a_ledger_statements_charts_are_checked(documents: DocumentStore) -> None:
    record(ArtifactInventory(documents), "op1", CSV, source=IN_S, path="data/t.csv")
    statement = f"Time grows:\n\n{chart()}\n\n{chart('msec')}"
    mutation = {
        "operation_id": "c1",
        "reason": "Recorded.",
        "changes": [{"type": "claim", "origin": "project", "title": "Quadratic",
                     "statement": statement, "fields": {"kind": "observation"}}],
    }  # fmt: skip
    ledger = Ledger(documents)
    made = ledger.change("p", mutation, AGENT)
    await checks(documents, Checker()).drain()
    found = results(documents)
    assert {key: status for key, (status, _) in found.items()} == {
        ("statement", "chart:0"): "accepted",
        ("statement", "chart:1"): "rejected",
    }
    # The entry's view carries them for the GUI (FG-23), at the revision checked.
    view = ledger.entry("p", made.entries[0].entry_id)
    assert [(c["figure"], c["status"]) for c in view.figure_checks] == [
        ("chart:0", "accepted"),
        ("chart:1", "rejected"),
    ]


async def test_the_run_service_starts_the_checks_as_it_looks(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    store = DocumentStore(store_path(tmp_path), producer="harness")
    stores = Stores(
        projects, store, JobState(tmp_path, control=store.control), RunState(projects.control)
    )
    core = Harness(cast(WorkerClient, object()), Role(ScriptedModel([]), "scripted"), stores=stores)
    checker = Checker()
    service = RunService(core, figures=cast(FigureChecker, checker))
    try:
        store.create("p", name="n", title="T", text=f"# T\n\n{chart()}\n", author=AGENT,
                     session_id="s")  # fmt: skip
        await service.tick()
        assert service.checking is not None
        assert await service.checking == 1
        assert [status for status, _ in results(store).values()] == ["rejected"]
    finally:
        store.close()
        projects.close()


# Security review of #514.


def sources(store: DocumentStore) -> list[tuple[str, int, float]]:
    with store.control.read() as tx:
        return tx.sql.execute("SELECT subject_type,tries,after FROM figure_sources").fetchall()


async def test_a_texts_data_are_read_a_batch_at_a_time_once_for_each_file(
    documents: DocumentStore,
) -> None:
    """HIGH: sixty fences naming one file read it once a batch, not sixty times at once;
    past the text's chart limit the rest are unavailable."""
    record(ArtifactInventory(documents), "op1", CSV, source=IN_S, path="data/t.csv")
    text = "# T\n\n" + "\n\n".join(chart() for _ in range(60)) + "\n"
    documents.create("p", name="n", title="T", text=text, author=AGENT, session_id="s")
    checker = Checker()
    figures = checks(documents, checker)
    reads: list[int] = []
    content = figures.inventory.content

    def counted(*args: Any, **kwargs: Any) -> bytes:
        reads.append(1)
        return content(*args, **kwargs)

    figures.inventory.content = counted  # type: ignore[method-assign]
    await figures.drain()
    assert len(checker.seen) == figurechecks.CHARTS_PER_TEXT == 32
    assert len(reads) == 32 // 8  # once a batch of eight, shared by its charts
    found = [result for status, result in results(documents).values() if status == "unavailable"]
    assert len(found) == 28
    assert {r["reason"] for r in found} == {figurechecks.TOO_MANY}


async def test_a_texts_data_past_its_bound_are_not_read(
    documents: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = ArtifactInventory(documents)
    record(inventory, "op1", CSV, source=IN_S, path="data/a.csv")
    record(inventory, "op2", CSV + b"256,20\n", source=IN_S, path="data/b.csv")
    monkeypatch.setattr(figurechecks, "TEXT_BYTES", len(CSV) + 1)
    text = f"# T\n\n{chart(url='data/a.csv')}\n\n{chart(url='data/b.csv')}\n"
    documents.create("p", name="n", title="T", text=text, author=AGENT, session_id="s")
    checker = Checker()
    await checks(documents, checker).drain()
    assert len(checker.seen) == 1
    found = sorted((status, r.get("reason")) for status, r in results(documents).values())
    assert found == [("accepted", None), ("unavailable", figurechecks.TOO_MUCH_DATA)]


async def test_one_texts_failure_holds_up_no_other(
    documents: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MED 1: a path too long for a message is cut short; a text whose check raises is
    logged and dropped, and the texts after it are checked."""
    long = "data/" + "x" * 1500 + ".csv"
    first = documents.create("p", name="a", title="A", text=f"# A\n\n{chart(url=long)}\n",
                             author=AGENT, session_id="s")  # fmt: skip
    await checks(documents, Checker()).drain()
    ((_, result),) = results(documents).values()
    message = result["errors"][0]["message"]
    assert message.startswith("data/xxx") and "…" in message and len(message) < 300  # noqa: PT018

    record(ArtifactInventory(documents), "op1", CSV, source=IN_S, path="data/t.csv")
    title = documents.mapped("p", first.document_id).block_ids[0]
    documents.patch("p", first.document_id, base=1, author=AGENT, operation_id="o2",
                    reason="r", operations=[{"op": "replace_text", "block": title,
                                             "quote": "A", "with": "AA"}])  # fmt: skip
    documents.create("p", name="c", title="C", text=f"# C\n\n{chart()}\n", author=AGENT,
                     session_id="s")  # fmt: skip
    loading = FigureChecks._text

    def failing(self: FigureChecks, db: Any, version: str, kind: str, subject: str,
                *rest: Any) -> Any:  # fmt: skip
        if subject == first.document_id:
            raise RuntimeError("a bug")
        return loading(self, db, version, kind, subject, *rest)

    monkeypatch.setattr(FigureChecks, "_text", failing)
    assert await checks(documents, Checker()).drain() == 2
    assert sources(documents) == []  # the failed text is not tried at every look
    assert "accepted" in [status for status, _ in results(documents).values()]


class Hanging(Checker):
    async def check_many(self, charts: list[Chart]) -> list[FigureCheck]:
        self.seen.extend(charts)
        reason = "figure checker deadline exceeded"
        return [FigureCheck(id=c.id, status="unavailable", reason=reason) for c in charts]


async def test_a_check_the_checker_could_not_finish_is_tried_again_later(
    documents: DocumentStore,
) -> None:
    """LOW: a deadline or a crash is retried with a growing wait, a few times."""
    record(ArtifactInventory(documents), "op1", CSV, source=IN_S, path="data/t.csv")
    documents.create("p", name="n", title="T", text=f"# T\n\n{chart()}\n", author=AGENT,
                     session_id="s")  # fmt: skip
    now = [1000.0]
    checker = Hanging()
    figures = FigureChecks(documents, cast(FigureChecker, checker), clock=lambda: now[0])
    await figures.drain()
    assert sources(documents) == [("document", 1, 1000.0 + figurechecks.BACKOFF)]
    assert await figures.drain() == 0  # not before its time
    waits = [figurechecks.BACKOFF]
    for tries in range(2, figurechecks.RETRIES + 1):
        now[0] = sources(documents)[0][2]
        await figures.drain()
        _, counted, after = sources(documents)[0]
        assert counted == tries
        waits.append(after - now[0])
    assert waits == [figurechecks.BACKOFF * 4**n for n in range(figurechecks.RETRIES)]
    now[0] = sources(documents)[0][2]
    await figures.drain()
    assert sources(documents) == []  # given up after its retries
    assert len(checker.seen) == figurechecks.RETRIES + 1
    assert [status for status, _ in results(documents).values()] == ["unavailable"]


async def test_texts_saved_before_the_checks_are_checked_once_at_start(
    documents: DocumentStore,
) -> None:
    """INFO: control store version 57 has no backfill; the run service records, as it
    starts, each current revision that may hold figures and has no checks."""
    documents.create("p", name="n", title="T", text=f"# T\n\n{chart()}\n", author=AGENT,
                     session_id="s")  # fmt: skip
    documents.create("p", name="m", title="M", text="# M\n\nNo figure.\n", author=AGENT,
                     session_id="s")  # fmt: skip
    with documents.control.write() as tx:
        tx.sql.execute("DELETE FROM figure_sources")  # as before version 57
    figures = checks(documents, Checker())
    assert figures.backfill() == 1
    await figures.drain()
    assert figures.backfill() == 0
