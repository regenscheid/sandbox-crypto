# SPDX-License-Identifier: MIT
"""The user's ``skills`` commands of ``sandbox-science-harness``: ``import`` a pack from
git or an archive, ``pin`` a source's import in a project, ``settings`` shows or changes
the skills a project loads at every run's start or excludes, and ``list`` the imports or
a project's pins (docs/SKILLS.md). Each prints JSON and is attributed to the user.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from .control import ControlError
from .control.errors import INVALID_REQUEST
from .documents import USER
from .model import OpenAICompatibleModel
from .pricing import GATEWAY
from .projects import ProjectError, ProjectStore
from .skills import SkillSettings, SkillStore, from_archive, from_git, snapshot
from .skills.builtin import builtin_pack
from .skills.derive import ANSWER_TOKENS, VERSION, Derived, PassError, derive
from .skills.embeddings import (
    MAX_MODEL_BYTES,
    Embedder,
    EmbeddingError,
    Paid,
    count_paid,
    ensure,
)
from .skills.packs import Snapshot


def skills(args: argparse.Namespace) -> int:
    with contextlib.closing(ProjectStore(args.state_dir)) as projects:
        store = SkillStore(projects.control)
        try:
            match args.action:
                case "import":
                    if args.project is not None and not _exists(projects, args.project):
                        print(
                            f"warning: there is no project {args.project!r} yet: the import's "
                            "model calls count for no project",
                            file=sys.stderr,
                        )
                        args.project = None
                    shown = _import(store, args)
                case "pin":
                    # The built-in skills the harness ships first, as a run's start pins
                    # them: a collision with one is refused now, not after the first run.
                    store.ensure(args.project, builtin_pack())
                    shown = asdict(store.pin(args.project, args.source, USER, args.revision))
                case "settings":
                    shown = asdict(_settings(store, args))
                case "override":
                    shown = asdict(_override(store, args))
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


def _exists(projects: ProjectStore, project: str) -> bool:
    """Whether the control store has ``project``, whose cost the import's calls count for."""
    try:
        projects.get(project)
    except ProjectError:
        return False
    return True


def _import(store: SkillStore, args: argparse.Namespace) -> dict[str, Any]:
    """Import the pack, with what the model pass derives from its catalog and its skills'
    embeddings when the arguments name models for them."""
    # The key is read before anything is fetched or kept: an import whose model calls
    # cannot even start leaves the store as it was.
    models = args.embedding_model is not None or args.metadata_model is not None
    key = args.api_key_file.read_text().strip() if models else ""
    fetched = (
        from_git(args.git, args.ref, args.path)
        if args.git is not None
        else from_archive(args.archive, args.path)
    )
    snap = snapshot(fetched)
    alias = cast(str | None, args.metadata_model)
    derived = asyncio.run(_derive(store, args, key, snap)) if alias is not None else None
    report = derived.report(alias) if isinstance(derived, Derived) and alias else None
    found, same = store.add(args.name, snap, USER, derived=report)
    if isinstance(derived, Derived) and alias and not derived.cached and derived.derived:
        store.add_metadata(
            snap.sha256, alias, VERSION, derived.model, derived.derived, derived.dropped
        )
    for warning in found.report.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    shown = asdict(found)
    shown["unchanged"] = same
    if isinstance(derived, PassError):
        shown["derived"] = {"alias": alias, "failed": str(derived)}
    elif report is not None:
        shown["derived"] = report
    if args.embedding_model is not None:
        shown["embeddings"] = asyncio.run(_embed(store, args, key, found.snapshot))
    return shown


