# SPDX-License-Identifier: MIT
"""The launcher refuses an OpenShell CLI that is not the release Sandbox Science is built
against (``contracts.openshell_version``), before it starts anything that would use it."""

from __future__ import annotations

import shutil
import subprocess

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION, pinned_version

from .config import LauncherError

TIMEOUT = 10.0
"""Seconds ``openshell --version`` has to answer."""


def problem(cli: str, *, path: str | None = None) -> str | None:
    """Why ``cli`` (a command name or path, found on ``path``, else ``PATH``) cannot be
    used, or None when it is the pinned release."""
    found = shutil.which(cli, path=path)
    if found is None:
        return f"no OpenShell CLI {cli}"
    try:
        run = subprocess.run(  # noqa: S603 - the operator's own configured CLI
            [found, "--version"], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            f"{found} --version did not answer ({type(exc).__name__}); this release needs "
            f"OpenShell {OPENSHELL_VERSION}"
        )
    said = " ".join((run.stdout + " " + run.stderr).split())
    if run.returncode == 0 and pinned_version(said):
        return None
    return (
        f"{found} says {said[:80] or 'nothing'!r}, but this release needs OpenShell "
        f"{OPENSHELL_VERSION}: the gateway, CLI and supervisor are versioned together"
    )


def require(cli: str, *, path: str | None = None) -> None:
    """Raise ``LauncherError`` unless ``cli`` is the pinned release."""
    found = problem(cli, path=path)
    if found is not None:
        raise LauncherError(found)
