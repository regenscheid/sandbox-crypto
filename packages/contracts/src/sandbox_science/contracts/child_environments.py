# SPDX-License-Identifier: MIT
"""Worker API types for child environments (docs/specs/child-environments.md, CE-1).

A child environment is an environment of kind ``child`` that the worker makes, from a
session environment, for one delegated run: from the session environment's image, policy
revision, providers and admitted hosts, placed by a trusted child profile, with the
session's packages replayed and the brief's inputs copied in before it is ready. The run
works in it as in a session environment. At the delegation's end the worker copies what
the parent needs into the session environment at ``work/<delegation_id>/`` (the
hand-back), then releases the child environment, or keeps it stopped when the hand-back
was incomplete.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from .context import WRITE_AREA_DIRECTORY, ExecutionContext, in_write_area
from .model import Contract, Identifier, Sha256
from .worker import (
    MAX_MANIFEST_FILES,
    MAX_PATH_BYTES,
    IsolationTier,
    JobProfileName,
    ManifestFile,
    RelativePath,
    ReplayedPackages,
    UncollectedFile,
)

CHILD_ENVIRONMENT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,107}-child-[0-9a-f]{12}$")
"""A child environment's ID (CE-1): ``<project>-child-<12 hex>``. No client may create an
environment by such an ID."""
MAX_CHILD_INPUTS: Final = 20
"""Paths a request copies in at most: a brief's inputs (DA-17's 20)."""
CHILD_WALL_TIME_MAX: Final = 7 * 24 * 3600.0
"""The longest a child environment may be kept for its run (CE-9)."""
HAND_BACK_MANIFEST: Final = ".handback/manifest.json"
"""The hand-back's manifest, under its target: a reserved name no handed-back file takes."""
HAND_BACK_RESERVED: Final = ".handback"
HAND_BACK_DIRECTORY: Final = WRITE_AREA_DIRECTORY
"""The session workspace directory a hand-back writes under, one directory a delegation
(DA-19's write area, CE-6)."""

ChildPhase = Literal["creating", "preparing", "ready", "collecting", "released", "retained"]
"""Where a child environment is (CE-4): being placed, then packages and inputs, then the
run's; the hand-back; then deleted, or stopped and kept until a deadline."""
ChildFailure = Literal[
    "environment_unavailable",
    "isolation_insufficient",
    "package_replay_failed",
    "environment_mismatch",
    "inputs_too_large",
    "staging_failed",
    "worker_restarted",
    "wall_time",
]
"""Why a child environment never became ready, or was ended by the worker."""
ChildEndReason = Literal["delegation_ended", "session_closed", "abandoned"]
"""Why the orchestrator ends a child environment: its delegation ended (released after a
complete hand-back, kept otherwise); its session closed (released without retention,
JS-25's rule); or nothing will use it again, such as one admitted for a delegation that
was never written, or an interrupted run's left idle (released without retention)."""


def child_environment_id(project_id: str) -> str:
    """A new child environment ID for the project, within the 128 characters of an ID."""
    return f"{project_id[:108]}-child-{secrets.token_hex(6)}"


def is_child_environment(environment_id: str) -> bool:
    """Whether the ID has a child environment's shape."""
    return CHILD_ENVIRONMENT.fullmatch(environment_id) is not None


def hand_back_target(delegation_id: str) -> str:
    """Where a delegation's hand-back writes in the session workspace (CE-6): its write
    area (DA-19)."""
    return f"{HAND_BACK_DIRECTORY}/{delegation_id}"


def hand_back_path(delegation_id: str, path: str) -> str:
    """Where a hand-back writes a child environment's ``path`` in the session workspace
    (CE-6, DA-19): a file in the delegation's write area keeps its path, so the path the
    child wrote and named is the one its parent reads; any other file goes below the
    write area, ``x/y`` to ``work/<delegation_id>/x/y``."""
    target = hand_back_target(delegation_id)
    return path if in_write_area(target, path) else f"{target}/{path}"


def hand_back_reserved(delegation_id: str, path: str) -> bool:
    """Whether a hand-back would write ``path`` of the child environment into its own
    ``.handback/`` (CE-6), which no handed-back file takes."""
    inside = hand_back_path(delegation_id, path)[len(hand_back_target(delegation_id)) + 1 :]
    return inside == HAND_BACK_RESERVED or inside.startswith(f"{HAND_BACK_RESERVED}/")