async def _embed(
    store: SkillStore, args: argparse.Namespace, key: str, snapshot: str
) -> dict[str, Any]:
    """Embed the snapshot's skills that have no vector in the model's space (SK-29). A
    failure leaves the import as it is, with a warning: runs embed what they lack, and
    importing again embeds the skills."""
    embedder = Embedder(args.model_url, args.embedding_model, key)
    paid: Paid = []
    try:
        made = await ensure(store, embedder, snapshot, paid=paid)
    except EmbeddingError as error:
        print(f"warning: embedding the skills failed: {error}", file=sys.stderr)
        costs = [cost for _, _, cost in paid if cost is not None]
        return {"alias": args.embedding_model, "failed": str(error), "cost": sum(costs) or None}
    finally:
        await embedder.close()
        await count_paid(store, args.project, paid)  # with --project, the project's cost
    if made is None:
        return {"alias": args.embedding_model, "computed": 0, "reused": 0}
    return {
        "alias": args.embedding_model,
        "model": made.space.model,
        "dimensions": made.space.dimensions,
        "computed": made.computed,
        "reused": made.reused,
        "cost": made.cost,
    }


async def _derive(
    store: SkillStore, args: argparse.Namespace, key: str, snap: Snapshot
) -> Derived | PassError:
    """What the pass derives from ``snap``'s catalog, kept from an earlier import of it by
    the same alias and version, or asked for; a failure leaves the import without it, with
    a warning (SK-30)."""
    alias = cast(str, args.metadata_model)
    if (kept := store.metadata(snap.sha256, alias, VERSION)) is not None:
        return Derived(kept["derived"], kept["dropped"], kept["model"], cached=True)
    model = OpenAICompatibleModel(args.model_url, alias, key, max_tokens=ANSWER_TOKENS)
    try:
        made = await derive(model, snap, alias)
    except PassError as error:
        print(
            f"warning: the pass over the catalog failed: {error}; the same import again, or "
            "sandbox-science skills update, runs it again",
            file=sys.stderr,
        )
        if error.cost is not None:  # the gateway answered, and charged for it
            store.add_metadata_call(
                args.project, snap.sha256, alias, (None, None), error.cost, GATEWAY
            )
        return error
    finally:
        await model.close()
    store.add_metadata_call(
        args.project, snap.sha256, made.model, made.tokens, made.cost, made.cost_source
    )
    for item in made.dropped:
        print(f"warning: the pass's {item['kind']} was dropped: {item['reason']}", file=sys.stderr)
    return made


def embedding_alias(value: str) -> str:
    """An embedding model alias, as the control store keeps it: refused here, before any
    call, when it is not."""
    if not value or len(value.encode()) > MAX_MODEL_BYTES:
        raise argparse.ArgumentTypeError(f"an alias of 1 to {MAX_MODEL_BYTES} bytes")
    return value


