# SPDX-License-Identifier: MIT
"""A tool result's gloss (B10, item 1): computed from the tool record by code."""

from __future__ import annotations

import json

from sandbox_science.orchestrator.tool_status import GLOSS_ERROR_CHARS, tool_gloss


def execution(
    state: str, exit_code: int | None, stdout: str = "", stderr: str = "", **more: object
):
    streams = {
        name: {"name": name, "text": text, "total_bytes": len(text.encode()), "truncated": False}
        for name, text in (("stdout", stdout), ("stderr", stderr))
        if text
    }
    return {"execution": {"state": state, "result": {"exit_code": exit_code, **streams, **more}}}


def test_an_ok_call_counts_its_output_streams() -> None:
    gloss = tool_gloss(execution("succeeded", 0, stdout="a\nb\nc\n"), "{}")
    assert gloss == {
        "outcome": "ok",
        "exit_code": 0,
        "lines": 3,
        "bytes": 6,
        "files": [],
        "more_files": 0,
        "error": None,
    }


def test_a_cut_stream_has_its_whole_size_and_no_line_count() -> None:
    record = execution("succeeded", 0)
    record["execution"]["result"]["stdout"] = {
        "name": "stdout",
        "text": "head\n…\ntail\n",
        "total_bytes": 90_000,
        "truncated": True,
    }
    record["saved_output"] = {"stdout": ".outputs/x/stdout.txt"}
    gloss = tool_gloss(record, "{}")
    assert (gloss["lines"], gloss["bytes"]) == (None, 90_000)


def test_a_failed_command_has_its_exit_code_and_its_errors_first_line() -> None:
    gloss = tool_gloss(execution("failed", 1, stderr="\nls: No such file\nmore\n"), "{}")
    assert (gloss["outcome"], gloss["exit_code"], gloss["error"]) == (
        "failed",
        1,
        "ls: No such file",
    )
    traceback = "Traceback (most recent call last):\n  File x\nValueError: bad n\n"
    assert tool_gloss(execution("failed", 1, stderr=traceback), "{}")["error"] == (
        "ValueError: bad n"
    )


def test_a_cells_exception_is_named_with_its_value() -> None:
    error = {"kind": "exception", "message": "x", "name": "ZeroDivisionError", "value": "div"}
    gloss = tool_gloss(execution("failed", None, error=error), "{}")
    assert (gloss["outcome"], gloss["error"]) == ("failed", "ZeroDivisionError: div")


def test_a_refusal_and_a_timeout_say_so_with_the_message_from_the_result() -> None:
    message = "purpose_note: at most 120 characters (got 164)\nsecond line"
    result = json.dumps({"error": {"kind": "invalid_request", "message": message}})
    gloss = tool_gloss({"error": "invalid_request"}, result)
    assert gloss["outcome"] == "refused"
    assert gloss["error"] == "purpose_note: at most 120 characters (got 164)"
    assert tool_gloss({"error": "timed_out"}, "{}")["outcome"] == "timed out"
    assert tool_gloss(execution("timed_out", None), "{}")["outcome"] == "timed out"
    assert tool_gloss({"error": "internal"}, "{}")["outcome"] == "failed"
    long = json.dumps({"error": {"kind": "internal", "message": "e" * 500}})
    assert len(str(tool_gloss({"error": "internal"}, long)["error"])) == GLOSS_ERROR_CHARS


def test_a_background_job_has_started() -> None:
    assert tool_gloss(execution("running", None), "{}")["outcome"] == "started"


def test_a_call_that_recorded_files_names_ten_and_counts_the_rest() -> None:
    files = [{"path": f"out/{n}.csv", "deleted": False} for n in range(12)]
    record = {"recorded_files": {"recorded": 12, "deleted": 0, "files": files}}
    gloss = tool_gloss(record, '{"ok": true}')
    assert gloss["files"] == [f"out/{n}.csv" for n in range(10)]
    assert gloss["more_files"] == 2
    published = tool_gloss({"artifact": {"source_path": "report.pdf"}}, "{}")
    assert (published["files"], published["more_files"]) == (["report.pdf"], 0)


def test_a_result_without_streams_is_measured_by_its_text() -> None:
    read = json.dumps({"path": "a.txt", "content": "x\ny\n"})
    gloss = tool_gloss({}, read)
    assert (gloss["outcome"], gloss["lines"], gloss["bytes"]) == ("ok", 2, len(read))
    assert tool_gloss({}, '{"entries": []}')["lines"] is None
    assert tool_gloss({}, "plain\ntext")["lines"] == 2
    # The model's text is never read: a call's arguments do not change the gloss.
    assert tool_gloss({"arguments": '{"script": "exit 1"}'}, "{}")["outcome"] == "ok"
