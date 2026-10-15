# SPDX-License-Identifier: MIT
"""The user's ``skills`` commands of ``sandbox-science-harness``: ``import`` a pack from
git or an archive, ``pin`` a source's import in a project, ``settings`` shows or changes
the skills a project loads at every run's start or excludes, and ``list`` the imports or
a project's pins (docs/SKILLS.md). Each prints JSON and is attributed to the user.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .control import ControlError
from .control.errors import INVALID_REQUEST
from .documents import USER
from .projects import ProjectStore
from .skills import SkillSettings, SkillStore, from_archive, from_git, snapshot


def skills(args: argparse.Namespace) -> int:
    with contextlib.closing(ProjectStore(args.state_dir)) as projects:
        store = SkillStore(projects.control)
        try:
            match args.action:
                case "import":
                    fetched = (
                        from_git(args.git, args.ref, args.path)
                        if args.git is not None
                        else from_archive(args.archive, args.path)
                    )
                    found, same = store.add(args.name, snapshot(fetched), USER)
                    for warning in found.report.get("warnings", []):
                        print(f"warning: {warning}", file=sys.stderr)
                    shown = asdict(found)
                    shown["unchanged"] = same
                case "pin":
                    shown = asdict(store.pin(args.project, args.source, USER, args.revision))
                case "settings":
                    shown = asdict(_settings(store, args))
                case _:
                    shown = (
                        [asdict(pin) for pin in store.pins(args.project)]
                        if args.project is not None
                        else [asdict(found) for found in store.imports(args.source)]
                    )
        except ControlError as error:
            print(f"error: {error.kind}: {error.message}", file=sys.stderr)
            return 1
    print(json.dumps(shown, indent=2))
    return 0


def _settings(store: SkillStore, args: argparse.Namespace) -> SkillSettings:
    """The project's skill settings, changed when ``--expected`` names the revision read.
    ``--no-always`` and ``--no-exclude`` empty a list first; ``--always`` and
    ``--exclude`` then add to it, and ``--not-always`` and ``--include`` take from it. A
    change without ``--expected`` is refused, naming the revision and the lists it would
    make."""
    current = store.settings(args.project)
    always = [] if args.no_always else list(current.always)
    excluded = [] if args.no_exclude else list(current.excluded)
    always += [skill for skill in args.always or () if skill not in always]
    excluded += [skill for skill in args.exclude or () if skill not in excluded]
    always = [skill for skill in always if skill not in (args.not_always or ())]
    excluded = [skill for skill in excluded if skill not in (args.include or ())]
    changing = (args.always, args.exclude, args.not_always, args.include)
    if args.expected is None:
        if any(changing) or args.no_always or args.no_exclude:
            raise ControlError(
                INVALID_REQUEST,
                f"give --expected {current.revision}, the settings' revision, to change them: "
                f"always then {always or 'none'}, excluded then {excluded or 'none'}",
            )
        return current
    return store.set_settings(
        args.project, USER, args.expected, always=tuple(always), excluded=tuple(excluded)
    )


def add_parser(commands: Any) -> None:
    command = commands.add_parser("skills", help="import skill packs and pin them in projects")
    actions = command.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, required=True)
    imported = actions.add_parser(
        "import", parents=[common], help="import a pack from git or an archive, with a report"
    )
    imported.add_argument("--name", required=True, help="the source's name, as in NAME/skill")
    origin = imported.add_mutually_exclusive_group(required=True)
    origin.add_argument("--git", help="a git URL, or a local repository's path")
    origin.add_argument("--archive", type=Path, help="a .tar, .tar.gz or .zip file")
    imported.add_argument(
        "--ref", help="a branch, a tag or a full commit hash (default: the default branch)"
    )
    imported.add_argument(
        "--path", help="the directory of the skills (default: skills/ if there, else the top)"
    )
    pinned = actions.add_parser(
        "pin", parents=[common], help="pin a source's latest import, or one, in a project"
    )
    pinned.add_argument("--project", required=True)
    pinned.add_argument("--source", required=True)
    pinned.add_argument(
        "--revision", help="the import's commit, archive or snapshot, by its first 12+ digits"
    )
    settings = actions.add_parser(
        "settings",
        parents=[common],
        help="show or change a project's skills loaded at every run's start, and excluded",
    )
    settings.add_argument("--project", required=True)
    settings.add_argument("--expected", type=int, help="the settings' revision, to change them")
    for flag, text in (
        ("--always", "load this skill in full at every run's start"),
        ("--not-always", "no longer load this skill at the start"),
        ("--exclude", "leave this skill out of the project's runs"),
        ("--include", "no longer leave this skill out"),
    ):
        settings.add_argument(flag, action="append", metavar="SOURCE/NAME", help=text)
    settings.add_argument("--no-always", action="store_true", help="empty the list first")
    settings.add_argument("--no-exclude", action="store_true", help="empty the list first")
    listed = actions.add_parser(
        "list", parents=[common], help="the imports, a source's, or a project's pins"
    )
    which = listed.add_mutually_exclusive_group()
    which.add_argument("--project", help="this project's pins")
    which.add_argument("--source", help="this source's imports")
