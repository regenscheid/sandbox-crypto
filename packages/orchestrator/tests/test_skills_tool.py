# SPDX-License-Identifier: MIT
"""The ``skills`` tool's loads and reads, and the settings' bounds (docs/specs/skills.md,
SK-10 to SK-14, SK-21, SK-38, SK-39): a bad name costs only itself, a body or file is never
cut by a notice and is served with its hidden characters shown, a read stays inside the
run's skills and under the cap, and the settings are refused as the store would refuse
them.

The skills are written here, under the MIT License, as an archive's files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts.context import ExecutionContext
from sandbox_science.orchestrator import loop
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.hooks import Notice
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills import tool as skills_tool
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.orchestrator.skills.tool import MAX_READ, SkillTool
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient


def _skill(name: str, body: str) -> PackFile:
    text = f"---\nname: {name}\ndescription: The {name} skill.\n---\n{body}"
    return PackFile(f"{name}/SKILL.md", text.encode())


@pytest.fixture
def projects(tmp_path: Path) -> ProjectStore:
    found = ProjectStore(tmp_path / "state")
    found.create("p", "minimal", "sha256:" + "a" * 64)
    return found


def _store(projects: ProjectStore, *files: PackFile) -> SkillStore:
    store = SkillStore(projects.control)
    digest = hashlib.sha256(repr(files).encode()).hexdigest()
    store.add(
        "ss", snapshot(Fetched("archive", "a.tar", "", files, (), archive_sha256=digest)), USER
    )
    store.pin("p", "ss", USER)
    return store


def _tool(store: SkillStore) -> SkillTool:
    return SkillTool(store, store.effective("p"), role="main", alias="a")


async def _call(tool: SkillTool, **args: object) -> tuple[str, dict[str, Any]]:
    text, record = await tool.call(dict(args), call_id="c", model="m")
    return text, cast(dict[str, Any], record["skills"])


async def test_a_name_that_names_no_skill_costs_only_itself(projects: ProjectStore) -> None:
    tool = _tool(_store(projects, _skill("alpha", "ALPHA-BODY\n")))
    text, record = await _call(tool, action="load", names=["alpha", "nope"])
    assert "ALPHA-BODY" in text
    assert "'nope' is not loaded: no skill 'nope'" in text
    assert [(load.get("id"), load.get("name"), load["status"]) for load in record["loads"]] == [
        ("ss/alpha", None, "loaded"),
        (None, "nope", "not_found"),
    ]
    text, record = await _call(tool, action="load", names=["alpha"])
    assert record["loads"][0]["status"] == "already_loaded"
    # A single name that names no skill is refused, as before.
    text, record = await _call(tool, action="load", names=["nope"])
    assert json.loads(text)["error"]["kind"] == "not_found"


async def test_a_notice_never_cuts_a_loaded_body(projects: ProjectStore) -> None:
    """A tool-point notice rides on the next result that may be cut; never a skill's."""
    body = "HEAD\n" + "step. " * 2500 + "\nMIDDLE-MARKER\n" + "more. " * 2500 + "\nTAIL\n"
    tool = _tool(_store(projects, _skill("alpha", body)))
    toolbox = Toolbox(
        cast(WorkerClient, None),
        ExecutionContext(project_id="p", session_id="s", environment_id="e"),
        skills=tool,
    )
    arguments = json.dumps({"action": "load", "names": ["alpha"]})
    outcome = await toolbox.call(ToolCall("c1", "skills", arguments), "op-1", model="m")

    class Jobs:
        name = "jobs"

        async def notices(self, point: str, run: object) -> list[Notice]:
            return [Notice("jobs", ("job-1",), "job_notices", [{"job_id": "job-1"}])]

    transcript = loop.Transcript(messages=[])
    await loop._attach([cast(Any, Jobs())], transcript, outcome, 20000)
    assert "MIDDLE-MARKER" in outcome.text
    assert "TAIL" in outcome.text
    assert "job_notices" not in outcome.text  # it waits for a later safe point


