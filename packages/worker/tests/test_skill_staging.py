# SPDX-License-Identifier: MIT
"""Skill packs staged in an environment, and bindings in its executions
(docs/specs/skills.md, SK-16): the harness writes a pack's files below the environment's
root in batches, a check seals them read-only once each file's SHA-256 is as expected,
and a later check finds what sandbox code changed, added or swapped for a symlink.
Bindings reach the executions that carry them and no other.

The pack here is written for the test, under the MIT License.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from .harness import CONTEXT, Harness, HostileBackend

pytestmark = pytest.mark.asyncio

SNAPSHOT = "ab" * 32
FILES = {
    "tool/SKILL.md": (b"---\nname: tool\ndescription: Runs.\n---\nRun run.sh.\n", False),
    "tool/run.sh": (b'#!/bin/sh\necho "helper ran from $SS_DIR"\n', True),
}


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as started:
        yield started


def _manifest() -> list[dict[str, Any]]:
    return [
        {"path": p, "sha256": hashlib.sha256(c).hexdigest(), "size": len(c), "executable": x}
        for p, (c, x) in FILES.items()
    ]


async def _check(worker: Harness, *, seal: bool = False) -> dict[str, Any]:
    body = {"context": CONTEXT, "source": "ss", "snapshot": SNAPSHOT, "files": _manifest()}
    status, found = await worker.post("/v1/skills/check", {**body, "seal": seal})
    assert status == 200, found
    return found


async def _stage(worker: Harness) -> dict[str, Any]:
    contents = [
        {"path": p, "content_b64": base64.b64encode(c).decode(), "executable": x}
        for p, (c, x) in FILES.items()
    ]
    for number, batch in enumerate((contents[:1], contents[1:])):  # two batches
        body = {"context": CONTEXT, "source": "ss", "snapshot": SNAPSHOT, "files": batch}
        status, put = await worker.post("/v1/skills/put", {**body, "reset": number == 0})
        assert (status, put) == (200, {"written": 1})
    return await _check(worker, seal=True)


async def _shell(worker: Harness, operation: str, script: str, **extra: Any) -> str:
    done = await worker.run(operation, kind="shell", script=script, **extra)
    return done["result"]["stdout"]["text"] if done["result"].get("stdout") else ""


async def test_a_pack_is_staged_sealed_and_bound_only_where_it_is_sent(worker: Harness) -> None:
    before = await _check(worker)
    assert (before["ready"], before["differing"]) == (False, sorted(FILES))
    staged = await _stage(worker)
    assert (staged["ready"], staged["sealed"], staged["differing"]) == (True, True, [])
    location = Path(staged["location"])
    assert location.parts[-3:] == (".skills", "ss", SNAPSHOT)
    script = location / "tool" / "run.sh"
    assert stat.S_IMODE(script.stat().st_mode) == 0o555
    assert stat.S_IMODE((location / "tool").stat().st_mode) == 0o555
    bound = {"bindings": {"SS_DIR": str(location)}}
    ran = await _shell(worker, "op-1", '"$SS_DIR/tool/run.sh"', **bound)
    assert ran == f"helper ran from {location}\n"
    assert await _shell(worker, "op-2", 'echo "[${SS_DIR-unset}]"') == "[unset]\n"
    assert (await _check(worker))["ready"]  # unchanged: nothing to stage again


async def test_what_sandbox_code_changes_is_found_and_staged_again(worker: Harness) -> None:
    location = Path((await _stage(worker))["location"])
    await _shell(
        worker,
        "op-t",
        f'chmod u+w "{location}/tool" "{location}/tool/run.sh"; '
        f'echo "echo owned" > "{location}/tool/run.sh"; touch "{location}/tool/extra.py"',
    )
    changed = await _check(worker, seal=True)
    assert (changed["ready"], changed["sealed"]) == (False, False)
    assert changed["differing"] == ["tool/extra.py", "tool/run.sh"]
    assert (await _stage(worker))["ready"]
    assert not (location / "tool" / "extra.py").exists()
    # A symlink swapped in for the staged folder is removed, never written through.
    elsewhere = worker.workspace / "kept"
    swap = f'chmod -R u+w "{location}" && rm -rf "{location}" && ln -s "$PWD/kept" "{location}"'
    await _shell(worker, "op-s", f"mkdir -p kept && {swap}")
    assert (await _check(worker))["ready"] is False
    assert (await _stage(worker))["ready"]
    assert location.is_dir()
    assert not location.is_symlink()
    assert list(elsewhere.iterdir()) == []


async def test_bindings_and_staged_paths_are_checked(worker: Harness) -> None:
    for bindings in (
        {"lower": "x"}, {"1X": "x"}, {"SS_DIR": "a\x00b"}, {"SS_DIR": ""}, {"SS_DIR": "a;b"},
        {"SS_DIR": "a b"}, {"SS_DIR": "a\tb"},
    ):  # fmt: skip
        status, _ = await worker.post(
            "/v1/executions",
            {"operation_id": "op-b", "context": CONTEXT, "kind": "shell", "script": "true",
             "bindings": bindings},
        )  # fmt: skip
        assert status in (400, 422)
    for path in ("../out", "/etc/passwd", "a//b"):
        body = {"context": CONTEXT, "source": "ss", "snapshot": SNAPSHOT, "reset": True}
        status, _ = await worker.post(
            "/v1/skills/put", {**body, "files": [{"path": path, "content_b64": ""}]}
        )
        assert status in (400, 422)
    status, _ = await worker.post(
        "/v1/skills/check",
        {"context": CONTEXT, "source": "../x", "snapshot": SNAPSHOT, "files": []},
    )
    assert status in (400, 422)
    assert not os.path.exists(worker.root / "out")


@pytest.mark.slow
async def test_a_kernel_takes_the_whole_set_when_it_starts_and_says_when_it_holds_others(
    worker: Harness,
) -> None:
    """A cell's bindings are the whole set a kernel's next start takes: a running kernel
    keeps those it started with, and the result says so; a restart takes the latest set,
    so bindings taken away leave the kernel too (review of #493, finding 3)."""
    read = "import os; os.environ.get('SS_DIR')"
    restart = {"context": CONTEXT, "language": "python"}
    first = await worker.run("op-c1", kind="cell", code=read)
    assert first["result"]["outputs"] == []
    bound = {"bindings": {"SS_DIR": "/staged"}}
    kept = await worker.run("op-c2", kind="cell", code=read, **bound)
    assert kept["result"]["outputs"] == []
    assert any("restart it to have the run's" in n for n in kept["result"]["notices"])
    assert (await worker.post("/v1/kernels/restart", restart))[0] == 200
    after = await worker.run("op-c3", kind="cell", code=read, **bound)
    assert after["result"]["outputs"][0]["data"]["text/plain"] == "'/staged'"
    assert not after["result"]["notices"]
    # Taken away: the running kernel still has it, and says so; restarted, it has not.
    revoked = await worker.run("op-c4", kind="cell", code=read)
    assert revoked["result"]["outputs"][0]["data"]["text/plain"] == "'/staged'"
    assert any("restart it to have the run's" in n for n in revoked["result"]["notices"])
    assert (await worker.post("/v1/kernels/restart", restart))[0] == 200
    gone = await worker.run("op-c5", kind="cell", code=read)
    assert gone["result"]["outputs"] == []


async def test_a_binding_never_replaces_the_environments_own_variables(worker: Harness) -> None:
    """A binding of ``SESSION_WORKSPACE`` would move the session's files out of sight of
    its written files (review of #493, finding 2): the executor refuses it, and so any
    name the environment sets."""
    for name in ("SESSION_WORKSPACE", "PROJECT_SHARED", "HOME", "PATH"):
        done = await worker.run(
            f"op-{name.lower().replace('_', '-')}",
            kind="shell",
            script="echo ran",
            bindings={name: "/elsewhere"},
        )
        assert done["state"] == "failed", done
        assert "the environment's own variable" in done["result"]["error"]["message"]


async def test_an_executor_from_before_bindings_is_refused_them(tmp_path: Path) -> None:
    """An executor of protocol 1 would run the execution without its bindings, saying
    nothing: the worker refuses to send them (review of #493, finding 10)."""
    async with await Harness.start(tmp_path, backend=HostileBackend("plain")) as old:
        status, body = await old.post(
            "/v1/executions",
            {"operation_id": "op-old", "context": CONTEXT, "kind": "shell", "script": "true",
             "bindings": {"SS_DIR": "/staged"}},
        )  # fmt: skip
        if status == 201:
            body = await old.finish(body["execution_id"])
            body = body["result"]["error"]
        assert "predates skill bindings" in json.dumps(body)


async def test_what_sandbox_code_puts_in_the_staged_tree_never_stalls_or_redirects_it(
    worker: Harness,
) -> None:
    """A FIFO is named, never read; a symlink at ``.skills`` is removed before a put."""
    location = Path((await _stage(worker))["location"])
    await _shell(worker, "op-f", f'chmod u+w "{location}/tool" && mkfifo "{location}/tool/pipe"')
    changed = await _check(worker)
    assert changed["differing"] == ["tool/pipe"]
    skills = location.parents[1]
    await _shell(
        worker, "op-l", f'chmod -R u+w "{skills}" && rm -rf "{skills}" && mkdir -p kept && '
        f'ln -s "$PWD/kept" "{skills}"',
    )  # fmt: skip
    assert (await _stage(worker))["ready"]
    assert not skills.is_symlink()
    assert list((worker.workspace / "kept").iterdir()) == []
