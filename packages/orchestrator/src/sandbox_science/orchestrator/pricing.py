# SPDX-License-Identifier: MIT
"""A call's cost from the gateway's own price list, when the gateway did not report it
(run-service spec RS-6a; lab 43, B2).

LiteLLM 1.102.1 sends no ``x-litellm-response-cost`` on ``/v1/responses`` and no
``usage.cost`` there, so GPT's calls came back without a cost and a daily cost limit
could not stop them. The cost is never estimated: when the gateway reports none, it is
priced from the gateway's model information (``GET /model/info``, LiteLLM's per-token
``input_cost_per_token``, ``cache_read_input_token_cost``,
``cache_creation_input_token_cost`` and ``output_cost_per_token`` of the alias's
deployments), applied to the tokens the call's usage reports, and the call says so
(``MODEL_INFO``). A gateway's cost of 0 for a call that used tokens is no report: it is
what LiteLLM says for a model it has no price for.

When the alias's deployments are not all priced, or the usage lacks a count the price
needs, the cost stays unknown (``None``). A call is priced at each of the alias's
deployments' prices, and the dearest is its cost. Cached input without a cache-read price
is priced as input, which is no less; written cache without a cache-write price leaves the
cost unknown, since writing costs more than input.

A model's price may rise with the size of a call (lab 43 follow-up: GPT-5.5's doubles
past 272,000 input tokens). LiteLLM's model information gives such a tier as the field
with ``_above_<N>k_tokens`` after it, such as ``input_cost_per_token_above_272k_tokens``,
and, as LiteLLM prices it, a call whose input, cached or not, passes N thousand tokens is
priced wholly at the highest tier it passes. A rate that tier does not give is never
taken lower than the list allows: an input or output rate is the highest the deployment
gives for that kind, a cached-input rate the tier's input rate, and a cache-write rate is
none, so a call that wrote the cache stays unpriced.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

import aiohttp

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock

_log = logging.getLogger(__name__)

CostSource = Literal["gateway", "model_info"]
GATEWAY: Final = "gateway"
"""A cost the gateway reported for the call: its header, or its answer's ``usage.cost``."""
MODEL_INFO: Final = "model_info"
"""A cost priced from the gateway's model information, its own price list."""
INFO_BYTES: Final = 4 * 1024 * 1024
"""The largest model information read from the gateway."""
RETRY_SECONDS: Final = 60.0
"""How long after a failed read of the model information it is asked again."""


