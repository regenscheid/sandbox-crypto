# SPDX-License-Identifier: MIT
"""The config's skill packs, imported and pinned through the harness (docs/SKILLS.md,
docs/specs/skills.md, SK-28).

``up`` imports a pack the control store has no import of, and pins it in each project
that names it and does not pin it yet; otherwise it fetches nothing and keeps the pin,
saying at which revision each project pins it. A pack that cannot be imported is reported
and left as it was; one that cannot be pinned in a project, as when a skill of it has the
name of a skill the project has, is reported for that project, and the others still pin
it. ``up`` goes on without them, and ``skills update`` exits 1.
``up --update-skills`` and ``sandbox-science skills update`` import each pack again from
its ref, and move each project's pin to the import, saying what changed since the pin it
left. Each step is a ``sandbox-science-harness skills`` command, run on the user's machine
with the user's git. With ``[skills] embedding_model`` the import embeds the skills
through the gateway, given the key file's path, which the launcher never opens.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

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
) -> tuple[list[str], list[str]]:
    """Import and pin the config's packs, or only those named: what happened, a line each,
    and the packs that failed, each left as it was."""
    chosen = set(only)
    unknown = chosen - set(config.skills.packs)
    if unknown:
        raise LauncherError(f"no skills.packs {', '.join(sorted(unknown))} in the config")
    packs = {n: p for n, p in config.skills.packs.items() if not chosen or n in chosen}
    if not packs:
        return [], []
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
    failed: list[str] = []
    for name, pack in packs.items():
        imports: list[dict[str, Any]] = []
        try:
            imports = cast(list[dict[str, Any]], await answer("list", "--source", name))
            if update or not imports:
                found = cast(dict[str, Any], await answer(*import_argv(config, name, pack)))
                lines.append(_imported(name, found, first=not imports))
            else:
                found = imports[0]
        except LauncherError as error:  # this pack's; the others go on
            failed.append(name)
            lines.append(f"skills: {name} {await _after(answer, name, imports)}: {error}")
            continue
        note = "" if update else _differs(pack, found)
        users = [p.id for p in config.projects if name in p.skills]
        if not users and not update and imports:
            lines.append(f"skills: {name} was imported before, at {_revision(found)}{note}")
        for project in users:
            had = pinned[project].get(name)
            if had is not None and not update:
                at = _revision(cast(dict[str, Any], had["pinned"]))
                lines.append(f"skills: {project} keeps {name} at {at}{note}")
                continue
            try:
                pin = await answer("pin", "--project", project, "--source", name)
            except LauncherError as error:  # this project's: the others still pin it
                failed.append(f"{name} in {project}")
                lines.append(
                    f"skills: {name} at {_revision(found)} is not pinned in {project}: {error}"
                )
                continue
            lines.append(_pinned(project, name, cast(dict[str, Any], pin), had))
    return lines, failed


async def _after(
    answer: Callable[..., Awaitable[Any]], name: str, before: list[dict[str, Any]]
) -> str:
    """What a failed import left: the store as it was, or an import it kept before it
    failed, which no pin moved to."""
    try:
        now = cast(list[dict[str, Any]], await answer("list", "--source", name))
    except LauncherError:
        return "may have been imported, and no pin moved"
    if now and (not before or now[0].get("import_id") != before[0].get("import_id")):
        return f"was imported at {_revision(now[0])}, and no pin moved to it"
    return "is left as it was"


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
    """A note when the config names another kind, place, ref or directory than the kept
    import's, as the import records them: a URL without its password, a repository's
    resolved path, an archive's name."""
    kind = "git" if pack.git is not None else "archive"
    if pack.git is not None:
        origin = _shown(pack.git) if _url(pack.git) else str(Path(pack.git).resolve())
    else:
        origin = Path(str(pack.archive)).name
    root = None if pack.path is None else posixpath.normpath(pack.path.strip("/") or ".")
    different = [
        what
        for what, same in (
            ("source", found.get("kind") == kind and found.get("origin") == origin),
            ("ref", found.get("ref") == pack.ref),
            ("path", root is None or posixpath.normpath(found.get("root") or ".") == root),
        )
        if not same
    ]
    if not different:
        return ""
    return (
        f" (the config names another {' and '.join(different)}: sandbox-science skills "
        "update imports it)"
    )


def _url(origin: str) -> bool:
    return "://" in origin or ":" in origin.partition("/")[0]


def _shown(origin: str) -> str:
    """A URL as the harness records it: without its password, and without its user name
    unless it is an SSH login."""
    parts = urlsplit(origin)
    if not parts.scheme or "@" not in parts.netloc:
        return origin
    host = parts.netloc.rpartition("@")[2]
    user = parts.username if parts.scheme == "ssh" and parts.password is None else None
    return urlunsplit(parts._replace(netloc=f"{user}@{host}" if user else host))
