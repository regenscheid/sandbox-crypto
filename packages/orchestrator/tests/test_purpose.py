# SPDX-License-Identifier: MIT
"""Every tool call names its purpose, which changes nothing about the call
(docs/specs/tools.md, "Purpose of each call", items 1 to 6, and fixture 22; context
management, CM-4a, CM-18 and CM-49 as decided for B9)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.chat import shown_call
from sandbox_science.orchestrator.connectors import Connectors, ConnectorTool, ProxyClient
from sandbox_science.orchestrator.consistency.prompt_bundle import (
    PURPOSE,
    PURPOSE_ASSEMBLY,
    assemble_reviewer,
)
from sandbox_science.orchestrator.consistency.rubric import (
    RECHECK,
    RECHECK_VERSION,
    RUBRIC,
    RUBRIC_VERSION,
)
from sandbox_science.orchestrator.consistency.runner import SPECS as REVIEWER_SPECS
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.context.history import Archive, Archived, Entry
from sandbox_science.orchestrator.context.history.log import entry_of
from sandbox_science.orchestrator.context.history.skeleton import skeleton
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.prompt import CHECK_PROMPT, TASK_PROMPT
from sandbox_science.orchestrator.hooks import Appended, Effect, Refusal, TurnHook
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT
from sandbox_science.orchestrator.model import ToolCall, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.purpose import (
    COLLISION,
    FIELD,
    GUIDANCE,
    PurposeError,
    check,
    offered,
    without,
)
from sandbox_science.orchestrator.tool_entry import Request, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.tools import MCP_TOOLS, Toolbox, operation_id
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.orchestrator.wraps import RUNAWAY, Runaway

from .test_artifact_publication import CONTEXT, TOKEN
from .test_artifact_publication import worker_url as worker_url
from .test_reviewer_prompt_provenance import store as store

Json = dict[str, Any]
SIXTY = "Reading the frozen probability-law definition for the bounds"
"""A purpose of 60 characters."""
PROJECT, SESSION = "p", "s"


def called(name: str, arguments: Json, *, ident: str = "c1") -> ToolCall:
    return ToolCall(ident, name, json.dumps(arguments))


def error_of(outcome: ToolOutcome) -> Json:
    return json.loads(outcome.text)["error"]


# The field and its limits (Purpose 1 and 2).


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "purpose_note: required, a clause of 8 to 120 characters"),
        (5, "purpose_note: must be a string of 8 to 120 characters"),
        ("Reading", "purpose_note: at least 8 characters (got 7)"),
        ("x" * 121, "purpose_note: at most 120 characters (got 121)"),
        ("x" * 164, "purpose_note: at most 120 characters (got 164)"),
        ("Reading the\nspecification", "purpose_note: one line, without line breaks"),
        ("Reading the file\r", "purpose_note: one line, without line breaks"),
        ('Reading "the" file', 'purpose_note: no [, ] or " (got " at character 9)'),
        ("Reading [3] of it", 'purpose_note: no [, ] or " (got [ at character 9)'),
        ("Reading ] of it", 'purpose_note: no [, ] or " (got ] at character 9)'),
        ("Reading the ‮specification", "purpose_note: holds U+202E"),
        ("Reading &#x202E; here", "purpose_note: holds U+202E written as &#x202E;"),
    ],
)
def test_a_purpose_outside_its_limits_is_refused_naming_the_field(
    value: object, message: str
) -> None:
    with pytest.raises(PurposeError) as refused:
        check(value)
    assert refused.value.message.startswith(message)


def test_a_purpose_within_its_limits_is_kept_as_written() -> None:
    assert check(SIXTY) == SIXTY
    assert check("x" * 8) == "x" * 8
    assert check("x" * 120) == "x" * 120


def test_the_field_is_added_first_and_required_without_changing_the_tool() -> None:
    spec = ToolSpec(
        "read_file",
        "Read a file.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    shown = offered(spec)
    assert list(shown.parameters["properties"]) == [FIELD, "path"]
    assert shown.parameters["required"] == [FIELD, "path"]
    assert shown.parameters["properties"][FIELD] == {
        "type": "string",
        "minLength": 8,
        "maxLength": 120,
    }
    assert (shown.name, shown.description) == (spec.name, spec.description)
    assert FIELD not in spec.parameters["properties"]  # the tool's own schema is untouched
    bare = offered(ToolSpec("ping", "Ping.", {}))
    assert bare.parameters == {
        "properties": {FIELD: {"type": "string", "minLength": 8, "maxLength": 120}},
        "required": [FIELD],
        "type": "object",
    }


# Fixture 22: a shell call (the harness's).


@pytest_asyncio.fixture
async def toolbox(
    worker_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Toolbox, list[Any]]]:
    """A toolbox on a real worker, and each request the worker was sent."""
    async with WorkerClient(worker_url, TOKEN) as client:
        sent: list[Any] = []
        run = client.run

        async def seen(request: Any, **kwargs: Any) -> Any:
            sent.append(request)
            return await run(request, **kwargs)

        monkeypatch.setattr(client, "run", seen)
        yield Toolbox(client, CONTEXT), sent


async def test_fixture_22_a_shell_call_runs_only_with_a_purpose(
    toolbox: tuple[Toolbox, list[Any]],
) -> None:
    tools, sent = toolbox
    refused = await tools.call(called("shell", {"script": "echo hi"}), "op-1")
    assert error_of(refused)["kind"] == ErrorKind.INVALID_REQUEST.value
    assert "purpose_note" in error_of(refused)["message"]
    assert refused.record["error"] == "invalid_request"
    assert sent == []  # nothing ran
    ran = await tools.call(called("shell", {"script": "echo hi", FIELD: SIXTY}), "op-2")
    assert json.loads(ran.text)["stdout"] == "hi\n"
    assert ran.record["purpose"] == SIXTY
    (request,) = sent
    assert FIELD not in request.model_dump_json()
    assert SIXTY not in request.model_dump_json()
    for bad in ("x" * 121, "Echoing the greeting ‮ backwards"):
        outcome = await tools.call(called("shell", {"script": "echo hi", FIELD: bad}), "op-3")
        assert error_of(outcome)["kind"] == "invalid_request"
        assert error_of(outcome)["message"].startswith("purpose_note: ")
    assert len(sent) == 1


async def test_fixture_22_the_purpose_is_in_the_results_header_in_the_archive(
    tmp_path: Path,
) -> None:
    call = called("shell", {"script": "echo hi", FIELD: SIXTY})
    outcome = ToolOutcome('{"state":"succeeded"}', {"purpose": SIXTY})
    entry = entry_of(
        {"role": "tool", "tool_call_id": "c1", "content": outcome.text},
        Appended("tool_result", call=call, outcome=outcome),
    )
    assert entry.purpose == SIXTY
    with archived(tmp_path) as archive:
        row = archive.append(PROJECT, SESSION, "r", entry)
        kept = archive.message(PROJECT, row.message_id)
        assert kept.entry.purpose == SIXTY
        assert str(kept.chat()["content"]).startswith(
            f'[{row.message_id} · tool result · shell · purpose: "{SIXTY}"]\n'
        )


# Purpose 3: it changes nothing.


class Watching(TurnHook):
    """A gate that records each call it is asked about, and refuses none."""

    name = "watching"

    def __init__(self) -> None:
        self.asked: list[tuple[str, Effect]] = []

    async def gate_call(self, tool: str, effect: Effect) -> Refusal | None:
        self.asked.append((tool, effect))
        return None


async def test_gates_and_handlers_raw_ones_included_never_see_the_purpose() -> None:
    async with WorkerClient("http://127.0.0.1:9", "test-secret-worker") as client:
        tools = Toolbox(client, CONTEXT)
        got: list[Request] = []

        async def handler(request: Request) -> ToolOutcome:
            got.append(request)
            return ToolOutcome("{}")

        spec = ToolSpec("probe", "Probe.", {"type": "object"})
        tools.entries["probe"] = ToolEntry(spec, "read", handler, raw=True)
        tools.entries["parsed"] = ToolEntry(ToolSpec("parsed", "P.", {}), "read", handler)
        gate = Watching()
        await tools.call(called("probe", {"x": 1, FIELD: SIXTY}), "op", gates=(gate,))
        await tools.call(called("parsed", {"x": 1, FIELD: SIXTY}), "op", gates=(gate,))
        refused = await tools.call(called("probe", {"x": 1}), "op", gates=(gate,))
    assert gate.asked == [("probe", "read"), ("parsed", "read")]  # not the refused call
    raw, parsed = got
    assert json.loads(raw.call.arguments) == {"x": 1}
    assert parsed.args == {"x": 1}
    assert FIELD not in parsed.call.arguments
    assert error_of(refused)["message"].startswith("purpose_note: required")


def test_an_operation_does_not_depend_on_its_purpose() -> None:
    plain = called("shell", {"script": "ls"})
    first = called("shell", {"script": "ls", FIELD: SIXTY})
    second = called("shell", {"script": "ls", FIELD: "Listing the files again, reworded"})
    assert operation_id("run-t1", first) == operation_id("run-t1", second)
    assert operation_id("run-t1", first) == operation_id("run-t1", plain)
    assert operation_id("run-t1", first) != operation_id(
        "run-t1", called("shell", {"script": "pwd"})
    )
    # Arguments without a purpose, or not an object, are as written: a call from before
    # purposes keeps its identity.
    assert without('{"script":  "ls"}') == '{"script":  "ls"}'
    assert without("not json") == "not json"


def test_fixture_22_a_reworded_purpose_does_not_reset_the_runaway_guard() -> None:
    guard = Runaway()
    same = ToolOutcome('{"content":"same"}')
    for n in range(RUNAWAY):
        call = called("read_file", {"path": "a.txt", FIELD: f"Reading the file again, take {n}"})
        guard.call(call, same)
    assert guard.tripped is not None
    assert guard.tripped["rule"] == "same_call"


async def test_a_refusal_for_its_purpose_is_a_failed_call_the_guard_counts() -> None:
    guard = Runaway()
    async with WorkerClient("http://127.0.0.1:9", "test-secret-worker") as client:
        tools = Toolbox(client, CONTEXT)
        for n in range(RUNAWAY):
            call = called("read_file", {"path": f"{n}.txt"}, ident=f"c{n}")
            guard.call(call, await tools.call(call, f"op-{n}"))
    assert guard.tripped is not None
    assert guard.tripped["rule"] == "same_error"


# Purpose 1 and 6: every offered tool, connector tools included, and the backstop.


def connectors_of(*tools: ConnectorTool) -> Connectors:
    found = Connectors(
        ProxyClient("http://127.0.0.1:9", "test-secret-proxy"), ["papers"], session="s-1"
    )
    for tool in tools:
        found.tools[tool.spec.name] = tool
    return found


SEARCH = ConnectorTool(
    "papers",
    ToolSpec("search", "Search", {"type": "object", "properties": {"q": {"type": "string"}}}),
    purpose="Search papers",
)
COLLIDING = ConnectorTool(
    "papers",
    ToolSpec(
        "annotate",
        "Annotate",
        {"type": "object", "properties": {FIELD: {"type": "string"}, "text": {}}},
    ),
    purpose="Annotate a paper",
)


async def test_every_offered_tool_requires_a_purpose_connector_tools_included() -> None:
    async with WorkerClient("http://127.0.0.1:9", "test-secret-worker") as client:
        tools = Toolbox(
            client,
            CONTEXT,
            languages=("python", "sage"),
            features=("pdf", "admission"),
            connectors=connectors_of(SEARCH),
        )
        loading = called(MCP_TOOLS, {"names": ["search"], FIELD: "Loading the paper search tool"})
        assert json.loads((await tools.call(loading, "op")).text)["loaded"] == ["search"]
        specs = tools.specs
    names = [spec.name for spec in specs]
    assert {"python", "sage", "shell", "read_file", MCP_TOOLS, "search"} <= set(names)
    for spec in specs:
        assert next(iter(spec.parameters["properties"])) == FIELD, spec.name
        assert spec.parameters["required"][0] == FIELD, spec.name
    # The run's own entries keep each tool's schema as defined.
    assert all(FIELD not in str(entry.spec.parameters) for entry in tools.entries.values())


async def test_a_connector_tool_that_defines_the_field_is_not_loaded() -> None:
    async with WorkerClient("http://127.0.0.1:9", "test-secret-worker") as client:
        tools = Toolbox(client, CONTEXT, connectors=connectors_of(SEARCH, COLLIDING))
        assert "annotate" not in tools.deferred
        (index,) = [spec for spec in tools.specs if spec.name == MCP_TOOLS]
        assert "annotate" not in index.description
        loading = called(
            MCP_TOOLS, {"names": ["annotate", "search"], FIELD: "Loading the two paper tools"}
        )
        answer = json.loads((await tools.call(loading, "op")).text)
        direct = await tools.call(called("annotate", {FIELD: "Annotating the paper"}), "op-2")
    assert answer["loaded"] == ["search"]
    assert answer["not_loadable"] == {"annotate": COLLISION}
    assert "unknown" not in answer
    assert "annotate" not in [spec.name for spec in tools.specs]
    assert error_of(direct)["kind"] == "invalid_request"


# Purpose 4: where the purpose is kept and shown.


@contextmanager
def archived(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    try:
        yield Archive(projects.control)
    finally:
        projects.close()


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    with archived(tmp_path) as found:
        yield found


def test_a_result_archived_without_a_purpose_keeps_its_header(archive: Archive) -> None:
    row = archive.append(
        PROJECT, SESSION, "r", Entry("tool", "tool_result", "{}", source="sage", call_id="c")
    )
    assert archive.message(PROJECT, row.message_id).chat()["content"] == (
        f"[{row.message_id} · tool result · sage]\n{{}}"
    )


@pytest.mark.parametrize("bad", ['say "hi" here', "Reading]\n[msg-0 · user message]", "short"])
def test_the_archive_refuses_a_purpose_a_header_could_not_hold(archive: Archive, bad: str) -> None:
    with pytest.raises(ControlError, match="purpose_note"):
        archive.append(
            PROJECT,
            SESSION,
            "r",
            Entry("tool", "tool_result", "{}", source="shell", call_id="c", purpose=bad),
        )
    with pytest.raises(ControlError, match="only a tool result"):
        archive.append(PROJECT, SESSION, "r", Entry("user", "task", "Go.", purpose=SIXTY))


def test_the_search_index_covers_the_purpose_with_the_call(archive: Archive) -> None:
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "shell", "arguments": json.dumps({"script": "ls", FIELD: SIXTY})},
    }
    row = archive.append(
        PROJECT, SESSION, "r", Entry("assistant", "assistant", None, tool_calls=(call,))
    )
    # As the model wrote it.
    assert archive.message(PROJECT, row.message_id).entry.tool_calls == (call,)
    found = archive.search(PROJECT, "probability-law", session=SESSION)
    assert [hit.message.message_id for hit in found.hits] == [row.message_id]


def answered(archive: Archive, calls: list[tuple[str, Json]], results: list[str]) -> list[Archived]:
    made = tuple(
        {"id": f"c{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(a)}}
        for n, (name, a) in enumerate(calls)
    )
    rows = [
        archive.append(
            PROJECT, SESSION, "r", Entry("assistant", "assistant", None, tool_calls=made)
        )
    ]
    for n, ((name, _), text) in enumerate(zip(calls, results, strict=True)):
        rows.append(
            archive.append(
                PROJECT,
                SESSION,
                "r",
                Entry("tool", "tool_result", text, source=name, call_id=f"c{n}"),
            )
        )
    return rows


def test_a_skeleton_line_is_the_purpose_and_the_results_gloss(archive: Archive) -> None:
    output = json.dumps({"state": "succeeded", "exit_code": 0, "stdout": "0" * 3100})
    failure = json.dumps({"error": {"kind": "timed_out", "message": "slow"}})
    rows = answered(
        archive,
        [
            ("shell", {"script": "cat law.txt", FIELD: SIXTY}),
            ("read_file", {"path": "old.txt"}),
            ("shell", {"script": "sleep 99", FIELD: "Waiting for the long computation"}),
        ],
        [output, '{"content":"x"}', failure],
    )
    made = skeleton(rows, 100_000, 1_000, 4.0)
    first, old, slow = made.lines
    assert first.startswith(f"{SIXTY} → succeeded, exit 0, 3.1 KiB, {rows[1].message_id}; ")
    assert "cat law.txt" not in first
    assert "shell" not in first
    assert old.startswith(f"{rows[2].message_id} read_file → done, 15 characters; arguments ")
    assert slow.startswith("Waiting for the long computation → error timed_out, ")
    (failures,) = [m for m in made.markers if m["kind"] == "failures"]
    assert (failures["subject"], failures["ids"]) == ("shell", [rows[3].message_id])


def test_a_squeezed_skeleton_keeps_the_purposes_head_and_the_whole_gloss(
    archive: Archive,
) -> None:
    purpose = "Reading section 3 of the SNOVA specification for the field size and its bounds"
    rows = answered(
        archive, [("read_file", {"path": "snova.pdf", FIELD: purpose})], ['{"content":"q"}']
    )
    gloss = f"done, 15 B, {rows[1].message_id}"
    (line,) = skeleton(rows, 100_000, 1_000, 4.0).lines
    assert line.startswith(f"{purpose} → {gloss}; ")
    (squeezed,) = skeleton(rows, 80, 1_000, 4.0).lines
    head = squeezed.removesuffix(f" → {gloss}")
    assert head != squeezed
    assert head.endswith(" [...]")
    assert purpose.startswith(head.removesuffix(" [...]"))


def test_the_runs_events_show_the_purpose_apart_from_the_arguments() -> None:
    call = {
        "id": "c1",
        "function": {"name": "search", "arguments": json.dumps({"q": "x", FIELD: SIXTY})},
    }
    assert shown_call(call) == {
        "id": "c1",
        "name": "search",
        "arguments": json.dumps({"q": "x"}),
        "purpose": SIXTY,
    }
    plain = {"id": "c2", "function": {"name": "search", "arguments": '{"q": "x"}'}}
    assert shown_call(plain) == {"id": "c2", "name": "search", "arguments": '{"q": "x"}'}


# The reviewer's calls (Purpose 4 and 6).


async def test_the_reviewers_calls_keep_their_purposes_in_the_reviews_record(store) -> None:
    reviews, documents, review = store
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    for spec in runner.specs:
        assert spec.parameters["required"][0] == FIELD
    kept = json.loads(
        await runner.tool(called("review_manifest", {FIELD: "Listing the snapshot's evidence"}))
    )
    assert "evidence" in kept
    refused = json.loads(await runner.tool(called("review_manifest", {})))
    assert refused["error"] == "invalid_request"
    assert refused["message"].startswith("purpose_note: required")
    with sqlite3.connect(documents.path) as db:
        rows = [
            json.loads(payload)
            for (payload,) in db.execute(
                "SELECT payload FROM review_records WHERE review_id=? AND kind='tool' "
                "ORDER BY rowid",
                (review,),
            )
        ]
    assert [(r["tool"], r["arguments"], r.get("purpose")) for r in rows] == [
        ("review_manifest", "{}", "Listing the snapshot's evidence"),
        ("review_manifest", "{}", None),
    ]


def test_the_reviewers_bundle_gives_and_pins_the_purpose_guidance() -> None:
    bundle = assemble_reviewer(RUBRIC, RUBRIC_VERSION, REVIEWER_SPECS)
    pin = bundle.provenance
    assert (pin.format_version, pin.assembly_version) == (3, PURPOSE_ASSEMBLY)
    assert pin.purpose is not None
    assert pin.memory is None
    assert (pin.purpose.id, pin.purpose.version) == ("reviewer-purpose", PURPOSE.version)
    # Format 3 also carries the re-check instructions, which come before the guidance.
    assert pin.recheck is not None
    assert pin.recheck.version == RECHECK_VERSION
    assert bundle.system == f"{RUBRIC}\n\n{RECHECK}\n\n{GUIDANCE}"


def test_every_prompt_says_each_call_names_its_purpose() -> None:
    for prompt in (SYSTEM_PROMPT, TASK_PROMPT, CHECK_PROMPT, PURPOSE.text):
        assert GUIDANCE in prompt
    assert "not a request" in GUIDANCE
    assert "changes nothing" in GUIDANCE
    assert "Reading section 3 of the SNOVA specification for the field size" in GUIDANCE
