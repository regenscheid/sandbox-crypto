# SPDX-License-Identifier: MIT
"""The model a delegated run calls (plans-and-delegation spec, PD-28 and PD-30; PD-F10,
PD-F11): the brief's choice among the aliases the project allows, the delegated role's
otherwise, and an alias the gateway does not list, refused or replaced by the project's
fallback, which the delegation records."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.delegation.models import Choice, ModelChoice
from sandbox_science.orchestrator.delegation.records import DelegationSettings
from sandbox_science.orchestrator.model import ModelGateway
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .loop_harness import API_KEY, Gateway, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import REPORT, children, configure, delegation, results, started, wait
from .test_delegation import delegating as delegating

ALLOWED = DelegationSettings(models=("checker-b",), fallback="main")


class Listing:
    """A gateway's list of aliases, and how often it was read."""

    def __init__(self, *aliases: str) -> None:
        self.aliases, self.calls = frozenset(aliases), 0

    async def __call__(self) -> frozenset[str] | None:
        self.calls += 1
        return self.aliases


async def test_a_brief_chooses_only_an_alias_the_project_allows() -> None:
    choice = ModelChoice("delegated", ALLOWED, Listing("delegated", "checker-b", "main"))
    assert await choice.choose(None) == Choice("delegated", "delegated")
    assert await choice.choose("delegated") == Choice("delegated", "delegated")
    assert await choice.choose("checker-b") == Choice("checker-b", "checker-b")
    with pytest.raises(WorkerAPIError, match=r"model_not_allowed: .*checker-b") as refused:
        await choice.choose("other")
    assert refused.value.kind == "policy_denied"
    with pytest.raises(WorkerAPIError, match="choose these models: none"):
        await ModelChoice("delegated", DelegationSettings()).choose("checker-b")


async def test_an_alias_the_gateway_does_not_list_falls_back_or_is_refused() -> None:
    listed = Listing("main")
    choice = ModelChoice("delegated", ALLOWED, listed)
    fell = await choice.choose(None)
    assert (fell.alias, fell.fallback) == ("main", True)
    assert fell.recorded() == {"alias": "main", "asked": "delegated", "fallback": True}
    assert (await choice.choose("checker-b")).alias == "main"
    assert listed.calls == 1  # the list is read once for the parent's run
    alone = ModelChoice("delegated", DelegationSettings(), Listing("main"))
    with pytest.raises(WorkerAPIError, match="names no fallback") as refused:
        await alone.choose(None)
    assert refused.value.kind == "unavailable"
    assert str(refused.value).startswith("model_unavailable: ")
    gone = DelegationSettings(fallback="old")
    with pytest.raises(WorkerAPIError, match="nor the project's fallback 'old'"):
        await ModelChoice("delegated", gone, Listing("main")).choose(None)


async def test_a_gateway_that_cannot_list_is_taken_at_its_word() -> None:
    async def unknown() -> frozenset[str] | None:
        return None

    choice = ModelChoice("delegated", ALLOWED, unknown)
    assert await choice.choose("checker-b") == Choice("checker-b", "checker-b")
    # Without a gateway, only the delegated role's model can be called.
    alone = ModelChoice("delegated", ALLOWED)
    assert await alone.choose(None) == Choice("delegated", "delegated")
    with pytest.raises(WorkerAPIError, match="no model gateway serves 'checker-b'"):
        await alone.choose("checker-b")


async def test_the_gateway_lists_its_aliases_or_says_it_cannot(gateway: Gateway) -> None:
    found = ModelGateway(gateway.url, API_KEY)
    try:
        assert await found.aliases() is None  # 404: it cannot list them
        gateway.listed = ["main", "checker-b"]
        assert await found.aliases() == {"main", "checker-b"}
        model = found.model("checker-b")
        assert model.model == "checker-b"
    finally:
        await found.close()


