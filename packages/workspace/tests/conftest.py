# SPDX-License-Identifier: MIT
"""Fixtures: a harness state directory with projects, sessions and runs, and a web build.

Test modules cannot import this file (``--import-mode=importlib``); they repeat the few
constants they compare against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

POLICY = "sha256:" + "a" * 64
STARTED = datetime(2026, 10, 11, 13, 1, 2, 345678, tzinfo=UTC)


def run_record(session: str, run_id: str, started: datetime, answer: str) -> RunRecord:
    """A run with one tool call, as the harness records it."""
    execution = {
        "execution_id": "exe-1",
        "attempt_id": "att-1",
        "operation_id": f"{run_id}-t0-call-1-0123456789abcdef",
        "kind": "cell",
        "state": "succeeded",
        "result": {"exit_code": None, "stdout": {"name": "stdout", "text": "1\n"}},
    }
    return RunRecord(
        run_id=run_id,
        context=ExecutionContext(project_id="p1", session_id=session, environment_id="env-1"),
        task="What is $x^2$ at $x = 1$?",
        started_at=started,
        ended_at=started + timedelta(seconds=4),
        stopped="answered",
        final_text=answer,
        messages=[
            {"role": "system", "content": "You are a research assistant."},
            {"role": "user", "content": "What is $x^2$ at $x = 1$?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "python", "arguments": '{"code": "print(1**2)"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"state": "succeeded"}'},
            {"role": "assistant", "content": answer},
        ],
        tools=[
            {
                "tool": "python",
                "arguments": '{"code": "print(1**2)"}',
                "execution": execution,
                "replayed": False,
            }
        ],
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Project p1 with sessions a and b in its default environment (b closed), c in its
    own, and two runs of a; project p2 with none; a damaged record, broken."""
    root = tmp_path / "harness-state"
    store = ProjectStore(root)
    store.create("p1", "crypto-base", POLICY)
    for session, dedicated in (("a", False), ("b", False), ("c", True)):
        store.open_session("p1", session, dedicated=dedicated)
    store.close_session("p1", "b")
    store.save_run(run_record("a", "run-1", STARTED, "It is $1$."))
    store.save_run(run_record("a", "run-2", STARTED + timedelta(hours=1), "Still $1$."))
    store.create("p2", "minimal", POLICY)
    with store.control.write() as tx:  # a damaged record: its policy is no revision
        tx.sql.execute(
            "INSERT INTO projects VALUES ('broken', 'minimal', 'not-a-policy', "
            "'broken-default', '2026-10-11T13:00:00Z', '2026-10-11T13:00:00Z')"
        )
    store.close()
    return root


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    """A stand-in for the web app's build, beside a file that must stay out of reach."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><title>Workspace</title><script type="module" '
        'src="/assets/index-1a2b3c.js"></script>'
    )
    (dist / "assets" / "index-1a2b3c.js").write_text("console.log('workspace')\n")
    (dist / "assets" / "index-1a2b3c.css").write_text("body { margin: 0 }\n")
    (dist / "assets" / "KaTeX_Main-Regular-4d5e6f.woff2").write_bytes(b"wOF2 font")
    (dist / ".env").write_text("test-secret-in-a-dotfile\n")
    (dist / "tool.exe").write_bytes(b"MZ not served")
    (tmp_path / "secret.txt").write_text("test-secret-outside-the-build\n")
    (dist / "link.txt").symlink_to(tmp_path / "secret.txt")
    return dist
