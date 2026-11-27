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
from typing import Any

from sandbox_science.orchestrator.control import ControlStore

from .actions import Earlier
from .snapshot import (
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
