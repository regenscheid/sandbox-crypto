# SPDX-License-Identifier: MIT
"""The agent's execution tools (docs/specs/tools.md), all running on the worker.

Each tool call becomes a worker operation whose ID is derived from the call,
so a repeated call is idempotent. Results go back to the model as compact
JSON, bounded in size; the full execution records stay in the transcript.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    CellRequest,
    ErrorKind,
    Execution,
    ExecutionContext,
    ListFilesRequest,
    PublishRequest,
    ReadFileRequest,
    ShellRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import Language

from .model import ToolCall, ToolSpec
from .worker_client import WorkerAPIError, WorkerClient, dump

TRACEBACK_LINES: Final = 20
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]")

_TIMEOUT = {"type": "number", "exclusiveMinimum": 0, "description": "Wall-time limit in seconds"}

SAGE: Final = ToolSpec(
    "sage",
    "Run SageMath code in the session's persistent Sage kernel inside the project sandbox. "
    "Sage syntax applies: ^ is exponentiation and integer division gives exact rationals. "
    "Variables persist between calls until that kernel restarts. Results include their LaTeX "
    "form when Sage gives one.",
    {
        "type": "object",
        "properties": {"code": {"type": "string"}, "timeout_seconds": _TIMEOUT},
        "required": ["code"],
        "additionalProperties": False,
    },
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
    """A worker operation ID for a tool call: stable, path-safe and short."""
    return _UNSAFE.sub("-", f"{prefix}-{call.id}")[:128].lstrip("-._") or "op"


class Toolbox:
    def __init__(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        *,
        languages: Sequence[Language] = ("python",),
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
        self.specs = tuple(specs)

    async def call(self, call: ToolCall, operation: str) -> ToolOutcome:
        try:
            arguments: object = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
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
        execution = await self.client.run(request)
        return ToolOutcome(
            self._summary(execution), {"execution": execution.model_dump(mode="json")}
        )

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
        execution = await self.client.run(request)
        return ToolOutcome(
            self._summary(execution), {"execution": execution.model_dump(mode="json")}
        )

    async def _restart(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        language = args.get("language", self.languages[0] if self.languages else "python")
        if language not in self.languages:
            return _failure(ErrorKind.UNSUPPORTED, f"no {language!r} kernel in this environment")
        kernel = await self.client.restart_kernel(self.context, language)
        return ToolOutcome(
            dump({"restarted": True, "kernel_generation": kernel.generation, "state_lost": True}),
            {"kernel": kernel.model_dump(mode="json")},
        )

    def _summary(self, execution: Execution) -> str:
        summary: dict[str, Any] = {"state": execution.state.value}
        result = execution.result
        if result is not None:
            if result.exit_code is not None or result.signal is not None:
                summary["exit_code"] = result.exit_code
                if result.signal is not None:
                    summary["signal"] = result.signal
            for stream in (result.stdout, result.stderr):
                if stream is not None and stream.total_bytes:
                    summary[stream.name] = self._bound(stream.text)
                    if stream.truncated:
                        summary[f"{stream.name}_total_bytes"] = stream.total_bytes
            outputs = [self._output(output.data) for output in result.outputs]
            if outputs:
                summary["outputs"] = outputs
            if result.error is not None:
                error: dict[str, Any] = {
                    "kind": result.error.kind.value,
                    "message": result.error.message,
                }
                if result.error.traceback:
                    error["traceback"] = "\n".join(result.error.traceback[-TRACEBACK_LINES:])
                summary["error"] = error
            if result.kernel is not None:
                summary["kernel_generation"] = result.kernel.generation
            if result.state_lost:
                summary["state_lost"] = (
                    "the kernel restarted before this cell; earlier state is gone"
                )
        if execution.location is not None:
            summary["ran_on"] = {
                "sandbox": execution.location.sandbox_id,
                "host": execution.location.hostname,
                "isolation_tier": execution.location.isolation_tier,
            }
        summary["attempt_id"] = execution.attempt_id
        return dump(summary)

    def _output(self, data: dict[str, str]) -> str:
        for mime in ("text/latex", "text/markdown", "text/plain"):
            if mime in data:
                return self._bound(data[mime])
        kinds = ", ".join(sorted(data))
        return f"[{kinds} output, not shown]"

    def _bound(self, text: str) -> str:
        limit = self.output_chars // 2
        if len(text) <= limit:
            return text
        half = limit // 2
        return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"

    # Files.

    async def _read(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = ReadFileRequest.model_validate(
            {"context": self.context, **_known(args, "path", "offset", "length")}
        )
        content = await self.client.read_file(request)
        data = base64.b64decode(content.content_b64)
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
        request = WriteFileRequest.model_validate(
            {
                "context": self.context,
                "path": args.get("path"),
                "content_b64": base64.b64encode(content.encode()).decode(),
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
            {"name": entry.name, "type": entry.type, "size": entry.size}
            for entry in listing.entries
        ]
        return ToolOutcome(
            dump({"path": listing.path, "entries": entries, "truncated": listing.truncated})
        )

    async def _publish(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        request = PublishRequest.model_validate(
            {"context": self.context, **_known(args, "path", "media_type")}
        )
        artifact = await self.client.publish(request)
        record = artifact.model_dump(mode="json")
        return ToolOutcome(dump({"published": record}), {"artifact": record})


def _known(args: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: args[name] for name in names if name in args}


def _failure(kind: ErrorKind, message: str) -> ToolOutcome:
    return ToolOutcome(
        dump({"error": {"kind": kind.value, "message": message}}), {"error": kind.value}
    )
