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

- ``user_quoted``: an anchor on the user's own words (``users_words``) quotes at least 20
  characters of them, or all of a shorter message, verbatim, line ends normalized;
- ``observed``: an anchor on a tool result (whole, or a range of its output) or an
  artifact revision, or evidence pinned to an artifact or a quote of a reference's PDF or
  ePrint; every number in the statement and fields occurs in one of them, its notation
  normalized (``numbers``, as CM-20 reads numbers), and no digit is left unread;
- every anchor exists where its author may look (``Scope``).

It verifies that the words and numbers are where the record says, not what they mean.
The platform's own records are anchored on what made them: an import on its results
file's artifact revision and line, ``observed`` only when an execution produced it
(``executed``); a check's outcome on its delegation (``observed``). The user's changes
have the basis ``user``. Revisions written before show no basis.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sandbox_science.orchestrator.artifact_blobs import ArtifactBlobs, Blob
from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError
from sandbox_science.orchestrator.notices import requests
from sandbox_science.orchestrator.projects import operator_ref, run_id_of

from . import evidence
from .models import MIN_QUOTE, AnchorInput, EvidenceLink

Json = dict[str, Any]

TEXT_BYTES: Final = 4 * 1024 * 1024
"""The most bytes of an artifact revision the number check reads."""
CANDIDATES: Final = 50
"""The most tool results one call ID is looked for among."""

_SUPERSCRIPT: Final = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUBSCRIPT: Final = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_SUPERSCRIPTS: Final = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")
_EXPONENT: Final = r"[-+]?[0-9]+(?:\.[0-9]+)?"
_POWER: Final = re.compile(
    rf"\s*(?:\^|\*\*)\s*(?:\{{\s*({_EXPONENT})\s*\}}|\(\s*({_EXPONENT})\s*\)|({_EXPONENT}))"
)
_SPACED: Final = re.compile(r"(?<![0-9.])[0-9]{1,3}(?:[ \u00a0][0-9]{3})+(?![0-9]|\.[0-9])")
"""A number whose thousands are set apart by spaces, ``1 000 003``."""
_EXPRESSION: Final = re.compile(r"[0-9]\s*(?:\^|\*\*)\s*(?![-+]?[0-9])")
"""A power whose exponent is not a plain number, ``2^{80+1}``, once plain ones are read."""
_SEPARATED: Final = re.compile(r"(?<=[0-9])(?:[,\u2009\u202f_]|\{,\})(?=[0-9]{3}(?![0-9]))")
_SCIENTIFIC: Final = re.compile(r"(?<![\w.])([0-9]+(?:\.[0-9]+)?)[eE]([-+]?[0-9]+)(?!\w|\.[0-9])")
_TIMES_TEN: Final = re.compile(
    r"(?<![\w.^])([0-9]+(?:\.[0-9]+)?)\s*(?:\\times|\\cdot|[\u00d7\u00b7\u22c5*])\s*10\^(-?[0-9]+)(?![0-9]|\.[0-9])"
)
_NUMBER: Final = re.compile(
    r"0[xX][0-9a-fA-F]+|[0-9]+(?:\.[0-9]+)*(?:e-?[0-9]+|\^-?[0-9]+(?:\.[0-9]+)?)?"
)
_NAMES: Final = re.compile(r"\b(?:I|C|R|A|D|T|REF)-[0-9]+\b|msg-[0-9a-f]{16}|\b\w+-[0-9a-f]{24}\b")
"""Ledger handles and the platform's IDs, whose digits are names, not numbers."""
_MESSAGE_ID: Final = re.compile(r"msg-[0-9a-f]{16}")


@dataclass(frozen=True)
class Scope:
    """The archive an agent's anchors may point into (CM-37, PD-13): a main run's, its
    session's runs, delegated ones included; a delegated run's, its own run and the
    messages its brief names as inputs. The user's and the platform's, the project's."""

    session: str | None = None
    run: str | None = None
    """The author's run, by its ID: a call ID is looked for there first."""
    delegated: bool = False
    inputs: frozenset[str] = frozenset()
    """The archive messages a delegated run's brief names."""

    def sees(self, session: str, run: str, message: str) -> bool:
        if self.session is not None and session != self.session:
            return False
        return not self.delegated or run_id_of(run) == self.run or message in self.inputs


def briefed(brief: str) -> frozenset[str]:
    """The archive messages a delegation's brief, as kept, names among its inputs."""
    try:
        inputs: object = json.loads(brief).get("inputs")
    except (ValueError, AttributeError):
        return frozenset()
    return frozenset(_MESSAGE_ID.findall(json.dumps(inputs)))


PREFIX: Final = "provenance_required: "


def refused(message: str) -> DocumentError:
    return DocumentError(POLICY_DENIED, PREFIX + message)


