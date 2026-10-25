# SPDX-License-Identifier: MIT
"""``sandbox-science-harness classify-draft``: a draft classification file for an MCP server's
tools, proposed by a model through the gateway (D96, docs/MCP-CONNECTORS.md).

It reads a tool list (a ``tools/list`` result or a pinned listing, as JSON), asks the model
once, reads its answer as data (``sandbox_science.policy.classify_draft``) and writes the
draft: to a file (``--out``), to standard output, or, with ``--project`` and
``--policy-store``, into the policy store as a draft of that project's, which the
workspace's review view lists beside the classification in force (CL-2). A draft decides
nothing: only a classification the user accepts, or the checked-in file, does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox_science.policy.classify_draft import (
    draft_messages,
    read_draft,
    render_draft,
    tool_list,
)
from sandbox_science.policy.store import PolicyStore, StoreError

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
    command.add_argument(
        "--project", help="store the draft as this project's, for the workspace's review view"
    )
    command.add_argument(
        "--policy-store",
        type=Path,
        help="the policy store (the policy middleware's state directory), with --project",
    )


async def draft(model: ChatModel, server: str, prefix: str, tools: list[dict[str, Any]]) -> str:
    """One model call's draft for ``tools``, checked and rendered as a classification file."""
    turn = await model.complete(draft_messages(server, tools), [])
    return render_draft(server, prefix, tools, read_draft(turn.text or "", tools))


def classify_draft(args: argparse.Namespace) -> int:
    if (args.project is None) != (args.policy_store is None):
        print("error: --project and --policy-store go together", file=sys.stderr)
        return 1
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
    if args.project is not None:
        try:
            stored = PolicyStore(args.policy_store).add_classification_draft(
                args.project,
                args.server,
                text.encode(),
                created_at=datetime.now(UTC),
                model=args.model,
            )
        except StoreError as exc:
            print(f"error: the draft was not stored: {exc}", file=sys.stderr)
            return 1
        print(
            f"stored draft {stored.draft} of {args.server} for project {args.project}: "
            "review it in the workspace before accepting it",
            file=sys.stderr,
        )
    if args.out is not None:
        args.out.write_text(text)
        print(f"wrote {args.out}: review every entry before checking it in", file=sys.stderr)
    elif args.project is None:
        sys.stdout.write(text)
    return 0
