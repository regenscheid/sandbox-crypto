# SPDX-License-Identifier: MIT
"""The basis and anchors of the ledger's revisions and work records
(docs/specs/context-management.md, CM-9; docs/specs/ledger.md, LG-12 as amended).

Every revision and work record an agent writes keeps its basis (``user_quoted``,
``observed`` or ``interpreted``, the default) and its anchors: the places it comes from,
besides the change's own tool call. An agent's change that creates an entry, changes its
statement or fields, or moves a decision to ``in_effect``, ``rejected`` or ``reversed``,
and every work record, has at least one; a revision's evidence links, and a work
record's references to a delegation, an artifact or a document, count as anchors. Code
checks what it can and refuses with ``provenance_required``:

- ``user_quoted``: an anchor on a message the user wrote (a ``user`` row, or a comment)
  quotes at least 20 characters of it, or all of a shorter one, verbatim, line ends
  normalized;
- ``observed``: an anchor on a tool result (whole, or a range of its output) or an
  artifact revision, or evidence pinned to an artifact or a quote of a reference's PDF or
  ePrint; every number in the statement and fields occurs in one of them, its notation
  normalized (``numbers``, as CM-20 reads numbers);
- every anchor exists in the project.

It verifies that the words and numbers are where the record says, not what they mean.
The platform's own records are anchored on what made them: an import on its results
file's line, a check's outcome on its delegation (``observed``). The user's changes
have the basis ``user``. Revisions written before show no basis.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError

from . import evidence
from .models import MIN_QUOTE, AnchorInput, EvidenceLink

Json = dict[str, Any]

TEXT_BYTES: Final = 4 * 1024 * 1024
"""The most bytes of an artifact revision the number check reads."""

_SUPERSCRIPT: Final = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_POWER: Final = re.compile(r"(?:\^|\*\*)\s*(?:\{\s*(-?\d+(?:\.\d+)?)\s*\}|(-?\d+(?:\.\d+)?))")
_SUPERSCRIPTS: Final = re.compile(r"(?<=\d)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
_SEPARATED: Final = re.compile(r"(?<=\d)[,\u2009\u202f_](?=\d{3}(?!\d))")
_NUMBER: Final = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:\^-?\d+(?:\.\d+)?)?(?!\w)")
_NAMES: Final = re.compile(r"\b(?:I|C|R|A|D|T|REF)-\d+\b|msg-[0-9a-f]{16}|\b\w+-[0-9a-f]{24}\b")
"""Ledger handles and the platform's IDs, whose digits are names, not numbers."""


def refused(message: str) -> DocumentError:
    return DocumentError(POLICY_DENIED, f"provenance_required: {message}")


def normal(text: str) -> str:
    """``text`` with numbers in one notation (CM-20): no thousands separators, and powers
    as ``2^80`` however they were written (``2^{80}``, ``2**80``, ``2⁸⁰``)."""
    text = _SUPERSCRIPTS.sub(lambda m: "^" + m[1].translate(_SUPERSCRIPT), text)
    text = _POWER.sub(lambda m: "^" + (m[1] or m[2]), text)
    return _SEPARATED.sub("", text)


def numbers(text: str) -> set[str]:
    """The numbers ``text`` holds, normalized, leaving out ledger handles and IDs."""
    return set(_NUMBER.findall(normal(_NAMES.sub(" ", text))))


def values(fields: Json) -> Iterable[str]:
    """Every text and number of a revision's fields, keys aside, as text."""
    stack: list[Any] = [fields]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, list):
            stack.extend(value)  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, float) and value.is_integer():
            yield str(int(value))
        else:
            yield str(value)


def resolve(
    db: sqlite3.Connection,
    project: str,
    anchors: list[AnchorInput],
    block: Callable[[str, int, str], str],
    find: Callable[[str], tuple[str, int]],
) -> list[Json]:
    """Each anchor as the record keeps it, checked to exist in the project: an archive
    message by its ID or a tool result by its call, an artifact revision with its
    SHA-256, a document block at a revision by the document's ID, or an entry revision."""
    kept: list[Json] = []
    for anchor in anchors:
        try:
            kept.append(_resolved(db, project, anchor, block, find))
        except DocumentError as error:
            raise refused(f"an anchor is not in this project: {error.message}") from None
    return kept


