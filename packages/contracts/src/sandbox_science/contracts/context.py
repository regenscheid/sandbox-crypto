# SPDX-License-Identifier: MIT
"""The context every execution request carries."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, StringConstraints

from .model import Contract, Identifier, Revision

WRITE_AREA_DIRECTORY: Final = "work"
"""The session workspace directory that holds each delegation's write area (DA-19)."""
WriteArea = Annotated[str, StringConstraints(pattern=r"^work/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
"""A delegated run's write area, ``work/<delegation_id>/``, without its final slash."""

OwnershipName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")]
"""A timestamped work/attempt name, including the longest 128-character run ID."""


class ExecutionAuthority(Contract):
    """Host-control ownership, never supplied by an agent's tool arguments."""

    work: OwnershipName
    run: OwnershipName
    epoch: Revision


class ExecutionContext(Contract):
    """Who asks for an execution, and for which project, session and environment.

    ``environment_id`` names the logical environment (the project's default
    environment or a dedicated session environment). The worker reports the
    concrete sandbox and the environment revision it used in ``Location``.
    """

    project_id: Identifier
    session_id: Identifier
    environment_id: Identifier
    policy_revision: str | None = None
    """The project policy revision in force, such as ``sha256:…``."""
    actor: Identifier = "agent"
    """Who requested the execution: ``agent``, ``user``, ``reviewer`` or a delegated role."""
    authority: ExecutionAuthority | None = Field(default=None, exclude_if=lambda v: v is None)
    write_area: WriteArea | None = Field(default=None, exclude_if=lambda v: v is None)
    """A delegated run's write area (DA-19), set by the platform on its ``write_file`` and
    ``edit_file`` requests, never by an agent's arguments: the worker writes them only
    below it and in ``shared/``. ``None`` for every other request and every main run."""


def write_area(delegation_id: str) -> str:
    """A delegation's write area in the session workspace (DA-19): ``work/<delegation_id>``."""
    return f"{WRITE_AREA_DIRECTORY}/{delegation_id}"


def in_write_area(area: str, path: str) -> bool:
    """Whether a workspace path, checked as ``validate_relative_path`` checks it, is a
    file below ``area``. The comparison is exact: case counts, as the sandbox's file
    system's does, and the area itself is not a file in it."""
    return path.startswith(area + "/")


def outside_write_area(context: ExecutionContext, path: str) -> str | None:
    """Why a file request may not write ``path`` (DA-19), or ``None`` when it may: a
    request without a write area writes anywhere; one with it writes below it and in
    ``shared/``, where the executor holds a write to its ``expected_sha256`` or
    ``create_only`` guard. The message names the allowed places, so the agent can write
    there instead. ``path`` is checked already: relative, with no empty, ``.`` or ``..``
    segment, which a write area's prefix cannot then be talked out of; symlinks on the
    way are refused by the executor, which follows none."""
    area = context.write_area
    if area is None or path == "shared" or path.startswith("shared/") or in_write_area(area, path):
        return None
    return (
        f"outside_write_area: {path!r} is outside your write area. As a delegated agent, "
        f"your write_file and edit_file write only in {area}/ and in shared/ (with "
        "create_only for a new file or expected_sha256 to replace one); you can still read "
        f"every file of the session. Write it in your write area instead, such as "
        f"{area}/{path}."
    )
