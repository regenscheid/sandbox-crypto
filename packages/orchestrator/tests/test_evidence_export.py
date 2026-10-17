# SPDX-License-Identifier: MIT
"""Exact stored evidence, snapshot boundaries and no transaction across consumer waits."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.export import EvidenceExport
from sandbox_science.orchestrator.history.operations import ToolJournal, ToolRecord
from sandbox_science.orchestrator.history.verify_export import verify
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects


def decode(lines: Iterable[bytes]) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Test consumer: checks both checksums, then reconstructs complete small fixtures."""
    digest, count = hashlib.sha256(), 0
    manifest: dict[str, Any] = {}
    rows: list[tuple[str, dict[str, Any]]] = []
    row: dict[str, Any] = {}
    field: dict[str, Any] = {}
    data = bytearray()
    complete = False
    for raw in lines:
        assert not complete
        value = json.loads(raw)
        if value["kind"] == "complete":
            assert value["sha256"] == digest.hexdigest()
            assert value["lines"] == count
            complete = True
            continue
        digest.update(raw)
        count += 1
        match value["kind"]:
            case "manifest":
                manifest = value
            case "row":
                row = {}
                rows.append((value["table"], row))
            case "value":
                row[value["field"]] = value["value"]
            case "field":
                field, data = value, bytearray()
            case "chunk":
                assert value["offset"] == len(data)
                data.extend(base64.b64decode(value["data_base64"], validate=True))
            case "field_end":
                assert hashlib.sha256(data).hexdigest() == value["sha256"]
                assert len(data) == field["bytes"]
                row[field["field"]] = data.decode() if field["storage"] == "text" else bytes(data)
    assert complete
    return manifest, rows


def test_snapshot_is_exact_and_excludes_later_rows_and_other_scopes(
    projects: ProjectStore, tmp_path: Path
) -> None:
    archive = Archive(projects.control)
    original = "\u03b1\x00😀\n" * 20000
    native = b"\x00\xffprovider\x00"
    answer = archive.append(
        "p",
        "s",
        RUN,
        Entry("assistant", "assistant", original, native=native, native_format="fixture"),
    )
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "op", ToolCall("c", "arithmetic", '{ "x": 2 }'))
    full = {"tool": "arithmetic", "value": 4, "output": original}
    archive.append(
        "p",
        "s",
        RUN,
        Entry("tool", "tool_result", "4 (sample)"),
        tool_record=ToolRecord(full, "op"),
    )
    export = EvidenceExport(projects.control, "p", "s", RUN)
    archive.append("p", "s", RUN, Entry("user", "user", "LATER"))
    archive.append("p", "s", RUN + "other", Entry("user", "user", "OTHER RUN"))
    projects.open_session("p", "other", dedicated=False)
    archive.append("p", "other", RUN, Entry("user", "user", "OTHER SESSION"))
    projects.create("other", "minimal", "sha256:" + "a" * 64)
    projects.open_session("other", "s", dedicated=False)
    archive.append("other", "s", RUN, Entry("user", "user", "OTHER PROJECT"))
    lines = export.lines()
    first = next(lines)
    assert not projects.control.in_transaction
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        # The original ControlStore lazily reopens, and the writer can append while
        # the consumer is stopped between chunks, without a held read transaction.
        Archive(reopened.control).append("p", "s", RUN, Entry("user", "user", "LATER STILL"))
        encoded = [first]
        for line in lines:
            assert not projects.control.in_transaction
            assert len(line) < 90_000
            encoded.append(line)
        manifest, rows = decode(encoded)
        assert verify(io.BytesIO(b"".join(encoded))).run == RUN
        with pytest.raises(ValueError, match="completion"):
            verify(io.BytesIO(b"".join(encoded[:-1])))
        with pytest.raises(ValueError, match="offset"):
            verify(io.BytesIO(b"".join(encoded).replace(b'"offset":0', b'"offset":1', 1)))
        with pytest.raises(ValueError, match="corrupt"):
            verify(io.BytesIO(b"".join(encoded) + b"trailing"))
        assert manifest["tables"]["runs"]["rows"] == 0
        messages = [r for table, r in rows if table == "archive_messages"]
        assert len(messages) == 2
        assert messages[0]["message_id"] == answer.message_id
        assert messages[0]["content"] == original
        assert messages[0]["native"] == native
        (record,) = [r for table, r in rows if table == "archive_tool_records"]
        assert json.loads(record["record"]) == full
        (intent,) = [r for table, r in rows if table == "tool_intents"]
        assert intent["arguments"] == '{ "x": 2 }'
        assert manifest["external_files_included"] is False
        with pytest.raises(AssertionError):
            decode(encoded[:-1])
    finally:
        reopened.close()
        projects.close()


