# SPDX-License-Identifier: MIT
"""The user's ``skills`` commands of ``sandbox-science-harness``: ``import`` a pack from
git or an archive, ``pin`` a source's import in a project, and ``list`` the imports or a
project's pins (docs/SKILLS.md). Each prints JSON and is attributed to the user.
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
from .documents import USER
from .projects import ProjectStore
from .skills import SkillStore, from_archive, from_git, snapshot


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
                    shown = asdict(found)
                    shown["unchanged"] = same
                case "pin":
                    shown = asdict(store.pin(args.project, args.source, USER, args.revision))
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
        "--revision", help="the import's commit, archive or snapshot, by its first 7+ digits"
    )
    listed = actions.add_parser(
        "list", parents=[common], help="the imports, a source's, or a project's pins"
    )
    which = listed.add_mutually_exclusive_group()
    which.add_argument("--project", help="this project's pins")
    which.add_argument("--source", help="this source's imports")
