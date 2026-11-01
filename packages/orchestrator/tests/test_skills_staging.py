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

import pytest

from sandbox_science.contracts import (
    ExecutionContext,
    StageCheck,
    StageCheckRequest,
    StagePutRequest,
    StagePutResult,
)
from sandbox_science.contracts.worker import BindingInventory
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills.derive import VERSION
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.orchestrator.skills.staging import Stager

from .loop_harness import PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

pytestmark = pytest.mark.slow

FILES = {
    "tool/SKILL.md": "---\nname: tool\ndescription: Runs the tool.\n---\nRun run.sh.\n",
    "tool/run.sh": '#!/bin/sh\necho "helper ran from $QZPACK_DIR"\n',
    "tool/start.py": "print('start-up code ran')\n",
    "plain/SKILL.md": "---\nname: plain\ndescription: Only words.\n---\nRead me.\n",
}
# What an older pass kept: one namespaced binding, one held, and start-up variables.
BINDINGS = {
    "QZPACK_DIR": "",
    "LIBFOO_PLUGIN_DIR": "tool",
    "PYTHONSTARTUP": "tool/start.py",
    "BASH_ENV": "tool/run.sh",
}
OPTIONS = ("--skills-metadata-model", "pass")
SHOW = 'echo "[${LIBFOO_PLUGIN_DIR-unset}][${PYTHONSTARTUP-unset}][${BASH_ENV-unset}]"'


