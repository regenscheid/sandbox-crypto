# SPDX-License-Identifier: MIT
"""Command line: run one task through the harness against a worker.

```
sandbox-science-harness run --worker http://127.0.0.1:8100 --token-file worker.token \\
    --project p1 --session s1 --environment env-1 \\
    (--script turns.json | --model-url http://127.0.0.1:4000 --model main --api-key-file key) \\
    [--min-isolation-tier 3 | --allow-unisolated] [--transcript out.json] "the task"
```

The harness refuses a worker whose isolation tier in effect is below the minimum,
so a misconfigured deployment cannot quietly run research code without a sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sandbox_science.contracts import ExecutionContext, Location, new_id

from .loop import run_task
from .model import ChatModel, ModelError, OpenAICompatibleModel, ScriptedModel
from .tools import Toolbox, ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient


def _read_secret(path: Path) -> str:
    return path.read_text().strip()


def tier_in_effect(location: Location) -> int | None:
    """The isolation tier the worker's probe left standing (D43); ``None`` without a probe.

    The worker lowers the declared tier when its probe finds less isolation, so the
    declared tier alone never counts.
    """
    return location.isolation_tier if location.isolation_evidence is not None else None


def describe_isolation(location: Location) -> str:
    """The tier in effect, the declared one if it differs, and what limits it."""
    evidence = location.isolation_evidence
    if evidence is None:
        return "no isolation evidence"
    text = f"isolation tier {location.isolation_tier}"
    if location.declared_isolation_tier != location.isolation_tier:
        text += f" (declared {location.declared_isolation_tier})"
    if evidence.findings:
        text += ": " + "; ".join(evidence.findings)
    return text


async def _run(args: argparse.Namespace) -> int:
    context = ExecutionContext(
        project_id=args.project, session_id=args.session, environment_id=args.environment
    )
    model: ChatModel
    if args.script is not None:
        model = ScriptedModel.from_json(json.loads(args.script.read_text()))
    else:
        model = OpenAICompatibleModel(args.model_url, args.model, _read_secret(args.api_key_file))
    async with WorkerClient(args.worker, _read_secret(args.token_file)) as client:
        capabilities = await client.capabilities(args.environment)
        location = capabilities.location
        tier = tier_in_effect(location)
        if not args.allow_unisolated and (tier is None or tier < args.min_isolation_tier):
            print(
                f"error: the worker offers {describe_isolation(location)}. At least tier "
                f"{args.min_isolation_tier} is required (--allow-unisolated for development)",
                file=sys.stderr,
            )
            return 2
        print(
            f"worker: pool {location.pool_id}, {location.backend} backend, sandbox "
            f"{location.sandbox_id}, host {location.hostname}, {describe_isolation(location)}"
        )

        async def show(name: str, arguments: str, outcome: ToolOutcome) -> None:
            print(f"\n→ {name} {arguments}\n← {outcome.text}")

        transcript = await run_task(
            model,
            Toolbox(
                client,
                context,
                languages=capabilities.languages,
                features=capabilities.features,
            ),
            args.task,
            run_id=args.run_id or new_id("run"),
            max_turns=args.max_turns,
            on_tool=show,
        )
    if isinstance(model, OpenAICompatibleModel):
        await model.close()
    print(f"\n{transcript.final_text or '(no answer)'}")
    if args.transcript is not None:
        args.transcript.write_text(
            json.dumps(
                {
                    "context": context.model_dump(),
                    "messages": transcript.messages,
                    "tools": transcript.tool_records,
                    "stopped": transcript.stopped,
                },
                indent=2,
            )
        )
    return 0 if transcript.stopped == "answered" else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one task to an answer")
    run.add_argument("--worker", required=True, help="worker API base URL")
    run.add_argument("--token-file", type=Path, required=True)
    run.add_argument("--project", required=True)
    run.add_argument("--session", required=True)
    run.add_argument("--environment", required=True)
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="replay model turns from a JSON file")
    source.add_argument("--model-url", help="OpenAI-compatible base URL (the LiteLLM proxy)")
    run.add_argument("--model", default="main", help="model alias")
    run.add_argument("--api-key-file", type=Path)
    run.add_argument("--min-isolation-tier", type=int, default=3)
    run.add_argument("--allow-unisolated", action="store_true")
    run.add_argument("--max-turns", type=int, default=30)
    run.add_argument(
        "--run-id",
        help="name the run (default: a new random ID). Under a reused ID, a call identical "
        "to one already made in the same place returns its earlier result, marked replayed",
    )
    run.add_argument("--transcript", type=Path)
    run.add_argument("task")
    args = parser.parse_args(argv)
    if args.model_url is not None and args.api_key_file is None:
        parser.error("--model-url needs --api-key-file")
    try:
        return asyncio.run(_run(args))
    except (WorkerAPIError, ModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
