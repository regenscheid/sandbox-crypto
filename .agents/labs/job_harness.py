# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Scripted harness acceptance checks against the disposable Lab 19 worker.

Run ``start`` while inspecting the workspace GUI, cancel its job there, then run
``notices`` in a fresh process. No model gateway or real credentials are needed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.jobs import JobTools
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, ensure_environment
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

LAB = Path.home() / ".local/state/openshell-lab"
STATE = LAB / "orch"
EVIDENCE = LAB / "evidence/job-harness.json"


def save(value: dict[str, Any]) -> None:
    EVIDENCE.write_text(json.dumps(value, indent=2) + "\n")


class MainModel:
    def __init__(self) -> None:
        self.calls = 0
        self.job_id = ""

    async def complete(self, messages: Any, tools: Any) -> ModelTurn:
        self.calls += 1
        if self.calls == 1:
            return ModelTurn(
                None,
                (
                    ToolCall(
                        "start",
                        "start_job",
                        json.dumps(
                            {
                                "script": "printf 'progress: started\\n'; sleep 180; printf 'done\\n'",
                                "purpose": "Lab 19 GUI and watcher acceptance",
                                "expected_progress": "Print the start marker, then remain running.",
                            }
                        ),
                    ),
                ),
            )
        if self.calls == 2:
            self.job_id = json.loads(messages[-1]["content"])["job_id"]
            return ModelTurn(
                None,
                (ToolCall("wait", "wait_jobs", json.dumps({"job_ids": [self.job_id]})),),
            )
        assert json.loads(messages[-1]["content"])["reason"] == "needs_attention"
        return ModelTurn("Watcher requested attention; the job remains available in the GUI.")


async def main() -> None:
    store = ProjectStore(STATE)
    project, _ = store.create("lab19", "minimal", (LAB / "policy.digest").read_text().strip())
    session = store.open_session("lab19", "gui", dedicated=False)
    context = ExecutionContext(
        project_id="lab19", session_id="gui", environment_id=session.environment_id
    )
    state = JobState(STATE)
    async with WorkerClient(
        "http://127.0.0.1:8100", (LAB / "worker/client-a.token").read_text().strip()
    ) as client:
        await ensure_environment(client, project, session.environment_id)
        capabilities = await client.capabilities(session.environment_id)
        if sys.argv[1] == "start":
            model = MainModel()
            watcher = ScriptedModel(
                [
                    ModelTurn(
                        '{"state":"fine","note":"The start marker is present."}',
                        model="lab-watcher",
                        input_tokens=50,
                        output_tokens=12,
                    ),
                    ModelTurn(
                        '{"state":"needs_attention","note":"Lab check asks for human attention."}',
                        model="lab-watcher",
                        input_tokens=55,
                        output_tokens=15,
                    ),
                ]
            )
            run_id = f"lab19-harness-{time.time_ns()}"
            jobs = JobTools(
                client,
                context,
                run_id=run_id,
                state=state,
                watcher=watcher,
                watcher_alias="lab-watcher",
                first_check_seconds=10,
                check_interval_seconds=10,
                poll_seconds=0.2,
            )
            started = datetime.now(UTC)

            async def on_tool(name: str, args: str, outcome: Any) -> None:
                if name == "start_job":
                    save(
                        {
                            "context": context.model_dump(mode="json"),
                            "job_id": json.loads(outcome.text)["job_id"],
                            "phase": "waiting",
                        }
                    )

            transcript = await run_task(
                model,
                Toolbox(client, context, features=capabilities.features, jobs=jobs),
                "Run the disposable GUI acceptance job and wait for watcher attention.",
                run_id=run_id,
                on_tool=on_tool,
            )
            assert model.calls == 3
            assert len(watcher.requests) == 2
            assert (
                await client.job_status(model.job_id, project_id="lab19")
            ).execution.state.value == "running"
            store.save_run(
                RunRecord(
                    run_id=run_id,
                    context=context,
                    task="Lab 19 watcher acceptance",
                    started_at=started,
                    ended_at=datetime.now(UTC),
                    stopped=transcript.stopped,
                    final_text=transcript.final_text,
                    messages=transcript.messages,
                    tools=transcript.tool_records,
                    job_notice_ids=transcript.job_notice_ids,
                )
            )
            state.acknowledge(context, run_id, transcript.job_notice_ids)
            save(
                {
                    "context": context.model_dump(mode="json"),
                    "job_id": model.job_id,
                    "phase": "attention",
                    "main_model_calls": model.calls,
                    "watcher_calls": len(watcher.requests),
                    "checkins": jobs.checkins,
                    "wait": transcript.tool_records[-1]["wait"],
                    "watcher_intervals_seconds": [10, 10],
                }
            )
        else:
            evidence = json.loads(EVIDENCE.read_text())
            job = await client.job_status(evidence["job_id"], project_id="lab19")
            assert job.execution.state.value == "cancelled"
            for expected in ([evidence["job_id"]], []):
                run_id = f"lab19-resume-{time.time_ns()}"
                jobs = JobTools(client, context, run_id=run_id, state=state)
                started = datetime.now(UTC)
                transcript = await run_task(
                    ScriptedModel([ModelTurn("Completion recorded.")]),
                    Toolbox(client, context, features=capabilities.features, jobs=jobs),
                    "Reconcile after the harness process ended.",
                    run_id=run_id,
                )
                assert transcript.job_notice_ids == expected
                store.save_run(
                    RunRecord(
                        run_id=run_id,
                        context=context,
                        task="Lab 19 notice reconciliation",
                        started_at=started,
                        ended_at=datetime.now(UTC),
                        stopped=transcript.stopped,
                        final_text=transcript.final_text,
                        messages=transcript.messages,
                        tools=transcript.tool_records,
                        job_notice_ids=transcript.job_notice_ids,
                    )
                )
                state.acknowledge(context, run_id, transcript.job_notice_ids)
            evidence["notice_after_process_restart"] = "delivered once, saved, acknowledged"
            evidence["next_run_notice_ids"] = []
            save(evidence)
        print(json.dumps({"check": sys.argv[1], "passed": True}))


if __name__ == "__main__":
    asyncio.run(main())
