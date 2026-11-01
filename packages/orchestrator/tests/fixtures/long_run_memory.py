# SPDX-License-Identifier: MIT
"""Fresh-process production loop/fold/review/save/retrieval scale fixture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.folding import FoldLifecycle
from sandbox_science.orchestrator.context.history import (
    Archive,
    Folder,
    RunArchive,
    geometry,
    session_view,
)
from sandbox_science.orchestrator.context.history.memory import RunMemory
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.harness import Finished, save
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ToolCall, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

PURPOSE = "Running one step of the fixture"
"""The purpose each scripted call names (docs/specs/tools.md, Purpose 1)."""


def rss() -> int:
    return next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith("VmHWM:")
    )


async def measure(root: Path, count: int) -> dict[str, object]:
    projects = ProjectStore(root)
    project, _ = projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p", session_id="s", environment_id=project.default_environment_id
    )
    start = datetime(2026, 10, 17, tzinfo=UTC)
    name = run_name(start, "scale")
    initial = rss()
    tracemalloc.start()
    documents = DocumentStore(projects.control.path)
    reviews = ReviewStore(documents)
    reviews.configure(
        "p", "s", ReviewConfig(min_interval_seconds=0, max_turns=4, calls_per_hour=10000)
    )

    class Reviewer:
        async def complete(self, messages, tools):
            if messages[-1]["role"] == "user":
                manifest = json.dumps({"purpose_note": PURPOSE})
                return ModelTurn(
                    None, tool_calls=(ToolCall("manifest", "review_manifest", manifest),)
                )
            return ModelTurn(
                json.dumps(
                    {"outcome": "inconclusive", "summary": "Fixture read.", "cap_reached": False}
                )
            )

    controller = ReviewController(
        reviews,
        context,
        run_id="scale",
        model=Reviewer(),
        alias="fixture-reviewer",
        archived_as=name,
    )
    hook = Reviews(controller)
    archive = Archive(projects.control)
    top = 100000
    shape = geometry(top, 0, 10000)
    folder = Folder(
        archive,
        context,
        run=name,
        view=session_view(archive, context, geometry=shape),
        start=shape,
        ceiling=top,
        chunk=10000,
    )
    box = Toolbox(WorkerClient("http://unused.invalid", "test-secret-unused"), context)

    class Model:
        def __init__(self):
            self.turn = 0

        async def complete(self, messages, tools):
            self.turn += 1
            if self.turn > count:
                return ModelTurn("The fixture completed.")
            return ModelTurn(
                None,
                tool_calls=(
                    ToolCall(
                        str(self.turn),
                        "arithmetic",
                        json.dumps({"step": self.turn, "purpose_note": PURPOSE}),
                    ),
                ),
                native=b"n" * 65536,
                native_format="fixture",
            )

    async def arithmetic(request):
        step = request.args["step"]
        if step % 10 == 0:
            await controller.author_call("request_review", {}, f"request-{step}")
        text = f"STEP={step:06d}\n" + "x" * (256 * 1024)
        return ToolOutcome(text[:32768], {"value": step, "stdout": text, "state": "succeeded"})

    box.entries = {
        "arithmetic": ToolEntry(
            ToolSpec("arithmetic", "Fixture arithmetic", {"type": "object"}), "read", arithmetic
        )
    }
    result = await run_task(
        Model(),
        box,
        "Compute each independent fixture step.",
        run_id="scale",
        max_turns=count + 1,
        hooks=(FoldLifecycle(folder), hook),
        folds=folder,
        log=RunArchive(archive, context, run=name, parent=None),
    )
    assert result.stopped == "answered", (result.stopped, result.summaries)
    assert len(result.tool_records) == count
    assert len(folder.folds) >= 2
    assert len(controller.review_ids) >= 1
    finished = Finished("scale", context, "Fixture", start, result, (FoldLifecycle(folder), hook))
    errors = []
    assert (
        await save(finished.fields(), 0, finished.hooks, projects=projects, error=errors.append)
        == 0
    ), errors
    saved = projects.run("p", "s", name)
    final_bytes = len(saved.to_json().encode())
    projects.control.close()
    rebuilt = session_view(Archive(projects.control), context, geometry=shape)
    assert rebuilt
    # Reopen and retrieve exact early/middle/late original outputs from the journal.
    memory = RunMemory(projects.control, ("p", "s", name))
    tools = memory.tools()
    for index in (0, count // 2, count - 1):
        message = tools[index]["_record_storage"]["message"]
        digest = hashlib.sha256()
        chunks = bytearray()
        offset = 0
        while True:
            chunk = Archive(projects.control).tool_record_chunk("p", message, offset=offset)
            digest.update(chunk.data)
            chunks.extend(chunk.data)
            if chunk.next_offset is None:
                break
            offset = chunk.next_offset
        original = json.loads(chunks)
        expected = f"STEP={index + 1:06d}\n" + "x" * (256 * 1024)
        assert original["stdout"] == expected
        assert digest.hexdigest() == hashlib.sha256(chunks).hexdigest()
    _, peak = tracemalloc.get_traced_memory()
    growth = rss() - initial
    tracemalloc.stop()
    # Positive control deliberately materializes canonical historical payloads.
    tracemalloc.start()
    control = [
        Archive(projects.control).tool_record("p", tools[index]["_record_storage"]["message"])
        for index in range(count)
    ]
    _, control_peak = tracemalloc.get_traced_memory()
    assert len(control) == count
    tracemalloc.stop()
    report: dict[str, object] = {
        "count": count,
        "python_peak": peak,
        "rss_growth": growth,
        "final_bytes": final_bytes,
        "folds": len(folder.folds),
        "reviews": len(controller.review_ids),
        "control_peak": control_peak,
    }
    await controller.close()
    await box.client.close()
    documents.close()
    projects.close()
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(measure(Path(sys.argv[1]), int(sys.argv[2])))))
