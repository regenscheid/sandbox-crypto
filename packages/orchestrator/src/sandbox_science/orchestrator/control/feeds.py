# SPDX-License-Identifier: MIT
"""Feed cursors: how far each session's consumer has read one of a project's logs.

A consumer pins the log's head, reads a bounded page after its cursor, makes its
observations, and then, in one change, moves the cursor from where it read to the
page's end and writes the observations. The move is a compare-and-set: when another
consumer moved the cursor meanwhile, nothing is written and the consumer reads again
(refactoring plan, B-04). ``feed_cursors`` keeps every feed's cursors.
"""

from __future__ import annotations

import sqlite3
from typing import Final


class FeedCursor:
    """The cursors of one feed, such as the reviewer's reading of the documents' events."""

    FEEDS: Final = ("document", "artifact", "ledger")

    def __init__(self, feed: str) -> None:
        if feed not in self.FEEDS:
            raise ValueError(f"unknown feed {feed!r}")
        self.feed = feed

    def position(self, db: sqlite3.Connection, project: str, session: str) -> int:
        """Where the session's consumer has read to: 0 before it has read anything."""
        row = db.execute(
            "SELECT cursor FROM feed_cursors WHERE feed=? AND project=? AND session=?",
            (self.feed, project, session),
        ).fetchone()
        return 0 if row is None else row[0]

    def advance(
        self, db: sqlite3.Connection, project: str, session: str, read: int, end: int
    ) -> bool:
        """Move the cursor from ``read`` to ``end`` in the open change ``db``; False, and
        nothing moved, when it is no longer at ``read``."""
        db.execute(
            "INSERT OR IGNORE INTO feed_cursors(feed,project,session,cursor) VALUES(?,?,?,0)",
            (self.feed, project, session),
        )
        return (
            db.execute(
                "UPDATE feed_cursors SET cursor=? WHERE feed=? AND project=? AND session=? "
                "AND cursor=?",
                (end, self.feed, project, session, read),
            ).rowcount
            == 1
        )
