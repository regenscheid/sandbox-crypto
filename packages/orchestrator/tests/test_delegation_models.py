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
from sandbox_science.orchestrator.projects import RunRecord
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
    alone = ModelChoice("delegated", ALLOWED, Listing("delegated"))
    with pytest.raises(WorkerAPIError, match="lists no model 'checker-b'") as refused:
        await alone.choose("checker-b")
    assert refused.value.kind == "unavailable"
    assert str(refused.value).startswith("model_unavailable: ")
    gone = DelegationSettings(models=("checker-b",), fallback="old")
    with pytest.raises(WorkerAPIError, match="nor the project's fallback 'old'"):
        await ModelChoice("delegated", gone, Listing("main")).choose("checker-b")


async def test_the_roles_own_alias_is_called_when_the_gateway_does_not_list_it() -> None:
    """Lab 40: the owner's LiteLLM served MAIN's alias, which its list of models left out,
    so every delegation was refused. The role's own alias is the deployment's, as MAIN's
    is, and the run's first call tells whether the gateway serves it."""
    for settings in (DelegationSettings(), DelegationSettings(fallback="old")):
        choice = ModelChoice("delegated", settings, Listing("main"))
        assert await choice.choose(None) == Choice("delegated", "delegated")
        assert await choice.choose("delegated") == Choice("delegated", "delegated")
    # A fallback the gateway lists still replaces it, and the delegation records that.
    replaced = await ModelChoice("delegated", ALLOWED, Listing("main")).choose(None)
    assert replaced == Choice("main", "delegated")


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


def answer(root: RunRecord, n: int) -> dict[str, Any]:
    """The root's ``n``-th tool result, without the delegation notices that ride on it
    when a child ended before the call returned."""
    found = json.loads(results(root)[n])
    return json.loads(found["tool_result"]) if "tool_result" in found else found


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
        "description": "Optional: the model alias the delegated run runs on, of those the "
        "project allows; without it, the delegated role's",
    }
    began = answer(root, 0)
    assert began["model"] == {"alias": "checker-b"}
    refused = answer(root, 1)["error"]
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
    refused = answer(root, 0)["error"]
    assert (refused["kind"], refused["message"].split(":")[0]) == (
        "policy_denied",
        "model_not_allowed",
    )


async def test_an_unlisted_alias_is_refused_without_a_fallback(delegating: Harness) -> None:
    """PD-F11: the alias a brief chose is missing from the gateway's list."""
    delegating.gateway.listed = ["main", "delegated"]
    configure(delegating, models=("checker-b",))
    check = call("delegate", check={"entry": "C-1", "revision": 1}, model="checker-b")
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), Reply("No check.")],
        options=["--delegated-model", "delegated"],
    )
    refused = answer(root, 0)["error"]
    assert refused["kind"] == "unavailable"
    assert refused["message"].startswith("model_unavailable: the model gateway lists no model")
    assert "delegations" not in root.summaries


async def test_the_roles_unlisted_alias_runs_the_check(delegating: Harness) -> None:
    """Lab 40: a gateway that serves the delegated role's alias without listing it."""
    delegating.gateway.listed = ["main"]
    check = call("delegate", check={"entry": "C-1", "revision": 1})
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        options=["--delegated-model", "delegated"],
    )
    assert answer(root, 0)["model"] == {"alias": "delegated"}
    (child,) = children(delegating, root)
    assert child.alias == "delegated"


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
    assert answer(root, 0)["model"] == recorded
    (child,) = children(delegating, root)
    assert child.alias == "main"
    assert {usage.model for usage in child.usage} == {"scripted-main"}
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.alias == "main"
    assert outcome.brief["model"] == recorded


# --- An alias that cannot call tools (PD-30) -------------------------------------------------


class Capable:
    """A gateway's description of its aliases, and how often it was read."""

    def __init__(self, **calls: bool) -> None:
        self.calls, self.read = dict(calls), 0

    async def __call__(self) -> dict[str, bool] | None:
        self.read += 1
        return self.calls


async def test_an_alias_that_cannot_call_tools_falls_back_or_is_refused() -> None:
    capable = Capable(delegated=False, main=True)
    listed = Listing("delegated", "main", "checker-b")
    choice = ModelChoice("delegated", ALLOWED, listed, capable)
    fell = await choice.choose(None)
    assert fell.recorded() == {"alias": "main", "asked": "delegated", "fallback": True}
    assert await choice.choose("checker-b") == Choice("checker-b", "checker-b")  # not described
    assert capable.read == 1  # read once for the parent's run
    alone = ModelChoice(
        "delegated", DelegationSettings(), Listing("delegated"), Capable(delegated=False)
    )
    with pytest.raises(WorkerAPIError, match="says 'delegated' cannot call tools") as refused:
        await alone.choose(None)
    assert refused.value.kind == "unavailable"
    assert str(refused.value).startswith("model_unavailable: ")
    worse = ModelChoice("delegated", ALLOWED, None, Capable(delegated=False, main=False))
    with pytest.raises(WorkerAPIError, match="fallback 'main' is no better"):
        await worse.choose(None)


async def test_the_gateway_says_which_aliases_call_tools(gateway: Gateway) -> None:
    found = ModelGateway(gateway.url, API_KEY)
    try:
        assert await found.tool_calling() is None  # 404: it cannot say
        gateway.capable = {"main": True, "plain": False}
        assert await found.tool_calling() == {"main": True, "plain": False}
    finally:
        await found.close()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"data": "x"}', None),
        ("not json", None),
        (
            json.dumps(
                {
                    "data": [
                        {"model_name": "a", "model_info": {"supports_function_calling": False}},
                        {"model_name": "a", "model_info": {"supports_function_calling": True}},
                        {"model_name": "b", "model_info": {"supports_function_calling": None}},
                        {"model_name": "c"},
                        "x",
                    ]
                }
            ),
            {"a": True},
        ),
    ],
)
async def test_a_description_that_says_nothing_leaves_the_alias_out(
    body: str, expected: dict[str, bool] | None
) -> None:
    async def answer(request: web.Request) -> web.Response:
        return web.Response(text=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/model/info", answer)
    async with TestServer(app) as server:
        found = ModelGateway(str(server.make_url("")), API_KEY)
        try:
            assert await found.tool_calling() == expected
        finally:
            await found.close()


async def test_a_delegated_alias_that_cannot_call_tools_is_refused(delegating: Harness) -> None:
    delegating.gateway.capable = {"delegated": False}
    check = call("delegate", check={"entry": "C-1", "revision": 1})
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), Reply("No check.")],
        options=["--delegated-model", "delegated"],
    )
    refused = answer(root, 0)["error"]
    assert refused["kind"] == "unavailable"
    assert "cannot call tools" in refused["message"]
    configure(delegating, fallback="main")
    delegating.gateway.capable = {"delegated": False, "main": True}
    root = await delegating.run(
        "again",
        [Reply(calls=[check]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        options=["--delegated-model", "delegated"],
    )
    assert answer(root, 0)["model"] == {"alias": "main", "asked": "delegated", "fallback": True}