def normal(text: str) -> str:
    """``text`` with numbers in one notation (CM-20): digits as ASCII, no thousands
    separators; powers as ``2^80`` however they were written (``2^{80}``, ``2 ** 80``,
    ``2^(80)``, ``2⁸⁰``); scientific notation as ``1.2e-5`` (``1.2 \\times 10^{-5}``,
    ``1.2E-05``), and as ``10^24`` when its mantissa is 1."""
    text = _SUPERSCRIPTS.sub(lambda m: "^" + m[0].translate(_SUPERSCRIPT), text)
    text = "".join(
        str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c
        for c in text.translate(_SUBSCRIPT)
    )
    text = _POWER.sub(lambda m: "^" + (m[1] or m[2] or m[3]).removeprefix("+"), text)
    text = _SPACED.sub(lambda m: re.sub(r"[ \u00a0]", "", m[0]), text)
    text = _SEPARATED.sub("", text)
    text = _SCIENTIFIC.sub(lambda m: _scientific(m[1], m[2]), text)
    return _TIMES_TEN.sub(lambda m: _scientific(m[1], m[2]), text)


def _scientific(mantissa: str, exponent: str) -> str:
    power = int(exponent)
    return f"10^{power}" if float(mantissa) == 1 else f"{mantissa}e{power}"


def numbers(text: str) -> set[str]:
    """The numbers ``text`` holds, normalized, leaving out ledger handles and IDs; digits
    next to letters count (``80bit``, ``AES-128``)."""
    return {n.lower() for n in _NUMBER.findall(normal(_NAMES.sub(" ", text)))}


def unread(text: str) -> str | None:
    """What of ``text`` holds a number ``numbers`` does not read, if anything, with what
    surrounds it: a power whose exponent is no plain number (``2^{80+1}``), or a digit or
    numeral left over (``①``, ``½``, ``Ⅷ``)."""
    read = normal(_NAMES.sub(" ", text))
    found = _EXPRESSION.search(read)
    if found is not None:
        return read[max(0, found.start() - 10) : found.end() + 10].strip()
    rest = _NUMBER.sub(" ", read)
    for index, c in enumerate(rest):
        if c.isdigit() or c.isnumeric():
            return rest[max(0, index - 10) : index + 10].strip()
    return None


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
    scope: Scope | None = None,
) -> list[Json]:
    """Each anchor as the record keeps it, checked to exist where its author may look
    (``scope``): an archive message by its ID or a tool result by its call, an artifact
    revision with its SHA-256, a document block at a revision by the document's ID, or
    an entry revision, or a project memory entry at a revision (``mem-3@2``)."""
    kept: list[Json] = []
    for anchor in anchors:
        try:
            kept.append(_resolved(db, project, anchor, block, find, scope or Scope()))
        except DocumentError as error:
            if error.message.startswith(PREFIX):
                raise
            raise refused(f"an anchor is not in this project: {error.message}") from None
    return kept


def _resolved(
    db: sqlite3.Connection,
    project: str,
    anchor: AnchorInput,
    block: Callable[[str, int, str], str],
    find: Callable[[str], tuple[str, int]],
    scope: Scope,
) -> Json:
    if anchor.message is not None or anchor.call is not None:
        row = _archived(db, project, anchor, scope)
        found: Json = {"message": row[0], "kind": row[1]}
        if anchor.quote is not None:
            found["quote"] = anchor.quote
        if anchor.start is not None or anchor.end is not None:
            if row[1] != "tool_result":
                raise refused(f"{row[0]} is no tool result: a range is of a tool's output")
            found.update(start=anchor.start or 0, end=anchor.end or row[2] or 0)
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
    if anchor.memory is not None:
        return _remembered(db, project, anchor.memory)
    assert anchor.entry is not None  # noqa: S101 - one target is named
    entry, head = find(anchor.entry)
    revision = anchor.revision or head
    if not 1 <= revision <= head:
        raise refused(f"no entry {anchor.entry} at revision {revision} in this project")
    return {"entry": entry, "revision": revision}


def _remembered(db: sqlite3.Connection, project: str, name: str) -> Json:
    """A memory entry at a revision (PM-13), which must exist: a version of the entry with
    that revision holds text. It is an anchor that exists and no more: the number check
    reads tool results, artifacts and PDFs only, so it never makes a change observed."""
    entry, _, revision = name.partition("@")
    found = db.execute(
        "SELECT 1 FROM memory_versions WHERE project=? AND entry_id=? AND revision=? "
        "AND text IS NOT NULL LIMIT 1",
        (project, entry, int(revision)),
    ).fetchone()
    if found is None:
        raise refused(f"no memory entry {name} in this project")
    return {"memory": entry, "revision": int(revision)}


