# SPDX-License-Identifier: MIT
"""The provisioning tools (docs/specs/conda-provisioning.md): an agent asks for a capability
or packages, and the platform decides, installs, records and probes.

``provision`` asks the worker for an operation and follows it until it ends or waits for
permission; ``provision_status`` follows one later, cancels it, or lists the environment's
queue. Everything runs on the worker, in the target environment's sandbox: the harness
only asks and reports. A tool call's operation ID is derived from the call, as for the
other tools, so a repeated call follows the same operation instead of asking again.

The result states what the worker's records show (CP-25): the outcome, the revisions
before and after, the package changes, the probes and, short of completion, where it
stopped and what to do next.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
)

from .effects import UnknownEffectError
from .model import ToolSpec
from .worker_client import WorkerAPIError, WorkerClient, dump

if TYPE_CHECKING:
    from .tools import ToolOutcome

PROVISIONING: Final = "provisioning"
"""The feature an environment's capabilities list when its worker provisions it."""
DEFAULT_WAIT: Final = 900.0
"""Seconds ``provision`` follows an operation before it reports where it is."""
MAX_WAIT: Final = 3600.0
POLL: Final = 30.0
"""Seconds of each wait the worker is asked for (it allows 60 at most)."""
SHOWN: Final = 40
"""Files, changes and probes listed before a note counts the rest."""
TEXT: Final = 2000
"""Characters of an error message, a note or the next step shown, at most."""
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""The contracts' identifiers: what may name an operation or environment in a route."""

_SPECS = {"type": "array", "items": {"type": "string"}, "maxItems": 32}
_WAIT = {
    "type": "number",
    "minimum": 0,
    "maximum": MAX_WAIT,
    "description": "Seconds to wait for the operation to end",
}

PROVISION: Final = ToolSpec(
    "provision",
    "Install a capability or packages into the environment through the platform, instead of "
    "running pip or conda yourself. The platform resolves the request in the sandbox, checks "
    "every package file against the project's policy and refuses AGPL-licensed software the "
    "policy does not allow, asks the user for permission when the policy says so, installs in "
    "the environment's turn, records the change with a lockfile, and tests the result. Ask for "
    "a capability of the platform's catalog by name (such as gp, gap, sage, sympy, fpylll or "
    "python-flint), which a functional probe tests and which is left alone when it works "
    "already; or for conda-forge package specs (conda), such as 'pari>=2.15'; or, only for what "
    "conda-forge lacks, PyPI requirements (pip) with the reason (pip_reason), in a request of "
    "their own after the Conda one, installed from wheels only. Ubuntu packages "
    "(apt) cannot be installed at runtime: name them in system to record the need. "
    "Optional action 'upgrade' updates only explicitly named, already recorded Conda specs "
    "under the same policy and permission checks; omitted action means 'install'. Upgrade "
    "does not accept capabilities, pip or system packages, or upgrade-all. The result "
    "gives the outcome (already_satisfied, completed, awaiting_permission, denied, failed, "
    "needs_recovery), the environment's revisions before and after, the package changes, the "
    "probe results and what to do next. A denied request is the policy's decision: do not "
    "ask for it again unchanged, or install the same software another way.",
    {
        "type": "object",
        "properties": {
            "purpose": {"type": "string", "description": "Why the work needs it"},
            "action": {"type": "string", "enum": ["install", "upgrade"]},
            "capability": {"type": "string"},
            "conda": _SPECS,
            "pip": _SPECS,
            "pip_reason": {"type": "string", "description": "What conda-forge lacks"},
            "system": _SPECS,
            "environment": {
                "type": "string",
                "description": "Another of the project's environments to install into; "
                "the session's own by default",
            },
            "wait_seconds": _WAIT,
        },
        "required": ["purpose"],
        "additionalProperties": False,
    },
)

PROVISION_STATUS: Final = ToolSpec(
    "provision_status",
    "Follow a provisioning operation, cancel it, or list the operations waiting in the "
    "environment. With operation (an operation's provision_id), it returns the operation, "
    "waiting up to wait_seconds for it to end: use it once the user has answered a permission "
    "request. With cancel, it cancels the operation. Without operation, it lists the "
    "environment's queue and whether its packages need recovery.",
    {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "cancel": {"type": "boolean"},
            "wait_seconds": _WAIT,
            "environment": {"type": "string"},
        },
        "additionalProperties": False,
    },
)

PROVISION_SPECS: Final = (PROVISION, PROVISION_STATUS)


def _outcome(text: str, record: dict[str, Any]) -> ToolOutcome:
    # tools.py imports this module, so its ToolOutcome is imported when first used.
    from .tools import ToolOutcome

    return ToolOutcome(text, record)