class ChildEnvironmentRequest(Contract):
    """Admit and create a child environment for one delegation (CE-1, CE-4).

    Refused at once, before anything is created, with the reason: a pool without child
    profiles, a profile the pool does not offer or whose tier is below
    ``min_isolation_tier``, the policy's ``child_environments_max`` or the pool's or the
    profile's count reached (``limit_reached``), or inputs over the profile's staging
    bound (``inputs_too_large``). Once admitted, it is created in the background; its
    record says when it is ready. The same delegation again returns the same record."""

    context: ExecutionContext
    """The session's: its environment is the one the child environment is made from."""
    delegation_id: Identifier
    profile: JobProfileName | None = None
    """A child profile the pool offers; ``None`` for the project's (CE-3)."""
    project_profile: JobProfileName | None = None
    """The project's own child profile (its delegation setting); ``None`` for the pool's
    default. Where the policy grants no list of child profiles, it is the one granted."""
    inputs: tuple[RelativePath, ...] = Field(default=(), max_length=MAX_CHILD_INPUTS)
    """Session workspace paths, files or directories, copied to ``inputs/`` (CE-7). A
    path that names nothing is listed as missing, since a brief's inputs are also text."""
    min_isolation_tier: IsolationTier | None = None


class RetainedFile(Contract):
    """A file whose content control retains already: a hand-back names its revision
    instead of copying it (CE-6)."""

    path: RelativePath
    artifact_id: Identifier
    revision: Annotated[int, Field(ge=1)]
    sha256: Sha256 | None = None


class HandBackRequest(Contract):
    """Copy a delegation's files from its child environment into the session's (CE-6).

    The first request is recorded, and every later one must be the same: a hand-back a
    restart cut short goes on from the last file it recorded, and copies none twice."""

    context: ExecutionContext
    """The session's: the hand-back writes into its environment."""
    delegation_id: Identifier
    """The delegation whose ``work/<delegation_id>/`` the files go to; a continuation's
    own after a Resume (CE-12)."""
    files: tuple[RelativePath, ...] = Field(default=(), max_length=2 * MAX_MANIFEST_FILES)
    """Child environment paths to copy, in order: past 1,000 they are left out."""
    retained: tuple[RetainedFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)


class ChildHandBack(Contract):
    """A hand-back's manifest: stored once, and written to
    ``work/<delegation_id>/.handback/manifest.json`` in the session (an untrusted copy)."""

    delegation_id: Identifier
    environment_id: Identifier
    target: Annotated[str, StringConstraints(max_length=MAX_PATH_BYTES)]
    collected: tuple[ManifestFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    """Copied, by their session paths under ``target``."""
    retained: tuple[RetainedFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    not_collected: tuple[UncollectedFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    complete: bool
    ended_at: datetime


class ChildEnvironmentEnd(Contract):
    """End a child environment: release it, or keep it stopped after an incomplete
    hand-back (CE-4). A hand-back a restart cut short is finished first."""

    context: ExecutionContext
    reason: ChildEndReason = "delegation_ended"


class ChildEnvironment(Contract):
    """A child environment's record, as the worker keeps it (CE-11)."""

    environment_id: Identifier
    delegation_id: Identifier
    project_id: Identifier
    session_id: Identifier
    session_environment: Identifier
    profile: JobProfileName
    phase: ChildPhase
    failure: ChildFailure | None = None
    message: Annotated[str, StringConstraints(max_length=1000)] | None = None
    """Why it failed or was kept, in words."""
    isolation_tier: IsolationTier | None = None
    """The tier the probe measured in it, once prepared."""
    inputs: tuple[ManifestFile, ...] = Field(default=(), max_length=MAX_MANIFEST_FILES)
    """The inputs copied, by their child environment paths under ``inputs/``."""
    inputs_missing: tuple[RelativePath, ...] = Field(default=(), max_length=MAX_CHILD_INPUTS)
    packages: ReplayedPackages = ReplayedPackages()
    created_at: datetime
    ready_by: datetime
    """When it must be ready, by its profile's ``schedule_timeout``."""
    ready_at: datetime | None = None
    expires_at: datetime
    """When its profile's ``wall_time_max`` runs out: the run ends ``limit_reached``."""
    retained_until: datetime | None = None
    hand_back: ChildHandBack | None = None


class ChildEnvironmentList(Contract):
    """A session's child environments, ended ones included, oldest first."""

    environments: tuple[ChildEnvironment, ...] = Field(max_length=1000)


__all__ = [
    "CHILD_ENVIRONMENT",
    "CHILD_WALL_TIME_MAX",
    "HAND_BACK_DIRECTORY",
    "HAND_BACK_MANIFEST",
    "HAND_BACK_RESERVED",
    "MAX_CHILD_INPUTS",
    "ChildEndReason",
    "ChildEnvironment",
    "ChildEnvironmentEnd",
    "ChildEnvironmentList",
    "ChildEnvironmentRequest",
    "ChildFailure",
    "ChildHandBack",
    "ChildPhase",
    "HandBackRequest",
    "RetainedFile",
    "child_environment_id",
    "hand_back_path",
    "hand_back_reserved",
    "hand_back_target",
    "is_child_environment",
]
