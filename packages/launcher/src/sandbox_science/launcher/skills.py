# SPDX-License-Identifier: MIT
"""The config's skill packs, imported and pinned through the harness (docs/SKILLS.md,
docs/specs/skills.md, SK-28).

``up`` imports a pack the control store has no import of, and pins it in each project
that names it and does not pin it yet; otherwise it fetches nothing and keeps the pin.
``up --update-skills`` and ``sandbox-science skills update`` import each pack again from
its ref, and move each project's pin to the import, saying what changed since the pin it
left. Each step is a ``sandbox-science-harness skills`` command, run on the user's machine
with the user's git. With ``[skills] embedding_model`` the import embeds the skills
through the gateway, given the key file's path, which the launcher never opens.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from . import parts
from .config import LauncherConfig, LauncherError, SkillPackConfig
from .state import Layout
from .supervise import Supervisor, passed


def import_argv(config: LauncherConfig, name: str, pack: SkillPackConfig) -> list[str]:
    """``skills import``'s action and options for ``pack``: the key file by its path."""
    origin = ["--git", pack.git] if pack.git is not None else ["--archive", str(pack.archive)]
    argv = ["import", "--name", name, *origin]
    if pack.ref is not None:
        argv += ["--ref", pack.ref]
    if pack.path is not None:
        argv += ["--path", pack.path]
    if (model := config.skills.embedding_model) is not None:
        argv += [
            "--embedding-model", model, "--model-url", config.model.url,
            "--api-key-file", str(config.model.api_key_file),
        ]  # fmt: skip
    return argv


async def keep(
    config: LauncherConfig,
    layout: Layout,
    supervisor: Supervisor,
    *,
    update: bool = False,
    only: Iterable[str] = (),
) -> list[str]:
    """Import and pin the config's packs, or only those named; what happened, a line each."""
    chosen = set(only)
    unknown = chosen - set(config.skills.packs)
    if unknown:
        raise LauncherError(f"no skills.packs {', '.join(sorted(unknown))} in the config")
    packs = {n: p for n, p in config.skills.packs.items() if not chosen or n in chosen}
    if not packs:
        return []
    # The harness's environment, as the run service's, with what git needs besides.
    env = {**passed(config.run_service.pass_env), **passed(config.skills.pass_env)}

    async def answer(action: str, *args: str) -> Any:
        """The harness's JSON answer, after any warnings it gave first."""
        argv = [*_harness(config, layout, action), *args]
        name = f"skills {action}"
        text = await supervisor.run(name, argv, layout.control, config.start_timeout, env)
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line[:1] in ("{", "[")), len(lines))
        try:
            return json.loads("\n".join(lines[start:]))
        except ValueError:
            raise LauncherError(f"{name} answered no JSON; see {layout.log('launcher')}") from None

    pinned: dict[str, dict[str, Any]] = {}
    for project in config.projects:
        if any(name in project.skills for name in packs):
            pins = cast(list[dict[str, Any]], await answer("list", "--project", project.id))
            pinned[project.id] = {pin["source"]: pin for pin in pins}
    lines: list[str] = []
    for name, pack in packs.items():
        imports = cast(list[dict[str, Any]], await answer("list", "--source", name))
        if update or not imports:
            found = cast(dict[str, Any], await answer(*import_argv(config, name, pack)))
            lines.append(_imported(name, found, first=not imports))
        else:
            lines.append(
                f"skills: {name} kept at {_revision(imports[0])}{_differs(pack, imports[0])}"
            )
        for project in (p.id for p in config.projects if name in p.skills):
            had = pinned[project].get(name)
            if had is None or update:
                pin = await answer("pin", "--project", project, "--source", name)
                lines.append(_pinned(project, name, cast(dict[str, Any], pin), had))
    return lines


def _harness(config: LauncherConfig, layout: Layout, action: str) -> list[str]:
    command = str(parts.commands(config) / parts.HARNESS)
    return [command, "skills", action, "--state-dir", str(layout.control)]


def _revision(found: dict[str, Any]) -> str:
    named = found.get("commit") or found.get("archive_sha256") or found.get("snapshot") or "?"
    return str(named)[:12]


def _imported(name: str, found: dict[str, Any], *, first: bool) -> str:
    report = cast(dict[str, Any], found.get("report") or {})
    count = len(cast(list[str], report.get("skills") or []))
    excluded = len(cast(list[object], report.get("excluded") or []))
    if found.get("unchanged"):
        line = f"skills: {name} is unchanged at {_revision(found)}"
    else:
        left = f", {excluded} excluded" if excluded else ""
        line = f"skills: {name} {'imported' if first else 'fetched'} at {_revision(found)}, "
        line += f"{count} skill{'' if count == 1 else 's'}{left}"
    embedded = cast(dict[str, Any], found.get("embeddings") or {})
    if "failed" in embedded:
        line += f"; embedding them failed, and runs embed them as they start: {embedded['failed']}"
    return line


def _pinned(project: str, name: str, pin: dict[str, Any], had: dict[str, Any] | None) -> str:
    at = _revision(cast(dict[str, Any], pin["pinned"]))
    if had is None:
        return f"skills: {project} pins {name} at {at}"
    changes = cast(dict[str, list[str]] | None, pin.get("changes"))
    if changes is None:
        return f"skills: {project} keeps {name} at {at}, unchanged"
    said = "; ".join(
        f"{what} {', '.join(changes[what])}"
        for what in ("added", "removed", "changed")
        if changes.get(what)
    )
    since = str(changes.get("since", ""))[:12]
    return f"skills: {project} moves {name} from {since} to {at}: {said or 'no skill changed'}"


def _differs(pack: SkillPackConfig, found: dict[str, Any]) -> str:
    """A note when the config names another kind or ref than the kept import's."""
    kind = "git" if pack.git is not None else "archive"
    if found.get("kind") == kind and found.get("ref") == pack.ref:
        return ""
    return (
        f" (the config names another {'ref' if found.get('kind') == kind else 'source'}: "
        "sandbox-science skills update imports it)"
    )
