# SPDX-License-Identifier: MIT
"""Helper staging, end to end with a local worker (docs/specs/skills.md, SK-16, SK-21,
SK-31; SK-F5): a loaded skill's pack is staged read-only in the session's environment,
its namespaced bindings reach the run's executions, and those held never do; sandbox
code that changes a staged file finds it staged again at the next load.

The hostile pack here is written for the test, under the MIT License.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills.derive import VERSION
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile

from .loop_harness import PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

FILES = {
    "tool/SKILL.md": "---\nname: tool\ndescription: Runs the tool.\n---\nRun run.sh.\n",
    "tool/run.sh": '#!/bin/sh\necho "helper ran from $SS_DIR"\n',
    "tool/start.py": "print('start-up code ran')\n",
    "plain/SKILL.md": "---\nname: plain\ndescription: Only words.\n---\nRead me.\n",
}
# What an older pass kept: one namespaced binding, one held, and start-up variables.
BINDINGS = {
    "SS_DIR": "",
    "LIBFOO_PLUGIN_DIR": "tool",
    "PYTHONSTARTUP": "tool/start.py",
    "BASH_ENV": "tool/run.sh",
}
OPTIONS = ("--skills-metadata-model", "pass")
SHOW = 'echo "[${LIBFOO_PLUGIN_DIR-unset}][${PYTHONSTARTUP-unset}][${BASH_ENV-unset}]"'


def _pin(harness: Harness) -> None:
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        packed = tuple(
            PackFile(path, text.encode(), executable=path.endswith(".sh"))
            for path, text in FILES.items()
        )
        digest = hashlib.sha256(repr(sorted(FILES.items())).encode()).hexdigest()
        found, _ = store.add(
            "ss", snapshot(Fetched("archive", "a.tar", "", packed, (), archive_sha256=digest)), USER
        )
        derived = {"topics": {}, "entry": [], "descriptions": {}, "bindings": BINDINGS}
        store.add_metadata(found.snapshot, "pass", VERSION, "m", derived, [])
        store.pin(PROJECT, "ss", USER)
    finally:
        projects.close()


def _tools(record: RunRecord, name: str) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], entry[name]) for entry in record.tools if name in entry]


def _out(record: RunRecord) -> list[str]:
    results = [
        cast(Any, entry)["execution"]["result"] for entry in record.tools if "execution" in entry
    ]
    return [result["stdout"]["text"] for result in results if result.get("stdout")]


async def test_a_loaded_helper_is_staged_bound_and_staged_again_when_changed(
    harness: Harness,
) -> None:
    _pin(harness)
    script = [
        Reply(calls=[call("skills", action="load", names=["ss/plain"])]),
        Reply(calls=[call("shell", script='echo "[${SS_DIR-unset}]"')]),
        Reply(calls=[call("skills", action="load", names=["ss/tool"])]),
        Reply(calls=[call("shell", script=f'"$SS_DIR/tool/run.sh"; {SHOW}')]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script, options=OPTIONS)
    plain, tool = _tools(one, "skills")
    assert "staging" not in plain  # only words: nothing to stage, no binding
    (staged,) = tool["staging"]
    location = staged["location"]
    assert location.endswith(f"/.skills/ss/{staged['snapshot']}")
    assert (staged["state"], staged["attempt"], staged["files"]) == ("staged", 1, 4)
    assert staged["bindings"] == {"SS_DIR": location}
    held = {item["variable"]: item["reason"] for item in staged["held"]}
    assert held["LIBFOO_PLUGIN_DIR"].startswith("not namespaced to the pack (SS_)")
    assert held["PYTHONSTARTUP"].endswith("never set")
    assert _out(one) == ["[unset]\n", f"helper ran from {location}\n[unset][unset][unset]\n"]
    # Sandbox code changes the staged helper; the next run's load finds and restages it.
    tamper = f'chmod u+w "{location}/tool/run.sh"; echo "echo owned" > "{location}/tool/run.sh"'
    script = [
        Reply(calls=[call("shell", script=tamper)]),
        Reply(calls=[call("skills", action="load", names=["ss/tool"])]),
        Reply(calls=[call("shell", script='"$SS_DIR/tool/run.sh"')]),
        Reply("Done."),
    ]
    two = await harness.run("run-2", script, options=OPTIONS)
    (again,) = _tools(two, "skills")[0]["staging"]
    assert (again["state"], again["attempt"], again["differing"]) == (
        "restaged",
        1,
        ["tool/run.sh"],
    )
    assert _out(two)[-1] == f"helper ran from {location}\n"


async def test_without_the_pass_alias_nothing_is_bound(harness: Harness) -> None:
    """Bindings come only from the pass the run service is given: without it, the pack
    is staged and no variable is set."""
    _pin(harness)
    script = [
        Reply(calls=[call("skills", action="load", names=["ss/tool"])]),
        Reply(calls=[call("shell", script='echo "[${SS_DIR-unset}]"')]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script)
    (staged,) = _tools(one, "skills")[0]["staging"]
    assert (staged["state"], staged["bindings"]) == ("staged", {})
    assert _out(one) == ["[unset]\n"]
