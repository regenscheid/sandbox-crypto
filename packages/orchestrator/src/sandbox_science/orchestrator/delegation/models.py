# SPDX-License-Identifier: MIT
"""The model a delegated run calls (plans-and-delegation spec, PD-28 and PD-30).

The brief's choice applies when the project lists that alias among those a brief may
choose; otherwise the run calls the delegated role's alias, which is MAIN's unless the
deployment names another. The choice is made as ``delegate`` is called, before anything
is written or reserved: an alias the gateway does not list is replaced by the project's
fallback when it names one the gateway lists, and the delegation records that it was;
otherwise a brief's choice is refused with ``model_unavailable``, and the delegated
role's own alias is called all the same, since a gateway may serve an alias it does not
list, as the owner's LiteLLM did MAIN's (lab 40). A gateway that cannot be asked for its
list is taken at its word. Either way, the run's first call tells. An alias the gateway
says cannot call tools, as a delegated run must, is replaced by the fallback when the
gateway says that one can, and otherwise refused (PD-30); one it does not describe is
taken at its word.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import JsonValue

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .records import DelegationSettings

Listed = Callable[[], Awaitable[frozenset[str] | None]]
"""The aliases the gateway lists, or ``None`` when it cannot say."""
Capable = Callable[[], Awaitable[dict[str, bool] | None]]
"""Whether each alias the gateway describes can call tools, or ``None`` when it cannot
say."""


@dataclass(frozen=True)
class Choice:
    """The alias a delegated run calls, and the one it was to call."""

    alias: str
    asked: str

    @property
    def fallback(self) -> bool:
        """Whether the project's fallback replaced the alias asked for."""
        return self.alias != self.asked

    def recorded(self) -> dict[str, JsonValue]:
        """The choice as the delegation's brief and its start keep it."""
        found: dict[str, JsonValue] = {"alias": self.alias}
        if self.fallback:
            found.update(asked=self.asked, fallback=True)
        return found


class ModelChoice:
    """Chooses the alias of a parent run's delegations: the brief's, if the project
    allows it, or ``default``, the delegated role's; ``listed`` is the gateway's list,
    read once for the parent's run, and without it only ``default`` can be called."""

    def __init__(
        self,
        default: str,
        settings: DelegationSettings,
        listed: Listed | None = None,
        capable: Capable | None = None,
    ) -> None:
        self.default, self.settings, self.listed = default, settings, listed
        self.capable = capable
        self._aliases: frozenset[str] | None = None
        self._asked = False
        self._capable: dict[str, bool] | None = None
        self._described = False

    async def aliases(self) -> frozenset[str] | None:
        if self.listed is not None and not self._asked:
            self._aliases, self._asked = await self.listed(), True
        return self._aliases

    async def calls_tools(self, alias: str) -> bool | None:
        """Whether the gateway says ``alias`` can call tools; ``None`` when it does not
        say, read once for the parent's run."""
        if self.capable is not None and not self._described:
            self._capable, self._described = await self.capable(), True
        return None if self._capable is None else self._capable.get(alias)

    async def choose(self, asked: str | None) -> Choice:
        """The alias for a brief that asked for ``asked`` (``_choose``), and one the
        gateway says can call tools, as a delegated run must (PD-30): one it says cannot
        is replaced by the project's fallback when the gateway says that one can, and
        otherwise refused with ``model_unavailable``."""
        chosen = await self._choose(asked)
        if await self.calls_tools(chosen.alias) is not False:
            return chosen
        fallback = self.settings.fallback
        listed = await self.aliases()
        if (
            fallback is not None
            and fallback != chosen.alias
            and (listed is None or fallback in listed)
            and await self.calls_tools(fallback) is not False
        ):
            return Choice(fallback, chosen.asked)
        raise WorkerAPIError(
            ErrorKind.UNAVAILABLE,
            f"model_unavailable: the model gateway says {chosen.alias!r} cannot call tools, "
            "which a delegated run needs"
            + (
                ""
                if fallback is None
                else f", and the project's fallback {fallback!r} is no better"
            ),
        )

    async def _choose(self, asked: str | None) -> Choice:
        """The alias for a brief that asked for ``asked``, or ``WorkerAPIError``:
        ``policy_denied`` for an alias the project does not allow, ``unavailable`` for
        a chosen one the gateway does not list when no fallback it lists replaces it."""
        alias = self.default if asked is None else asked
        if alias != self.default and alias not in self.settings.models:
            allowed = ", ".join(self.settings.models) or "none"
            raise WorkerAPIError(
                ErrorKind.POLICY_DENIED,
                f"model_not_allowed: the project lets a brief choose these models: {allowed}",
            )
        if self.listed is None:
            if alias != self.default:
                raise WorkerAPIError(
                    ErrorKind.UNAVAILABLE,
                    f"model_unavailable: no model gateway serves {alias!r} to this harness",
                )
            return Choice(alias, alias)
        listed = await self.aliases()
        if listed is None or alias in listed:
            return Choice(alias, alias)
        fallback = self.settings.fallback
        if fallback is not None and fallback in listed:
            return Choice(fallback, alias)
        if alias == self.default:
            # The deployment's own alias, like MAIN's, which no list checks: a gateway may
            # serve an alias it does not list, as the owner's LiteLLM did MAIN's (lab 40), so
            # the run's first call tells.
            return Choice(alias, alias)
        named = (
            f"nor the project's fallback {fallback!r}"
            if fallback is not None
            else "and the project names no fallback"
        )
        raise WorkerAPIError(
            ErrorKind.UNAVAILABLE,
            f"model_unavailable: the model gateway lists no model {alias!r}, {named}",
        )