def _pin(
    harness: Harness,
    source: str = "qzpack",
    bindings: dict[str, str] = BINDINGS,
    files: dict[str, str] = FILES,
) -> None:
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        packed = tuple(
            PackFile(path, text.encode(), executable=path.endswith(".sh"))
            for path, text in files.items()
        )
        digest = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()
        found, _ = store.add(
            source,
            snapshot(Fetched("archive", "a.tar", "", packed, (), archive_sha256=digest)),
            USER,
        )
        derived = {"topics": {}, "entry": [], "descriptions": {}, "bindings": bindings}
        store.add_metadata(found.snapshot, "pass", VERSION, "m", derived, [])
        store.pin(PROJECT, source, USER)
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
        Reply(calls=[call("skills", action="load", names=["qzpack/plain"])]),
        Reply(calls=[call("shell", script='echo "[${QZPACK_DIR-unset}]"')]),
        Reply(calls=[call("skills", action="load", names=["qzpack/tool"])]),
        Reply(calls=[call("shell", script=f'"$QZPACK_DIR/tool/run.sh"; {SHOW}')]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script, options=OPTIONS)
    plain, tool = _tools(one, "skills")
    assert "staging" not in plain  # only words: nothing to stage, no binding
    (staged,) = tool["staging"]
    assert "location" in staged, staged  # a failed staging names its worker error
    location = staged["location"]
    assert location.endswith(f"/.skills/qzpack/{staged['snapshot']}")
    assert (staged["state"], staged["attempt"], staged["files"]) == ("staged", 1, 4)
    assert staged["bindings"] == {"QZPACK_DIR": location}
    held = {item["variable"]: item["reason"] for item in staged["held"]}
    assert held["LIBFOO_PLUGIN_DIR"].startswith("not namespaced to the pack (QZPACK_)")
    assert held["PYTHONSTARTUP"].endswith("never set")
    assert _out(one) == ["[unset]\n", f"helper ran from {location}\n[unset][unset][unset]\n"]
    # Sandbox code changes the staged helper; the next run's load finds and restages it.
    tamper = f'chmod u+w "{location}/tool/run.sh"; echo "echo owned" > "{location}/tool/run.sh"'
    script = [
        Reply(calls=[call("shell", script=tamper)]),
        Reply(calls=[call("skills", action="load", names=["qzpack/tool"])]),
        Reply(calls=[call("shell", script='"$QZPACK_DIR/tool/run.sh"')]),
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
        Reply(calls=[call("skills", action="load", names=["qzpack/tool"])]),
        Reply(calls=[call("shell", script='echo "[${QZPACK_DIR-unset}]"')]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script)
    (staged,) = _tools(one, "skills")[0]["staging"]
    assert (staged["state"], staged["bindings"]) == ("staged", {})
    assert _out(one) == ["[unset]\n"]


async def test_a_pack_named_after_a_command_the_environment_has_gets_no_namespace(
    harness: Harness,
) -> None:
    """``sh`` is on every environment's PATH: a pack named ``sh`` would bind ``SH_*``,
    that command's family, so its namespace is refused and the binding held."""
    _pin(harness, "sh", {"SH_DIR": ""})
    script = [
        Reply(calls=[call("skills", action="load", names=["sh/tool"])]),
        Reply(calls=[call("shell", script='echo "[${SH_DIR-unset}]"')]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script, options=OPTIONS)
    (staged,) = _tools(one, "skills")[0]["staging"]
    assert staged["bindings"] == {}
    (held,) = staged["held"]
    assert "the namespace of the command sh's own variables" in held["reason"]
    assert _out(one) == ["[unset]\n"]


async def test_a_model_that_sends_bindings_is_refused(harness: Harness) -> None:
    _pin(harness)
    script = [
        Reply(calls=[call("shell", script="true", bindings={"QZPACK_DIR": "/x"})]),
        Reply(calls=[call("python", code="1", bindings={"QZPACK_DIR": "/x"})]),
        Reply("Done."),
    ]
    one = await harness.run("run-1", script, options=OPTIONS)
    refused = [cast(Any, entry) for entry in one.tools if "error" in entry]
    assert [entry["error"] for entry in refused] == ["invalid_request", "invalid_request"]
    assert not any("execution" in entry for entry in one.tools)


async def test_a_staging_that_fails_takes_the_packs_bindings_away(harness: Harness) -> None:
    """Bindings set by an earlier load name files that a failed restage left as they are
    not expected: the run's executions lose them (review of #493, low 5)."""
    _pin(harness)

    class Client:
        ready = True

        async def stage_check(self, request: StageCheckRequest) -> StageCheck:
            return StageCheck(
                location="/staged",
                ready=self.ready,
                sealed=self.ready,
                inventory=BindingInventory(state="ready", digest="ab" * 32, fingerprint="fixture"),
            )

        async def stage_put(self, request: StagePutRequest) -> StagePutResult:
            return StagePutResult(written=len(request.files))

    client = Client()
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        (entry,) = [e for e in store.effective(PROJECT).entries if e.name == "tool"]
        context = ExecutionContext(project_id=PROJECT, session_id="s", environment_id="e")
        stager = Stager(cast(Any, client), context, store, ("pass", VERSION))
        first = await stager.stage(entry)
        assert first is not None
        assert stager.bindings == {"QZPACK_DIR": "/staged"}
        client.ready = False
        failed = await stager.stage(entry)
        assert failed is not None
        assert failed["state"] == "failed"
        assert stager.bindings == {}
    finally:
        projects.close()


async def test_refreshing_one_pack_revalidates_every_loaded_sources_bindings(
    harness: Harness,
) -> None:
    _pin(harness)
    _pin(
        harness,
        "other",
        {"OTHER_DIR": ""},
        {
            "other/SKILL.md": "---\nname: other\ndescription: Another pack.\n---\nRun run.sh.\n",
            "other/run.sh": "#!/bin/sh\nprintf other\n",
        },
    )

    class Client:
        version = 1

        async def stage_check(self, request: StageCheckRequest) -> StageCheck:
            assert "QZPACK_DIR" in request.binding_names
            if self.version == 3:
                observed = BindingInventory()
            else:
                observed = BindingInventory(
                    state="ready",
                    digest=("ab" if self.version == 1 else "cd") * 32,
                    fingerprint=str(self.version),
                    reserved=() if self.version == 1 else ("QZPACK_DIR",),
                )
            return StageCheck(location=f"/staged/{request.source}", ready=True, inventory=observed)

    client = Client()
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        entries = {
            e.source: e for e in store.effective(PROJECT).entries if e.name in {"tool", "other"}
        }
        context = ExecutionContext(project_id=PROJECT, session_id="s", environment_id="e")
        stager = Stager(cast(Any, client), context, store, ("pass", VERSION))
        await stager.stage(entries["qzpack"])
        assert stager.bindings == {"QZPACK_DIR": "/staged/qzpack"}
        assert stager.inventory_digest == "ab" * 32
        client.version = 2
        await stager.stage(entries["other"])
        assert stager.bindings == {"OTHER_DIR": "/staged/other"}
        assert stager.inventory_digest == "cd" * 32
        client.version = 3
        await stager.stage(entries["other"])
        assert stager.bindings == {}
        assert stager.inventory_digest is None
    finally:
        projects.close()
