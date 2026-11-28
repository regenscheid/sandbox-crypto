# SPDX-License-Identifier: MIT
"""Bounded, scoped reads used to prepare a review snapshot.

These reads own no review transaction, lease, watermark update or call accounting.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlStore

from .actions import Earlier
from .snapshot import (
    MAX_ITEM_BYTES,
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FINDINGS,
    MAX_SNAPSHOT_ITEMS,
    PRIOR_BYTES,
    PRIOR_ITEMS,
    EvidenceItem,
    SnapshotWindow,
)
from .values import Json, digest, dump, stored_json


def revision_context(found: Json) -> Json:
    """A document observation's revision and changed blocks, for the actions item."""
    return {
        "document": found.get("document"),
        "revision": found.get("revision"),
        **(found.get("blocks") or {}),
    }


def fix_actions(
    control: ControlStore, project: str, session: str, findings: Sequence[str]
) -> EvidenceItem | None:
    """The actions item of what the session's runs did since the reviews that raised
    ``findings`` ended, and the blocks each document revision since changed; None when
    there are none."""
    from .actions import MAX_ACTIONS, actions_item

    with control.read() as tx:
        db = tx.sql
        (start,) = db.execute(
            "SELECT coalesce(min(r.end_seq),0) FROM review_findings f JOIN review_runs r "
            "ON r.review_id=f.review_id WHERE f.finding_id IN (SELECT value FROM json_each(?))",
            (json.dumps(findings),),
        ).fetchone()
        rows = db.execute(
            "SELECT seq,source_key,kind,locator,payload FROM review_feed WHERE project=? "
            "AND session=? AND seq>? AND (kind='action' OR (kind='document' "
            "AND source_key GLOB 'document:*')) ORDER BY seq LIMIT ?",
            (project, session, start, 2 * MAX_ACTIONS),
        ).fetchall()
    if not rows:
        return None
    actions = [
        (stored_json(locator), stored_json(payload))
        for _, _, kind, locator, payload in rows
        if kind == "action"
    ]
    revised = [
        revision_context(stored_json(payload))
        for _, _, kind, _, payload in rows
        if kind == "document"
    ]
    located, listed = actions_item(actions, revised, start + 1, rows[-1][0])
    encoded = dump(listed)
    return EvidenceItem("actions", dump(located), encoded, digest(encoded), 1)


def dispatch_window(db: sqlite3.Connection, project: str, session: str) -> SnapshotWindow:
    """The session's watermark, its failed window's retry end, and the end a review
    covers: that retry end, or the feed's last item."""
    found = db.execute(
        "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
        (project, session),
    ).fetchone()
    watermark, retry, failures = found if found else (0, None, 0)
    end = (
        retry
        if retry is not None
        else db.execute(
            "SELECT coalesce(max(seq),?) FROM review_feed WHERE project=? AND session=?",
            (watermark, project, session),
        ).fetchone()[0]
    )
    return SnapshotWindow(watermark, retry, end, failures)


def earlier(
    db: sqlite3.Connection, project: str, session: str, window: SnapshotWindow, run: str
) -> Earlier | None:
    """Run ``run``'s actions and document revisions in its windows reviewed before
    ``window``: those the feed holds from the run's first observation to the
    watermark, its delegated runs' included, the latest twice ``MAX_ACTIONS`` of them;
    None when the run had no observation before."""
    from .actions import MAX_ACTIONS, Earlier

    (first,) = db.execute(
        "SELECT min(seq) FROM review_feed WHERE project=? AND session=? AND seq<=? "
        "AND json_extract(locator,'$.run')=?",
        (project, session, window.watermark, run),
    ).fetchone()
    if first is None:
        return None
    # The run's own actions, its delegated runs' (brought for its review), and the
    # document revisions they made: not another run's, nor work brought for one.
    rows = [
        (kind, stored_json(locator), payload)
        for kind, locator, payload in db.execute(
            "SELECT kind,locator,payload FROM (SELECT seq,kind,locator,payload FROM "
            "review_feed WHERE project=? AND session=? AND seq>=? AND seq<=? AND "
            "((kind='action' AND (json_extract(locator,'$.run')=? OR "
            "json_extract(locator,'$.for_run')=?)) OR (kind='document' AND "
            "source_key GLOB 'document:*')) ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            (project, session, first, window.watermark, run, run, 2 * MAX_ACTIONS),
        )
    ]
    runs = {run} | {loc.get("run") for _, loc, _ in rows if loc.get("for_run") == run}
    return Earlier(
        [(loc, stored_json(found)) for kind, loc, found in rows if kind == "action"],
        [
            revision_context(stored_json(found))
            for kind, loc, found in rows
            if kind == "document" and loc.get("run") in runs
        ],
        first,
        window.watermark,
    )


