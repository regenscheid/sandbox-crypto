# SPDX-License-Identifier: MIT
"""One logging setup for every Sandbox Science service (D103, docs/KUBERNETES-BUNDLE.md).

Each service's entry point calls ``configure_logging("<service>")`` once, before it
serves. Two formats, chosen by ``SS_LOG_FORMAT`` (``json`` or ``text``, ``text`` by
default) and read at that call:

``text``
    The line the services always wrote, for a person at a terminal or in a journal:
    ``2026-10-24 12:00:00,123 INFO sandbox_science.worker.app: message``, with the
    context fields (``[project=p run=r]``) after it when there are any.

``json``
    One JSON object per line, for Loki: ``ts`` (UTC, milliseconds), ``level`` (lower
    case), ``service``, ``logger`` and ``msg``; ``event`` when the call names one; the
    context fields; the call's other ``extra`` fields; ``exc`` for an exception.

Both go to standard error, as the services always logged, and in both every message,
field and traceback passes ``redact_text`` first, and a field whose name ends in a
secret word (``token``, ``password``, ``api_key``, ...) is never written at all.

**Context.** ``log_context(project=..., run=...)`` adds fields to every record logged in
its scope, in the same task or in the tasks it creates (a ``ContextVar``), so a service
tags a request or a run once instead of at each of its calls. The stable names are
``project``, ``session``, ``run``, ``environment`` and ``request``; a call's own
``extra={"event": "worker_request", "status": 200}`` adds to them or overrides them.

``SS_LOG_LEVEL`` is the level when the entry point is not given one (``--log-level``).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import re
import sys
from collections.abc import Generator, Iterable, Mapping
from datetime import UTC, datetime
from itertools import islice
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from .redaction import REDACTED, is_secret_name, redact_text

LogFormat = Literal["json", "text"]

FORMAT_ENV: Final = "SS_LOG_FORMAT"
LEVEL_ENV: Final = "SS_LOG_LEVEL"
CONTEXT_FIELDS: Final = ("project", "session", "run", "environment", "request")
"""The context names the services share, so a query on one finds all of them."""
TEXT_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_MESSAGE: Final = 8192
MAX_VALUE: Final = 1024
MAX_ITEMS: Final = 32
_DEPTH: Final = 3
_RESERVED: Final = frozenset({"ts", "level", "service", "logger", "event", "msg", "exc"})
_STANDARD: Final = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
"""The attributes every ``LogRecord`` has; the rest came from a call's ``extra``."""

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
"""A field is one line: a path a caller sent may decode to a line break."""

_context: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "sandbox_science_log_context", default=MappingProxyType({})
)


