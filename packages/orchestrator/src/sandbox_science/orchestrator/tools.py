# SPDX-License-Identifier: MIT
"""The agent's execution tools (docs/specs/tools.md), all running on the worker.

Each tool call becomes a worker operation whose ID is derived from the call,
so a repeated call is idempotent. Results go back to the model as compact
JSON, bounded in size as a whole; the full execution records stay in the
transcript. A tool call that fails, however it fails, becomes an error the
model reads: it never ends the task.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    CellRequest,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    ListFilesRequest,
    PdfRequest,
    PublishRequest,
    ReadFileRequest,
    ShellRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import Language

from .model import ToolCall, ToolSpec
from .worker_client import WorkerAPIError, WorkerClient, dump

_log = logging.getLogger(__name__)

TRACEBACK_LINES: Final = 20
SHOWN_OUTPUTS: Final = 20
"""Rich outputs shown to the model, the first and last half; a note counts the rest."""
SHOWN_ENTRIES: Final = 200
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]")

_TIMEOUT = {"type": "number", "exclusiveMinimum": 0, "description": "Wall-time limit in seconds"}

SAGE: Final = ToolSpec(
    "sage",
    "Run SageMath code in the session's persistent Sage kernel inside the project sandbox. "
    "Sage syntax applies: ^ is exponentiation, 1/3 is an exact rational, and integer literals "
    "are Sage Integers (convert them with int() before json.dumps). Variables persist between "
    "calls until that kernel restarts. Results come back as plain text; run %display latex "
    "first, or call latex(x), to get LaTeX.",
    {
        "type": "object",
        "properties": {"code": {"type": "string"}, "timeout_seconds": _TIMEOUT},
        "required": ["code"],
        "additionalProperties": False,
    },
)

_PATH = {"type": "string", "description": "A PDF in the workspace, or shared/… for the project's"}

PDF_SPECS: Final = (
    ToolSpec(
        "pdf_info",
        "Describe a PDF: its pages, numbered physically from 1, with the labels the document "
        "prints (such as iv or 12); which pages have extractable text; its outline; and its "
        "SHA-256. The PDF is parsed inside the project sandbox.",
        {
            "type": "object",
            "properties": {"path": _PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_pages",
        'Read pages of a PDF as numbered lines, at most 20 pages a call, such as "3-5,9". '
        "Cite a line as p<page>:<line>, with the physical page number. Extracted text of "
        "equations, matrices and figures can be wrong: render the page to check it.",
        {
            "type": "object",
            "properties": {
                "path": _PATH,
                "pages": {"type": "string", "description": "Physical page numbers, from 1"},
                "include_artifacts": {
                    "type": "boolean",
                    "description": "Keep running headers, footers and page numbers",
                },
            },
            "required": ["path", "pages"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_search",
        "Find a case-insensitive string in a PDF. Each hit gives its page, the page's printed "
        "label, and the line's number and text.",
        {
            "type": "object",
            "properties": {"path": _PATH, "query": {"type": "string"}},
            "required": ["path", "query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "pdf_render",
        "Render a PDF page, or a region of it, to a PNG in the workspace, to check equations, "
        "matrices and figures against the extracted text. A region is [x0, y0, x1, y1] in PDF "
        "points from the page's bottom left, as pdf_pages reports them. Publish the PNG to "
        "keep it as evidence.",
        {
            "type": "object",
            "properties": {
                "path": _PATH,
                "page": {"type": "integer", "minimum": 1},
                "region": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                },
                "dpi": {"type": "integer", "minimum": 18, "maximum": 300},
            },
            "required": ["path", "page"],
            "additionalProperties": False,
        },
    ),
)

SPECS: Final = (
    ToolSpec(
        "shell",
        "Run a bash script in the session's workspace inside the project sandbox. Each call "
        "starts afresh in the workspace (no state carries over except files); stdin is empty. "
        "Returns the exit code and the output.",
        {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "cwd": {"type": "string", "description": "Directory relative to the workspace"},
                "timeout_seconds": _TIMEOUT,
            },
            "required": ["script"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "python",
        "Run Python code in the session's persistent kernel inside the project sandbox. "
        "Variables persist between calls until the kernel restarts. Returns printed output, "
        "the value of the last expression and any exception.",
        {
            "type": "object",
            "properties": {"code": {"type": "string"}, "timeout_seconds": _TIMEOUT},
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "read_file",
        "Read a file from the workspace (or shared/… for the project's shared area).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "write_file",
        "Write a text file in the workspace, replacing it if it exists.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "list_files",
        "List a directory in the workspace (or shared/…).",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "publish",
        "Publish a workspace file as an immutable project artifact, recording its hash.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "media_type": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
)


@dataclass
class ToolOutcome:
    """What the model sees, and what the transcript keeps."""

    text: str
    record: dict[str, Any] = field(default_factory=dict[str, Any])


def operation_id(prefix: str, call: ToolCall) -> str:
    """A worker operation ID for a tool call: stable, path-safe and at most 128 characters.

    It ends with a digest of the whole call, its name and arguments included. A
    different call never meets an earlier call's operation, however long the
    prefix; the same call at the same place in a run with the same ID does, and
    gets the earlier result instead of running again.
    """
    whole = json.dumps([prefix, call.id, call.name, call.arguments])
    digest = hashlib.sha256(whole.encode()).hexdigest()[:16]
    readable = _UNSAFE.sub("-", f"{prefix}-{call.id}").lstrip("-._")[:111].rstrip("-._")
    return f"{readable}-{digest}" if readable else f"op-{digest}"


class Toolbox:
    def __init__(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        *,
        languages: Sequence[Language] = ("python",),
        features: Sequence[str] = (),
        output_chars: int = 20000,
    ) -> None:
        self.client = client
        self.context = context
        self.output_chars = output_chars
        self.languages = tuple(languages)
        specs = [spec for spec in SPECS if spec.name != "python" or "python" in self.languages]
        if "sage" in self.languages:
            specs.append(SAGE)
        if self.languages:
            specs.append(
                ToolSpec(
                    "restart_kernel",
                    "Restart one of the session's kernels, discarding all its state.",
                    {
                        "type": "object",
                        "properties": {
                            "language": {"type": "string", "enum": list(self.languages)}
                        },
                        "additionalProperties": False,
                    },
                )
            )
        if "pdf" in features:
            specs.extend(PDF_SPECS)
        self.specs = tuple(specs)

    async def call(self, call: ToolCall, operation: str) -> ToolOutcome:
        """Run one tool call; whatever goes wrong comes back as an error for the model."""
        try:
            return await self._call(call, operation)
        except Exception as exc:  # one bad call must not end the task
            _log.exception("the %s tool failed", call.name)
            return _failure(
                ErrorKind.INTERNAL,
                f"the {call.name} tool failed unexpectedly ({type(exc).__name__})",
            )

    async def _call(self, call: ToolCall, operation: str) -> ToolOutcome:
        try:
            arguments: object = json.loads(call.arguments or "{}")
        except (ValueError, RecursionError) as exc:
            return _failure(ErrorKind.INVALID_REQUEST, f"arguments are not JSON: {exc}")
        if not isinstance(arguments, dict):
            return _failure(ErrorKind.INVALID_REQUEST, "arguments must be a JSON object")
        args = cast(dict[str, Any], arguments)
        handlers = {
            "shell": self._shell,
            "python": self._python,
            "sage": self._sage,
            "restart_kernel": self._restart,
            "read_file": self._read,
            "write_file": self._write,
            "list_files": self._list,
            "publish": self._publish,
            "pdf_info": partial(self._pdf, "info"),
            "pdf_pages": partial(self._pdf, "pages"),
            "pdf_search": partial(self._pdf, "search"),
            "pdf_render": partial(self._pdf, "render"),
        }
        handler = handlers.get(call.name)
        if handler is None or call.name not in {spec.name for spec in self.specs}:
            return _failure(ErrorKind.INVALID_REQUEST, f"there is no tool named {call.name!r}")
        try:
            return await handler(args, operation)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            return _failure(ErrorKind.INVALID_REQUEST, f"{where}: {first.get('msg')}")
        except WorkerAPIError as exc:
            return _failure(exc.kind, exc.message)

    # Executions.

    async def _shell(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ShellRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                **_known(args, "script", "cwd", "timeout_seconds"),
            }
        )
        return await self._run(request)

    async def _run(self, request: ShellRequest | CellRequest) -> ToolOutcome:
        replayed = False

        def submitted(_: Execution, created: bool) -> None:
            nonlocal replayed
            replayed = not created

        execution = await self.client.run(request, on_submitted=submitted)
        record = {"execution": execution.model_dump(mode="json"), "replayed": replayed}
        return ToolOutcome(self._summary(execution, replayed=replayed), record)

    async def _pdf(self, kind: str, args: dict[str, Any], operation: str) -> ToolOutcome:
        """A PDF operation in the sandbox; the helper's JSON is formatted for the model."""
        request = PdfRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                "operation": kind,
                **_known(args, "path", "pages", "query", "page", "region", "dpi"),
                **_known(args, "include_artifacts"),
            }
        )
        replayed = False

        def submitted(_: Execution, created: bool) -> None:
            nonlocal replayed
            replayed = not created

        execution = await self.client.run(request, on_submitted=submitted)
        record: dict[str, Any] = {
            "execution": execution.model_dump(mode="json"),
            "replayed": replayed,
        }
        result = execution.result
        stdout = result.stdout if result is not None else None
        helper_error = result is not None and result.exit_code == PDF_HELPER_ERROR
        if stdout is None or (execution.state is not ExecutionState.SUCCEEDED and not helper_error):
            return ToolOutcome(self._summary(execution, replayed=replayed), record)
        if stdout.truncated:
            return _failure(
                ErrorKind.INVALID_REQUEST,
                "the result was larger than the output limit; ask for fewer pages",
            )
        try:
            document: object = json.loads(stdout.text)
        except (ValueError, RecursionError):
            return ToolOutcome(self._summary(execution, replayed=replayed), record)
        if not isinstance(document, dict):
            return ToolOutcome(self._summary(execution, replayed=replayed), record)
        parsed = cast(dict[str, Any], document)
        error = parsed.get("error")
        if isinstance(error, dict):
            failure = cast(dict[str, Any], error)
            try:
                kind_of = ErrorKind(str(failure.get("kind")))
            except ValueError:
                kind_of = ErrorKind.INTERNAL
            return _failure(kind_of, str(failure.get("message", "the PDF helper failed")))
        record["pdf"] = parsed
        return ToolOutcome(self._bound(format_pdf(kind, parsed)), record)

    async def _python(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        return await self._cell("python", args, operation)

    async def _sage(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        return await self._cell("sage", args, operation)

    async def _cell(self, language: Language, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = CellRequest.model_validate(
            {
                "operation_id": operation,
                "context": self.context,
                "language": language,
                **_known(args, "code", "timeout_seconds"),
            }
        )
        return await self._run(request)

    async def _restart(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        language = args.get("language", self.languages[0] if self.languages else "python")
        if language not in self.languages:
            return _failure(ErrorKind.UNSUPPORTED, f"no {language!r} kernel in this environment")
        kernel = await self.client.restart_kernel(self.context, language)
        return ToolOutcome(
            dump({"restarted": True, "kernel_generation": kernel.generation, "state_lost": True}),
            {"kernel": kernel.model_dump(mode="json")},
        )

    def _summary(self, execution: Execution, *, replayed: bool = False) -> str:
        """The execution for the model, in at most ``output_chars`` characters of JSON.

        Each text is bounded; while they add up to more, every bound is halved.
        """
        limit = self.output_chars // 2
        while True:
            text = dump(_describe(execution, limit, replayed))
            if len(text) <= self.output_chars or limit <= 64:
                return text
            limit //= 2

    def _bound(self, text: str) -> str:
        return _bound(text, self.output_chars // 2)

    # Files.

    async def _read(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ReadFileRequest.model_validate(
            {"context": self.context, **_known(args, "path", "offset", "length")}
        )
        content = await self.client.read_file(request)
        try:
            data = base64.b64decode(content.content_b64, validate=True)
        except binascii.Error:
            return _failure(ErrorKind.INTERNAL, "the worker sent file content that is not base64")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        summary: dict[str, Any] = {
            "path": content.path,
            "size": content.size,
            "sha256": content.sha256,
        }
        if text is None:
            summary["binary"] = f"{len(data)} bytes, not shown"
        else:
            summary["content"] = self._bound(text)
        if not content.eof:
            summary["more"] = f"read again with offset {content.offset + len(data)}"
        return ToolOutcome(
            dump(summary), {"read": {"path": content.path, "sha256": content.sha256}}
        )

    async def _write(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        content = args.get("content")
        if not isinstance(content, str):
            return _failure(ErrorKind.INVALID_REQUEST, "content must be a string")
        try:
            encoded = content.encode()
        except UnicodeEncodeError:
            return _failure(
                ErrorKind.INVALID_REQUEST, "content is not valid text: it has an unpaired surrogate"
            )
        request = WriteFileRequest.model_validate(
            {
                "context": self.context,
                "path": args.get("path"),
                "content_b64": base64.b64encode(encoded).decode(),
            }
        )
        written = await self.client.write_file(request)
        return ToolOutcome(
            dump({"written": request.path, **written.model_dump()}), {"write": written.model_dump()}
        )

    async def _list(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ListFilesRequest.model_validate({"context": self.context, **_known(args, "path")})
        listing = await self.client.list_files(request)
        entries = [
            {"name": _bound(entry.name, 256), "type": entry.type, "size": entry.size}
            for entry in listing.entries
        ]
        shown = min(len(entries), SHOWN_ENTRIES)
        while True:
            text = dump(
                {
                    "path": listing.path,
                    "entries": entries[:shown],
                    "truncated": listing.truncated or shown < len(entries),
                }
            )
            if len(text) <= self.output_chars or shown == 0:
                return ToolOutcome(text)
            shown //= 2

    async def _publish(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = PublishRequest.model_validate(
            {"context": self.context, **_known(args, "path", "media_type")}
        )
        artifact = await self.client.publish(request)
        record = artifact.model_dump(mode="json")
        return ToolOutcome(dump({"published": record}), {"artifact": record})


def _describe(execution: Execution, limit: int, replayed: bool) -> dict[str, Any]:
    """The parts of an execution the model reads, each text within ``limit`` characters."""
    summary: dict[str, Any] = {"state": execution.state.value}
    if replayed:
        summary["replayed"] = (
            "this call was made before with the same operation ID; this is its earlier result"
        )
    result = execution.result
    if result is not None:
        if result.exit_code is not None or result.signal is not None:
            summary["exit_code"] = result.exit_code
            if result.signal is not None:
                summary["signal"] = result.signal
        for stream in (result.stdout, result.stderr):
            if stream is not None and stream.total_bytes:
                summary[stream.name] = _bound(stream.text, limit)
                if stream.truncated:
                    summary[f"{stream.name}_total_bytes"] = stream.total_bytes
        outputs: list[str] = [_output(output.data, limit) for output in result.outputs]
        if len(outputs) > SHOWN_OUTPUTS:
            half = SHOWN_OUTPUTS // 2
            omitted = f"[… {len(outputs) - 2 * half} outputs omitted …]"
            outputs = [*outputs[:half], omitted, *outputs[-half:]]
        if outputs:
            summary["outputs"] = outputs
        if result.error is not None:
            error: dict[str, Any] = {
                "kind": result.error.kind.value,
                "message": _bound(result.error.message, limit),
            }
            if result.error.traceback:
                traceback = "\n".join(result.error.traceback[-TRACEBACK_LINES:])
                error["traceback"] = _bound(traceback, limit)
            summary["error"] = error
        if result.kernel is not None:
            summary["kernel_generation"] = result.kernel.generation
        if result.state_lost:
            summary["state_lost"] = "the kernel restarted before this cell; earlier state is gone"
        if result.notices:
            summary["platform_notices"] = [_bound(notice, limit) for notice in result.notices]
    if execution.location is not None:
        summary["ran_on"] = {
            "sandbox": execution.location.sandbox_id,
            "host": _bound(execution.location.hostname, 256),
            "isolation_tier": execution.location.isolation_tier,
        }
        before = execution.location.environment_revision
        after = result.environment_revision if result is not None else None
        if before is not None and after is not None and before != after:
            summary["environment_changed"] = (
                f"the environment's installed packages changed while this ran (revision "
                f"{before} to {after}); kernels started before keep the packages they loaded"
            )
    summary["attempt_id"] = execution.attempt_id
    return summary


def _output(data: dict[str, str], limit: int) -> str:
    for mime in ("text/latex", "text/markdown", "text/plain"):
        if mime in data:
            return _bound(data[mime], limit)
    kinds = ", ".join(sorted(data))
    return _bound(f"[{kinds} output, not shown]", limit)


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"


PDF_HELPER_ERROR: Final = 3
"""The PDF helper's exit status for an error it describes in JSON."""


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, Any]]:
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def format_pdf(kind: str, result: dict[str, Any]) -> str:
    """A PDF helper result as text for the model, with lines citable as ``p<page>:<line>``."""
    source = _mapping(result.get("source"))
    head = f"{source.get('path')} (sha256 {str(source.get('sha256', ''))[:16]}…)"
    lines: list[str] = []
    if kind == "info":
        pages = _items(result.get("pages"))
        lines.append(f"{head}: {result.get('page_count')} pages")
        labels = ", ".join(f"{page.get('index')}: {page.get('label')}" for page in pages)
        lines.append(f"pages (physical: printed label): {labels}")
        empty = [str(page.get("index")) for page in pages if not page.get("has_text")]
        if empty:
            lines.append("pages without extractable text (render them): " + ", ".join(empty))
        for entry in _items(result.get("outline")):
            level = entry.get("level")
            indent = "  " * max(0, (level if isinstance(level, int) else 1) - 1)
            lines.append(f"{indent}- {entry.get('title')} -> p{entry.get('page_index')}")
    elif kind == "pages":
        lines.append(head)
        for page in _items(result.get("pages")):
            index = page.get("index")
            lines.append(f"--- page {index} (printed {page.get('label')}) ---")
            for line in _items(page.get("lines")):
                lines.append(f"[p{index}:{line.get('n')}] {line.get('text')}")
            if page.get("note"):
                lines.append(f"({page.get('note')})")
            if page.get("truncated"):
                lines.append("(the page has more lines than were returned)")
    elif kind == "search":
        hits = _items(result.get("hits"))
        lines.append(f"{len(hits)} hits for {result.get('query')!r} in {head}")
        for hit in hits:
            lines.append(
                f"[p{hit.get('page_index')}:{hit.get('n')}] (printed {hit.get('page_label')}) "
                f"{hit.get('text')}"
            )
        if result.get("truncated"):
            lines.append("(more hits were not returned; search for something narrower)")
    else:
        image = _mapping(result.get("image"))
        region = result.get("region")
        where = f"region {region} of " if region else ""
        lines.append(
            f"rendered {where}page {result.get('page_index')} "
            f"(printed {result.get('page_label')}) of {head} at {result.get('dpi')} dpi "
            f"to {image.get('path')}: {image.get('width')}x{image.get('height')} px, "
            f"sha256 {image.get('sha256')}. Publish it to keep it as evidence."
        )
    return "\n".join(lines)


def _known(args: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: args[name] for name in names if name in args}


def _failure(kind: ErrorKind, message: str) -> ToolOutcome:
    return ToolOutcome(
        dump({"error": {"kind": kind.value, "message": _bound(message, 4000)}}),
        {"error": kind.value},
    )