def viewed(
    db: sqlite3.Connection, project: str, session: str, viewed: Sequence[tuple[str, str]]
) -> list[tuple[Json, Json]]:
    """The actions of the earlier runs' calls ``viewed``, each its run's ID and its
    own, as the feed holds them, in order, the latest ``MAX_ACTIONS``."""
    from .actions import MAX_ACTIONS

    wanted = set(viewed)
    if not wanted:
        return []
    found: list[tuple[Json, Json]] = []
    for locator, payload in db.execute(
        "SELECT locator,payload FROM review_feed WHERE project=? AND session=? AND "
        "kind='action' AND json_extract(locator,'$.run') IN (SELECT value FROM "
        "json_each(?)) ORDER BY seq",
        (project, session, json.dumps(sorted({run for run, _ in wanted}))),
    ):
        located, action = stored_json(locator), stored_json(payload)
        if "delegation" not in located and (located.get("run"), action.get("call")) in wanted:
            found.append((located, action))
    return found[-MAX_ACTIONS:]


def feed_items(
    db: sqlite3.Connection,
    project: str,
    session: str,
    window: SnapshotWindow,
    earlier: Earlier | None = None,
    room: tuple[int, int] | None = None,
    *,
    ceiling_items: int = MAX_SNAPSHOT_ITEMS,
    prior_items: int = PRIOR_ITEMS,
    prior_bytes: int = PRIOR_BYTES,
) -> tuple[int, list[EvidenceItem]]:
    """The feed's next items after the watermark, as many as ``room``, the bytes and
    items the snapshot leaves them, allows, and the last one's sequence number. What a
    tool call read for a quote (a raw ``source``) is not an item: the transcript holds
    the result the agent saw, and only a source a quote cites comes in, pinned beside
    its entry (``quoted:``), so a long read that no quote cites leaves the coverage
    whole (CR-4). The actions item lists the window's actions, as many as
    ``MAX_ACTIONS``, and ``earlier``'s as context; the rows it folds take no item.

    A long delegation can cross windows. Each later piece brings the pinned outcome
    back as context, so an execution is never presented without its claim."""
    from .actions import MAX_ACTIONS, actions_item

    max_bytes, max_items = room or (MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_ITEMS)
    items: list[EvidenceItem] = []
    actions: list[tuple[Json, Json]] = []
    revised: list[Json] = []
    size, new_items, end = 0, 0, window.watermark
    first = db.execute(
        "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
        "seq>? AND seq<=? ORDER BY seq LIMIT 1",
        (project, session, window.watermark, window.end),
    ).fetchone()
    if window.watermark and first is not None:
        # A claim and its contradicting observation can straddle two windows.
        # Carry recent items as context; only rows after the watermark
        # advance coverage. A prior ledger entry is never a newly reviewed
        # ledger item, even if the reviewer uses it to find a contradiction.
        carried: list[EvidenceItem] = []
        prior_size = 0
        prior_limit = max(1, prior_items // (4 ** min(window.failures, 2)))
        byte_limit = max(1, prior_bytes // (4 ** min(window.failures, 2)))
        for kind, locator, payload, sha, complete in db.execute(
            "SELECT kind,locator,payload,sha256,complete FROM review_feed WHERE "
            "project=? AND session=? AND seq<=? AND "
            "kind!='action' AND "
            "(kind!='source' OR source_key GLOB 'quoted:*') "
            "ORDER BY seq DESC LIMIT ?",
            (project, session, window.watermark, prior_limit),
        ):
            item = EvidenceItem(
                "context" if kind == "ledger" else kind,
                dump({"prior_kind": kind, "locator": stored_json(locator)})
                if kind == "ledger"
                else locator,
                payload,
                sha,
                complete,
            )
            if prior_size + item.size > byte_limit:
                if not carried:
                    marker = dump(
                        {
                            "prior_kind": kind,
                            "sha256": sha,
                            "bytes": item.size,
                            "omitted": "prior item exceeds carry allowance",
                        }
                    )
                    carried.append(EvidenceItem("context", locator, marker, digest(marker), 1))
                break
            if len(carried) >= max(0, ceiling_items - 1):
                break
            carried.append(item)
            prior_size += item.size
        items.extend(reversed(carried))
        size += prior_size
    if first is not None:
        parts = str(first[0]).split(":", 2)
        if len(parts) == 3 and parts[0] == "delegated":
            prior_row = db.execute(
                "SELECT kind,locator,payload,sha256,complete FROM review_feed WHERE "
                "project=? AND session=? AND source_key=? AND seq<=?",
                (project, session, ":".join(parts[:2]), window.watermark),
            ).fetchone()
            if prior_row is not None:
                kind, locator, payload, sha, complete = prior_row
                context = EvidenceItem(kind, locator, payload, sha, complete)
                if context not in items:
                    items.append(context)
                    size += context.size
    # A raw source's row is passed over without reading its payload, which may be
    # 256 KiB: it takes no item and only moves the window's end.
    for seq, key, kind, locator, payload, sha, complete in db.execute(
        "SELECT seq,source_key,kind,locator,CASE WHEN kind='source' AND source_key NOT "
        "GLOB 'quoted:*' THEN '' ELSE payload END,sha256,complete FROM review_feed "
        "WHERE project=? AND session=? AND seq>? AND seq<=? ORDER BY seq",
        (project, session, window.watermark, window.end),
    ):
        key = str(key)
        raw = kind == "source" and not key.startswith("quoted:")
        folded = raw or kind == "action"
        item = None if folded else EvidenceItem(kind, locator, payload, sha, complete)
        # Always take one new row, even when its pinned payload alone exceeds the
        # target. Otherwise a large first item would strand the feed forever.
        full = (
            len(actions) >= min(MAX_ACTIONS, max_items)
            if kind == "action"
            else item is not None
            and end > window.watermark
            and (
                size + item.size > max_bytes - min(16 * 1024, max_bytes // 8)
                or new_items >= max_items
                or len(items) >= ceiling_items
            )
        )
        if full:
            break
        end = seq
        if raw:
            continue
        if item is None:
            # One derived item lists the window's actions, after the others.
            actions.append((stored_json(locator), stored_json(payload)))
            continue
        items.append(item)
        size += item.size
        new_items += 1
        if kind == "document" and key.startswith("document:"):
            revised.append(revision_context(stored_json(payload)))
    if (
        actions
        or revised
        or (earlier is not None and (earlier.actions or earlier.documents or earlier.runs))
    ):
        located, listed = actions_item(actions, revised, window.watermark + 1, end, earlier)
        encoded = dump(listed)
        items.append(EvidenceItem("actions", dump(located), encoded, digest(encoded), 1))
    # A delegated outcome is a claim about its whole batch. The partial pieces
    # remain reviewable for concrete contradictions, but cannot pass as complete.
    if first is not None:
        initial = str(first[0]).split(":", 2)
        last = db.execute(
            "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
            "seq<=? AND seq>? ORDER BY seq DESC LIMIT 1",
            (project, session, end, window.watermark),
        ).fetchone()
        following = db.execute(
            "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
            "seq>? AND seq<=? ORDER BY seq LIMIT 1",
            (project, session, end, window.end),
        ).fetchone()
        tail = str(last[0]).split(":", 2) if last is not None else []
        unfinished = (
            len(tail) >= 2
            and tail[0] == "delegated"
            and following is not None
            and str(following[0]).startswith(":".join(tail[:2]) + ":")
        )
        if (len(initial) == 3 and initial[0] == "delegated") or unfinished:
            items = [
                replace(item, complete=0) if item.kind == "delegation" else item for item in items
            ]
    return end, items


_FOLDED = "(kind='action' OR (kind='source' AND source_key NOT GLOB 'quoted:*'))"
"""Feed rows that are not items one by one: actions, which the actions item lists, and
raw sources, which only a quote brings in."""


def backlog(db: sqlite3.Connection, project: str, session: str, after: int) -> int:
    """How many new items the session's feed holds after position ``after``: its rows but
    actions and raw sources (``_FOLDED``)."""
    (count,) = db.execute(
        f"SELECT count(*) FROM review_feed WHERE project=? AND session=? AND seq>? "  # noqa: S608 - fixed fragment
        f"AND NOT {_FOLDED}",
        (project, session, after),
    ).fetchone()
    return int(count)


def backlog_skip(
    db: sqlite3.Connection, project: str, session: str, window: SnapshotWindow, items: int
) -> int | None:
    """For a checkpoint whose window starts with more than ``BACKLOG_WINDOWS`` windows of
    ``items`` new items, the last position before the newest window, which it skips to
    (CR-49); None when the backlog is smaller or a failed window waits for its retry."""
    from .models import BACKLOG_WINDOWS

    if window.retry is not None or window.failures:
        return None
    (count,) = db.execute(
        f"SELECT count(*) FROM review_feed WHERE project=? AND session=? AND seq>? AND seq<=? "  # noqa: S608 - fixed fragment
        f"AND NOT {_FOLDED}",
        (project, session, window.watermark, window.end),
    ).fetchone()
    if count <= BACKLOG_WINDOWS * items:
        return None
    (skip,) = db.execute(
        f"SELECT seq FROM review_feed WHERE project=? AND session=? AND seq>? AND seq<=? "  # noqa: S608 - fixed fragment
        f"AND NOT {_FOLDED} ORDER BY seq DESC LIMIT 1 OFFSET ?",
        (project, session, window.watermark, window.end, items),
    ).fetchone()
    return int(skip)


def reviewed_ranges(db: sqlite3.Connection, project: str, session: str) -> list[tuple[int, int]]:
    """The feed ranges the session's author-work reviews covered with a verdict that read
    what they were given (CR-31, CR-49): each from the first position it covered to the
    last, merged and in order. An inconclusive review covered none; a record from before
    coverage named its first position covered from its window's start."""
    found: list[tuple[int, int]] = []
    for start, payload in db.execute(
        "SELECT r.start_seq,c.payload FROM review_runs r JOIN review_records c "
        "ON c.review_id=r.review_id AND c.kind='coverage' WHERE r.project=? AND r.session=? "
        "AND r.kind IN ('checkpoint','terminal') "
        "AND r.state NOT IN ('inconclusive','failed','dropped','running')",
        (project, session),
    ):
        coverage = stored_json(payload)
        through, first = coverage.get("through"), coverage.get("from", start)
        if coverage.get("verdict") and isinstance(through, int) and isinstance(first, int):
            found.append((first, through))
    return merged(found)


QUESTION_CHARACTERS: Final = 600
"""The most characters of a question's text, the agent's, a request item copies."""
DETAIL_CHARACTERS: Final = 200
"""The most characters of an option's detail, the agent's, a request item copies."""
STUB_CHARACTERS: Final = 240
"""How much of a user's message a request item keeps when the messages do not fit whole."""


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit] + " …", True)


def _delivered(db: sqlite3.Connection, project: str, session: str, message: str) -> str | None:
    """The archive message that delivered chat message ``message`` to a run, which a
    response cites it by (CR-42)."""
    found = db.execute(
        "SELECT message_id FROM archive_messages WHERE project=? AND session=? "
        "AND kind IN ('user','task') AND EXISTS(SELECT 1 FROM json_each(refs) WHERE value=?) "
        "ORDER BY kind='task',position LIMIT 1",
        (project, session, message),
    ).fetchone()
    return None if found is None else str(found[0])


def _answers(db: sqlite3.Connection, project: str, session: str) -> list[Json]:
    """The session's answered questions (PD-23), whole: the question and its options as
    the workspace showed them to the user, the one the agent recommended, what the user
    picked or wrote, or that the user left it to the agent, when, and the archive message
    of the ``ask_user`` result that carried it, which a response cites it by."""
    from sandbox_science.orchestrator.question_tool import ASK_USER

    found: list[Json] = []
    for question, run, call, text, options, asked, answer, closed, message in db.execute(
        "SELECT question_id,run,tool_call,text,options,asked_at,answer,closed_at,message_id "
        "FROM user_questions WHERE project_id=? AND session_id=? AND state='answered' "
        "ORDER BY closed_at,rowid",
        (project, session),
    ):
        given = cast(Json, json.loads(answer)) if answer else {}
        listed: list[Json] = []
        for option in cast(list[Json], json.loads(options)):
            shown: Json = {"label": option.get("label")}
            if option.get("detail") is not None:
                shown["detail"] = option["detail"]
            if option.get("recommended"):
                shown["recommended"] = True
            listed.append(shown)
        entry: Json = {
            "answer_to": question,
            "at": closed,
            "asked_at": asked,
            "question": text,
            "options": listed,
            "answer": given.get("kind"),
        }
        if given.get("picked"):
            entry["picked"] = given["picked"]
        if given.get("text") is not None:
            entry["text"] = given["text"]
        if message is not None:
            entry["message"] = message
        result = db.execute(
            "SELECT message_id FROM archive_messages WHERE project=? AND session=? AND run=? "
            "AND kind='tool_result' AND source=? AND call_id=? AND instr(content,?)>0 "
            "ORDER BY position LIMIT 1",
            (project, session, run, ASK_USER, call, question),
        ).fetchone()
        if result is not None:
            entry["archive"] = result[0]
        found.append(entry)
    return found


def _when(entry: Json) -> tuple[int, str]:
    """An entry's place in the request's order: by its time, which the message and the
    question stores both write as ISO 8601; one that cannot be read goes last."""
    from datetime import UTC, datetime

    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return (1, "")
    return (0, (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC).isoformat())


def entry_time(entry: Json) -> str:
    """An entry's time as UTC ISO 8601, for comparing; empty for none."""
    return _when(entry)[1]


def _size(entry: Json) -> int:
    return len(dump(entry).encode()) + 1


def request_entries(db: sqlite3.Connection, project: str, session: str) -> list[Json]:
    """Everything of the user's a review weighs, whole and in order (CR-48 as amended):
    the session's messages the user wrote, refused ones and the platform's aside, the
    first first, and the user's answers to the agent's questions, by time; each message
    with the archive message that delivered it to a run (``archive``). In a session with
    no chat messages, its first transcript message of role user, the task a run was
    started with, stands as the first message."""
    from sandbox_science.orchestrator.notices import PLATFORM_AUTHORS

    messages: list[Json] = []
    for message, text, author, created in db.execute(
        "SELECT message_id,text,author,created_at FROM session_messages "
        "WHERE project_id=? AND session_id=? AND state!='refused' ORDER BY number",
        (project, session),
    ):
        if author in PLATFORM_AUTHORS:
            continue
        entry: Json = {"message": message, "at": created, "text": text}
        delivered = _delivered(db, project, session, str(message))
        if delivered is not None:
            entry["archive"] = delivered
        messages.append(entry)
    answers = _answers(db, project, session)
    if not messages:
        first = db.execute(
            "SELECT payload FROM review_feed WHERE project=? AND session=? AND kind='transcript' "
            "AND json_extract(payload,'$.role')='user' ORDER BY seq LIMIT 1",
            (project, session),
        ).fetchone()
        if first is not None:
            messages = [{"message": None, "at": None, "text": stored_json(first[0])["content"]}]
    head, rest = (messages[:1], messages[1:])
    return head + sorted([*rest, *answers], key=_when)


def entry_id(entry: Json) -> str | None:
    """The ID the request item names an entry by: its chat message's or its question's."""
    found = entry.get("answer_to") or entry.get("message")
    return None if found is None else str(found)


def _shown(entry: Json) -> Json:
    """An entry as the request item first lists it: a message whole; an answer with the
    agent's question and option details clipped to ``QUESTION_CHARACTERS`` and
    ``DETAIL_CHARACTERS``, the user's pick or words whole."""
    if "answer_to" not in entry:
        return dict(entry)
    shown = dict(entry)
    question, cut = _clip(str(entry["question"]), QUESTION_CHARACTERS)
    shown["question"] = question
    options: list[Json] = []
    for option in cast(list[Json], entry.get("options") or []):
        copied = dict(option)
        if option.get("detail") is not None:
            copied["detail"], more = _clip(str(option["detail"]), DETAIL_CHARACTERS)
            cut = cut or more
        options.append(copied)
    shown["options"] = options
    if cut:
        shown["question_clipped"] = True
    return shown


def _summarised(entry: Json) -> Json:
    """An answer that picked the agent's option, or left it to the agent, in brief: the
    choice and the question's start; the user's own words are never summarised."""
    question, _ = _clip(str(entry["question"]), STUB_CHARACTERS // 2)
    brief: Json = {
        k: entry[k] for k in ("answer_to", "at", "archive", "answer", "picked") if k in entry
    }
    return {**brief, "question": question, "summarised": True}


def _stub(entry: Json) -> Json:
    """``entry`` with its text, and an answer's question, clipped to ``STUB_CHARACTERS``,
    saying how long each is whole (``characters``, ``question_characters``)."""
    stubbed = dict(entry)
    for key, length in (("text", "characters"), ("question", "question_characters")):
        whole = entry.get(key)
        if isinstance(whole, str) and len(whole) > STUB_CHARACTERS + 2 and length not in entry:
            stubbed[key] = _clip(whole, STUB_CHARACTERS)[0]
            stubbed[length] = len(whole)
            stubbed["clipped"] = True
    return stubbed


def _group(entries: Sequence[Json]) -> Json:
    """The oldest ``entries``, too many to list even clipped, as one range that names its
    first and last and how to read them (``review_request_read``)."""
    return {
        "group": len(entries),
        "messages": sum(1 for e in entries if "answer_to" not in e),
        "answers": sum(1 for e in entries if "answer_to" in e),
        "first": entry_id(entries[0]),
        "last": entry_id(entries[-1]),
        "from": entries[0].get("at"),
        "to": entries[-1].get("at"),
        "read_with": "review_request_read",
    }


def _grouped(
    entries: Sequence[Json], listed: Sequence[Json], sizes: Sequence[int], room: int
) -> tuple[list[Json], int]:
    """``listed`` within ``room``: the oldest entries clipped or in brief merged into
    ranges (``_group``), the whole short ones between them kept as they are; and, only if
    that is not enough, the oldest of all into one range. How many entries the ranges
    hold."""
    bound = _size(_group([{"message": "msg-" + "0" * 24, "at": "0" * 32}] * 2)) + 64

    def merge(count: int, every: bool) -> list[Json]:
        found: list[Json] = []
        run: list[Json] = []
        for index in range(count):
            if every or listed[index].get("clipped") or listed[index].get("summarised"):
                run.append(entries[index])
                continue
            if run:
                found.append(_group(run))
                run = []
            found.append(listed[index])
        if run:
            found.append(_group(run))
        return [*found, *listed[count:]]

    for every in (False, True):
        total, runs, inside = sum(sizes), 0, False
        for count in range(1, len(listed) + 1):
            index = count - 1
            reducible = every or listed[index].get("clipped") or listed[index].get("summarised")
            if reducible:
                total -= sizes[index]
                runs += 0 if inside else 1
            inside = bool(reducible)
            if total + runs * bound <= room:
                merged = merge(count, every)
                return merged, sum(e.get("group", 0) for e in merged)
    merged = merge(len(listed), True)
    return merged, len(listed)


def request_item(db: sqlite3.Connection, project: str, session: str) -> EvidenceItem | None:
    """The user's request as a review's context (CR-48 as amended): an index of every
    message the user wrote and every answer the user gave the agent's questions, each
    with its ID, time and the archive message a response cites it by, and as much of its
    text as fits ``REQUEST_BYTES``, a bound it always meets. Whole first; then answers
    that picked the agent's option or left it to the agent in brief; then the longest
    texts clipped first to ``STUB_CHARACTERS``, so a short message is never clipped
    before a longer one; then the oldest entries grouped as ranges that name their first
    and last. Whatever is clipped, in brief or grouped the reviewer reads whole with
    ``review_request_read``. None when there is nothing of the user's."""
    from .models import REQUEST_BYTES

    entries = request_entries(db, project, session)
    if not entries:
        return None
    answered = any("answer_to" in e for e in entries)
    payload: Json = {
        "about": (
            "What the user asked in this session, oldest first: the user's messages and "
            "the user's answers to the agent's questions (answer_to): context, not under "
            "review. Read any entry whole with review_request_read."
            if answered
            else "What the user asked in this session, oldest first: context, not under review."
        ),
        "messages": [],
    }
    room = REQUEST_BYTES - _size(payload) - 512
    listed = [_shown(e) for e in entries]
    sizes = [_size(e) for e in listed]

    def over() -> bool:
        return sum(sizes) > room

    if over():
        for index, entry in enumerate(entries):
            if "answer_to" in entry and entry.get("answer") != "free":
                listed[index] = _summarised(entry)
                sizes[index] = _size(listed[index])
    if over():
        longest = sorted(
            range(len(listed)),
            key=lambda i: -max(len(str(listed[i].get(k) or "")) for k in ("text", "question")),
        )
        for index in longest:
            if not over():
                break
            listed[index] = _stub(listed[index])
            sizes[index] = _size(listed[index])
    grouped = 0
    if over():
        listed, grouped = _grouped(entries, listed, sizes, room)
    payload["messages"] = listed
    clipped_count = sum(1 for e in listed if e.get("clipped"))
    if clipped_count:
        payload["clipped"] = clipped_count
    if any(e.get("summarised") for e in listed):
        payload["summarised"] = sum(1 for e in listed if e.get("summarised"))
    if grouped:
        payload["grouped"] = grouped
    encoded = dump(payload)
    return EvidenceItem("request", dump({"request": session}), encoded, digest(encoded), 1)


def request_entry(db: sqlite3.Connection, project: str, session: str, entry: str) -> Json | None:
    """One message the user wrote, or one answer of the user's with its question and
    options, whole, by the ID the request item names; None for any other ID, the
    platform's messages included."""
    return next((e for e in request_entries(db, project, session) if entry_id(e) == entry), None)


def acceptances_item(db: sqlite3.Connection, project: str, session: str) -> EvidenceItem | None:
    """The session's accepted delegated tasks, the latest ``MAX_ACCEPTANCES`` first, each
    with the files and deliverables its acceptance recorded and whether the accepting
    run read each whole (``delegations.review_reads``, PD-46): a final review's context,
    read from the durable record, so that an acceptance in an earlier window or by an
    earlier run is seen too (CR-48). None when there is none."""
    from sandbox_science.orchestrator.delegation.acceptance import unread_text

    from .models import MAX_ACCEPTANCES

    listed: list[Json] = []
    for delegation, kind, reason, reads in db.execute(
        "SELECT delegation_id,kind,review_reason,review_reads FROM delegations "
        "WHERE project_id=? AND session_id=? AND review='accepted' "
        "ORDER BY reviewed_at DESC LIMIT ?",
        (project, session, MAX_ACCEPTANCES),
    ):
        account = cast(Json, json.loads(reads)) if reads else None
        listed.append(
            {
                "delegation": delegation,
                "kind": kind,
                "accepted_by": reason,
                "files": (account or {}).get("files", []),
                "unread": unread_text(account) if account is not None else "not recorded",
            }
        )
    if not listed:
        return None
    payload: Json = {
        "about": "The session's accepted delegated tasks and whether the accepting run read "
        "each result file whole after the task returned: context, not under review.",
        "acceptances": listed,
    }
    encoded = dump(payload)
    while len(encoded.encode()) > MAX_ITEM_BYTES // 4 and len(listed) > 1:
        listed.pop()
        encoded = dump(payload)
    return EvidenceItem("acceptances", dump({"acceptances": session}), encoded, digest(encoded), 1)


def unrecorded_through(db: sqlite3.Connection, project: str, session: str) -> int:
    """The last feed position of the session's author-work reviews that ended with no
    coverage record, from before reviews recorded their coverage: what they covered is
    not known, so the session's coverage says so for the positions up to it rather than
    call them unreviewed (CR-49); 0 when there are none."""
    (found,) = db.execute(
        "SELECT coalesce(max(r.end_seq),0) FROM review_runs r WHERE r.project=? "
        "AND r.session=? AND r.kind IN ('checkpoint','terminal') "
        "AND r.state IN ('passed','findings','inconclusive','capped') AND NOT EXISTS("
        "SELECT 1 FROM review_records c WHERE c.review_id=r.review_id AND c.kind='coverage')",
        (project, session),
    ).fetchone()
    return int(found)


def merged(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """``ranges`` merged and in order."""
    found: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if found and first <= found[-1][1] + 1:
            found[-1] = (found[-1][0], max(found[-1][1], last))
        else:
            found.append((first, last))
    return found


_READINGS: Final = {"unread": 0, "partial": 1, "whole": 2}
"""How an item was read, worst first: the best reading of an item counts."""


def item_readings(db: sqlite3.Connection, project: str, session: str) -> dict[int, str]:
    """How the session's author-work reviews read each new item they covered, by its feed
    position: ``whole``, ``partial`` (cut short) or ``unread`` (nothing to read), the
    best of its readings, as each review's coverage record names its items (CR-49 as
    amended); a record from before coverage named items covers its range whole."""
    found: dict[int, str] = {}
    for review, start, end, payload in db.execute(
        "SELECT r.review_id,r.start_seq,r.end_seq,c.payload FROM review_runs r "
        "JOIN review_records c ON c.review_id=r.review_id AND c.kind='coverage' "
        "WHERE r.project=? AND r.session=? AND r.kind IN ('checkpoint','terminal') "
        "AND r.state NOT IN ('inconclusive','failed','dropped','running')",
        (project, session),
    ):
        coverage = stored_json(payload)
        through, first = coverage.get("through"), coverage.get("from", start)
        if not coverage.get("verdict") or not isinstance(through, int):
            continue
        covered = set(cast(list[str], coverage.get("covered") or []))
        partial = set(cast(list[str], coverage.get("partial") or []))
        unread = set(cast(list[str], coverage.get("unread") or []))
        readings: dict[int, str] = {}
        if not covered:
            for (seq,) in db.execute(
                f"SELECT seq FROM review_feed WHERE project=? AND session=? AND seq>=? "  # noqa: S608 - fixed fragment
                f"AND seq<=? AND NOT {_FOLDED}",
                (project, session, first, through),
            ):
                readings[int(seq)] = "whole"
        for evidence, seq in db.execute(
            "SELECT e.evidence_id,f.seq FROM review_evidence e JOIN review_feed f "
            "ON f.project=? AND f.session=? AND f.seq>=? AND f.seq<=? AND f.kind=e.kind "
            "AND f.locator=e.locator AND f.sha256=e.sha256 WHERE e.review_id=?",
            (project, session, start, end, review),
        ):
            if evidence not in covered:
                continue
            reading = (
                "unread" if evidence in unread else "partial" if evidence in partial else "whole"
            )
            seq = int(seq)
            if _READINGS[reading] > _READINGS.get(readings.get(seq, ""), -1):
                readings[seq] = reading
        for seq, reading in readings.items():
            if _READINGS[reading] > _READINGS.get(found.get(seq, ""), -1):
                found[seq] = reading
    return found


def recorded_unreviewed(
    db: sqlite3.Connection, project: str, session: str
) -> list[tuple[int, int]]:
    """The feed ranges the session's reviews recorded as unreviewed (``review.unreviewed``
    with a start), merged: a checkpoint's skipped backlog, a final review's ranges its
    budget left out, a review ended at its deadline, an answering turn's work."""
    return merged(
        [
            (int(start), int(end))
            for start, end in db.execute(
                "SELECT json_extract(payload,'$.start'),json_extract(payload,'$.end') FROM "
                "control_events WHERE project=? AND session=? AND kind='review.unreviewed' "
                "AND json_type(payload,'$.start')='integer' "
                "AND json_type(payload,'$.end')='integer'",
                (project, session),
            )
        ]
    )


def range_reasons(
    db: sqlite3.Connection, project: str, session: str, left: Sequence[tuple[int, int]]
) -> list[list[str]]:
    """For each of the ranges ``left`` unreviewed, the reasons the session's reviews
    recorded for the ranges it meets (``review.unreviewed``: ``backlog``,
    ``final_budget``, ``deadline``, ``final_inconclusive`` and the like), sorted;
    ``not_reached`` for one no record names, which no review has reached yet."""
    recorded = [
        (int(start), int(end), str(reason))
        for start, end, reason in db.execute(
            "SELECT json_extract(payload,'$.start'),json_extract(payload,'$.end'),"
            "json_extract(payload,'$.reason') FROM control_events WHERE project=? AND "
            "session=? AND kind='review.unreviewed' AND json_type(payload,'$.start')='integer' "
            "AND json_type(payload,'$.end')='integer' AND json_type(payload,'$.reason')='text'",
            (project, session),
        )
    ]
    return [
        sorted({reason for a, b, reason in recorded if a <= end and b >= start} or {"not_reached"})
        for start, end in left
    ]


def unreviewed_reasons(
    db: sqlite3.Connection, project: str, session: str, left: Sequence[tuple[int, int]]
) -> dict[str, int]:
    """How many of the ranges ``left`` unreviewed meet a range the session's reviews
    recorded as unreviewed with each reason (``range_reasons``); ``not_reached`` counts
    those no record names. A range of several reasons counts under each."""
    counts: dict[str, int] = {}
    for reasons in range_reasons(db, project, session, left):
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def items_in(
    db: sqlite3.Connection, project: str, session: str, ranges: Sequence[tuple[int, int]]
) -> list[int]:
    """How many new items (rows but actions and raw sources) each of ``ranges`` holds."""
    return [
        int(
            db.execute(
                f"SELECT count(*) FROM review_feed WHERE project=? AND session=? "  # noqa: S608 - fixed fragment
                f"AND seq>=? AND seq<=? AND NOT {_FOLDED}",
                (project, session, start, end),
            ).fetchone()[0]
        )
        for start, end in ranges
    ]


def covered(ranges: Sequence[tuple[int, int]], seq: int) -> bool:
    """Whether ``seq`` lies in one of the merged, ordered ``ranges``."""
    index = bisect.bisect_right(ranges, (seq, float("inf"))) - 1
    return index >= 0 and ranges[index][0] <= seq <= ranges[index][1]


def uncovered(
    db: sqlite3.Connection,
    project: str,
    session: str,
    ranges: Sequence[tuple[int, int]],
    after: int,
    through: int,
) -> list[tuple[int, int]]:
    """The session's feed rows after ``after``, through ``through``, outside ``ranges``,
    as ranges of rows next to each other in the session's feed: what no review covered."""
    found: list[tuple[int, int]] = []
    open_range = False
    for (seq,) in db.execute(
        "SELECT seq FROM review_feed WHERE project=? AND session=? AND seq>? AND seq<=? "
        "ORDER BY seq",
        (project, session, after, through),
    ):
        if covered(ranges, seq):
            open_range = False
        elif open_range:
            found[-1] = (found[-1][0], seq)
        else:
            found.append((seq, seq))
            open_range = True
    return found


def final_items(
    db: sqlite3.Connection,
    project: str,
    session: str,
    candidates: Sequence[tuple[int, int]],
    end: int,
    ranges: Sequence[tuple[int, int]],
    room: tuple[int, int],
    *,
    run: str,
    viewed: list[tuple[Json, Json]] | None = None,
) -> tuple[int, list[EvidenceItem]]:
    """A final review's new items (CR-48): the session's feed rows in ``candidates``,
    merged and ordered ranges, through ``end``, that no review covered (outside
    ``ranges``), newest first, as many
    as ``room``, its bytes and items, allows; and the first position given. The newest
    item is always given. The actions item lists the actions among them, as a window's
    does, after the items, with run ``run``'s actions before them and the earlier runs'
    calls its view held (``viewed``) as context."""
    from .actions import MAX_ACTIONS, actions_item

    max_bytes, max_items = room
    items: list[EvidenceItem] = []
    actions: list[tuple[Json, Json]] = []
    revised: list[Json] = []
    size, start = 0, end + 1
    lower = candidates[0][0] - 1 if candidates else end
    for seq, key, kind, locator, payload, sha, complete in db.execute(
        "SELECT seq,source_key,kind,locator,CASE WHEN kind='source' AND source_key NOT "
        "GLOB 'quoted:*' THEN '' ELSE payload END,sha256,complete FROM review_feed "
        "WHERE project=? AND session=? AND seq>? AND seq<=? ORDER BY seq DESC",
        (project, session, lower, end),
    ):
        if covered(ranges, seq) or not covered(candidates, seq):
            continue
        key = str(key)
        if kind == "source" and not key.startswith("quoted:"):
            start = seq
            continue
        if kind == "action":
            if len(actions) >= MAX_ACTIONS:
                break
            actions.append((stored_json(locator), stored_json(payload)))
            start = seq
            continue
        item = EvidenceItem(kind, locator, payload, sha, complete)
        if items and (size + item.size > max_bytes or len(items) >= max_items):
            break
        items.append(item)
        size += item.size
        start = seq
        if kind == "document" and key.startswith("document:"):
            revised.append(revision_context(stored_json(payload)))
    context = earlier(db, project, session, SnapshotWindow(start - 1, None, end, 0), run)
    if viewed:
        context = replace(context or Earlier([], [], 0, 0), runs=viewed)
    if actions or revised or context is not None:
        actions.reverse()
        revised.reverse()
        located, listed = actions_item(actions, revised, start, end, context)
        encoded = dump(listed)
        items.append(EvidenceItem("actions", dump(located), encoded, digest(encoded), 1))
    return start, items


def open_findings(
    db: sqlite3.Connection,
    project: str,
    session: str,
    targets: Sequence[tuple[str, int]],
    *,
    limit: int = MAX_SNAPSHOT_FINDINGS,
) -> list[tuple[Any, ...]]:
    """The findings a review weighs: re-check targets first, a stale finding of an earlier
    session among them (CR-44), then the author's
    dismissals awaiting a review's decision (CR-43), then open, then stale ones; one more
    than a snapshot copies. Fixed and superseded findings, and dismissals already decided
    or the user's, are settled."""
    # Open finding state is copied, so a later user response cannot change what the
    # detached reviewer saw. History remains separately append-only.
    pending = (
        "(status='dismissed' AND json_extract(dismissal,'$.by')='agent' "
        "AND json_type(dismissal,'$.review') IS 'null')"
    )
    return [
        tuple(row)
        for row in db.execute(
            "SELECT finding_id,severity,category,statement,anchors,remedy,"  # noqa: S608 - fixed SQL
            "status,revision,reflags,supersedes,recheck,dismissal,session FROM review_findings f "
            "WHERE project=? AND (session=? OR finding_id IN (SELECT value FROM json_each(?))) "
            "AND category!='math' "
            f"AND (status IN ('open','stale') OR {pending}) ORDER BY CASE WHEN "
            "finding_id IN (SELECT value FROM json_each(?)) THEN 0 "
            f"WHEN {pending} THEN 1 WHEN status='open' THEN 2 ELSE 3 END,f.rowid "
            "LIMIT ?",
            (
                project,
                session,
                dump([target[0] for target in targets]),
                dump([target[0] for target in targets]),
                limit + 1,
            ),
        )
    ]