@contextlib.contextmanager
def log_context(**fields: str | None) -> Generator[None]:
    """Add ``fields`` to every record logged inside the block, and in the tasks it starts.
    A field that is ``None`` is left out; a nested block overrides the outer one's value."""
    merged = {**_context.get(), **{name: str(value) for name, value in fields.items() if value}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> Mapping[str, str]:
    """The context fields in effect here."""
    return _context.get()


def log_format(environ: Mapping[str, str] | None = None) -> LogFormat:
    """The format ``SS_LOG_FORMAT`` asks for; anything but ``json`` is ``text``."""
    value = (os.environ if environ is None else environ).get(FORMAT_ENV, "")
    return "json" if value.strip().lower() == "json" else "text"


def _timestamp(moment: float) -> str:
    stamp = datetime.fromtimestamp(moment, UTC).isoformat(timespec="milliseconds")
    return stamp.removesuffix("+00:00") + "Z"


def _value(name: str, value: object, depth: int = 0) -> object:
    """``value`` as JSON may carry it: bounded, with secrets removed."""
    if is_secret_name(name):
        return REDACTED
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return redact_text(_CONTROL.sub(" ", value[:MAX_VALUE]))
    if depth < _DEPTH and isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        return {
            str(key)[:64]: _value(str(key), item, depth + 1)
            for key, item in islice(items.items(), MAX_ITEMS)
        }
    if depth < _DEPTH and isinstance(value, list | tuple | set | frozenset):
        members = cast(Iterable[object], value)
        return [_value(name, item, depth + 1) for item in islice(members, MAX_ITEMS)]
    return redact_text(_CONTROL.sub(" ", str(value)[:MAX_VALUE]))


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


class JsonFormatter(logging.Formatter):
    """One JSON object per record (see the module's description)."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": _timestamp(record.created),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
        }
        extras = {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        event = extras.pop("event", None)
        if event is not None:
            entry["event"] = _value("event", event)
        entry["msg"] = redact_text(_bounded(record.getMessage(), MAX_MESSAGE))
        for name, value in {**_context.get(), **extras}.items():
            entry[f"x_{name}" if name in _RESERVED else name] = _value(name, value)
        if record.exc_info:
            entry["exc"] = redact_text(_bounded(self.formatException(record.exc_info), MAX_MESSAGE))
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    """The services' long-standing line, redacted, with the context fields after it."""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        fields = {**_context.get(), **extras}
        if fields:
            line += " [" + " ".join(f"{k}={_value(k, v)}" for k, v in fields.items()) + "]"
        return redact_text(_bounded(line, MAX_MESSAGE))


class _Handler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at the time, so a stream the caller replaced
    (a test's capture) is never written after it is closed."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


_installed: _Handler | None = None


def _level(level: str | int | None, environ: Mapping[str, str]) -> int:
    if isinstance(level, int):
        return level
    name = (level or environ.get(LEVEL_ENV) or "info").strip().upper()
    number = logging.getLevelNamesMapping().get(name)
    if number is None:
        raise ValueError(f"{name!r} is not a log level")
    return number


def configure_logging(
    service: str,
    *,
    level: str | int | None = None,
    fmt: LogFormat | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Set up the root logger for ``service``: its records go to standard error in the
    format ``SS_LOG_FORMAT`` (or ``fmt``) names, at ``level`` (or ``SS_LOG_LEVEL``,
    else info). Calling it again replaces the handler it installed, leaving others
    (a test's capture) alone."""
    global _installed
    env = os.environ if environ is None else environ
    chosen = fmt or log_format(env)
    number = _level(level, env)  # before anything changes, so a bad level changes nothing
    handler = _Handler()
    handler.setFormatter(JsonFormatter(service) if chosen == "json" else TextFormatter())
    root = logging.getLogger()
    if _installed is not None:
        root.removeHandler(_installed)
    root.addHandler(handler)
    root.setLevel(number)
    _installed = handler


def reset_logging() -> None:
    """Remove the handler ``configure_logging`` installed (for tests)."""
    global _installed
    if _installed is not None:
        logging.getLogger().removeHandler(_installed)
        _installed = None


def json_line(
    service: str, message: str, *, level: str = "info", event: str | None = None, **fields: Any
) -> str:
    """A JSON record line for ``service`` that no logger wrote: the unit's own notes, and
    output a child wrote as plain text (a traceback, a library's print). ``event`` names
    the record's event, as a logger call's ``extra`` does."""
    entry: dict[str, object] = {
        "ts": _timestamp(datetime.now(UTC).timestamp()),
        "level": level,
        "service": service,
        "logger": "output",
    }
    if event is not None:
        entry["event"] = _value("event", event)
    entry["msg"] = redact_text(_bounded(message, MAX_MESSAGE))
    for name, value in fields.items():
        entry[f"x_{name}" if name in _RESERVED else name] = _value(name, value)
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)


def relayed_line(service: str, line: str, *, prefix: str, fmt: LogFormat | None = None) -> str:
    """A line another process wrote, as this one passes it on. In ``text`` it is ``prefix``
    then the line. In ``json`` a line that is already a record (an object with ``service``
    and ``msg``) goes on unchanged, since its own ``service`` names it, and anything else
    becomes a record of ``service``'s, so the stream stays JSON throughout."""
    if (fmt or log_format()) != "json":
        return f"{prefix}{line}"
    if line.startswith("{"):
        try:
            parsed: object = json.loads(line)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and "service" in parsed and "msg" in parsed:
            return line
    return json_line(service, line)
