# SPDX-License-Identifier: MIT
"""A delegated run's write area end to end (delegated agents spec, DA-19, DA-F4's
writes): through the CLI, the harness and a real worker, a child writes and edits in
``work/<delegation_id>/`` and in ``shared/``, is refused elsewhere with
``outside_write_area`` naming where it may write, and its parent reads what it wrote.
The main run is unaffected. A child in an environment of its own is tested with the
child environments, in ``test_delegation_environments``."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from sandbox_science.orchestrator.model import Message

from .loop_harness import AREA, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, results, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE, TASK

pytestmark = pytest.mark.slow


def written(messages: list[Message]) -> str:
    """The write area of the first delegation the parent started."""
    for message in messages:
        found = re.search(r"dlg-[0-9a-f]{24}", str(message["content"]))
        if message["role"] == "tool" and found:
            return f"work/{found[0]}"
    raise AssertionError("no delegation started")


def errors(record: Any) -> list[dict[str, Any]]:
    return [
        found["error"]
        for text in results(record)
        if isinstance(found := json.loads(text), dict) and "error" in found
    ]


async def test_a_child_writes_only_in_its_write_area_and_shared(delegating: Harness) -> None:
    """DA-F4's writes: the child writes work/<id>/notes.md and then sources/b.md; the
    first succeeds, the second is refused with outside_write_area naming the area. An
    edit is held as a write is; shared/ keeps its guard; the parent reads the area."""
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("write_file", path="sources/a.md", content="parent's\n")]),
            Reply(calls=[TASK]),
            wait(),
            Reply(calls_from=lambda m: [call("read_file", path=f"{written(m)}/notes.md")]),
            Reply("Done."),
        ],
        delegated=[
            Reply(calls=[call("write_file", path=f"{AREA}/notes.md", content="draft\n")]),
            Reply(calls=[call("write_file", path="sources/b.md", content="child's\n")]),
            Reply(
                calls=[call("edit_file", path="sources/a.md", old_string="parent", new_string="x")]
            ),
            Reply(
                calls=[
                    call("edit_file", path=f"{AREA}/notes.md", old_string="draft", new_string="17")
                ]
            ),
            Reply(
                calls=[
                    call("write_file", path="shared/out.md", content="1\n"),
                    call("write_file", path="shared/out.md", content="1\n", create_only=True),
                ]
            ),
            Reply(json.dumps({**DONE, "summary": "Wrote my notes."})),
        ],
    )
    (delegation_id,) = started(root)
    area = f"work/{delegation_id}"
    began = json.loads(results(root)[1])
    assert began["write_area"] == f"{area}/"
    (child,) = children(delegating, root)
    brief = str(delegating.gateway.children[0][1]["content"])
    assert f"Your write area: {area}/." in brief
    refused = errors(child)
    assert [e["kind"] for e in refused] == ["policy_denied", "policy_denied", "invalid_request"]
    for error, path in zip(refused, ("sources/b.md", "sources/a.md"), strict=False):
        assert error["message"].startswith(f"outside_write_area: {path!r}"), error
        assert f"only in {area}/ and in shared/" in error["message"]
    assert "create_only" in refused[2]["message"]
    workspace = delegating.tmp / "worker" / "environments"
    (session_root,) = workspace.glob(f"*/project/sessions/{SESSION}")
    assert (session_root / area / "notes.md").read_text() == "17\n"
    assert not (session_root / "sources" / "b.md").exists()
    assert (session_root / "sources" / "a.md").read_text() == "parent's\n"
    # The parent reads the child's write area.
    read = json.loads(results(root)[-1])
    assert (read["path"], read["content"]) == (f"{area}/notes.md", "17\n")


async def test_a_main_run_writes_anywhere(delegating: Harness) -> None:
    """The write area binds delegated runs only, their file tools and their code."""
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("write_file", path="sources/a.md", content="x\n")]),
            Reply(calls=[call("edit_file", path="sources/a.md", old_string="x", new_string="y")]),
            Reply(calls=[call("shell", script="echo z >> sources/a.md && cat sources/a.md")]),
            Reply("Done."),
        ],
    )
    assert errors(root) == []
    shell = json.loads(results(root)[-1])
    assert (shell["state"], shell["stdout"]) == ("succeeded", "y\nz\n")


CHILD_SHELL = (
    'pwd; echo "$RUN_WORK"; echo kept > notes.md; '
    '(echo child > "$SESSION_WORKSPACE/sources/a.md") 2>/dev/null || echo refused'
)
CHILD_CELL = (
    "import os\n"
    "open('cell.md', 'w').write('kept')\n"
    "try:\n"
    "    open(os.environ['SESSION_WORKSPACE'] + '/sources/a.md', 'w')\n"
    "except PermissionError:\n"
    "    print('refused', os.getcwd() == os.environ['RUN_WORK'])\n"
)


async def test_a_childs_code_writes_only_in_its_write_area(delegating: Harness) -> None:
    """DA-F9 end to end: the child's shell and Python cell start in its write area and
    write there; the parent's file is refused to both, and stays the parent's. The brief
    says so."""
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("write_file", path="sources/a.md", content="parent's\n")]),
            Reply(calls=[TASK]),
            wait(),
            Reply("Done."),
        ],
        delegated=[
            Reply(calls=[call("shell", script=CHILD_SHELL)]),
            Reply(calls=[call("python", code=CHILD_CELL)]),
            Reply(json.dumps({**DONE, "summary": "Wrote my notes."})),
        ],
    )
    (delegation_id,) = started(root)
    area = f"work/{delegation_id}"
    (child,) = children(delegating, root)
    brief = str(delegating.gateway.children[0][1]["content"])
    assert "start in your write area ($RUN_WORK) and can write only there" in brief
    workspace = delegating.tmp / "worker" / "environments"
    (session_root,) = workspace.glob(f"*/project/sessions/{SESSION}")
    shell, cell = (json.loads(text) for text in results(child)[:2])
    assert shell["state"] == "succeeded", shell
    assert shell["stdout"].split() == [str(session_root / area)] * 2 + ["refused"]
    assert cell["state"] == "succeeded", cell
    assert cell["stdout"] == "refused True\n"
    assert (session_root / area / "notes.md").read_text() == "kept\n"
    assert (session_root / area / "cell.md").read_text() == "kept"
    assert (session_root / "sources" / "a.md").read_text() == "parent's\n"