@pytest.mark.parametrize(
    "body", ['{"data": "x"}', "[1]", "not json", '{"data": [{"id": 3}, "x", {"id": "m"}]}']
)
async def test_an_answer_that_is_not_a_list_lists_nothing(body: str) -> None:
    async def answer(request: web.Request) -> web.Response:
        return web.Response(text=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/models", answer)
    async with TestServer(app) as server:
        found = ModelGateway(str(server.make_url("")), API_KEY)
        try:
            listed = await found.aliases()
        finally:
            await found.close()
    assert listed == ({"m"} if "id" in body else None)


def model_tool(harness: Harness) -> dict[str, Any] | None:
    (first, *_) = harness.gateway.tools["main"]
    (delegate,) = [t for t in first if t["function"]["name"] == "delegate"]
    return delegate["function"]["parameters"]["properties"].get("model")


async def test_a_delegation_runs_on_the_alias_its_brief_chose(delegating: Harness) -> None:
    """PD-F10: the project allows checker-b only; one brief chooses it, another chooses an
    alias it does not allow, and is refused."""
    configure(delegating, models=("checker-b",))
    chosen = call("delegate", check={"entry": "C-1", "revision": 1}, model="checker-b")
    other = call("delegate", check={"entry": "C-1", "revision": 1}, model="other")
    root = await delegating.run(
        "root",
        [Reply(calls=[chosen, other]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
    )
    assert model_tool(delegating) == {
        "type": "string",
        "enum": ["checker-b"],
        "description": "Optional: the model alias the checker runs on, of those the project "
        "allows; without it, the delegated role's",
    }
    began = json.loads(results(root)[0])
    assert began["model"] == {"alias": "checker-b"}
    refused = json.loads(results(root)[1])["error"]
    assert refused["kind"] == "policy_denied"
    assert refused["message"].startswith("model_not_allowed")
    (child,) = children(delegating, root)
    assert child.alias == "checker-b"
    assert {usage.model for usage in child.usage} == {"scripted-checker-b"}
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.alias, outcome.models) == ("checker-b", ("scripted-checker-b",))
    assert outcome.brief["model"] == {"alias": "checker-b"}


async def test_a_run_offered_no_choice_has_no_model_to_choose(delegating: Harness) -> None:
    chosen = call("delegate", check={"entry": "C-1", "revision": 1}, model="checker-b")
    root = await delegating.run("root", [Reply(calls=[chosen]), Reply("Refused.")])
    assert model_tool(delegating) is None
    refused = json.loads(results(root)[0])["error"]
    assert (refused["kind"], refused["message"].split(":")[0]) == (
        "policy_denied",
        "model_not_allowed",
    )


async def test_an_unlisted_alias_is_refused_without_a_fallback(delegating: Harness) -> None:
    """PD-F11: the delegated alias is missing from the gateway's list."""
    delegating.gateway.listed = ["main"]
    check = call("delegate", check={"entry": "C-1", "revision": 1})
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), Reply("No check.")],
        options=["--delegated-model", "delegated"],
    )
    refused = json.loads(results(root)[0])["error"]
    assert refused["kind"] == "unavailable"
    assert refused["message"].startswith("model_unavailable: the model gateway lists no model")
    assert "delegations" not in root.summaries


async def test_a_fallback_replaces_an_unlisted_alias_and_is_recorded(
    delegating: Harness,
) -> None:
    delegating.gateway.listed = ["main"]
    configure(delegating, fallback="main")
    check = call("delegate", check={"entry": "C-1", "revision": 1})
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        options=["--delegated-model", "delegated"],
    )
    recorded = {"alias": "main", "asked": "delegated", "fallback": True}
    assert json.loads(results(root)[0])["model"] == recorded
    (child,) = children(delegating, root)
    assert child.alias == "main"
    assert {usage.model for usage in child.usage} == {"scripted-main"}
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.alias == "main"
    assert outcome.brief["model"] == recorded