def test_intent_without_response_is_exportable_and_unknown_run_is_not_found(
    projects: ProjectStore,
) -> None:
    journal = ToolJournal(projects.control)
    journal.start(CONTEXT, RUN, "op", ToolCall("c", "arithmetic", "{}"))
    manifest, rows = decode(EvidenceExport(projects.control, "p", "s", RUN).lines())
    assert manifest["tables"]["archive_messages"]["rows"] == 0
    assert [table for table, _ in rows] == ["tool_intents"]
    with pytest.raises(ControlError, match="no stored evidence"):
        EvidenceExport(projects.control, "p", "s", RUN + "missing")


def test_export_empty_strings_and_binary_fields(projects: ProjectStore) -> None:
    Archive(projects.control).append(
        "p", "s", RUN, Entry("assistant", "assistant", "", native=b"", native_format="fixture")
    )
    _, rows = decode(EvidenceExport(projects.control, "p", "s", RUN).lines())
    (message,) = [r for table, r in rows if table == "archive_messages"]
    assert message["content"] == ""
    assert message["native"] == b""


def test_verifier_still_accepts_v1_and_checks_the_versions_table_set(
    projects: ProjectStore,
) -> None:
    Archive(projects.control).append("p", "s", RUN, Entry("user", "task", "Legacy"))
    encoded = list(EvidenceExport(projects.control, "p", "s", RUN).lines())
    manifest = json.loads(encoded[0])
    manifest["format"] = "sandbox-run-evidence-v1"
    del manifest["tables"]["run_inputs"]
    encoded[0] = json.dumps(manifest).encode() + b"\n"
    encoded[-1] = (
        json.dumps(
            {
                "kind": "complete",
                "lines": len(encoded) - 1,
                "sha256": hashlib.sha256(b"".join(encoded[:-1])).hexdigest(),
            }
        ).encode()
        + b"\n"
    )
    assert verify(io.BytesIO(b"".join(encoded))).format == "sandbox-run-evidence-v1"
    with pytest.raises(ValueError, match="complete table set"):
        verify(
            io.BytesIO(
                b"".join(encoded).replace(b"sandbox-run-evidence-v1", b"sandbox-run-evidence-v2")
            )
        )


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("fault", ["empty", "missing", "unknown", "duplicate"])
def test_verifier_rejects_malformed_rows_even_with_valid_checksums(
    projects: ProjectStore, version: str, fault: str
) -> None:
    Archive(projects.control).append("p", "s", RUN, Entry("user", "task", "Evidence"))
    items = [json.loads(line) for line in EvidenceExport(projects.control, "p", "s", RUN).lines()]
    if version == "v1":
        items[0]["format"] = "sandbox-run-evidence-v1"
        del items[0]["tables"]["run_inputs"]
    row = next(i for i, item in enumerate(items) if item["kind"] == "row")
    end = next(i for i, item in enumerate(items) if item["kind"] == "row_end")
    position = next(i for i, item in enumerate(items) if item.get("field") == "position")
    match fault:
        case "empty":
            del items[row + 1 : end]
            error = "incomplete evidence row fields"
        case "missing":
            del items[position]
            error = "incomplete evidence row fields"
        case "unknown":
            items.insert(
                end, {"kind": "value", "field": "unexpected", "storage": "null", "value": None}
            )
            error = "unknown evidence field"
        case _:
            items.insert(end, items[position])
            error = "duplicate"
    raw = b"".join(json.dumps(item).encode() + b"\n" for item in items[:-1])
    raw += (
        json.dumps(
            {"kind": "complete", "lines": len(items) - 1, "sha256": hashlib.sha256(raw).hexdigest()}
        ).encode()
        + b"\n"
    )
    with pytest.raises(ValueError, match=error):
        verify(io.BytesIO(raw))


def test_verifier_rejects_unknown_fields_before_accumulating_more(projects: ProjectStore) -> None:
    Archive(projects.control).append("p", "s", RUN, Entry("user", "task", "Evidence"))
    lines = iter(EvidenceExport(projects.control, "p", "s", RUN).lines())
    prefix = next(lines) + next(lines)  # manifest and row
    unknown = b'{"kind":"value","field":"extra_a","storage":"null","value":null}\n'
    stream = io.BytesIO(prefix + unknown + unknown.replace(b"extra_a", b"extra_b") * 10_000)
    with pytest.raises(ValueError, match="unknown evidence field"):
        verify(stream)
    assert stream.tell() == len(prefix) + len(unknown)
