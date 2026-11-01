# SPDX-License-Identifier: MIT
"""Deterministic test shards: ``--shard I/N`` keeps the I-th of N parts of a selection.

A pytest plugin, loaded with ``-p sandbox_science.testing.shards`` (``scripts/tiers.py
run --shard I/N`` does so). It acts after every other selection (``-m``, ``-k``,
``--deselect``, ``--ignore``), so the shards of a tier together are that tier.

Tests are shared out by module (the node ID's file path), so a module's tests and its
module-scoped fixtures stay in one shard. The modules go, largest first, each to the
shard with the fewest tests so far (ties by name and by shard number), which balances
the shards by test count. The assignment depends only on the selected node IDs: every
job, and every xdist worker within a job, given the same selection computes the same
shards.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

import pytest

_SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


def parse_shard(text: str) -> tuple[int, int]:
    """``"I/N"`` as ``(I, N)``, with ``1 <= I <= N``; ``ValueError`` otherwise."""
    match = _SHARD.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"a shard is I/N, such as 1/3, not {text!r}")
    index, total = int(match[1]), int(match[2])
    if index > total:
        raise ValueError(f"shard {index} of {total} does not exist")
    return index, total


def module_of(nodeid: str) -> str:
    """The test module a node ID belongs to: its file path."""
    return nodeid.split("::", 1)[0]


def assign(nodeids: Iterable[str], total: int) -> dict[str, int]:
    """Each module of ``nodeids`` with its shard, numbered from 1 to ``total``."""
    if total < 1:
        raise ValueError("there is at least one shard")
    sizes = Counter(module_of(nodeid) for nodeid in nodeids)
    loads = [0] * total
    shards: dict[str, int] = {}
    for module, size in sorted(sizes.items(), key=lambda entry: (-entry[1], entry[0])):
        lightest = min(range(total), key=lambda shard: (loads[shard], shard))
        loads[lightest] += size
        shards[module] = lightest + 1
    return shards


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only the I-th of N shards of the selected tests, divided by module",
    )


def pytest_configure(config: pytest.Config) -> None:
    text = config.getoption("shard")
    if text is not None:
        try:
            parse_shard(str(text))
        except ValueError as error:
            raise pytest.UsageError(f"--shard: {error}") from error


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the tests of other shards, after every other selection has been made."""
    text = config.getoption("shard")
    if text is None:
        return
    index, total = parse_shard(str(text))
    shards = assign((item.nodeid for item in items), total)
    kept = [item for item in items if shards[module_of(item.nodeid)] == index]
    if len(kept) != len(items):
        config.hook.pytest_deselected(
            items=[item for item in items if shards[module_of(item.nodeid)] != index]
        )
        items[:] = kept