def _archived(
    db: sqlite3.Connection, project: str, anchor: AnchorInput, scope: Scope
) -> tuple[str, str, int | None]:
    """The archive message an anchor names, as its author may see it: by its ID, or the
    tool result of its call, in the author's own run first, then in the rest of what it
    may see, where it must be the only one. What it may not see reads as missing."""
    if anchor.message is not None:
        rows = db.execute(
            "SELECT message_id,kind,length(content),session,run FROM archive_messages "
            "WHERE project=? AND message_id=?",
            (project, anchor.message),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT message_id,kind,length(content),session,run FROM archive_messages "
            "WHERE project=? AND call_id=? AND kind='tool_result' AND (? IS NULL OR session=?) "
            "ORDER BY coalesce(substr(run, -1 - length(?)) = '-' || ?, 0) DESC LIMIT ?",
            (project, anchor.call, scope.session, scope.session, scope.run, scope.run, CANDIDATES),
        ).fetchall()
    seen = [r for r in rows if scope.sees(r[3], r[4], r[0])]
    if not seen and anchor.call is not None and _MESSAGE_ID.fullmatch(anchor.call):
        # A tool result's message ID, where its call ID was meant: it names that result.
        # Models read the ID in the result's header, and the call ID only in the history.
        by_id = db.execute(
            "SELECT message_id,kind,length(content),session,run FROM archive_messages "
            "WHERE project=? AND message_id=?",
            (project, anchor.call),
        ).fetchall()
        seen = [r for r in by_id if scope.sees(r[3], r[4], r[0])]
        if seen and seen[0][1] != "tool_result":
            raise refused(
                f"{anchor.call} is the ID of a {seen[0][1]} message, not of a tool result: "
                f'anchor it as {{"message": "{anchor.call}"}}, or anchor a tool result by '
                "its call ID or its own message ID"
            )
    own = [r for r in seen if scope.run is not None and run_id_of(r[4]) == scope.run]
    found = own or seen
    name = anchor.message or f"tool result of call {anchor.call}"
    if not found:
        where = "this project" if scope.session is None else "what this run may see"
        hint = (
            " (it looks like a message ID, and none is visible; a call is anchored by "
            'its call ID, a message by {"message": …})'
            if anchor.call is not None and _MESSAGE_ID.fullmatch(anchor.call)
            else ""
        )
        raise refused(f"no archive message {name} in {where}{hint}")
    if len(found) > 1:
        raise refused(
            f"call {anchor.call} has a tool result in more than one run: anchor on the "
            "message ID its header shows instead"
        )
    return found[0][0], found[0][1], found[0][2]


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
    evidence it holds, when its basis is not borne out: a ``user_quoted`` one has no
    quote, or a quote that is not the user's words verbatim; another holds a quote its
    message does not; an ``observed`` one has no observation, holds a number, in
    ``texts``, that none of its observations does, or digits the check cannot read."""
    quoted = [a for a in anchors if "quote" in a]
    if basis == "user_quoted":
        # Every quote, not one of them: a real quote must not carry invented ones.
        failed = next((a for a in quoted if not _users_quote(db, project, a)), None)
        if not quoted or failed is not None:
            which = "" if failed is None else f" ({failed['message']} does not hold its quote)"
            raise refused(
                f"{where} is user_quoted: it anchors on a message the user wrote (a chat "
                "message, a run's task from the user's messages, or a comment's request), "
                f"with a quote of at least {MIN_QUOTE} characters of it (or all of a shorter "
                f"one), word for word, and every quote it has is one{which}"
            )
    else:
        for anchor in quoted:
            # Elsewhere a quote says only that its text appears where it points.
            if _lines(anchor["quote"]) not in _lines(_quoted_text(db, project, anchor)):
                raise refused(
                    f"{where} quotes {anchor['message']}, which does not hold those words: "
                    "quote it word for word, or leave the quote out"
                )
    if basis != "observed":
        return
    seen = _observations(db, project, anchors, held)
    if not seen:
        raise refused(
            f"{where} is observed: it anchors on a tool result (or a range of its output), "
            "an artifact revision, or holds evidence pinned to an artifact or a quote"
        )
    for text in texts:
        left = unread(text)
        if left is not None:
            raise refused(
                f"{where} is observed, but the check cannot read the number in "
                f"{left!r}: write numbers in digits (2^80, 1.2e-5, 1000000), or record it "
                "as interpreted"
            )
    found = set[str]().union(*(numbers(text) for text in seen))
    missing = sorted(set[str]().union(*(numbers(text) for text in texts)) - found)
    if missing:
        raise refused(
            f"{where} is observed, but {', '.join(missing[:5])} "
            f"{'is' if len(missing) == 1 else 'are'} in none of its observations: copy "
            "numbers from what was observed, or record it as interpreted"
        )


_USERS: Final = ("user", "task", "comment")
"""The archive's kinds that can hold the user's own words."""


def _users_quote(db: sqlite3.Connection, project: str, anchor: Json) -> bool:
    """Whether ``anchor`` is on the user's own words and its quote is found in them."""
    if anchor.get("kind") not in _USERS:
        return False
    quote = _lines(anchor["quote"])
    for said in map(_lines, users_words(db, project, anchor["message"])):
        wanted = min(MIN_QUOTE, len(said.strip()))
        if len(quote.strip()) >= wanted and quote in said:
            return True
    return False


