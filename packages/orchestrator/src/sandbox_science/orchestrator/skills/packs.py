# SPDX-License-Identifier: MIT
"""The skills in a pack's files, and the snapshot that keeps them (docs/specs/skills.md,
SK-3, SK-6; docs/SKILLS.md).

A skill is a directory directly under the skills' directory holding a ``SKILL.md`` whose
front matter is read strictly; one that fails is excluded with the reason. A snapshot is
named by a SHA-256 over its files' paths, executable bits and contents' SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Final, cast

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

from sandbox_science.orchestrator.documents.agent_text import (
    hidden_characters,
    newlines,
    visible,
)
from sandbox_science.orchestrator.documents.frontmatter import NoAliases

from .sources import Fetched, PackFile, Skipped

SKILL_FILE: Final = "SKILL.md"
MAX_BODY_BYTES: Final = 64 * 1024
MAX_FRONT_MATTER: Final = 16 * 1024
MAX_DESCRIPTION: Final = 1024
MAX_METADATA_BYTES: Final = 16 * 1024
NAME: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
"""A skill's name, and a source's: lowercase letters, digits and inner hyphens."""
_FRONT: Final = re.compile(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)", re.DOTALL)
_FORMAT: Final = "sandbox-skill-snapshot-1"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body_sha256: str
    metadata: dict[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True)
class Snapshot:
    """A pack's skills and files, as an import keeps them."""

    fetched: Fetched
    skills: tuple[Skill, ...]
    excluded: tuple[Skipped, ...]
    """Skill directories whose ``SKILL.md`` failed, and why."""
    sha256: str


def snapshot(fetched: Fetched) -> Snapshot:
    skills: list[Skill] = []
    excluded: list[Skipped] = []
    for file in fetched.files:
        directory, _, rest = file.path.rpartition("/")
        if rest != SKILL_FILE or "/" in directory:
            continue
        if not directory:
            excluded.append({"path": file.path, "reason": "a skill has a directory of its own"})
            continue
        found = read_skill(directory, file.content)
        if isinstance(found, Skill):
            skills.append(found)
        else:
            excluded.append({"path": file.path, "reason": found})
    return Snapshot(fetched, tuple(skills), tuple(excluded), snapshot_sha256(fetched.files))


class _RefusedError(Exception):
    """Why a skill's front matter is refused."""


class _Strict(NoAliases):
    """The safe loader without aliases, refusing merge keys and repeated keys."""

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        keys: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise _RefusedError("the front matter merges a mapping with <<")
            key: object = cast(Any, self).construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _RefusedError("the front matter has a key that is not text")
            if key in keys:
                raise _RefusedError(f"the front matter repeats the key {key[:40]!r}")
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def read_skill(directory: str, content: bytes) -> Skill | str:
    """The skill in a ``SKILL.md``, or why it is excluded. Text in the front matter
    holding a character a reader may not see is refused; its runs of white space become
    single spaces."""
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return "SKILL.md is not UTF-8"
    found = _FRONT.match(text)
    if found is None:
        return "SKILL.md does not start with front matter between --- lines"
    front, body = found[1], found[2]
    if len(front) > MAX_FRONT_MATTER:
        return "the front matter is longer than 16 KiB"
    if len(body.encode()) > MAX_BODY_BYTES:
        return "the body is longer than 64 KiB"
    try:
        data: object = yaml.load(front, Loader=_Strict)  # noqa: S506 - a safe loader
        if not isinstance(data, dict):
            return "the front matter is not a mapping"
        fields = cast(dict[str, object], _clean(cast(object, data), "the front matter"))
    except _RefusedError as refused:
        return str(refused)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        return "the front matter is not valid YAML without aliases"
    name, description = fields.pop("name", None), fields.pop("description", None)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return "name is missing, or not lowercase letters, digits and hyphens (at most 64)"
    if name != directory:
        return f"name {name!r} is not its directory's name"
    if not isinstance(description, str) or not description:
        return "description is missing"
    if len(description) > MAX_DESCRIPTION:
        return "description is longer than 1,024 characters"
    if len(json.dumps(fields, allow_nan=False).encode()) > MAX_METADATA_BYTES:
        return "the other front matter fields are longer than 16 KiB"
    digest = hashlib.sha256(body.encode()).hexdigest()
    return Skill(name, description, digest, fields)