async def test_a_read_is_text_or_binary_by_the_whole_file(projects: ProjectStore) -> None:
    """A cut that splits a character still shows text, cut before the character."""
    content = ("a" * (MAX_READ - 1) + "é" + "rest\n").encode()
    store = _store(
        projects,
        _skill("alpha", "body\n"),
        PackFile("alpha/reference/notes.md", content),
        PackFile("alpha/reference/blob.bin", b"\xff\xfe" + b"x" * 10),
    )
    text, record = await _call(
        _tool(store), action="read", skill="alpha", path="reference/notes.md"
    )
    assert "a" * 100 in text
    assert "é" not in text
    assert f"[first {MAX_READ - 1:,} of {len(content):,} bytes]" in text
    assert record["sha256"] == hashlib.sha256(content[: MAX_READ - 1]).hexdigest()
    text, record = await _call(
        _tool(store), action="read", skill="alpha", path="reference/blob.bin"
    )
    assert json.loads(text)["binary"] is True
    assert record["binary"] is True


async def test_a_read_stays_in_the_runs_skills_and_under_the_cap(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = "y" * 60 + "\n"
    store = _store(
        projects,
        _skill("alpha", "A" + big),
        _skill("beta", "B" + big),
        _skill("secret", "EXCLUDED-SECRET-BODY\n"),
        PackFile("shared/table.md", b"a shared resource\n"),
    )
    store.set_settings("p", USER, 0, always=(), excluded=("ss/secret",))
    tool = _tool(store)
    text, record = await _call(tool, action="read", skill="alpha", path="../secret/SKILL.md")
    assert "EXCLUDED-SECRET-BODY" not in text
    assert record["status"] == "not_in_set"
    text, record = await _call(tool, action="read", skill="alpha", path="../shared/table.md")
    assert "a shared resource" in text
    # A skill's own SKILL.md, read, counts toward the cap as a load does.
    monkeypatch.setattr(skills_tool, "MAX_LOADED", 100)
    _, record = await _call(tool, action="load", names=["alpha"])
    assert record["loads"][0]["status"] == "loaded"
    text, record = await _call(tool, action="read", skill="alpha", path="../beta/SKILL.md")
    assert record["status"] == "over_budget"
    assert "B" + "y" * 50 not in text


async def test_hidden_characters_are_shown_wherever_pack_text_is_served(
    projects: ProjectStore,
) -> None:
    """SK-14: a body or file's hidden characters reach the model as code points, and the
    import's report counts them."""
    hidden = "Do the analysis.‮\U000e0049\U000e0067 hidden\n"
    store = _store(
        projects,
        _skill("alpha", hidden),
        PackFile("alpha/notes.md", "note​\n".encode()),
    )
    (imported,) = store.imports("ss")
    assert imported.report["hidden_characters"] == [
        {"path": "alpha/SKILL.md", "characters": 3},
        {"path": "alpha/notes.md", "characters": 1},
    ]
    store.set_settings("p", USER, 0, always=("ss/alpha",), excluded=())
    tool = _tool(store)
    section = tool.given.section
    assert section is not None
    for text in (
        section.text,
        (await _call(tool, action="read", skill="alpha", path="SKILL.md"))[0],
        (await _call(tool, action="read", skill="alpha", path="notes.md"))[0],
    ):
        assert "‮" not in text
        assert not any(0xE0000 <= ord(char) <= 0xE007F or char == "​" for char in text)
    assert "⟦U+202E⟧⟦U+E0049⟧⟦U+E0067⟧" in section.text


def test_settings_are_refused_as_the_store_would_refuse_them(projects: ProjectStore) -> None:
    store = _store(projects, _skill("alpha", "a" * 60_000 + "\n"), _skill("beta", "b" * 50_000))
    with pytest.raises(ControlError, match="come to 110,001 characters, past the run's 100,000"):
        store.set_settings("p", USER, 0, always=("ss/alpha", "ss/beta"), excluded=())
    # Excluded, beta is not loaded, and alpha alone fits.
    found = store.set_settings("p", USER, 0, always=("ss/alpha", "ss/beta"), excluded=("ss/beta",))
    assert found.revision == 1
    longest = tuple(f"{'s' * 60}{i:04d}/{'n' * 64}" for i in range(500))
    with pytest.raises(ControlError, match="the excluded list passes 65,536 bytes"):
        store.set_settings("p", USER, 1, always=(), excluded=longest)
