# SPDX-License-Identifier: MIT
"""Offline request-prefix experiment through the production adapters, no model calls.

Usage: uv run python .agents/labs/cache_layout.py --output /path/outside/repo/report.json
The only server is a loopback fixture. Measurements are byte prefixes, not cache hits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.messages import MessagesModel
from sandbox_science.orchestrator.model import Message, ToolSpec
from sandbox_science.orchestrator.responses import ResponsesModel


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def shared(left: bytes, right: bytes) -> int:
    return next(
        (n for n, pair in enumerate(zip(left, right, strict=False)) if pair[0] != pair[1]),
        min(len(left), len(right)),
    )


def views(layout: str) -> list[tuple[str, list[Message]]]:
    system: Message = {
        "role": "system",
        "content": "Benign arithmetic fixture. Preserve exact values.",
    }
    history: list[Message] = [
        {
            "role": "assistant" if n % 2 == 0 else "user",
            "content": f"Recorded arithmetic observation {n}: " + "1234567890 " * 400,
        }
        for n in range(80)
    ]
    state: Message = {
        "role": "user",
        "content": "[msg-1111111111111111 · state block]\nQuestion: squares.",
    }
    changed: Message = {
        "role": "user",
        "content": "[msg-2222222222222222 · state block]\nQuestion: cubes.",
    }
    later: list[Message] = [
        {"role": "assistant", "content": "A partial answer."},
        {"role": "user", "content": "Continue with exact arithmetic."},
    ]

    def make(block: Message, tail: list[Message]) -> list[Message]:
        if layout == "state_after_history":
            return [system, *tail, block]
        return [system, block, *tail]

    # Reusing a snapshot is valid only when both its content and identity are unchanged.
    current = state if layout == "unchanged_snapshot" else changed
    first = make(state, history)
    restart = make(current, [*history, *later])
    folded = [
        {"role": "user", "content": "Fold: first forty arithmetic observations."},
        *history[40:],
        *later,
    ]
    return [
        ("first_session_call", first),
        ("ordinary_append", [*first, *later]),
        ("later_run_first_call", restart),
        ("first_after_fold", make(current, folded)),
    ]


async def measure(output: Path) -> None:
    bodies: list[dict[str, Any]] = []

    async def capture(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        if request.path == "/responses":
            body = {
                "id": "resp_fixture",
                "object": "response",
                "status": "completed",
                "model": "fixture",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Fixture."}],
                    }
                ],
            }
        else:
            body = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "fixture",
                "content": [{"type": "text", "text": "Fixture."}],
                "stop_reason": "end_turn",
            }
        return web.json_response(body)

    async def info(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": [
                    {
                        "model_name": "fixture",
                        "litellm_params": {"model": "anthropic/claude-fixture"},
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/responses", capture)
    app.router.add_post("/messages", capture)
    app.router.add_get("/model/info", info)
    tool = ToolSpec("arithmetic", "A harmless fixture", {"type": "object"})
    results: list[dict[str, object]] = []
    async with TestServer(app) as server:
        for adapter in (ResponsesModel, MessagesModel):
            for layout in ("state_first", "state_after_history", "unchanged_snapshot"):
                model = adapter(str(server.make_url("")), "fixture", "test-secret-cache-layout")
                previous: dict[str, Any] | None = None
                try:
                    for scenario, messages in views(layout):
                        await model.complete(messages, [tool])
                        body = bodies.pop()
                        history_key = "input" if adapter is ResponsesModel else "messages"
                        current = encoded(body[history_key])
                        before = encoded(previous[history_key]) if previous is not None else b""
                        left = previous[history_key] if previous is not None else []
                        same_items = next(
                            (
                                n
                                for n, pair in enumerate(zip(left, body[history_key], strict=False))
                                if pair[0] != pair[1]
                            ),
                            min(len(left), len(body[history_key])),
                        )
                        results.append(
                            {
                                "adapter": adapter.__name__,
                                "layout": layout,
                                "scenario": scenario,
                                "rendered_history_bytes": len(current),
                                "shared_history_prefix_bytes": shared(before, current),
                                "identical_leading_history_items": same_items,
                                "wire_body_prefix_bytes": shared(encoded(previous), encoded(body))
                                if previous is not None
                                else 0,
                                "provider_cache_usage": None,
                            }
                        )
                        previous = body
                finally:
                    await model.close()
    output.write_text(
        json.dumps(
            {
                "scope": "Offline production-adapter serialization; loopback fixture only",
                "limitations": [
                    "Byte prefixes are not token prefixes or observed cache hits.",
                    "Cache-control annotations are included in the serialized comparison.",
                    "Unchanged snapshots apply only to unchanged content and identity.",
                    "Provider latency, cost, cache reads and writes were not measured.",
                ],
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(measure(args.output))


if __name__ == "__main__":
    main()