@dataclass(frozen=True, slots=True)
class Tier:
    """A deployment's prices per token for a call whose input passes ``above`` tokens; a
    rate it does not give is ``None``."""

    above: int
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass(frozen=True, slots=True)
class Prices:
    """A deployment's prices per token, in the gateway's currency, with its tiers."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    tiers: tuple[Tier, ...] = ()

    def at(self, given: int) -> Prices:
        """The prices of a call whose input, cached or not, is ``given`` tokens: the
        highest tier it passes. An input or output rate the tier lacks is the highest the
        deployment gives for that kind; a cached-input rate it lacks is its input rate,
        which is no less; a cache-write rate it lacks is none, which leaves a call that
        wrote the cache unpriced."""
        passed = [tier for tier in self.tiers if given > tier.above]
        if not passed:
            return replace(self, tiers=())
        tier = max(passed, key=lambda found: found.above)

        def rate(kind: str) -> float | None:
            if (own := getattr(tier, kind)) is not None:
                return cast(float, own)
            known = [getattr(self, kind), *(getattr(found, kind) for found in self.tiers)]
            return max((v for v in known if v is not None), default=None)

        return Prices(
            cast(float, rate("input")),
            cast(float, rate("output")),
            tier.cache_read,
            tier.cache_write,
        )


@dataclass(frozen=True, slots=True)
class Tokens:
    """A call's tokens as they are priced: input not cached, read from the cache,
    written to it, and output (reasoning included)."""

    fresh: int
    cache_read: int
    cache_write: int
    output: int


def source_of(cost: float | None, source: CostSource | None) -> CostSource | None:
    """Where a cost came from, as a call's record keeps it: none without a cost; else the
    source given, or ``gateway``, which every cost was before model information priced
    any."""
    return None if cost is None else source or GATEWAY


def cost(prices: Prices | Sequence[Prices], tokens: Tokens) -> float | None:
    """What ``tokens`` cost at ``prices``, the dearest of the deployments' when there are
    several, each at the tier the call's input passes; ``None`` when a price they need is
    missing."""
    if not isinstance(prices, Prices):
        found = [cost(each, tokens) for each in prices]
        if not found or any(each is None for each in found):
            return None
        return max(cast(list[float], found))
    prices = prices.at(tokens.fresh + tokens.cache_read + tokens.cache_write)
    if tokens.cache_write and prices.cache_write is None:
        return None
    read = prices.cache_read if prices.cache_read is not None else prices.input
    total = (
        tokens.fresh * prices.input
        + tokens.cache_read * read
        + tokens.cache_write * (prices.cache_write or 0.0)
        + tokens.output * prices.output
    )
    return total if math.isfinite(total) and total >= 0 else None


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


_TIERED: Final = re.compile(
    r"(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost"
    r"|cache_creation_input_token_cost)_above_(\d{1,7}(?:\.\d{1,3})?)k_tokens"
)
_KINDS: Final = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read",
    "cache_creation_input_token_cost": "cache_write",
}


def _tiers(fields: dict[str, Any]) -> tuple[Tier, ...] | None:
    """A deployment's tiers from its ``…_above_<N>k_tokens`` fields; ``None`` when one of
    them is not a price, which leaves the deployment unpriced."""
    rates: dict[int, dict[str, float]] = {}
    for key, value in fields.items():
        if (named := _TIERED.fullmatch(key)) is None:
            continue
        if (price := _price(value)) is None:
            return None
        above = round(float(named.group(2)) * 1000)
        rates.setdefault(above, {})[_KINDS[named.group(1)]] = price
    return tuple(Tier(above, **found) for above, found in sorted(rates.items()))


def prices_of(info: object, alias: str) -> tuple[Prices, ...] | None:
    """``alias``'s prices in a ``/model/info`` answer, one for each of its deployments, or
    ``None`` when any of them lacks an input or output price, or gives a tier's price that
    is not one."""
    found = cast(dict[str, Any], info).get("data") if isinstance(info, dict) else None
    entries = [
        cast(dict[str, Any], entry)
        for entry in (cast(list[object], found) if isinstance(found, list) else [])
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("model_name") == alias
    ]
    priced: list[Prices] = []
    for entry in entries:
        model_info = entry.get("model_info")
        fields = cast(dict[str, Any], model_info) if isinstance(model_info, dict) else {}
        given, made = (
            _price(fields.get("input_cost_per_token")),
            _price(fields.get("output_cost_per_token")),
        )
        tiers = _tiers(fields)
        if given is None or made is None or tiers is None:
            return None
        priced.append(
            Prices(
                given,
                made,
                _price(fields.get("cache_read_input_token_cost")),
                _price(fields.get("cache_creation_input_token_cost")),
                tiers,
            )
        )
    return tuple(priced) or None


def count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _details(usage: Mapping[str, object], key: str, field: str) -> int:
    details = usage.get(key)
    found = cast(dict[str, object], details).get(field) if isinstance(details, dict) else None
    return count(found) or 0


def chat_tokens(usage: Mapping[str, object]) -> Tokens | None:
    """A chat completion's tokens: ``prompt_tokens`` counts the cached ones
    (``prompt_tokens_details.cached_tokens``), and LiteLLM's ``cache_creation_input_tokens``
    for a Claude model behind it."""
    prompt, output = count(usage.get("prompt_tokens")), count(usage.get("completion_tokens"))
    if prompt is None or output is None:
        return None
    read = _details(usage, "prompt_tokens_details", "cached_tokens")
    write = count(usage.get("cache_creation_input_tokens")) or 0
    return Tokens(max(0, prompt - read - write), read, write, output)


def responses_tokens(usage: Mapping[str, object]) -> Tokens | None:
    """A response's tokens: OpenAI's ``input_tokens`` counts the cached ones
    (``input_tokens_details.cached_tokens``)."""
    given, output = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
    if given is None or output is None:
        return None
    read = _details(usage, "input_tokens_details", "cached_tokens")
    return Tokens(max(0, given - read), read, 0, output)


def messages_tokens(usage: Mapping[str, object]) -> Tokens | None:
    """A Claude message's tokens: its ``input_tokens`` leave out the cache's."""
    given, output = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
    if given is None or output is None:
        return None
    read = count(usage.get("cache_read_input_tokens")) or 0
    write = count(usage.get("cache_creation_input_tokens")) or 0
    return Tokens(given, read, write, output)


class PriceList:
    """The gateway's prices by alias, read from its model information once and kept; a
    failed read is tried again after ``RETRY_SECONDS``. Models made by one gateway share
    one."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._clock = clock
        self._info: object | None = None
        self._failed_at: float | None = None

    async def prices(
        self, alias: str, session: Callable[[], aiohttp.ClientSession]
    ) -> tuple[Prices, ...] | None:
        """``alias``'s prices, or ``None`` when the gateway gives none."""
        if self._info is None:
            if (
                self._failed_at is not None
                and self._clock.monotonic() - self._failed_at < RETRY_SECONDS
            ):
                return None
            self._info = await self._read(session())
            if self._info is None:
                self._failed_at = self._clock.monotonic()
                return None
        return prices_of(self._info, alias)

    async def _read(self, session: aiohttp.ClientSession) -> object | None:
        from .model import ModelError, read_limited  # which imports this module

        try:
            async with session.get(
                f"{self.base_url}/model/info",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                data = await read_limited(response, INFO_BYTES)
                if not 200 <= response.status < 300:
                    _log.warning("the gateway gives no prices: HTTP %s", response.status)
                    return None
        except (aiohttp.ClientError, TimeoutError, ModelError) as error:
            _log.warning("the gateway gives no prices: %s", type(error).__name__)
            return None
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            return None
