# SPDX-License-Identifier: MIT
"""A project's skills through HTTP (SK-24): its settings, its pins with their imports'
reports and the skills a run is given, typed and no-store; and the user's change of the
always-loaded and excluded skills, at the settings' revision read."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.skills import SkillStore, snapshot
from sandbox_science.orchestrator.skills.derive import VERSION
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.workspace.skills import ProjectSkills, SkillSettingsView

from .route_support import ClientFactory, error_of, origin, workspace

BASE = "/api/v0/projects/p1/skills"
HOSTILE = '"><script>alert(1)</script>'
FILES = {
    "alpha/SKILL.md": f"---\nname: alpha\ndescription: Says the marker {HOSTILE}.\n---\nA\n",
    "beta/SKILL.md": "---\nname: beta\ndescription: Counts lines.\n---\nSee [x](missing.md).\n",
}


@pytest.fixture
def pinned(state_dir: Path) -> Path:
    documents = DocumentStore(store_path(state_dir))
    try:
        store = SkillStore.of(documents)
        packed = tuple(PackFile(path, text.encode()) for path, text in FILES.items())
        digest = hashlib.sha256(repr(sorted(FILES.items())).encode()).hexdigest()
        snap = snapshot(Fetched("archive", "a.tar", "", packed, (), archive_sha256=digest))
        store.add("ss", snap, USER)
        store.pin("p1", "ss", USER)
    finally:
        documents.close()
    return state_dir


async def test_a_projects_skills_read_with_their_pins_reports_and_settings(
    aiohttp_client: ClientFactory, pinned: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, pinned, static_dir)
    response = await client.get(BASE)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    found = ProjectSkills.model_validate(await response.json())
    (pin,) = [p for p in found.pins if p.source == "ss"]
    assert (pin.revision, pin.skills, pin.pinned.kind) == (1, ["alpha", "beta"], "archive")
    assert pin.pinned.report["skills"] == ["alpha", "beta"]
    assert "broken_links" in pin.pinned.report
    ids = {skill.id: skill for skill in found.effective}
    assert ids["ss/alpha"].description == f"Says the marker {HOSTILE}."
    assert found.settings.revision == 0
    assert (await client.get(BASE, params={"x": "1"})).status == 400
    assert (await client.get("/api/v0/projects/nope/skills")).status == 404


async def test_the_user_changes_the_skill_settings_at_their_revision(
    aiohttp_client: ClientFactory, pinned: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, pinned, static_dir)
    body = {"expected": 0, "always": ["ss/alpha"], "excluded": ["ss/beta"]}
    response = await client.post(BASE + "/settings", json=body, headers=origin(client))
    assert response.status == 200, await response.text()
    settings = SkillSettingsView.model_validate(await response.json())
    assert (settings.revision, settings.always, settings.excluded) == (
        1,
        ["ss/alpha"],
        ["ss/beta"],
    )
    found = ProjectSkills.model_validate(await (await client.get(BASE)).json())
    assert [skill.id for skill in found.effective if skill.source == "ss"] == ["ss/alpha"]
    # A change read at an older revision is a conflict, and changes nothing.
    stale = await client.post(BASE + "/settings", json=body, headers=origin(client))
    await error_of(stale, 409, "conflict")
    wrong = await client.post(
        BASE + "/settings", json={"expected": 1, "always": [1]}, headers=origin(client)
    )
    assert wrong.status == 400
    refused = await client.post(BASE + "/settings", json=body)
    assert refused.status == 403


async def test_the_page_shows_what_the_configured_pass_derived_as_runs_get_it(
    aiohttp_client: ClientFactory, pinned: Path, static_dir: Path
) -> None:
    """Lab 43, B5: with the alias runs are given (``--skills-metadata-model``), the
    page's skills carry the pass's topics and entry skills; without it, none."""
    documents = DocumentStore(store_path(pinned))
    try:
        store = SkillStore.of(documents)
        (snap,) = {
            entry.snapshot for entry in store.effective("p1").entries if entry.source == "ss"
        }
        derived = {
            "topics": {"Counting": ["beta"]},
            "entry": ["alpha"],
            "bindings": {"SS_DATA": "alpha", "OTHER_DIR": ""},
        }
        store.add_metadata(snap, "pass", VERSION, "m", derived, [])
    finally:
        documents.close()
    for alias, topic, entry in (("pass", "Counting", True), (None, None, False)):
        client = await workspace(aiohttp_client, pinned, static_dir, skills_metadata=alias)
        found = ProjectSkills.model_validate(await (await client.get(BASE)).json())
        await client.close()
        ids = {skill.id: skill for skill in found.effective}
        assert (ids["ss/beta"].topic, ids["ss/alpha"].entry) == (topic, entry)
        if alias is None:
            assert found.bindings == []
            continue
        (bound,) = found.bindings
        assert (bound.source, bound.set) == ("ss", {"SS_DATA": "alpha"})
        (held,) = bound.held
        assert held.variable == "OTHER_DIR"
        assert held.reason.startswith("not namespaced to the pack (SS_)")
