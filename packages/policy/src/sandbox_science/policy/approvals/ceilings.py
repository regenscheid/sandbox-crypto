# SPDX-License-Identifier: MIT
"""How widely a user may grant what a request asks for (D97).

An answer is ``once`` (this request), or a grant for ``session`` or ``project``; a grant
is never wider than the request's ceiling (``max_grant``). Tool calls take their class's
ceiling on their server from the policy; network requests from research code take
``project`` for reads and ``once`` for anything else; a package install takes ``project``.
``unclassified`` calls are never granted wider than ``once``, whatever a policy says.
"""

from __future__ import annotations

from typing import Final, Literal

from sandbox_science.policy.effects import EffectClass, MaxGrant

GrantScope = Literal["once", "session", "project"]
SCOPE_ORDER: Final[dict[str, int]] = {"once": 0, "session": 1, "project": 2}
READ_METHODS: Final = frozenset({"GET", "HEAD"})
PACKAGES_MAX_GRANT: Final = MaxGrant.PROJECT
"""A package install's ceiling: the files come through the package cache (D95, D97)."""


def network_max_grant(method: str) -> MaxGrant:
    """The ceiling of an escalated request from research code: a read may be granted for
    the project, by host; anything else, once (D97)."""
    return MaxGrant.PROJECT if method.upper() in READ_METHODS else MaxGrant.ONCE


def widest(max_grant: str | None, effect: str | None = None) -> GrantScope:
    """The widest answer a ceiling allows: ``none``, an unknown ceiling and an
    ``unclassified`` call allow an answer for the one request only."""
    if effect == EffectClass.UNCLASSIFIED.value:
        return "once"
    if max_grant == MaxGrant.PROJECT.value:
        return "project"
    if max_grant == MaxGrant.SESSION.value:
        return "session"
    return "once"


def allowed(
    scope: str, max_grant: str | None, effect: str | None = None, *, session: bool = True
) -> bool:
    """Whether an answer of ``scope`` is within the ceiling; ``session`` says whether the
    request names a session, without which there is no session grant."""
    if scope not in SCOPE_ORDER or (scope == "session" and not session):
        return False
    return SCOPE_ORDER[scope] <= SCOPE_ORDER[widest(max_grant, effect)]


def offered(max_grant: str | None, effect: str | None = None, *, session: bool) -> list[str]:
    """The scopes a card offers, narrowest first."""
    return [scope for scope in SCOPE_ORDER if allowed(scope, max_grant, effect, session=session)]