def _resolved(
    db: sqlite3.Connection,
    project: str,
    anchor: AnchorInput,
    block: Callable[[str, int, str], str],
    find: Callable[[str], tuple[str, int]],
) -> Json:
    if anchor.message is not None or anchor.call is not None:
        row = db.execute(
            "SELECT message_id,role,kind,length(content) FROM archive_messages "
            "WHERE project=? AND (message_id=? OR (call_id=? AND kind='tool_result')) "
            "ORDER BY position DESC LIMIT 1",
            (project, anchor.message, anchor.call),
        ).fetchone()
        if row is None:
            raise refused(f"no archive message {anchor.message or anchor.call} in this project")
        found: Json = {"message": row[0], "kind": row[2]}
        if anchor.quote is not None:
            found["quote"] = anchor.quote
        if anchor.start is not None or anchor.end is not None:
            if row[2] != "tool_result":
                raise refused(f"{row[0]} is no tool result: a range is of a tool's output")
            found.update(start=anchor.start or 0, end=anchor.end or row[3] or 0)
        return found
    if anchor.artifact is not None:
        assert anchor.revision is not None  # noqa: S101 - the model requires it
        digest = evidence.retained(db, project, anchor.artifact, anchor.revision, anchor.sha256)
        return {"artifact": anchor.artifact, "revision": anchor.revision, "sha256": digest}
    if anchor.document is not None:
        assert anchor.revision is not None  # noqa: S101 - the model requires it
        assert anchor.block is not None  # noqa: S101 - the model requires it
        document = block(anchor.document, anchor.revision, anchor.block)
        return {"document": document, "revision": anchor.revision, "block": anchor.block}
    assert anchor.entry is not None  # noqa: S101 - one target is named
    entry, head = find(anchor.entry)
    revision = anchor.revision or head
    if not 1 <= revision <= head:
        raise refused(f"no entry {anchor.entry} at revision {revision} in this project")
    return {"entry": entry, "revision": revision}


def check(
    db: sqlite3.Connection,
    project: str,
    basis: str,
    anchors: list[Json],
    held: list[EvidenceLink],
    texts: list[str],
    where: str,
) -> None:
    """Refuse ``where``, a revision or work record of ``basis`` with ``anchors`` and the
    evidence it holds, when its basis is not borne out: a ``user_quoted`` one quotes no
    message of the user's verbatim; an ``observed`` one has no observation, or holds a
    number, in ``texts``, that none of its observations does."""
    if basis == "user_quoted":
        quoted = [a for a in anchors if a.get("kind") in ("user", "comment") and "quote" in a]
        for anchor in quoted:
            (content,) = db.execute(
                "SELECT content FROM archive_messages WHERE project=? AND message_id=?",
                (project, anchor["message"]),
            ).fetchone()
            said, quote = _lines(content or ""), _lines(anchor["quote"])
            wanted = min(MIN_QUOTE, len(said.strip()))
            if len(quote.strip()) >= wanted and quote in said:
                return
        raise refused(
            f"{where} is user_quoted: it anchors on a message the user wrote, with a quote of "
            f"at least {MIN_QUOTE} characters of it (or all of a shorter one), word for word"
        )
    if basis != "observed":
        return
    seen = _observations(db, project, anchors, held)
    if not seen:
        raise refused(
            f"{where} is observed: it anchors on a tool result (or a range of its output), "
            "an artifact revision, or holds evidence pinned to an artifact or a quote"
        )
    found = set[str]().union(*(numbers(text) for text in seen))
    missing = sorted(set[str]().union(*(numbers(text) for text in texts)) - found)
    if missing:
        raise refused(
            f"{where} is observed, but {', '.join(missing[:5])} "
            f"{'is' if len(missing) == 1 else 'are'} in none of its observations: copy "
            "numbers from what was observed, or record it as interpreted"
        )


def _observations(
    db: sqlite3.Connection, project: str, anchors: list[Json], held: list[EvidenceLink]
) -> list[str]:
    """The texts of the observations a record rests on: its tool results (or the range
    of one), artifact revisions, and the artifacts and quotes its evidence pins."""
    seen: list[str] = []
    for anchor in anchors:
        if anchor.get("kind") == "tool_result":
            (content,) = db.execute(
                "SELECT content FROM archive_messages WHERE project=? AND message_id=?",
                (project, anchor["message"]),
            ).fetchone()
            text = content or ""
            if "start" in anchor:
                text = text[anchor["start"] : anchor["end"]]
            seen.append(text)
        elif "artifact" in anchor:
            seen.append(_artifact(db, project, anchor["artifact"], anchor["revision"]))
    for link in held:
        if link.target == "artifact" and link.revision is not None:
            seen.append(_artifact(db, project, link.target_id, link.revision))
        elif link.target == "quote" and link.locator is not None:
            seen.append(str(link.locator.get("text") or ""))
    return seen


def _artifact(db: sqlite3.Connection, project: str, artifact: str, revision: int) -> str:
    row = db.execute(
        "SELECT substr(data,1,?) FROM artifact_revisions WHERE project=? AND artifact=? "
        "AND number=?",
        (TEXT_BYTES, project, artifact, revision),
    ).fetchone()
    data = row[0] if row is not None else None
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else ""


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encoded(anchors: list[Json]) -> str:
    return json.dumps(anchors, sort_keys=True, ensure_ascii=False)
