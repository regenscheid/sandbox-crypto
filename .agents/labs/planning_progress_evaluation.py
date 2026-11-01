# SPDX-License-Identifier: MIT
"""Opt-in production-path planning calibration; default has no external effects."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from sandbox_science.orchestrator.model import ChatModel, Message, ModelTurn, ToolSpec
    from sandbox_science.orchestrator.session_plans import CompactPlan
    from sandbox_science.orchestrator.tools import ToolOutcome

PROJECT = "planning-evaluation"
SESSION = "synthetic-pilot"
PURPOSE = "disposable production-path planning evaluation"
MAX_CONFIG = 8192
MAX_KEY = 4096
MAX_PUBLIC = 32768
MAX_INPUT = 128 * 1024
PLAN_NAMES = frozenset(
    {"plan_read", "plan_create", "plan_revise", "plan_activate", "plan_progress", "plan_finish"}
)
STEPS = ("inspect", "report")
TASK = (
    "This is a synthetic planning/progress evaluation, not research execution. Organize "
    "a two-step investigation using stable step IDs inspect and report. First inspect the "
    "supplied toy observations 2+2=4 and 3+3=6. Then prepare a qualified report: the "
    "requested independent third observation is unavailable, so the report step is blocked "
    "and the goal only partially achieved, with remaining work explicit. Keep a short "
    "durable plan and record progress as work actually proceeds, before your final reply. "
    "Use the offered plan tools only; no worker, file, network or scientific computation "
    "is needed or authorized by this fixture. Do not claim verification of a general rule."
)
SOURCE_BASE = "packages/orchestrator/src/sandbox_science/orchestrator/"
SOURCE_HASHES: dict[str, str] = {
    "loop.py": "3a60565213877c398e7480440ca9d863348ff3bd9e7c24475a032309412d9612",
    "plan_tools.py": "f5022fc7f390664e4da39fd93feef8edf0411ffbe0f771e01adb1dc919de928c",
    "session_plans.py": "7a78d9062b65a848c874c14e126bdb2d58d71d10be01c1f164564493357e67de",
    "tools.py": "36721ea76117565fbea767e9bb817aa352ef316d3367d801ba83a2ee387dafdf",
    "projects.py": "b007ec185139de0e103bf36c90ac1adfd6a3d6134619380d07b48e8d357bd97e",
    "model.py": "e5f221fba71cc2fd361c89ee019345469665ffd9a0597bb5f3d676f30d0975ab",
    "prompt_bundle.py": "23fcd0a35fa34bcf2d0b048f557cafd4ba24908b626a455f2e42b282a7b69c3d",
    "stock_prompts.py": "4b2833330ce6de75d5b55c26694864c5f079f880ba121d7f6767410bba4fcbc4",
    "documents/store.py": "f6f5ec25e8f40e58d60306fe5e6a9e5e9de00955572ec20db404108fad6bcc42",
    "documents/mathjobs.py": "b72419456dcdffd2fbe077e94f83d2870b892a71ed0b1796829378ea52495316",
}


@dataclass(frozen=True)
class Budget:
    calls: int
    output_each: int
    output_total: int
    input_total: int
    seconds: int

    def validate(self) -> None:
        values = (
            (self.calls, 1, 12),
            (self.output_each, 64, 1024),
            (self.output_total, 64, 8192),
            (self.input_total, MAX_INPUT, 2 * 1024 * 1024),
            (self.seconds, 5, 180),
        )
        if any(type(value) is not int or not low <= value <= high for value, low, high in values):
            raise ValueError("invalid finite evaluation budget")
        if self.calls * self.output_each > self.output_total:
            raise ValueError("output reservation does not cover every admitted call")


@dataclass(frozen=True, repr=False)
class PrivateModel:
    endpoint: str = field(repr=False)
    alias: str = field(repr=False)
    key: str = field(repr=False)


def private_bytes(path: Path, maximum: int) -> bytes:
    """Read one root-selected private regular file without logging or hashing it."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= maximum
        ):
            raise ValueError("private input is not a bounded owned mode-0600 regular file")
        data = bytearray()
        while len(data) <= maximum:
            part = os.read(descriptor, min(4096, maximum + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(descriptor)

        def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if (
            len(data) != before.st_size
            or len(data) > maximum
            or identity(before) != identity(after)
        ):
            raise ValueError("private input changed or exceeded its bound")
        return bytes(data)
    finally:
        os.close(descriptor)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate input field")
        result[key] = value
    return result


def model_configuration(path: Path) -> PrivateModel:
    data: object = json.loads(private_bytes(path, MAX_CONFIG), object_pairs_hook=unique_object)
    if type(data) is not dict or set(data) != {"base_url", "model", "api_key_file"}:
        raise ValueError("invalid private model configuration shape")
    endpoint, alias, key_path = (data[name] for name in ("base_url", "model", "api_key_file"))
    if any(type(value) is not str or not value for value in (endpoint, alias, key_path)):
        raise ValueError("invalid private model configuration scalar")
    endpoint, alias, key_path = cast(str, endpoint), cast(str, alias), cast(str, key_path)
    if len(endpoint) > 2048 or len(alias) > 256 or len(key_path) > 4096:
        raise ValueError("private model configuration exceeds its bound")
    if any(ord(char) < 32 or ord(char) == 127 for char in endpoint + alias + key_path):
        raise ValueError("invalid private model configuration text")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"})
        or not Path(key_path).is_absolute()
    ):
        raise ValueError("private model endpoint or key reference is unsupported")
    key = private_bytes(Path(key_path), MAX_KEY).decode("utf-8", "strict").strip()
    if not key or any(ord(char) < 33 or ord(char) == 127 for char in key):
        raise ValueError("invalid private model credential")
    return PrivateModel(endpoint, alias, key)


def encoded_input(value: object) -> int:
    """Conservative preflight before bounded aggregate JSON encoding."""
    nodes, charged = 0, 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 8192 or depth > 32:
            raise ValueError("model input structure exceeds its bound")
        kind = type(current)
        if kind is str:
            if len(cast(str, current)) > MAX_INPUT:
                raise ValueError("model input scalar exceeds its bound")
            charged += 12 * len(cast(str, current)) + 2
        elif kind in {list, tuple}:
            sequence = cast("Sequence[object]", current)
            if len(sequence) > 512:
                raise ValueError("model input sequence exceeds its bound")
            charged += len(sequence) + 2
            pending.extend((item, depth + 1) for item in sequence)
        elif kind is dict:
            mapping = cast(dict[object, object], current)
            if len(mapping) > 256 or any(type(key) is not str for key in mapping):
                raise ValueError("model input mapping exceeds its bound")
            charged += 2 * len(mapping) + 2
            pending.extend((item, depth + 1) for pair in mapping.items() for item in pair)
        elif current is None or kind is bool:
            charged += 5
        elif kind is int and -(2**63) <= cast(int, current) < 2**63:
            charged += 21
        else:
            raise ValueError("unsupported model input type")
        if charged > 12 * MAX_INPUT:
            raise ValueError("model input preflight exceeds its bound")
    size = len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    if size > MAX_INPUT:
        raise ValueError("model input exceeds its complete byte cap")
    return size


def source_guard(repo: Path, expected_head: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head) or not repo.is_absolute():
        raise ValueError("exact reviewed source selection required")

    def git(*args: str) -> str:
        return subprocess.check_output(  # noqa: S603 -- fixed read-only git subcommands below
            ["/usr/bin/git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
        ).strip()

    if git("rev-parse", "HEAD") != expected_head or git("log", "-1", "--format=%G?") != "G":
        raise ValueError("source is not the selected signed head")
    if git("status", "--porcelain"):
        raise ValueError("source is not clean")
    for name, expected in SOURCE_HASHES.items():
        path = repo / SOURCE_BASE / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 256 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("reviewed production source changed")
    return {"head": expected_head, "signature": "G", "production_files": dict(SOURCE_HASHES)}


class Boundary:
    """Owned application-call budget; blocks every non-plan request before dispatch."""

    def __init__(self, model: ChatModel, budget: Budget) -> None:
        self.model, self.budget = model, budget
        self.calls = self.input_bytes = self.reserved_output = self.nonplan = 0
        self.reason: str | None = None
        self.started = time.monotonic()

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        from sandbox_science.orchestrator.model import ModelError

        try:
            self.budget.validate()
            size = encoded_input(
                [
                    list(messages),
                    [
                        {
                            "name": item.name,
                            "description": item.description,
                            "parameters": item.parameters,
                        }
                        for item in tools
                    ],
                ]
            )
            if (
                self.calls >= self.budget.calls
                or self.input_bytes + size > self.budget.input_total
                or self.reserved_output + self.budget.output_each > self.budget.output_total
                or time.monotonic() - self.started >= self.budget.seconds
            ):
                self.reason = "budget_exhausted"
                raise ValueError("budget")
            self.calls += 1
            self.input_bytes += size
            self.reserved_output += self.budget.output_each
            reply = await self.model.complete(messages, tools)
        except asyncio.CancelledError:
            self.reason = "cancelled_unknown_call_outcome"
            raise
        except Exception:
            self.reason = self.reason or "model_or_input_failure"
            raise ModelError("evaluation boundary failed; private error data omitted") from None
        if len(reply.tool_calls) > 8 or any(
            call.name not in PLAN_NAMES for call in reply.tool_calls
        ):
            self.nonplan += sum(call.name not in PLAN_NAMES for call in reply.tool_calls)
            self.reason = "unsupported_tool_request"
            raise ModelError("evaluation refuses non-plan or oversized tool batches")
        return reply


class Calibration:
    """One small deterministic control, using tool receipts from the actual loop."""

    def __init__(self, omit: bool) -> None:
        self.omit, self.index = omit, 0

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        from sandbox_science.orchestrator.model import ModelTurn, ToolCall

        if not PLAN_NAMES.issubset({spec.name for spec in tools}):
            raise ValueError("missing actual planning tools")
        prior = [
            json.loads(message["content"]) for message in messages if message.get("role") == "tool"
        ]
        body = {
            "goal": "Inspect toy evidence and qualify the report",
            "feasibility": "Two supplied toy observations; third independent observation absent",
            "outputs": [
                {
                    "title": "Qualified report",
                    "description": "Synthetic evidence only",
                    "evidence_standard": "Separate observations from general claims",
                }
            ],
            "tracks": [],
            "phases": [
                {
                    "phase_id": "analysis",
                    "title": "Toy analysis",
                    "steps": [
                        {
                            "step_id": step,
                            "title": step.title(),
                            "description": "Inspect supplied facts"
                            if step == "inspect"
                            else "Report missing independent evidence",
                            "evidence_standard": "Retain scope and limits",
                        }
                        for step in STEPS
                    ],
                }
            ],
        }
        ref = prior[0]["receipt"]["ref"] if prior else None
        actions: list[tuple[str, dict[str, object]]] = [
            (
                "plan_create",
                {"operation_id": "create", "body": body, "reason": "Synthetic calibration"},
            ),
            (
                "plan_activate",
                {
                    "operation_id": "activate",
                    "base": ref,
                    "expected_status_revision": 1,
                    "expected_progress_cursor": 0,
                    "reason": "Existing synthetic task authorization",
                },
            ),
        ]
        cursor = 0
        for step, final in (("inspect", "completed"), ("report", "blocked")):
            if not self.omit:
                actions.append(
                    (
                        "plan_progress",
                        {
                            "operation_id": step + "-start",
                            "base": ref,
                            "step_id": step,
                            "expected_step_cursor": 0,
                            "expected_status_revision": 2,
                            "status": "in_progress",
                            "note": "Beginning synthetic analysis",
                        },
                    )
                )
                cursor += 1
            actions.append(
                (
                    "plan_progress",
                    {
                        "operation_id": step + "-end",
                        "base": ref,
                        "step_id": step,
                        "expected_step_cursor": cursor if not self.omit else 0,
                        "expected_status_revision": 2,
                        "status": final,
                        "note": "Toy observations only; independent evidence remains absent",
                    },
                )
            )
            cursor += 1
        actions.append(
            (
                "plan_finish",
                {
                    "operation_id": "finish",
                    "base": ref,
                    "expected_status_revision": 2,
                    "expected_progress_cursor": cursor,
                    "reason": "Qualified synthetic outcome",
                    "outcome": {
                        "status": "partially_achieved",
                        "summary": "Toy evidence inspected; independent report blocked",
                        "evidence": [],
                        "remaining_work": ["Obtain independent evidence"],
                    },
                },
            )
        )
        index, self.index = self.index, self.index + 1
        if index >= len(actions):
            return ModelTurn(
                "Synthetic partial outcome; no general result verified.",
                model="scripted-evaluation",
            )
        name, arguments = actions[index]
        return ModelTurn(
            None,
            (
                ToolCall(
                    f"calibration-{index}",
                    name,
                    json.dumps({"purpose_note": "Scripted planning calibration step", **arguments}),
                ),
            ),
            model="scripted-evaluation",
        )


class Fixture:
    """Own only a newly created marked local control fixture, never a worker."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="planning-progress-evaluation-"))
        self.identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self.marker = {"version": 1, "purpose": PURPOSE, "run_id": uuid.uuid4().hex}
        self.marker_bytes = json.dumps(self.marker).encode()
        self.closed = False
        try:
            (self.root / "owner.json").write_bytes(self.marker_bytes)
        except BaseException:
            # This root is newly allocated and no model/tool has seen it yet.
            shutil.rmtree(self.root)
            raise

    def remove(self) -> bool:
        info = self.root.lstat()
        descriptor = os.open(self.root / "owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            marker_info = os.fstat(descriptor)
            marker_matches = (
                stat.S_ISREG(marker_info.st_mode)
                and marker_info.st_uid == os.getuid()
                and marker_info.st_size == len(self.marker_bytes)
                and os.read(descriptor, len(self.marker_bytes) + 1) == self.marker_bytes
            )
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != self.identity
            or not marker_matches
            or not shutil.rmtree.avoids_symlink_attacks
        ):
            raise ValueError("owned fixture identity changed")
        shutil.rmtree(self.root)
        self.closed = True
        return not self.root.exists()


def criteria(
    snapshot: CompactPlan | None, started: dict[str, tuple[str, int, str]]
) -> dict[str, bool]:
    """Evaluate observations; never create progress or infer scientific success."""
    matches = bool(
        snapshot
        and snapshot.head.scope.project_id == PROJECT
        and snapshot.head.scope.session_id == SESSION
        and snapshot.version.scope == snapshot.head.scope
        and len(snapshot.steps) == len(STEPS)
        and {step.step_id for step in snapshot.steps} == set(STEPS)
    )
    progress = bool(
        matches
        and snapshot
        and all(
            step.matches_current_step
            and step.latest
            and step.step_id in started
            and started[step.step_id][0] == snapshot.head.ref.plan_id
            and started[step.step_id][1] < step.latest.sequence
            and started[step.step_id][2] == step.step_sha256
            for step in snapshot.steps
        )
    )
    terminal = bool(
        matches
        and snapshot
        and snapshot.head.status == "completed"
        and snapshot.head.outcome
        and snapshot.head.outcome.status == "partially_achieved"
        and snapshot.head.outcome.remaining_work
        and all(
            step.latest
            and step.latest.status == ("completed" if step.step_id == "inspect" else "blocked")
            for step in snapshot.steps
        )
    )
    return {
        "matching_in_progress_before_terminal": progress,
        "qualified_terminal_outcome": terminal,
        "current_scope_and_steps": matches,
    }


async def settled(awaitable: Awaitable[object]) -> object:
    """Drain an admitted close/read even through repeated caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def reserve_output(path: Path) -> int:
    """Reserve public output before a model call, never replace a prior record."""
    if not path.is_absolute():
        raise ValueError("absolute exclusive public output required")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def publish(descriptor: int, value: dict[str, object]) -> None:
    encoded = json.dumps(value, sort_keys=True, indent=2).encode()
    if len(encoded) > MAX_PUBLIC:
        raise ValueError("invalid public output bound")
    view = memoryview(encoded + b"\n")
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("public record write failed")
        view = view[count:]
    os.fsync(descriptor)


async def evaluate(args: argparse.Namespace) -> dict[str, object]:
    # Imports and control-store work happen only after explicit root opt-in.
    source = source_guard(args.repo, args.head)
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.documents import DocumentStore
    from sandbox_science.orchestrator.documents.mathjobs import threaded
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.model import OpenAICompatibleModel
    from sandbox_science.orchestrator.plan_tools import PlanTools
    from sandbox_science.orchestrator.projects import ProjectStore
    from sandbox_science.orchestrator.session_plans import SessionPlans
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    for name in SOURCE_HASHES:
        module_name = "sandbox_science.orchestrator." + name[:-3].replace("/", ".")
        module = sys.modules.get(module_name)
        filename = getattr(module, "__file__", None)
        if (
            type(filename) is not str
            or Path(filename).resolve() != (args.repo / SOURCE_BASE / name).resolve()
        ):
            raise ValueError("loaded production module differs from reviewed source")
    budget = Budget(args.calls, args.output_each, args.output_total, args.input_total, args.seconds)
    budget.validate()
    private = model_configuration(args.config) if args.mode == "pilot" else None
    model = (
        OpenAICompatibleModel(
            private.endpoint,
            private.alias,
            private.key,
            timeout=budget.seconds,
            max_tokens=budget.output_each,
            max_response_bytes=32768,
        )
        if private
        else None
    )
    fixture = Fixture()
    writer: DocumentStore | None = None
    reader: DocumentStore | None = None
    adapter: PlanTools | None = None
    started: dict[str, tuple[str, int, str]] = {}
    observations = 0
    outcome: CompactPlan | None = None
    result: dict[str, object] = {
        "version": 1,
        "mode": args.mode,
        "calibration_case": args.case if args.mode == "calibrate" else None,
        "recorded_at": datetime.now(UTC).isoformat(),
        "source": source,
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture": str(fixture.root),
        "fixture_run_id": fixture.marker["run_id"],
        "budget": {
            "calls": budget.calls,
            "requested_output_each": budget.output_each,
            "requested_output_total": budget.output_total,
            "application_input_bytes_total": budget.input_total,
            "seconds": budget.seconds,
        },
        "worker_effects": 0,
        "provider_dispatch_proved": False,
        "scientific_verification": False,
    }
    boundary = Boundary(model if model else Calibration(args.case == "missing-progress"), budget)
    try:
        projects = ProjectStore(fixture.root / "state")
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        session = projects.open_session(PROJECT, SESSION, dedicated=False)
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
        )
        writer = DocumentStore(fixture.root / "control.sqlite3")
        core = SessionPlans(writer)
        adapter = PlanTools(
            core,
            projects,
            context,
            run_id="evaluation-main",
            # A fixed logical evaluator label avoids copying private config into
            # hashed control attribution; it is not an upstream alias claim.
            alias="planning-evaluation" if private else "scripted-evaluation",
        )
        reader = DocumentStore(writer.path, readonly=True)
        readonly = SessionPlans(reader)
        authority = adapter.authority

        async def observe(_name: str, _arguments: str, _outcome: ToolOutcome) -> None:
            nonlocal observations
            if observations >= 32:
                raise ValueError("observation budget exhausted")
            snapshot = await threaded(lambda: readonly.compact_current(authority))
            observations += 1
            if snapshot is not None:
                for step in snapshot.steps:
                    latest = step.latest
                    if (
                        step.step_id in STEPS
                        and step.matches_current_step
                        and latest
                        and latest.status == "in_progress"
                        and latest.author.run_id == "evaluation-main"
                    ):
                        started[step.step_id] = (
                            snapshot.head.ref.plan_id,
                            latest.sequence,
                            step.step_sha256,
                        )

        # No WorkerClient instance or network session is constructed. Boundary refuses
        # every non-plan request before run_task can dispatch to this unused argument.
        tools = Toolbox(cast(WorkerClient, object()), context, plans=adapter)
        remaining = budget.seconds - (time.monotonic() - boundary.started)
        if remaining <= 0:
            boundary.reason = "budget_exhausted"
            raise TimeoutError("evaluation setup exhausted the wall budget")
        async with asyncio.timeout(remaining):
            transcript = await run_task(
                boundary,
                tools,
                TASK,
                run_id="evaluation-main",
                max_turns=budget.calls,
                on_tool=observe,
            )
        outcome = await threaded(lambda: readonly.compact_current(authority))
        checked = criteria(outcome, started)
        passed = (
            transcript.stopped == "answered"
            and transcript.failure is None
            and all(checked.values())
            and boundary.reason is None
        )
        result.update(
            {
                "passed": passed,
                "run_stop": transcript.stopped
                if transcript.stopped in {"answered", "turn_limit", "user_message", "error"}
                else "unknown",
                "failed_phase": transcript.failure,
                **checked,
                "commits_observed_before_any_run_save": observations > 0,
                "final_run_record_saved": False,
            }
        )
    except asyncio.CancelledError:
        result.update({"passed": False, "failure": "cancelled_unknown_outcome"})
    except TimeoutError:
        result.update({"passed": False, "failure": "deadline_unknown_outcome"})
    except Exception:
        result.update({"passed": False, "failure": "setup_or_evaluation_failure"})
    finally:
        cleanup: dict[str, object] = {}
        for name, close in (
            ("adapter_closed", adapter.close if adapter else None),
            ("reader_closed", reader.close if reader else None),
            ("writer_closed", writer.close if writer else None),
        ):
            try:
                if close is not None:
                    await settled(threaded(close))
                cleanup[name] = True
            except Exception:
                cleanup[name] = False
        try:
            if model is not None:
                await settled(model.close())
            cleanup["model_client_closed"] = True
        except Exception:
            cleanup["model_client_closed"] = False
        try:
            cleanup["owned_fixture_removed"] = fixture.remove() if all(cleanup.values()) else False
        except Exception:
            cleanup["owned_fixture_removed"] = False
        result.update(
            {
                "calls_admitted": boundary.calls,
                "input_bytes_admitted": boundary.input_bytes,
                "requested_output_reserved": boundary.reserved_output,
                "nonplan_attempts": boundary.nonplan,
                "boundary_reason": boundary.reason,
                "observations": observations,
                "cleanup": cleanup,
            }
        )
        if not all(cleanup.values()):
            result["passed"] = False
    result["limits"] = [
        "Synthetic plan-tool work only; no worker-start or effect ordering proof.",
        "No browser, USER-edit runtime race, saved RunRecord or notice ACK acceptance.",
        "Application output caps are not provider token/spend proof; input tokenization unknown.",
        "Private inputs and arbitrary model text/transcript are neither exported nor hashed.",
        "No retry; interrupted model calls can remain unknown. Cleanup may outlast deadline.",
    ]
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument(
        "mode", nargs="?", default="describe", choices=("describe", "calibrate", "pilot")
    )
    command.add_argument("--repo", type=Path)
    command.add_argument("--head")
    command.add_argument("--output", type=Path)
    command.add_argument("--config", type=Path)
    command.add_argument("--case", choices=("positive", "missing-progress"), default="positive")
    for flag in ("calls", "output-each", "output-total", "input-total", "seconds"):
        command.add_argument("--" + flag, type=int)
    return command


def main() -> int:
    args = parser().parse_args()
    if args.mode == "describe":
        print(
            json.dumps(
                {
                    "version": 1,
                    "default": "description_only",
                    "external_effects": False,
                    "modes": ["calibrate", "pilot"],
                    "required_budgets": [
                        "calls",
                        "output-each",
                        "output-total",
                        "input-total",
                        "seconds",
                    ],
                    "pilot": "Fixed synthetic task; private config; no worker or retry.",
                },
                sort_keys=True,
            )
        )
        return 0
    if (
        any(
            getattr(args, name) is None
            for name in (
                "repo",
                "head",
                "output",
                "calls",
                "output_each",
                "output_total",
                "input_total",
                "seconds",
            )
        )
        or (args.mode == "pilot" and (args.config is None or args.case != "positive"))
        or (args.mode == "calibrate" and args.config is not None)
    ):
        print("evaluation refused: explicit source, output and finite resource selection required")
        return 2
    logging.disable(logging.CRITICAL)

    async def owned() -> dict[str, object]:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("owned evaluation task missing")
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await evaluate(args)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    descriptor = -1
    try:
        # Refuse an invalid/preexisting output before any private config/model work.
        descriptor = reserve_output(args.output)
        result = asyncio.run(owned())
        publish(descriptor, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("passed") is True else 3
    except Exception:
        print("evaluation failed: private error data omitted; no automatic retry")
        return 2
    finally:
        if descriptor >= 0:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
