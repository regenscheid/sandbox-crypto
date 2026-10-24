# SPDX-License-Identifier: MIT
"""``sandbox-science-harness classify-draft``: a draft classification file for an MCP server's
tools, proposed by a model through the gateway (D96, docs/MCP-CONNECTORS.md).

It reads a tool list (a ``tools/list`` result or a pinned listing, as JSON), asks the model
once, reads its answer as data (``sandbox_science.policy.classify_draft``) and writes the
draft. The admin reviews it; only the accepted, checked-in file decides calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sandbox_science.policy.classify_draft import (
    draft_messages,
    read_draft,
    render_draft,
    tool_list,
)

from .model import ChatModel, ModelError, gateway_model

ANSWER_TOKENS = 16_000


def add_parser(commands: Any) -> None:
    command = commands.add_parser(
        "classify-draft",
        help="draft a classification file for an MCP server's tools, for the admin to review",
    )
    command.add_argument("server", help="the server's name in the policy")
    command.add_argument(
        "--tools", type=Path, required=True, help="its tool list: a tools/list result, as JSON"
    )
    command.add_argument("--prefix", default="", help="the start every tool name shares")
    command.add_argument("--model-url", required=True, help="the gateway's base URL")
    command.add_argument("--model", default="main", help="the model alias to ask")
    command.add_argument("--api-key-file", type=Path, required=True)
    command.add_argument(
        "--api", default="chat", help="the gateway API: chat, responses or messages"
    )
    command.add_argument("--out", type=Path, help="where to write the draft (default: stdout)")


async def draft(model: ChatModel, server: str, prefix: str, tools: list[dict[str, Any]]) -> str:
    """One model call's draft for ``tools``, checked and rendered as a classification file."""
    turn = await model.complete(draft_messages(server, tools), [])
    return render_draft(server, prefix, tools, read_draft(turn.text or "", tools))


def classify_draft(args: argparse.Namespace) -> int:
    try:
        tools = tool_list(json.loads(args.tools.read_text()))
    except (OSError, ValueError) as exc:
        print(f"error: {args.tools}: {exc}", file=sys.stderr)
        return 1
    key = args.api_key_file.read_text().strip()

    async def ask() -> str:
        model = gateway_model(
            args.model_url, args.model, key, api=args.api, max_tokens=ANSWER_TOKENS
        )
        try:
            return await draft(model, args.server, args.prefix, tools)
        finally:
            await model.close()

    try:
        text = asyncio.run(ask())
    except (ModelError, TimeoutError) as exc:
        print(f"error: the model call failed: {exc}", file=sys.stderr)
        return 1
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text)
        print(f"wrote {args.out}: review every entry before checking it in", file=sys.stderr)
    return 0
