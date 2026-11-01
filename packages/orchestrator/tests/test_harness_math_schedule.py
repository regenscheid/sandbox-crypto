# SPDX-License-Identifier: MIT
"""A revision the agent saves through ``run`` is scheduled for a math check: making the
harness's ``Stores`` subscribes the math checks (B-05)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import DocumentStore, store_path

from .scripted import script as scripted
from .test_harness import TOKEN, worker_url  # noqa: F401  (fixture)


async def test_an_agent_saved_revision_is_scheduled_for_a_math_check(
    worker_url: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    state, token = tmp_path / "harness-state", tmp_path / "token"
    token.write_text(TOKEN)
    created = await asyncio.to_thread(
        cli.main,
        ["project", "create", "--state-dir", str(state), "--project", "p1",
         "--profile", "minimal", "--policy", "sha256:" + "d" * 64],
    )  # fmt: skip
    assert created == 0
    script, transcript = tmp_path / "script.json", tmp_path / "t.json"
    turns: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "name": "document_create",
                    "arguments": {
                        "name": "ff",
                        "title": "Fermat",
                        "scope": "project",
                        "text": "Let $p = 17$.",
                    },
                }
            ]
        },
        {"text": "done"},
    ]
    script.write_text(json.dumps(scripted(turns)))
    status = await asyncio.to_thread(
        cli.main,
        ["run", "--worker", worker_url, "--token-file", str(token), "--project", "p1",
         "--session", "a", "--state-dir", str(state), "--allow-unisolated",
         "--script", str(script), "--transcript", str(transcript), "--run-id", "run-a",
         "task"],
    )  # fmt: skip
    assert status == 0
    store = DocumentStore(store_path(state), readonly=True)
    try:
        document = store.document("p1", "ff").document_id
        with store.control.read() as tx:
            row = tx.sql.execute(
                "SELECT target FROM math_sources WHERE document=?", (document,)
            ).fetchone()
    finally:
        store.close()
    assert row == (1,)