def _quoted_text(db: sqlite3.Connection, project: str, anchor: Json) -> str:
    """The text an anchor's quote is looked for in: the user's words of a message that
    holds them, or the message as archived, a tool result's range only."""
    if anchor.get("kind") in _USERS:
        return "\n\n".join(users_words(db, project, anchor["message"]))
    row = db.execute(
        "SELECT content FROM archive_messages WHERE project=? AND message_id=?",
        (project, anchor["message"]),
    ).fetchone()
    text = (row[0] if row is not None else None) or ""
    return text[anchor["start"] : anchor["end"]] if "start" in anchor else text


def users_words(db: sqlite3.Connection, project: str, message: str) -> list[str]:
    """The user's own words an archive message holds, each apart: a chat message the run
    received (``user``); the chat messages a main run's task is made of (``task``, by the
    chat messages it names: a delegation's brief and the stand-in for comments name
    none), or the task the operator typed at the command line, which names its own run;
    the request text of each comment a notice delivered (``comment``), not the platform's
    framing or the document text beside it, which an agent may have written."""
    row = db.execute(
        "SELECT kind,content,refs,session,run FROM archive_messages "
        "WHERE project=? AND message_id=?",
        (project, message),
    ).fetchone()
    if row is None:
        return []
    kind, content, refs, session, run = row
    if kind == "user":
        return [content or ""]
    if kind == "comment":
        return [str(r["request_text"]) for r in requests(content or "") if "request_text" in r]
    if kind != "task":
        return []
    named = [ref for ref in json.loads(refs) if isinstance(ref, str)]
    if operator_ref(run) in named:
        return [content or ""]
    return [
        text
        for (text,) in db.execute(
            "SELECT text FROM session_messages WHERE project_id=? AND session_id=? "
            "AND message_id IN (SELECT value FROM json_each(?)) ORDER BY number",
            (project, session, json.dumps(named)),
        )
    ]


def executed(
    db: sqlite3.Connection, project: str, artifact: str, revision: int, sha256: str
) -> bool | None:
    """Whether an artifact revision holding ``sha256`` has an execution's lineage: the
    worker reported it written by an execution that succeeded (RA-34, RA-11). ``None``
    when the project has no such revision with that content."""
    row = db.execute(
        "SELECT json_extract(record,'$.reference.sha256'),"
        "json_extract(record,'$.publication.kind'),"
        "json_extract(record,'$.publication.execution_id'),"
        "json_extract(record,'$.publication.attempt_state') "
        "FROM artifact_revisions WHERE project=? AND artifact=? AND number=?",
        (project, artifact, revision),
    ).fetchone()
    if row is None or row[0] != sha256:
        return None
    return row[1] == "execution_record" and row[2] is not None and row[3] == "succeeded"


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
        "SELECT substr(r.data,1,?),b.sha256,b.size FROM artifact_revisions r "
        "LEFT JOIN artifact_revision_blobs b ON b.project=r.project AND b.artifact=r.artifact "
        "AND b.number=r.number WHERE r.project=? AND r.artifact=? AND r.number=?",
        (TEXT_BYTES, project, artifact, revision),
    ).fetchone()
    data = row[0] if row is not None else None
    if row is not None and row[1] is not None:
        database = next(
            path for _, name, path in db.execute("PRAGMA database_list") if name == "main"
        )
        data = ArtifactBlobs(Path(database)).prefix(Blob(row[1], row[2]), TEXT_BYTES)
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else ""


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def label(anchor: Json) -> str:
    """An anchor as one short name: a message by its ID (with the range of an output), an
    artifact revision, a document block at a revision, an entry at a revision, or a memory
    entry as ``mem-3@2`` (PM-13)."""
    if "memory" in anchor:
        return f"{anchor['memory']}@{anchor['revision']}"
    if "message" in anchor:
        span = f"[{anchor['start']}:{anchor['end']}]" if "start" in anchor else ""
        return f"{anchor['message']}{span}"
    if "artifact" in anchor:
        return f"{anchor['artifact']}@{anchor['revision']}"
    if "document" in anchor:
        return f"{anchor['document']}#{anchor['block']}@{anchor['revision']}"
    if "entry" in anchor:
        return f"{anchor['entry']}@{anchor['revision']}"
    return next(iter(map(str, anchor.values())), "")


def encoded(anchors: list[Json]) -> str:
    return json.dumps(anchors, sort_keys=True, ensure_ascii=False)