class ProvisionTools:
    """The provisioning tools of one session's toolbox."""

    def __init__(
        self, client: WorkerClient, context: ExecutionContext, *, output_chars: int = 20000
    ) -> None:
        self.client = client
        self.context = context
        self.output_chars = output_chars

    def _context(self, args: dict[str, Any]) -> ExecutionContext:
        environment = args.get("environment")
        if environment is None:
            return self.context
        return self.context.model_copy(update={"environment_id": environment})

    async def provision(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        """Ask for an operation and follow it until it ends or waits for permission."""
        action = args.get("action", "install")
        if type(action) is not str or action not in ("install", "upgrade"):
            return _failure(ErrorKind.INVALID_REQUEST, "action must be install or upgrade")
        try:
            fields = {
                key: args[key]
                for key in (
                    "purpose",
                    "action",
                    "capability",
                    "conda",
                    "pip",
                    "pip_reason",
                    "system",
                )
                if key in args
            }
            request = ProvisionRequest.model_validate(
                {
                    "operation_id": operation,
                    "context": self._context(args).model_dump(),
                    **fields,
                }
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            return _failure(ErrorKind.INVALID_REQUEST, f"{where}: {first.get('msg')}")
        submitted, created = await self.client.provision(request)
        try:
            followed = await self._follow(
                submitted, _seconds(args.get("wait_seconds"), DEFAULT_WAIT)
            )
        except Exception as exc:
            # The worker already accepted the operation. An unreadable later status
            # cannot turn that acceptance into a safely repeatable tool failure.
            raise UnknownEffectError(
                "provision_post_submit",
                "provision was accepted but its later status could not be confirmed",
            ) from exc
        return self._result(followed, replayed=not created)

    async def status(self, args: dict[str, Any], operation: str) -> ToolOutcome:
        """Follow, cancel, or list the queue."""
        del operation
        provision_id = args.get("operation")
        project_id = self.context.project_id
        if provision_id is None:
            environment = args.get("environment", self.context.environment_id)
            if not isinstance(environment, str) or not _IDENTIFIER.fullmatch(environment):
                return _failure(ErrorKind.INVALID_REQUEST, "environment must be an environment ID")
            queue = await self.client.provisioning_queue(environment, project_id=project_id)
            return self._queue(queue)
        if not isinstance(provision_id, str) or not _IDENTIFIER.fullmatch(provision_id):
            return _failure(ErrorKind.INVALID_REQUEST, "operation must be an operation's ID")
        if args.get("cancel") is True:
            found = await self.client.cancel_provision(
                provision_id, project_id=project_id, context=self._context(args)
            )
        else:
            found = await self.client.provision_status(provision_id, project_id=project_id)
        wait = _seconds(args.get("wait_seconds"), 0.0)
        if wait > 0 and found.state is not ProvisionState.FINISHED:
            found = await self._follow(found, wait, until_finished=True)
        return self._result(found)

    async def _follow(
        self, operation: ProvisionOperation, wait: float, *, until_finished: bool = False
    ) -> ProvisionOperation:
        """The operation once it ends, waits for permission (unless ``until_finished``), or
        ``wait`` seconds pass."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            state = operation.state
            if state is ProvisionState.FINISHED or (
                state is ProvisionState.AWAITING_PERMISSION and not until_finished
            ):
                return operation
            remaining = deadline - loop.time()
            if remaining <= 0:
                return operation
            try:
                operation = await self.client.provision_status(
                    operation.provision_id,
                    after=operation.seq,
                    wait=min(POLL, remaining),
                    project_id=self.context.project_id,
                )
            except WorkerAPIError as exc:
                if exc.kind is not ErrorKind.UNAVAILABLE:
                    raise
                await asyncio.sleep(min(2.0, max(0.0, remaining)))

    def _result(self, operation: ProvisionOperation, *, replayed: bool = False) -> ToolOutcome:
        record = {"provision": operation.model_dump(mode="json"), "replayed": replayed}
        limit = max(10, SHOWN)
        while True:
            text = dump(describe(operation, limit, replayed=replayed))
            if len(text) <= self.output_chars or limit <= 2:
                return _outcome(text, record)
            limit //= 2

    def _queue(self, queue: ProvisioningQueue) -> ToolOutcome:
        summary: dict[str, Any] = {
            "environment": queue.environment_id,
            "packages": queue.status,
            "revision": queue.revision,
            "operations": [
                {
                    "operation": op.provision_id,
                    "state": op.state.value,
                    "asked_for": _asked(op),
                    **({"place": op.queue_position} if op.queue_position is not None else {}),
                    **(
                        {"permission_request": op.permission_request}
                        if op.permission_request is not None
                        else {}
                    ),
                }
                for op in queue.operations
            ],
        }
        return _outcome(dump(summary), {"provisioning_queue": queue.model_dump(mode="json")})


def describe(
    operation: ProvisionOperation, limit: int, *, replayed: bool = False
) -> dict[str, Any]:
    """An operation as the model reads it, each list within ``limit`` entries."""
    summary: dict[str, Any] = {
        "operation": operation.provision_id,
        "outcome": operation.outcome.value if operation.outcome is not None else None,
        "state": operation.state.value,
        "environment": operation.request.context.environment_id,
        "asked_for": _asked(operation),
    }
    if operation.request.action == "upgrade":
        summary["action"] = "upgrade"
    if replayed:
        summary["replayed"] = "this call was made before; this is the same operation"
    revisions = {
        "planned_against": operation.planned_revision,
        "before": operation.revision_before,
        "after": operation.revision_after,
    }
    if any(value is not None for value in revisions.values()):
        summary["revisions"] = {key: value for key, value in revisions.items() if value is not None}
    if operation.queue_position is not None:
        summary["queue_position"] = operation.queue_position
    if operation.permission_request is not None:
        summary["permission_request"] = operation.permission_request
    denied = operation.outcome is not None and operation.outcome.value == "denied"
    if operation.files and (operation.state is not ProvisionState.FINISHED or denied):
        # What is being asked for, or what was refused.
        files = [
            " ".join(
                part
                for part in (
                    f"{file.channel}::{file.name}" if file.channel else file.name,
                    file.version,
                    file.build or "",
                    f"({file.license})" if file.license else "",
                    file.action,
                    "AGPL" if file.agpl else "",
                )
                if part
            )
            for file in operation.files
        ]
        summary["files"] = _bounded(files, limit)
    if operation.changes:
        changes = [
            f"+ {c.name} {c.after}"
            if c.before is None
            else f"- {c.name} {c.before}"
            if c.after is None
            else f"{c.name} {c.before} -> {c.after}"
            for c in operation.changes
        ]
        summary["changes"] = _bounded(changes, limit)
    if operation.probes:
        probes = [
            f"{p.probe} ({p.kind}) {p.status}"
            + (f" at revision {p.revision}" if p.revision is not None else "")
            + (f": {_line(p.output)}" if p.status != "passed" and p.output else "")
            for p in operation.probes
        ]
        summary["probes"] = _bounded(probes, limit)
    if operation.stage is not None:
        summary["stopped_at"] = operation.stage
    if operation.reason is not None:
        summary["reason"] = operation.reason
    # Texts shrink with the lists, so that the whole result fits the tool's limit.
    chars = max(100, TEXT * limit // SHOWN)
    if operation.error is not None:
        message = _clip(operation.error.message, chars)
        summary["error"] = {"kind": operation.error.kind.value, "message": message}
    if operation.notes:
        summary["notes"] = [_clip(note, chars) for note in _bounded(list(operation.notes), limit)]
    if operation.next_step is not None:
        summary["next"] = _clip(operation.next_step, chars)
    if operation.state is not ProvisionState.FINISHED:
        summary["follow"] = (
            f"call provision_status with operation {operation.provision_id} and wait_seconds "
            "to follow it"
        )
    return summary


def _asked(operation: ProvisionOperation) -> str:
    request = operation.request
    if request.capability is not None:
        return f"capability {request.capability}"
    parts = [*request.conda, *(f"pip:{spec}" for spec in request.pip)]
    parts += [f"system:{name}" for name in request.system]
    return ("upgrade " if request.action == "upgrade" else "") + ", ".join(parts)


def _bounded(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return items
    return [*items[:limit], f"[… {len(items) - limit} more]"]


def _clip(text: str, chars: int) -> str:
    """``text`` within ``chars`` characters: its head and its tail, where errors end."""
    if len(text) <= chars:
        return text
    half = chars // 2
    return f"{text[:half]} […] {text[-half:]}"


def _line(text: str) -> str:
    """The last line of a probe's output, where a failure usually says what went wrong."""
    last = text.strip().splitlines()[-1] if text.strip() else ""
    return last[:200]


def _seconds(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(0.0, min(float(value), MAX_WAIT))


def _failure(kind: ErrorKind, message: str) -> ToolOutcome:
    return _outcome(
        json.dumps({"error": {"kind": kind.value, "message": message[:4000]}}),
        {"error": kind.value},
    )