def _override(store: SkillStore, args: argparse.Namespace) -> SkillSettings:
    """The project's overrides of what the import's pass derives, changed when
    ``--expected`` names the settings' revision read (SK-33): for ``--skill``, a
    ``--description``, ``--topic`` and ``--entry`` or ``--not-entry`` to set, and
    ``--clear`` to give back to the pass; for ``--source``, ``--unbind`` and ``--rebind``
    of its derived bindings, ``--approve`` and ``--unapprove`` of those not namespaced to
    the pack, and ``--prefix`` or ``--no-prefix`` for its own namespace (SK-31). A change
    without ``--expected`` is refused, naming the revision and the overrides it would make."""
    current = store.settings(args.project)
    lists = {
        key: {k: list(v) for k, v in cast(dict[str, Any], current.overrides.get(key, {})).items()}
        for key in ("unbound", "approved")
    }
    skills = {
        k: dict(v) for k, v in cast(dict[str, Any], current.overrides.get("skills", {})).items()
    }
    prefixes = dict(cast(dict[str, str], current.overrides.get("prefixes", {})))
    given = {
        "description": args.description,
        "topic": args.topic,
        "entry": True if args.entry else False if args.not_entry else None,
    }
    changing = any(v is not None for v in given.values()) or bool(args.clear)
    if changing:
        if args.skill is None:
            raise ControlError(
                INVALID_REQUEST, "--description, --topic, --entry and --clear need --skill"
            )
        fields = skills.setdefault(args.skill, {})
        fields |= {key: value for key, value in given.items() if value is not None}
        for key in args.clear or ():
            for field in ("description", "topic", "entry") if key == "all" else (key,):
                fields.pop(field, None)
    pairs = (("unbound", args.unbind, args.rebind), ("approved", args.approve, args.unapprove))
    for key, adding, taking in pairs:
        if adding or taking:
            if args.source is None:
                raise ControlError(INVALID_REQUEST, "bindings and prefixes need --source")
            changing = True
            names = set(lists[key].get(args.source, [])) | set(adding or ())
            lists[key][args.source] = sorted(names - set(taking or ()))
    if args.prefix is not None or args.no_prefix:
        if args.source is None:
            raise ControlError(INVALID_REQUEST, "bindings and prefixes need --source")
        changing = True
        if args.prefix is not None:
            prefixes[args.source] = args.prefix
        else:
            prefixes.pop(args.source, None)
    if args.expected is None:
        if changing:
            then = {"skills": skills, **lists, "prefixes": prefixes}
            raise ControlError(
                INVALID_REQUEST,
                f"give --expected {current.revision}, the settings' revision, to change them: "
                f"overrides then {json.dumps(then)}",
            )
        return current
    return store.set_overrides(
        args.project,
        USER,
        args.expected,
        skills=skills,
        unbound=lists["unbound"],
        approved=lists["approved"],
        prefixes=prefixes,
    )


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
    imported.add_argument(
        "--embedding-model",
        type=embedding_alias,
        help="the gateway's embedding model alias, to embed the skills",
    )
    imported.add_argument(
        "--metadata-model",
        type=embedding_alias,
        help="the gateway's model alias for the pass that derives topics, entry skills, "
        "short descriptions and bindings from the catalog",
    )
    imported.add_argument(
        "--model-url", help="--embedding-model, --metadata-model: the gateway's base URL"
    )
    imported.add_argument(
        "--api-key-file", type=Path, help="--embedding-model, --metadata-model: its key"
    )
    imported.add_argument(
        "--project", help="the project the import's model calls count toward (RS-6a)"
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
    override = actions.add_parser(
        "override",
        parents=[common],
        help="show or change a project's overrides of what the import's pass derives",
    )
    override.add_argument("--project", required=True)
    override.add_argument("--expected", type=int, help="the settings' revision, to change them")
    override.add_argument("--skill", metavar="SOURCE/NAME", help="the skill to override")
    override.add_argument("--description", help="the skill's short description in the index")
    override.add_argument("--topic", help="the topic the skill is listed under")
    entry = override.add_mutually_exclusive_group()
    entry.add_argument("--entry", action="store_true", help="an entry skill")
    entry.add_argument("--not-entry", action="store_true", help="no entry skill")
    override.add_argument(
        "--clear",
        action="append",
        choices=("description", "topic", "entry", "all"),
        help="give the field back to the pass (repeat)",
    )
    override.add_argument("--source", help="the pack whose derived bindings change")
    override.add_argument("--unbind", action="append", metavar="VARIABLE")
    override.add_argument("--rebind", action="append", metavar="VARIABLE")
    override.add_argument(
        "--approve",
        action="append",
        metavar="VARIABLE",
        help="set a derived binding not namespaced to the pack (repeat)",
    )
    override.add_argument("--unapprove", action="append", metavar="VARIABLE")
    named = override.add_mutually_exclusive_group()
    named.add_argument("--prefix", help="the pack's own namespace, such as CAS_")
    named.add_argument("--no-prefix", action="store_true", help="the source's name alone")
    listed = actions.add_parser(
        "list", parents=[common], help="the imports, a source's, or a project's pins"
    )
    which = listed.add_mutually_exclusive_group()
    which.add_argument("--project", help="this project's pins")
    which.add_argument("--source", help="this source's imports")
