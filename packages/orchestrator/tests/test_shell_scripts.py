# SPDX-License-Identifier: MIT
"""What the tools that run a bash script tell the model, and that the harness runs a
script as written (lane K's triage of the owner's prompt research, P5), through a real
worker's HTTP API."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from sandbox_science.orchestrator.jobs import JOB_SPECS, STOP_AT_FAILURE
from sandbox_science.orchestrator.tools import SPECS, Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ToolCall
from .test_artifact_publication import CONTEXT, TOKEN
from .test_artifact_publication import worker_url as worker_url

_CALLS = itertools.count(1)


@pytest_asyncio.fixture
async def toolbox(worker_url: str) -> AsyncIterator[Toolbox]:
    async with WorkerClient(worker_url, TOKEN) as client:
        yield Toolbox(client, CONTEXT)


async def shell(toolbox: Toolbox, script: str) -> dict[str, object]:
    number = next(_CALLS)
    call = ToolCall(id=f"call{number}", name="shell", arguments=json.dumps({"script": script}))
    return json.loads((await toolbox.call(call, f"op{number}", model="m")).text)


def test_the_script_tools_ask_a_script_of_several_steps_to_stop_at_a_failure() -> None:
    (shell_spec,) = [spec for spec in SPECS if spec.name == "shell"]
    (job_spec,) = [spec for spec in JOB_SPECS if spec.name == "start_job"]
    for spec in (shell_spec, job_spec):
        assert STOP_AT_FAILURE in spec.description
    assert "set -euo pipefail" in STOP_AT_FAILURE
    assert "check that they exist" in STOP_AT_FAILURE


@pytest.mark.asyncio
async def test_a_script_runs_as_written(toolbox: Toolbox) -> None:
    """Nothing is prepended: a failed step a later one hides is the script's own choice,
    and a single command keeps the exit code it has on its own."""
    hidden = await shell(toolbox, "false; echo after")
    assert (hidden["exit_code"], hidden["stdout"]) == (0, "after\n")
    piped = await shell(toolbox, "yes | head -n 2")
    assert (piped["exit_code"], piped["stdout"]) == (0, "y\ny\n")
    unset = await shell(toolbox, 'echo "[$NEVER_SET_ANYWHERE]"')
    assert (unset["exit_code"], unset["stdout"]) == (0, "[]\n")
    stopped = await shell(toolbox, "set -euo pipefail; false; echo after")
    assert stopped["exit_code"] == 1
    assert "stdout" not in stopped
