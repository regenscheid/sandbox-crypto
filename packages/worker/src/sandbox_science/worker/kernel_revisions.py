# SPDX-License-Identifier: MIT
"""Durable kernel birth pins, separate from monotonic generation bookkeeping (CP-12).

These are observations from the sandbox, not authority to execute or install packages.
A first observation of an already-running kernel never establishes its start revision.
"""

from __future__ import annotations

import re
import sqlite3

from pydantic import ValidationError

from sandbox_science.contracts import KernelInfo
from sandbox_science.contracts.kernel_lineage import KernelBirthPin

_MAX_RECORD = 2048
# Compatibility name: retained JSON and existing start transactions are unchanged.
KernelBirth = KernelBirthPin


class KernelStarts:
    """One current birth record per existing kernel scope; no output or package data."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def current(self, environment_id: str) -> tuple[int, str] | None:
        """Bounded current-revision projection; damaged metadata means unknown."""
        row = self.db.execute(
            "SELECT CASE WHEN typeof(revision)='integer' THEN revision END,"
            " substr(fingerprint,1,33) FROM environment_revisions WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        if row is None:
            return None
        number, fingerprint = row
        if (
            type(number) is not int
            or not 0 <= number <= 2**62
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{32}", fingerprint) is None
        ):
            return None
        return number, fingerprint

    def epoch(
        self,
        environment_id: str,
        incarnation: str,
        epoch: object,
        fingerprint: object,
        *,
        remember: bool = False,
    ) -> int | None:
        """Disambiguate returning fingerprints with a monotonic sandbox package epoch.

        Only hello/revision events establish a mapping. Older/inconsistent observations
        cannot manufacture a start revision from today's fingerprint.
        """
        current = self.current(environment_id)
        if (
            type(epoch) is not int
            or not 0 <= epoch < 2**62
            or current is None
            or fingerprint != current[1]
            or re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None
        ):
            return None
        row = self.db.execute(
            "SELECT substr(incarnation,1,29),"
            " CASE WHEN typeof(epoch)='integer' THEN epoch END,"
            " CASE WHEN typeof(revision)='integer' THEN revision END, substr(fingerprint,1,33)"
            " FROM kernel_package_epochs WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        if row is not None:
            old_inc, old_epoch, old_revision, old_fingerprint = row
            if (
                not isinstance(old_inc, str)
                or re.fullmatch(r"inc-[0-9a-f]{24}", old_inc) is None
                or type(old_epoch) is not int
                or not 0 <= old_epoch < 2**62
                or type(old_revision) is not int
                or not 0 <= old_revision <= 2**62
                or not isinstance(old_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{32}", old_fingerprint) is None
            ):
                return None
            if old_inc == incarnation:
                if (epoch, current[0], fingerprint) == (old_epoch, old_revision, old_fingerprint):
                    return old_revision
                if epoch <= old_epoch:
                    return None
        if remember:
            self.db.execute(
                "INSERT OR REPLACE INTO kernel_package_epochs VALUES(?,?,?,?,?)",
                (environment_id, incarnation, epoch, current[0], fingerprint),
            )
            return current[0]
        return None

    def observe(
        self,
        birth: KernelBirth,
        info: KernelInfo,
        current_revision: int | None,
        *,
        remember: bool = True,
    ) -> KernelInfo:
        """Retain the first exact birth pin; never rebase one onto today's packages."""
        row = self.db.execute(
            "SELECT substr(record,1,?) FROM kernel_start_revisions"
            " WHERE environment_id=? AND session_id=? AND language=?",
            (_MAX_RECORD + 1, birth.environment_id, birth.session_id, birth.language),
        ).fetchone()
        saved: KernelBirth | None = None
        damaged = False
        if row is not None:
            try:
                raw = row[0]
                if not isinstance(raw, str) or len(raw) > _MAX_RECORD:
                    raise ValueError("invalid kernel birth record")
                saved = KernelBirth.model_validate_json(raw)
                if (saved.environment_id, saved.session_id, saved.language) != (
                    birth.environment_id,
                    birth.session_id,
                    birth.language,
                ):
                    raise ValueError("kernel birth scope mismatch")
            except (ValueError, ValidationError):
                damaged = True
        identity = birth.model_copy(update={"start_revision": None})
        if saved is not None and saved.model_copy(update={"start_revision": None}) == identity:
            revision = saved.start_revision
        elif saved is not None and (
            saved.project_id != birth.project_id
            or (saved.incarnation == birth.incarnation and saved.generation >= birth.generation)
        ):
            revision = None  # stale/colliding identity cannot replace the current birth
        elif damaged:
            # Corrupt history cannot prove whether this is the original birth.
            revision = None
        else:
            revision = birth.start_revision
            if remember:
                self.db.execute(
                    "INSERT OR REPLACE INTO kernel_start_revisions VALUES(?,?,?,?)",
                    (
                        birth.environment_id,
                        birth.session_id,
                        birth.language,
                        birth.model_dump_json(),
                    ),
                )
        if revision is not None and current_revision is not None and revision > current_revision:
            revision = None  # revision history was reset or is incoherent; never claim current
        status = "unknown"
        if revision is not None and current_revision is not None:
            status = "current" if revision == current_revision else "predates"
        return KernelInfo(
            kernel_id=info.kernel_id,
            language=info.language,
            generation=info.generation,
            start_revision=revision,
            current_revision=current_revision,
            revision_status=status,
        )

    def forget(self, environment_id: str) -> None:
        """Confirmed environment deletion forgets only that environment's birth pins."""
        self.db.execute(
            "DELETE FROM kernel_start_revisions WHERE environment_id=?", (environment_id,)
        )
        self.db.execute(
            "DELETE FROM kernel_package_epochs WHERE environment_id=?", (environment_id,)
        )