def _clean(value: object, where: str) -> object:
    """``value`` as JSON can keep it, its text checked and its white space collapsed."""
    if isinstance(value, str):
        hidden = hidden_characters(value)
        if hidden:
            raise _RefusedError(f"{where} holds {hidden[0][1]}, a character a reader may not see")
        return " ".join(value.split())
    if isinstance(value, float) and not math.isfinite(value):
        raise _RefusedError(f"{where} holds a number that is not finite")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return [_clean(item, where) for item in cast(list[object], value)]
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise _RefusedError(f"{where} has a key that is not text")
            cleaned[cast(str, _clean(key, where))] = _clean(item, key[:40])
        return cleaned
    return _clean(str(value), where)


def body(text: str) -> str:
    """A ``SKILL.md``'s body, as its SHA-256 was taken: the text after its front matter."""
    found = _FRONT.match(text)
    return found[2] if found is not None else text


def served(text: str) -> str:
    """Pack text as a run is given it: line ends made line feeds, as kept text's are, then
    each hidden character, and each that can pass for other text, written as its code
    point (``visible``), so that none acts or passes unseen in the model's context."""
    return visible(newlines(text))


def hidden(files: tuple[PackFile, ...]) -> list[dict[str, Any]]:
    """The text files with hidden characters, and how many each has, for the report."""
    found: list[dict[str, Any]] = []
    for file in files:
        try:
            text = file.content.decode()
        except UnicodeDecodeError:
            continue  # binary: read as such, never shown as text
        if count := len(hidden_characters(newlines(text))):  # a CR line end is no secret
            found.append({"path": file.path, "characters": count})
    return found


_LINKED: Final = (".md", ".markdown", ".txt")
"""The files whose relative links the report checks: the Markdown a skill links from."""


def broken_links(files: tuple[PackFile, ...]) -> list[dict[str, Any]]:
    """Each relative link or image in the pack's Markdown that names no file of the
    snapshot (SK-32), as written, with the file that has it; and the file the link would
    find if case did not count, since a loader that matches case finds no other."""
    paths = {file.path for file in files}
    folded: dict[str, str] = {path.casefold(): path for path in sorted(paths)}
    folders = {path.rsplit("/", i)[0] for path in paths for i in range(1, path.count("/") + 1)}
    found: list[dict[str, Any]] = []
    parser = MarkdownIt("commonmark")
    for file in files:
        if not file.path.lower().endswith(_LINKED):
            continue
        try:
            text = file.content.decode()
        except UnicodeDecodeError:
            continue
        for href in dict.fromkeys(_targets(parser.parse(newlines(text)))):
            target = urllib.parse.unquote(href.partition("#")[0].partition("?")[0])
            if not target or "://" in href or href.startswith(("#", "/", "mailto:")):
                continue
            where = posixpath.normpath(posixpath.join(posixpath.dirname(file.path), target))
            if where in paths or where in folders:
                continue
            item = {"path": file.path, "link": href[:200]}
            if (near := folded.get(where.casefold())) is not None:
                item["differs_by_case_from"] = near
            found.append(item)
    return found


def _targets(tokens: list[Token]) -> list[str]:
    """The link and image targets of parsed Markdown, outside code."""
    found: list[str] = []
    for token in tokens:
        for child in token.children or ():
            if child.type == "link_open" and isinstance(href := child.attrs.get("href"), str):
                found.append(href)
            elif child.type == "image" and isinstance(src := child.attrs.get("src"), str):
                found.append(src)
    return found


def snapshot_sha256(files: tuple[PackFile, ...]) -> str:
    listing = [_FORMAT]
    for file in sorted(files, key=lambda f: f.path):
        mode = "x" if file.executable else "-"
        listing.append(f"{file.path}\0{mode}\0{hashlib.sha256(file.content).hexdigest()}")
    return hashlib.sha256("\n".join(listing).encode()).hexdigest()
