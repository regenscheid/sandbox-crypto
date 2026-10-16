# SPDX-License-Identifier: MIT
"""A project's skills, for the GUI's skills page (docs/specs/skills.md, SK-24).

``GET /api/v0/projects/{project}/skills`` answers the project's skill settings (always
loaded and excluded skills, and the user's overrides of what the import's pass derives,
with their revision), its pins, each at its revision with the import it pins and that
import's report (topics, entry skills, the pass's derived items, left-out built-ins,
hidden characters, broken links), and the skills a run of it is given now. ``POST
…/skills/settings`` is the user's change of the always-loaded and excluded skills, at the
settings' revision read (``expected``), as ``skills settings --expected`` makes it. What
each run was shown and loaded is in its run record (``summaries.skills``).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Final

from aiohttp import web
from pydantic import AwareDatetime, Field, JsonValue, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.skills import SkillSettings, SkillStore
from sandbox_science.orchestrator.skills.derive import VERSION as PASS_VERSION

from . import documents
from .context import json_body

PATH: Final = "/api/v0/projects/{project}/skills"
METADATA: Final = web.AppKey("skills_metadata", str)
"""The alias of the import's pass whose topics, entry skills and short descriptions the
page shows, as runs are given them (``--skills-metadata-model``)."""


class SkillImportView(Contract):
    """An import of a pack: where it came from, and its report."""

    import_id: int
    source: str
    kind: str
    origin: str
    ref: str | None
    commit: str | None
    archive_sha256: str | None
    created: str
    report: dict[str, JsonValue]


class SkillPinView(Contract):
    """A source pinned in the project, at its revision, with the import it pins; for the
    built-in skills, those a pack's skill of the same name leaves out; and the user's
    overrides of this source's skills (SK-33)."""

    source: str
    revision: int
    skills: list[str]
    left_out: list[str]
    user_set: dict[str, JsonValue] | None
    pinned: SkillImportView


class SkillView(Contract):
    """A skill a run is given, with its short description and topic, and who set them
    (``derived`` by the import's pass, or ``user``)."""

    id: str
    source: str
    name: str
    description: str
    revision: str
    short: str | None
    topic: str | None
    entry: bool
    short_by: str | None
    topic_by: str | None


class SkillSettingsView(Contract):
    """The project's skill settings and overrides, at their revision (0: never set)."""

    revision: int
    always: list[str]
    excluded: list[str]
    overrides: dict[str, JsonValue]


class ProjectSkills(Contract):
    """A project's skills: its settings, its pins and the skills a run is given now."""

    project_id: Identifier
    observed_at: AwareDatetime
    settings: SkillSettingsView
    pins: list[SkillPinView]
    effective: list[SkillView]


class SkillSettingsChange(Contract):
    """The user's change of the always-loaded and excluded skills, at ``expected``."""

    expected: int = Field(ge=0)
    always: list[str] = Field(default_factory=list[str], max_length=256)
    excluded: list[str] = Field(default_factory=list[str], max_length=4096)


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, skills)
    app.router.add_post(PATH + "/settings", settings)


def _settings(found: SkillSettings) -> SkillSettingsView:
    return SkillSettingsView(
        revision=found.revision,
        always=list(found.always),
        excluded=list(found.excluded),
        overrides=found.overrides,
    )


def _read(store: DocumentStore, project: str, alias: str | None) -> dict[str, object]:
    skills = SkillStore.of(store)
    derived = None if alias is None else (alias, PASS_VERSION)
    pins = [
        SkillPinView(
            source=pin.source,
            revision=pin.revision,
            skills=list(pin.skills),
            left_out=list(pin.left_out),
            user_set=pin.user_set,
            pinned=SkillImportView.model_validate(
                {k: v for k, v in asdict(pin.pinned).items() if k not in ("snapshot", "root")}
            ),
        )
        for pin in skills.pins(project)
    ]
    effective = [
        SkillView(
            id=entry.qualified,
            source=entry.source,
            name=entry.name,
            description=entry.description,
            revision=entry.revision,
            short=entry.short,
            topic=entry.topic,
            entry=entry.entry,
            short_by=entry.short_by,
            topic_by=entry.topic_by,
        )
        for entry in skills.effective(project, derived=derived).entries
    ]
    return ProjectSkills(
        project_id=project,
        observed_at=datetime.now(UTC),
        settings=_settings(skills.settings(project)),
        pins=pins,
        effective=effective,
    ).model_dump(mode="json")


async def skills(request: web.Request) -> web.Response:
    if request.query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the skills read takes no parameters")
    project = request.match_info["project"]
    alias = request.app.get(METADATA)
    return await documents.answer(request, lambda store: _read(store, project, alias))


async def settings(request: web.Request) -> web.Response:
    if request.query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the settings change takes no parameters")
    try:
        change = SkillSettingsChange.model_validate(await json_body(request, "skill settings"))
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid skill settings") from None
    project = request.match_info["project"]

    def changed(store: DocumentStore) -> dict[str, object]:
        found = SkillStore.of(store).set_settings(
            project,
            USER,
            change.expected,
            always=tuple(change.always),
            excluded=tuple(change.excluded),
        )
        return _settings(found).model_dump(mode="json")

    return await documents.answer(request, changed, writer=True)
