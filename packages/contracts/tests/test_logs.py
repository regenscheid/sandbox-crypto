# SPDX-License-Identifier: MIT
"""The shared logging setup: the JSON record's shape, the text line, context fields, and
that no secret reaches either (D103)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from sandbox_science.contracts.logs import (
    configure_logging,
    json_line,
    log_context,
    log_format,
    relayed_line,
    reset_logging,
)
from sandbox_science.contracts.redaction import is_secret_name, redact_text

_log = logging.getLogger("sandbox_science.test_logs")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    level = root.level
    yield
    reset_logging()
    root.setLevel(level)


def records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


def test_a_json_record_has_the_stable_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json")
    _log.info("started %s", "run-1", extra={"event": "run_started", "status": 200})
    (record,) = records(capsys)
    assert record.pop("ts").endswith("Z")
    assert record == {
        "level": "info",
        "service": "worker",
        "logger": "sandbox_science.test_logs",
        "event": "run_started",
        "msg": "started run-1",
        "status": 200,
    }


def test_context_fields_ride_along_and_nest(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("harness", fmt="json")
    with log_context(project="p", session="s", run="r1", environment=None):
        _log.info("outer")
        with log_context(run="r2"):
            _log.info("inner")
        _log.info("outer again", extra={"project": "other"})
    _log.info("outside")
    outer, inner, again, outside = records(capsys)
    assert (outer["project"], outer["session"], outer["run"]) == ("p", "s", "r1")
    assert "environment" not in outer
    assert (inner["project"], inner["run"]) == ("p", "r2")
    assert (again["project"], again["run"]) == ("other", "r1")
    assert not {"project", "session", "run"} & outside.keys()


async def test_context_follows_the_tasks_it_starts(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("harness", fmt="json")

    async def child() -> None:
        _log.info("in the child")

    with log_context(request="abc"):
        await asyncio.create_task(child())
    (record,) = records(capsys)
    assert record["request"] == "abc"


def test_an_extra_cannot_replace_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json")
    _log.info("real", extra={"service": "forged", "level": "debug", "ts": "then", "exc": "none"})
    (record,) = records(capsys)
    assert (record["service"], record["level"], record["msg"]) == ("worker", "info", "real")
    assert record["ts"] != "then"
    assert "exc" not in record
    assert (record["x_service"], record["x_level"], record["x_ts"]) == ("forged", "debug", "then")
    assert record["x_exc"] == "none"


def test_an_exception_is_one_redacted_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json")
    try:
        raise RuntimeError("fetch https://user:test-secret-pw@host.invalid/x failed")
    except RuntimeError:
        _log.exception("could not fetch")
    (record,) = records(capsys)
    assert "RuntimeError" in record["exc"]
    assert "test-secret-pw" not in json.dumps(record)
    assert "https://***@host.invalid/x" in record["exc"]


def test_a_value_that_is_not_json_is_written_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json")
    _log.info("odd", extra={"things": {1, 2}, "mapping": {"k": object}, "ratio": float("nan")})
    (record,) = records(capsys)
    assert sorted(record["things"]) == [1, 2]
    assert isinstance(record["mapping"]["k"], str)
    assert record["ratio"] == "nan"


def test_a_line_break_in_a_field_does_not_make_a_second_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("worker", fmt="text")
    with log_context(project="p\n2026-10-24 INFO forged line"):
        _log.warning("one record")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "WARNING sandbox_science.test_logs: one record [project=p 2026-10-24" in lines[0]


def test_the_text_format_is_the_long_standing_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cache", fmt="text")
    _log.info("plain")
    with log_context(project="p", run="r"):
        _log.info("tagged")
    plain, tagged = capsys.readouterr().err.splitlines()
    assert plain.endswith(" INFO sandbox_science.test_logs: plain")
    assert plain[4] == "-"
    assert "," in plain.split()[1]
    assert tagged.endswith(" INFO sandbox_science.test_logs: tagged [project=p run=r]")


def test_the_format_and_level_come_from_the_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("worker", environ={"SS_LOG_FORMAT": "JSON", "SS_LOG_LEVEL": "warning"})
    _log.info("below the level")
    _log.warning("at it")
    (record,) = records(capsys)
    assert record["msg"] == "at it"
    assert log_format({}) == "text"
    assert log_format({"SS_LOG_FORMAT": "yaml"}) == "text"
    assert log_format({"SS_LOG_FORMAT": " json "}) == "json"


def test_an_explicit_level_beats_the_environment() -> None:
    configure_logging("worker", level="DEBUG", environ={"SS_LOG_LEVEL": "error"})
    assert logging.getLogger().level == logging.DEBUG
    with pytest.raises(ValueError, match="not a log level"):
        configure_logging("worker", level="loud")


def test_configuring_twice_leaves_one_handler(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("a", fmt="json")
    configure_logging("b", fmt="json")
    _log.info("once")
    (record,) = records(capsys)
    assert record["service"] == "b"


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_no_secret_is_logged(fmt: str, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json" if fmt == "json" else "text")
    with log_context(request="test-secret-request"):
        _log.info(
            "token=test-secret-1 and Authorization: Bearer test-secret-2 "
            "via https://u:test-secret-3@h.invalid/p?api_key=test-secret-4",
            extra={"password": "test-secret-5", "headers": {"cookie": "test-secret-6"}},
        )
        _log.error(
            "failed %s", "client_secret: test-secret-7", extra={"api_token": "test-secret-8"}
        )
    shown = capsys.readouterr().err
    for number in range(1, 9):
        assert f"test-secret-{number}" not in shown
    assert "test-secret-request" in shown  # a request ID is not a secret


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "pip install https://user:pw@idx.invalid/simple",
            "pip install https://***@idx.invalid/simple",
        ),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: ***"),
        ("sent Bearer abcdefghijkl1234", "sent Bearer ***"),
        ("password='two words'", "password=***"),
        ('{"api_key": "abc123"}', '{"api_key": ***}'),
        ("GET /x?token=abc&page=2", "GET /x?token=***&page=2"),
        ("key sk-abcdefghijklmnopqrstuv", "key ***"),
        ("ghp_abcdefghijklmnopqrstuv", "***"),
    ],
)
def test_redact_text_removes_the_secret(text: str, expected: str) -> None:
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096 input_tokens: 12",
        "token refreshed in 3 s",
        "basic authentication is off",
        "http://host:8080/path?page=2&email=a",
        "the keyboard key was pressed",
    ],
)
def test_redact_text_leaves_ordinary_text(text: str) -> None:
    assert redact_text(text) == text


def test_secret_names() -> None:
    assert all(
        is_secret_name(name)
        for name in ("token", "api_key", "X-Auth-Token", "client_secret", "Cookie", "private-key")
    )
    assert not any(is_secret_name(name) for name in ("max_tokens", "project", "key", "tokens"))


def test_a_long_message_is_cut(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", fmt="json")
    _log.info("x" * 20_000)
    (record,) = records(capsys)
    assert len(record["msg"]) < 8_300
    assert record["msg"].endswith("[truncated]")


def test_relayed_lines_stay_json_in_json_mode() -> None:
    own = json.dumps({"service": "worker", "msg": "hello", "level": "info"})
    assert relayed_line("worker", own, prefix="[worker] ", fmt="json") == own
    wrapped = json.loads(
        relayed_line("cache", "Traceback (most recent call last):", prefix="[cache] ", fmt="json")
    )
    assert (wrapped["service"], wrapped["msg"]) == ("cache", "Traceback (most recent call last):")
    for odd in ('{"unrelated": 1}', "{not json", "[1, 2]"):
        assert json.loads(relayed_line("cache", odd, prefix="", fmt="json"))["msg"] == odd
    assert relayed_line("cache", "plain", prefix="[cache] ", fmt="text") == "[cache] plain"


def test_json_line_redacts() -> None:
    line = json.loads(json_line("unit", "using password=test-secret-9", level="error", port=1))
    assert (line["service"], line["level"], line["port"]) == ("unit", "error", 1)
    assert "test-secret-9" not in line["msg"]
