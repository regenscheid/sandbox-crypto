# SPDX-License-Identifier: MIT
"""The projects this deployment acts for, as its control service last started with them.

The control service's settings list the projects it admits work for (``projects`` in
``control.json``), and the site configuration that wrote them gives the worker's clients
the same list: a project outside it is recorded by ``project create`` but never runs,
since control queues no work for it and the worker refuses its environments. The control
service records the list in its state directory when it starts, so that ``project
create``, which reads only that directory, can say so (docs/KUBERNETES-BUNDLE.md#adding-
a-project). The file grants nothing: it is a copy of settings for a warning.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract, Identifier

SCOPE_FILE = "deployment-projects.json"
"""The file in the control state directory that holds the deployment's project list."""

MAX_SCOPE_BYTES = 16384
"""The largest scope file read: 128 project IDs of at most 63 characters fit well within."""

GUIDE = "docs/KUBERNETES-BUNDLE.md#adding-a-project"
"""Where an operator reads how to add a project to a deployment."""


class DeploymentScope(Contract):
    projects: tuple[Identifier, ...] = Field(min_length=1, max_length=128)


def record(state_dir: Path, projects: Iterable[str]) -> None:
    """Publish the control service's project list atomically in ``state_dir``."""
    data = DeploymentScope(projects=tuple(projects)).model_dump_json().encode()
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".deployment-projects-", dir=state_dir)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_dir / SCOPE_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(state_dir: Path) -> tuple[frozenset[str] | None, str | None]:
    """The recorded project list, or None and why it is unknown: no reason when no
    control service has started here. A damaged file is never taken as a list."""
    path = state_dir / SCOPE_FILE
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SCOPE_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"the deployment's project list ({path}) cannot be read: {error}"
    if len(data) > MAX_SCOPE_BYTES:
        return None, f"the deployment's project list ({path}) exceeds its size bound"
    try:
        return frozenset(DeploymentScope.model_validate_json(data).projects), None
    except ValidationError:
        return None, f"the deployment's project list ({path}) is damaged"


def outside(state_dir: Path, project_id: str) -> str | None:
    """What an operator must know when this deployment will not run ``project_id``."""
    projects, reason = read(state_dir)
    if reason is not None:
        return f"{reason}; whether this deployment runs project {project_id!r} is unknown"
    if projects is None or project_id in projects:
        return None
    listed = ", ".join(sorted(projects))
    return (
        f"project {project_id!r} is recorded, but this deployment's control service and "
        f"worker act only for the projects its site configuration lists ({listed}): no run "
        f"of it starts, and the worker refuses its environments (policy_denied), until the "
        f"site configuration names it in [[projects]] and the site is generated and "
        f"deployed again ({GUIDE})"
    )
