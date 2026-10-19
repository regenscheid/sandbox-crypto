# SPDX-License-Identifier: MIT
"""RC-10/RC-14: dispatch and result crash windows through the real tool loop.

Only an in-process arithmetic fixture executes. No model service or worker runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.history import Archive, Entry, RunArchive
from sandbox_science.orchestrator.history.operations import PAGE, ToolJournal, ToolRecord
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Request, Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .control_versions import older_store

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env-p")
RUN = "20261016T030000000000Z-run-1"
CALL = ToolCall("call-1", "arithmetic", '{ "value": "\u03b1", "n": 2 }')
FULL = {
    "result": {"value": 4, "stdout": "\u03b1\x00\n" * 5000},
    "saved_output": {"stdout": "full.txt"},
}


@pytest.fixture
def projects(tmp_path: Path) -> Iterator[ProjectStore]:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    yield store
    store.close()


def logger(projects: ProjectStore, *, parent: str | None = None) -> RunArchive:
    return RunArchive(Archive(projects.control), CONTEXT, run=RUN, parent=parent)


def toolbox(handler, *, effect: Effect = "read") -> Toolbox:
    box = Toolbox(WorkerClient("http://unused.invalid", "fixture"), CONTEXT)
    box.entries = {
        "arithmetic": ToolEntry(
            ToolSpec("arithmetic", "Fixture arithmetic", {"type": "object"}), effect, handler
        )
    }
    return box


def model(*calls: ToolCall) -> ScriptedModel:
    return ScriptedModel([ModelTurn(None, tool_calls=calls), ModelTurn("Done")])


@pytest.mark.parametrize("parent", [None, "20261016T020000000000Z-parent"])
async def test_intent_precedes_dispatch_and_result_precedes_delivery_without_a_final_record(
    projects: ProjectStore, tmp_path: Path, parent: str | None
) -> None:
    observed: list[str] = []

    async def arithmetic(request: Request) -> ToolOutcome:
        # A different connection sees the committed intent before any effect starts.
        other = ControlStore(projects.control.path, readonly=True)
        try:
            journal = ToolJournal(other)
            (pending,) = journal.pending("p", "s", RUN)
            observed.append(request.operation)
            assert pending.operation_id == request.operation
            intent = journal.intent("p", "s", RUN, request.operation)
            assert intent["arguments"] == CALL.arguments
            assert intent["context"] == CONTEXT.model_dump(mode="json")
            assert (intent["call_id"], intent["tool"]) == (CALL.id, CALL.name)
        finally:
            other.close()
        return ToolOutcome("4 (sample)", dict(FULL))

    async def delivered(name, arguments, outcome) -> None:
        journal = ToolJournal(projects.control)
        assert journal.pending("p", "s", RUN) == []
        result = Archive(projects.control).messages("p", "s")[-1]
        assert journal.record("p", result.message_id) == {
            "tool": name,
            "arguments": arguments,
            **outcome.record,
        }
        assert projects.runs("p", "s") == []

    transcript = await run_task(
        model(CALL),
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects, parent=parent),
        on_tool=delivered,
    )
    assert transcript.final_text == "Done"
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        results = [
            m for m in Archive(reopened.control).messages("p", "s") if m.entry.kind == "tool_result"
        ]
        assert len(results) == len(observed) == 1
        assert results[0].parent == parent
        assert results[0].entry.content == "4 (sample)"
        record = ToolJournal(reopened.control).record("p", results[0].message_id)
        assert record == transcript.tool_records[0]
        assert record["result"] == FULL["result"]
        assert reopened.runs("p", "s") == []  # no final record needed for recovery
    finally:
        reopened.close()


async def test_interruption_during_dispatch_keeps_an_uncertain_intent(
    projects: ProjectStore, tmp_path: Path
) -> None:
    entered = asyncio.Event()

    async def interrupted(request: Request) -> ToolOutcome:
        entered.set()
        await asyncio.Future()
        raise AssertionError("never returns")

    task = asyncio.create_task(
        run_task(
            model(CALL),
            toolbox(interrupted),
            "Compute",
            run_id="run-1",
            hooks=(),
            log=logger(projects),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        (pending,) = ToolJournal(reopened.control).pending("p", "s", RUN)
        assert (
            ToolJournal(reopened.control).intent("p", "s", RUN, pending.operation_id)["arguments"]
            == CALL.arguments
        )
        assert not [
            m for m in Archive(reopened.control).messages("p", "s") if m.entry.kind == "tool_result"
        ]
    finally:
        reopened.close()


@pytest.mark.parametrize("source", ["worker", "connector"])
async def test_unknown_tool_effect_halts_without_settling_its_intent(
    projects: ProjectStore, source: str
) -> None:
    async def uncertain(request: Request) -> ToolOutcome:
        if source == "worker":
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "fixture accepted then disconnected", uncertain=True
            )
        raise UnknownEffectError("no_usable_answer", "fixture reply was lost")

    transcript = await run_task(
        model(CALL),
        toolbox(uncertain, effect="effect"),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
    )
    assert (transcript.stopped, transcript.failure) == ("error", "tool")
    assert len(ToolJournal(projects.control).pending("p", "s", RUN)) == 1
    assert not [
        message
        for message in Archive(projects.control).messages("p", "s")
        if message.entry.kind == "tool_result"
    ]


async def test_definitive_worker_refusal_is_delivered_for_agent_recovery(
    projects: ProjectStore,
) -> None:
    async def refused(request: Request) -> ToolOutcome:
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "fixture worker refused", status=503)

    transcript = await run_task(
        model(CALL),
        toolbox(refused, effect="effect"),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
    )
    assert transcript.final_text == "Done"
    assert ToolJournal(projects.control).pending("p", "s", RUN) == []


async def test_persistence_failure_before_dispatch_prevents_the_effect(
    projects: ProjectStore,
) -> None:
    with projects.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER refuse_intent BEFORE INSERT ON tool_intents "
            "BEGIN SELECT RAISE(ABORT, 'fixture storage failure'); END"
        )
    called = []

    async def arithmetic(request: Request) -> ToolOutcome:
        called.append(request)
        return ToolOutcome("4")

    transcript = await run_task(
        model(CALL), toolbox(arithmetic), "Compute", run_id="run-1", hooks=(), log=logger(projects)
    )
    assert called == []
    assert transcript.failure == "harness"
    assert ToolJournal(projects.control).pending("p", "s", RUN) == []


async def test_failure_after_dispatch_rolls_back_message_and_record_together(
    projects: ProjectStore,
) -> None:
    called, delivered = [], []

    async def arithmetic(request: Request) -> ToolOutcome:
        called.append(request)
        # The index is written after the archive message and the full tool record.
        with projects.control.write() as tx:
            tx.sql.execute(
                "CREATE TRIGGER refuse_index BEFORE INSERT ON archive_docs "
                "BEGIN SELECT RAISE(ABORT, 'fixture index failure'); END"
            )
        return ToolOutcome("4", FULL)

    async def observe(*args) -> None:
        delivered.append(args)

    transcript = await run_task(
        model(CALL),
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
        on_tool=observe,
    )
    assert len(called) == 1
    assert delivered == []
    assert transcript.failure == "harness"
    assert transcript.tool_records == []
    assert len(ToolJournal(projects.control).pending("p", "s", RUN)) == 1
    assert not [
        m for m in Archive(projects.control).messages("p", "s") if m.entry.kind == "tool_result"
    ]
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM archive_tool_records").fetchone() == (0,)


async def test_report_refusals_have_full_records_but_no_dispatch_intent(
    projects: ProjectStore,
) -> None:
    called = []

    async def arithmetic(request: Request) -> ToolOutcome:
        called.append(request)
        return ToolOutcome("4", FULL)

    report_call = ToolCall("report-call", "arithmetic", "{}")
    scripted = ScriptedModel(
        [ModelTurn(None, tool_calls=(CALL,)), ModelTurn("Report", tool_calls=(report_call,))]
    )
    transcript = await run_task(
        scripted,
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
        max_turns=1,
    )
    assert len(called) == 1
    assert transcript.stopped == "turn_limit"
    messages = [
        m for m in Archive(projects.control).messages("p", "s") if m.entry.kind == "tool_result"
    ]
    assert len(messages) == 2
    assert [ToolJournal(projects.control).record("p", m.message_id) for m in messages] == (
        transcript.tool_records
    )
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM tool_intents").fetchone() == (1,)


def test_duplicates_scopes_immutability_and_bounded_pending_pages(projects: ProjectStore) -> None:
    journal = ToolJournal(projects.control)
    for n in range(PAGE + 2):
        journal.start(CONTEXT, RUN, f"op-{n}", CALL)
    with pytest.raises(ControlError, match="reconcile") as error:
        journal.start(CONTEXT, RUN, "op-0", CALL)
    assert error.value.kind == "conflict"
    page = journal.pending("p", "s", RUN, limit=100000)
    assert len(page) == PAGE
    assert [p.operation_id for p in journal.pending("p", "s", RUN, after=page[-1].seq)] == [
        f"op-{PAGE}",
        f"op-{PAGE + 1}",
    ]
    assert journal.pending("other", "s", RUN) == journal.pending("p", "other", RUN) == []
    with pytest.raises(ControlError) as error:
        journal.intent("p", "s", "other", "op-0")
    assert error.value.kind == "not_found"
    entry = Entry("tool", "tool_result", "4", source=CALL.name, call_id=CALL.id)
    with pytest.raises(ControlError):
        Archive(projects.control).append(
            "p", "s", "other", entry, tool_record=ToolRecord(FULL, "op-0")
        )
    assert Archive(projects.control).messages("p", "s") == []
    archived = Archive(projects.control).append(
        "p", "s", RUN, entry, tool_record=ToolRecord(FULL, "op-0")
    )
    assert page[0] not in journal.pending("p", "s", RUN)
    with pytest.raises(ControlError) as error:
        journal.record("other", archived.message_id)
    assert error.value.kind == "not_found"
    for table in ("tool_intents", "archive_tool_records"):
        for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET project=project"):
            with (
                pytest.raises(sqlite3.IntegrityError, match="written once"),
                projects.control.write() as tx,
            ):
                tx.sql.execute(sql)


def test_byte_limits_are_checked_before_loading_original_values(projects: ProjectStore) -> None:
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "op", CALL)
    entry = Entry("tool", "tool_result", "sample")
    archived = Archive(projects.control).append(
        "p", "s", RUN, entry, tool_record=ToolRecord(FULL, "op")
    )

    def no_payload_reads(action, table, column, database, trigger):
        if (
            action == sqlite3.SQLITE_READ
            and table in {"tool_intents", "archive_tool_records"}
            and column in {"record", "arguments", "context", "call_id", "tool", "created"}
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with projects.control.read() as tx:
        tx.sql.set_authorizer(no_payload_reads)
    try:
        with pytest.raises(ControlError, match="byte limit") as error:
            journal.record("p", archived.message_id, max_bytes=1)
        assert error.value.details["bytes"] == len(json.dumps(FULL, ensure_ascii=False).encode())
        with pytest.raises(ControlError, match="byte limit"):
            journal.intent("p", "s", RUN, "op", max_bytes=1)
        assert journal.pending("p", "s", RUN) == []
    finally:
        with projects.control.read() as tx:
            tx.sql.set_authorizer(None)


def test_migration_preserves_old_messages_without_inventing_tool_records(
    projects: ProjectStore, tmp_path: Path
) -> None:
    archived = Archive(projects.control).append("p", "s", RUN, Entry("tool", "tool_result", "4"))
    target = tmp_path / "old" / "control.sqlite3"
    target.parent.mkdir()
    older_store(projects.control.path, target, 57)
    old = ControlStore(target)
    try:
        old.initialize()
        assert Archive(old).message("p", archived.message_id) == archived
        with pytest.raises(ControlError) as error:
            ToolJournal(old).record("p", archived.message_id)
        assert error.value.kind == "not_found"
        ToolJournal(old).start(CONTEXT, RUN, "op", CALL)
        assert len(ToolJournal(old).pending("p", "s", RUN)) == 1
    finally:
        old.close()


def test_record_pages_preserve_exact_bytes_across_reopen_and_mid_character_cuts(
    projects: ProjectStore, tmp_path: Path
) -> None:
    import hashlib

    from sandbox_science.orchestrator.history.operations import CHUNK_BYTES, READ_BYTES

    full = {"output": "\u00e9\U0001f680\x00\n" * (READ_BYTES // 8), "value": "0123456789"}
    raw = json.dumps(full, ensure_ascii=False, allow_nan=False).encode()
    archived = Archive(projects.control).append(
        "p", "s", RUN, Entry("tool", "tool_result", "sample"), tool_record=ToolRecord(full)
    )
    projects.close()
    readonly = ControlStore(tmp_path / "control.sqlite3", readonly=True)
    try:
        archive = Archive(readonly)
        with pytest.raises(ControlError, match="byte limit"):
            archive.tool_record("p", archived.message_id)
        # The API's offsets count bytes. A page may deliberately split a UTF-8 character.
        cut = raw.index("\u00e9".encode()) + 1
        first = archive.tool_record_chunk("p", archived.message_id, limit=cut)
        assert first.data == raw[:cut]
        with pytest.raises(UnicodeDecodeError):
            first.data.decode()
        assert archive.tool_record_chunk("p", archived.message_id, limit=cut) == first
        offset, digest = 0, hashlib.sha256()
        while True:
            page = archive.tool_record_chunk("p", archived.message_id, offset=offset)
            assert page.total == len(raw)
            assert page.data == raw[offset : offset + CHUNK_BYTES]
            assert len(page.data) <= CHUNK_BYTES
            digest.update(page.data)
            if page.next_offset is None:
                break
            assert page.next_offset == offset + len(page.data)
            offset = page.next_offset
        assert digest.digest() == hashlib.sha256(raw).digest()
        last = archive.tool_record_chunk("p", archived.message_id, offset=len(raw))
        assert (last.data, last.total, last.next_offset) == (b"", len(raw), None)
    finally:
        readonly.close()


@pytest.mark.parametrize(
    ("offset", "limit"),
    [
        (-1, 1),
        (2**62 + 1, 1),
        (False, 1),
        (1.5, 1),
        ("0", 1),
        (0, 0),
        (0, -1),
        (0, 65537),
        (0, True),
        (0, 1.5),
        (0, "1"),
    ],
)
def test_record_pages_refuse_invalid_bounds(projects: ProjectStore, offset, limit) -> None:
    with pytest.raises(ControlError) as error:
        Archive(projects.control).tool_record_chunk("p", "missing", offset=offset, limit=limit)
    assert error.value.kind == "invalid_request"


def test_record_pages_are_scoped_and_release_their_read_transaction(projects: ProjectStore) -> None:
    archive = Archive(projects.control)
    saved = archive.append(
        "p", "s", RUN, Entry("tool", "tool_result", "4"), tool_record=ToolRecord({"value": 4})
    )
    old = archive.append("p", "s", RUN, Entry("tool", "tool_result", "legacy"))
    for project, message_id in [
        ("other", saved.message_id),
        ("p", old.message_id),
        ("p", "absent"),
    ]:
        with pytest.raises(ControlError) as error:
            archive.tool_record_chunk(project, message_id)
        assert error.value.kind == "not_found"
    first = archive.tool_record_chunk("p", saved.message_id, limit=1)
    assert not projects.control.in_transaction
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "op-between-pages", CALL)
    assert archive.tool_record_chunk("p", saved.message_id, limit=1) == first
    with pytest.raises(ControlError) as error:
        archive.tool_record_chunk("p", saved.message_id, offset=first.total + 1)
    assert error.value.kind == "invalid_request"
    assert not projects.control.in_transaction
    journal.start(CONTEXT, RUN, "op-after-refusal", CALL)


def test_record_discovery_survives_restart_and_appends_without_loading_the_run(
    projects: ProjectStore, tmp_path: Path
) -> None:
    archive = Archive(projects.control)
    entry = Entry("tool", "tool_result", "sample")
    record = {"value": "exact 0004"}
    size = len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode())
    ToolJournal(projects.control).start(CONTEXT, RUN, "op-first", CALL)
    first = archive.append("p", "s", RUN, entry, tool_record=ToolRecord(record, "op-first"))
    archive.append("p", "s", RUN, Entry("assistant", "assistant", "next"))
    old = archive.append("p", "s", RUN, entry)
    archive.append("p", "s", "other-run", entry, tool_record=ToolRecord(record))
    projects.open_session("p", "other-session", dedicated=False)
    archive.append("p", "other-session", RUN, entry, tool_record=ToolRecord(record))
    projects.create("other-project", "minimal", "sha256:" + "a" * 64)
    projects.open_session("other-project", "s", dedicated=False)
    archive.append("other-project", "s", RUN, entry, tool_record=ToolRecord(record))
    refused = archive.append("p", "s", RUN, entry, tool_record=ToolRecord(record))
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        assert reopened.runs("p", "s") == []
        archive = Archive(reopened.control)
        page = archive.tool_records("p", "s", RUN, limit=1)
        assert [
            (r.message_id, r.position, r.operation_id, r.record_bytes) for r in page.records
        ] == [(first.message_id, first.position, "op-first", size)]
        assert page.has_more
        assert page.next_after == first.position
        added = archive.append("p", "s", RUN, entry, tool_record=ToolRecord(record))
        assert archive.tool_records("p", "s", RUN, limit=1) == page
        following = archive.tool_records("p", "s", RUN, after=page.next_after, limit=2)
        assert [(r.message_id, r.operation_id, r.record_bytes) for r in following.records] == [
            (old.message_id, None, None),
            (refused.message_id, None, size),
        ]
        assert following.has_more
        last = archive.tool_records("p", "s", RUN, after=following.next_after)
        assert [r.message_id for r in last.records] == [added.message_id]
        assert last.next_after == added.position
        assert not last.has_more
        empty = archive.tool_records("p", "s", RUN, after=last.next_after)
        assert (empty.records, empty.next_after, empty.has_more) == ((), last.next_after, False)
        # Reaching the current end does not prevent discovery of a later append.
        late = archive.append("p", "s", RUN, entry, tool_record=ToolRecord(record))
        resumed = archive.tool_records("p", "s", RUN, after=empty.next_after)
        assert [r.message_id for r in resumed.records] == [late.message_id]
        assert archive.tool_records("p", "s", "absent").records == ()
    finally:
        reopened.close()


def test_record_discovery_has_a_fixed_page_bound(projects: ProjectStore) -> None:
    archive = Archive(projects.control)
    for _ in range(PAGE + 1):
        archive.append("p", "s", RUN, Entry("tool", "tool_result", "legacy"))
    first = archive.tool_records("p", "s", RUN)
    assert len(first.records) == PAGE
    assert first.has_more
    second = archive.tool_records("p", "s", RUN, after=first.next_after)
    assert len(second.records) == 1
    assert not second.has_more


@pytest.mark.parametrize(
    ("after", "limit"),
    [
        (-1, 1),
        (2**62 + 1, 1),
        (False, 1),
        ("0", 1),
        (1.5, 1),
        (0, 0),
        (0, PAGE + 1),
        (0, True),
        (0, "1"),
        (0, 1.5),
    ],
)
def test_record_discovery_refuses_invalid_bounds(projects: ProjectStore, after, limit) -> None:
    with pytest.raises(ControlError) as error:
        Archive(projects.control).tool_records("p", "s", RUN, after=after, limit=limit)
    assert error.value.kind == "invalid_request"
