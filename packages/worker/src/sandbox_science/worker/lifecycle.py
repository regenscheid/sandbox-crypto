# SPDX-License-Identifier: MIT
"""What the execution manager needs from the sandboxes the worker manages.

The environments (``environments.py``) create, suspend and recycle sandboxes on an
OpenShell gateway. The manager asks them a few things: the sandbox an environment is
bound to, whether a sandbox is proven gone, to recycle one, which admission notices
a session has not seen, and the job limits of the environment's policy.
``EnvironmentLifecycle`` carries those answers into the manager when it is built.
Without managed environments, such as on the local backend, each is absent and the
manager does without it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sandbox_science.policy.model import Resources

DaemonIdentity = Callable[[str], tuple[str, str] | None]
"""An environment's trusted sandbox binding, by name and ID; None when unknown."""
ConfirmedLoss = Callable[[str], Awaitable[bool]]
"""Whether the environment's sandbox is proven deleted, stopped or replaced."""
Recycle = Callable[[str, str, str], Awaitable[None]]
"""Stop and start the environment's original sandbox, by its name and ID."""
Notices = Callable[[str, datetime], list[tuple[datetime, str]]]
"""Admission notices for an environment since a time, each with its time (D69)."""
JobResources = Callable[[str], Resources]
"""The job limits of the environment's policy."""


@dataclass(frozen=True)
class EnvironmentLifecycle:
    """The managed environments' answers, each None where the worker manages none."""

    daemon_identity: DaemonIdentity | None = None
    """A managed environment's binding. Without it, the backend's own binding stands in,
    and a kernel's start revision is not pinned to a sandbox."""
    confirmed_daemon_loss: ConfirmedLoss | None = None
    """Without it, recovery never concludes that a sandbox is gone."""
    recycle_daemon: Recycle | None = None
    """Without it, an environment whose daemon was replaced stays fenced."""
    notices: Notices | None = None
    """Without it, results carry no admission notices."""
    requires_workspace_prefix: Callable[[str], bool] | None = None
    """Whether the original allocation requires the durable research prefix layout."""
    job_resources: JobResources | None = None
    """Without it, jobs get the default limits."""


UNMANAGED: Final = EnvironmentLifecycle()
"""Nothing managed and no sandbox lifecycle: the local backend."""
