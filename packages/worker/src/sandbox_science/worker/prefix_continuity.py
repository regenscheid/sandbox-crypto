# SPDX-License-Identifier: MIT
"""A retained research prefix must survive executor replacement before revision admission."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .store import Store

_log = logging.getLogger(__name__)
_IDENTITY = r"^[0-9a-f]{64}$"


class PrefixDescription(BaseModel):
    """Bounded executor hello, independent of operator and agent volume configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol: Annotated[int, Field(strict=True, ge=1, le=1)]
    path: Literal["/sandbox/.sandbox-science-prefix"]
    identity: Annotated[str, Field(pattern=r"^prefix-[0-9a-f]{32}$", max_length=39)]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", max_length=32)]
    journal_pending: bool
    seeded: Annotated[str, Field(pattern=_IDENTITY, max_length=64)] | None = None
    """A layer's: the identity of the base it was seeded over (docs/IMAGES.md)."""
    base: Annotated[str, Field(pattern=_IDENTITY, max_length=64)] | None = None
    """A layer's: the identity of the base it runs over now. A layer's fingerprint is its own
    digest's first half and this identity's first 16 characters (provisioning.fingerprint)."""


def base_changed(prefix: PrefixDescription, fingerprint: str) -> bool:
    """Whether a layer's packages differ from ``fingerprint`` only by the base under it, as
    when the image is rebuilt with other packages under a retained workspace."""
    return (
        prefix.base is not None
        and prefix.fingerprint[16:] == prefix.base[:16]
        and prefix.fingerprint[:16] == fingerprint[:16]
        and prefix.fingerprint[16:] != fingerprint[16:]
    )


def hold(store: Store, environment_id: str) -> None:
    """Retain the last known revision and any earlier interrupted-operation hold."""
    revision = store.revision(environment_id)
    store.set_package_status(
        environment_id,
        "needs_recovery",
        "workspace-prefix",
        None if revision is None else revision.revision,
    )


def accept(
    store: Store, environment_id: str, incarnation: str, raw: object, reported: object
) -> bool:
    """Whether hello may establish a baseline; a surviving daemon reconciles by replay.

    False also means a durable hold when layout, identity or a replacement daemon's
    actual packages changed. A current revision always outranks old hello state:
    legitimate managed revisions need not refresh this continuity record.
    """
    try:
        prefix = PrefixDescription.model_validate(raw)
    except ValidationError:
        hold(store, environment_id)
        return False
    previous = store.workspace_prefix(environment_id)
    revision = store.revision(environment_id)
    marked = store.package_status(environment_id)
    prefix_hold = (
        marked is not None and marked[1] == "workspace-prefix"
    ) or store.workspace_prefix_restore_pending(environment_id) is not None
    if (
        previous is not None
        and prefix.identity == previous[0]
        and incarnation == previous[1]
        and not prefix_hold
    ):
        # Pending journals and already-buffered revision events belong to this daemon.
        # Do not turn its ahead/behind hello into an unmanaged revision before replay.
        return revision is None
    rebased = revision is not None and base_changed(prefix, revision.fingerprint)
    if (
        prefix.journal_pending
        or (prefix.fingerprint != reported and not prefix_hold)
        or (previous is None and revision is not None)
        or (previous is not None and prefix.identity != previous[0])
        or (revision is not None and prefix.fingerprint != revision.fingerprint and not rebased)
    ):
        hold(store, environment_id)
        return False
    if rebased:
        # Not drift in the workspace: a new image's base. Hello's fingerprint becomes a new
        # revision, as any change of packages found at an executor's start does.
        _log.info(
            "environment %s: the image's base changed under its retained layer (%s, seeded"
            " over %s)",
            environment_id,
            prefix.base,
            prefix.seeded,
        )
    if prefix_hold:
        return store.restore_workspace_prefix(environment_id, prefix.identity, prefix.fingerprint)
    store.remember_workspace_prefix(environment_id, prefix.identity, incarnation)
    return True
