# SPDX-License-Identifier: MIT
"""Notices leased to a run (B-04), the same for the user's work requests, review
findings and math findings: an offer leases them to the run; a lease runs out unless
renewed; a run releases what it did not deliver; an acknowledged notice is never offered
again; and a run is offered at most 128 of a source, in batches that fit its budget."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.mathchecks.notices import MathNotices

from .test_consistency import claim, dispatch, raise_finding
from .test_document_math import BAD, MAIN, ScriptedChecker, create, drain
from .test_document_math import stores as stores

Json = dict[str, Any]


@dataclass
class Source:
    offer: Callable[..., list[Json]]
    renew: Callable[[str], None]
    release: Callable[[str, set[str] | None], None]
    acknowledge: Callable[[str, list[str]], None]
    field: str

    def ids(self, run: str, seen: set[str] | None = None, **budget: int) -> list[str]:
        return [str(n[self.field]) for n in self.offer(run, seen or set(), **budget)]


def requests(docs, reviews, clock) -> Source:
    doc = docs.create("p", name="asked", title="A", text="One.\n\nTwo.\n", author=USER)
    api = Annotations(docs, clock=lambda: clock[0])
    for number in range(3):
        api.change(
            "p",
            doc.document_id,
            {
                "action": "create",
                "anchor": {"revision": 1, "block": "b1"},
                "body": f"Request {number}.",
                "addressed_to": "s",
                "operation_id": f"ask-{number}",
            },
            USER,
        )
    return Source(
        lambda run, seen, **budget: api.pending("p", "s", run, seen=seen, **budget),
        lambda run: api.renew("p", "s", run),
        lambda run, keep: api.release("p", "s", run, keep=keep),
        lambda run, ids: api.acknowledge("p", "s", run, ids),
        "request_id",
    )


def findings(docs, reviews, clock) -> Source:
    claim(reviews)
    review = dispatch(reviews)
    for category in ("contradicting_value", "evidence_strength", "method"):
        raise_finding(reviews, review, category=category, operation_id=category)
    return _findings(FindingNotices(reviews))


def math(docs, reviews, clock) -> Source:
    create(docs, MAIN, "\n\n".join(f"Paragraph {n}: {BAD}" for n in range(3)) + "\n")
    return _findings(MathNotices(reviews))


def _findings(api: FindingNotices) -> Source:
    return Source(
        lambda run, seen, **budget: api.pending("p", "s", run, seen, **budget),
        lambda run: api.renew("p", "s", run),
        lambda run, keep: api.release("p", "s", run, keep),
        lambda run, ids: api.acknowledge("p", "s", run, ids),
        "notice_id",
    )


SOURCES = {"requests": requests, "findings": findings, "math": math}


@pytest.fixture(params=sorted(SOURCES))
async def source(request, stores) -> Source:
    docs, reviews, clock = stores
    found = SOURCES[request.param](docs, reviews, clock)
    if request.param == "math":
        await drain(reviews, ScriptedChecker())
    assert len(found.ids("probe")) == 3
    found.release("probe", None)
    return found


@pytest.mark.asyncio
async def test_an_offer_leases_its_notices_to_the_run(source):
    offered = source.ids("r1")
    assert len(offered) == 3
    assert source.ids("r2") == []
    assert source.ids("r1") == offered  # the run itself is offered them again


@pytest.mark.asyncio
async def test_a_lease_runs_out_unless_renewed(source, stores):
    _, _, clock = stores
    offered = source.ids("r1")
    clock[0] += 100
    source.renew("r1")
    clock[0] += 100
    assert source.ids("r2") == []
    clock[0] += 21
    assert source.ids("r2") == offered


@pytest.mark.asyncio
async def test_a_run_releases_what_it_did_not_deliver(source):
    first, *rest = source.ids("r1")
    source.release("r1", {first})
    assert source.ids("r2") == rest


@pytest.mark.asyncio
async def test_an_acknowledged_notice_is_not_offered_again(source, stores):
    _, _, clock = stores
    first, *rest = source.ids("r1")
    source.acknowledge("r1", [first])
    source.release("r1", None)
    assert source.ids("r2") == rest
    clock[0] += 1000
    assert source.ids("r3") == rest


@pytest.mark.asyncio
async def test_a_run_is_offered_at_most_128_in_batches_that_fit(source):
    assert source.ids("r1", {f"x{n}" for n in range(128)}) == []
    first, *rest = source.ids("r1")
    assert source.ids("r1", {first}) == rest
    source.release("r1", None)
    (notice,) = source.offer("r2", set(), max_chars=10_000)[:1]
    size = len(json.dumps([notice], ensure_ascii=False, separators=(",", ":")))
    source.release("r2", None)
    assert source.ids("r3", max_chars=size) == [first]
