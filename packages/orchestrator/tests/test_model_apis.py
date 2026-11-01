# SPDX-License-Identifier: MIT
"""Each alias's adapter (docs/specs/context-management.md, CM-41): the deployment's
setting, else ``messages`` when the gateway lists the alias's provider as Anthropic, else
the deployment's default; chosen once by the run service, adopted by a confined control
service from its runtime's handshake, and kept by a continuation in its pinned profile.
The gateways are fakes in this process, with synthetic keys."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.models import (
    CallbackGateway,
    CallbackModel,
    RuntimeModels,
)
from sandbox_science.orchestrator.confinement.transport import Capability, Channel
from sandbox_science.orchestrator.confinement.wire import ModelRequest, payload
from sandbox_science.orchestrator.harness import Harness, Role, RunRequest
from sandbox_science.orchestrator.messages import MessagesModel
from sandbox_science.orchestrator.model import (
    ModelGateway,
    OpenAICompatibleModel,
    adapter_of,
)
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole, profiles
from sandbox_science.orchestrator.responses import ResponsesModel
from sandbox_science.orchestrator.worker_client import WorkerClient

from .loop_harness import Harness as LoopHarness
from .loop_harness import Reply as Scripted
from .loop_harness import call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_messages_model import KEY, Fake
from .test_messages_model import fake as fake

ATTEMPT = "cap-" + "a" * 48


def entry(alias: str, provider: str | None) -> dict[str, Any]:
    info = {} if provider is None else {"litellm_provider": provider}
    return {"model_name": alias, "litellm_params": {"model": f"{provider}/x"}, "model_info": info}


DESCRIBED = [
    entry("claude", "anthropic"),
    entry("gpt", "openai"),
    entry("mixed", "anthropic"),
    entry("mixed", "bedrock"),
    entry("twice", "anthropic"),
    entry("twice", "anthropic"),
    entry("unsaid", None),
    entry("named", "anthropic"),
]


@pytest_asyncio.fixture
async def info() -> AsyncIterator[dict[str, Any]]:
    """A gateway's ``/model/info``: ``data`` is what it describes; ``status`` its code."""
    state: dict[str, Any] = {"data": DESCRIBED, "status": 200, "asked": 0}

    async def answer(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        state["asked"] += 1
        if state["status"] != 200:
            return web.Response(status=state["status"], text="no")
        return web.json_response({"data": state["data"]})

    app = web.Application()
    app.router.add_get("/v1/model/info", answer)
    async with TestServer(app) as server:
        state["url"] = str(server.make_url("/v1"))
        yield state


async def test_an_alias_left_out_takes_messages_when_the_gateway_lists_it_as_anthropic(
    info: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    gateway = ModelGateway(info["url"], KEY, apis={"named": "chat"}, default_api="responses")
    try:
        with caplog.at_level(logging.INFO, "sandbox_science.orchestrator.model"):
            chosen = await gateway.choose_apis()
        assert chosen == {
            "claude": "messages",
            "twice": "messages",
            "gpt": "responses",
            "mixed": "responses",  # one deployment is not Anthropic's
            "unsaid": "responses",
            "named": "chat",  # the deployment's setting always wins
        }
        said = caplog.text
        assert (
            "model API for claude: messages (the gateway lists its provider as anthropic)" in said
        )
        assert "model API for named: chat (named by the deployment)" in said
        assert "model API for mixed: responses (the deployment's default; the gateway lists" in said
        assert "anthropic, bedrock" in said
        assert info["asked"] == 1
        assert gateway.profile("claude").api == "messages"
        assert isinstance(gateway.model("claude"), MessagesModel)
        assert isinstance(gateway.model("gpt"), ResponsesModel)
        assert isinstance(gateway.model("named"), OpenAICompatibleModel)
        # An alias the gateway does not describe takes the default.
        assert gateway.api("elsewhere") == "responses"
    finally:
        await gateway.close()


async def test_the_choice_covers_the_aliases_asked_for(info: dict[str, Any]) -> None:
    gateway = ModelGateway(info["url"], KEY)
    try:
        assert await gateway.choose_apis(["claude", "elsewhere"]) == {
            "claude": "messages",
            "elsewhere": "chat",
        }
    finally:
        await gateway.close()


async def test_a_gateway_that_cannot_be_asked_leaves_the_default(
    info: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    info["status"] = 500
    gateway = ModelGateway(info["url"], KEY, apis={"named": "messages"})
    try:
        chosen = await gateway.choose_apis(["claude", "named"])
    finally:
        await gateway.close()
    assert chosen == {"claude": "chat", "named": "messages"}
    assert "aliases the deployment names no API for use chat" in caplog.text


async def test_a_pinned_profile_keeps_its_adapter(info: dict[str, Any]) -> None:
    """A continuation keeps the adapter its attempt started with, whatever the
    deployment's choice is now (CM-41)."""
    gateway = ModelGateway(info["url"], KEY, apis={"claude": "messages"})
    try:
        assert adapter_of(gateway.model("claude", role="main")) == "messages"
        with profiles({"main": ModelProfile(alias="claude", api="chat")}):
            model = gateway.model("claude", role="main")
            assert isinstance(model, OpenAICompatibleModel)
            assert model.validate_profile().api == "chat"
            # Another role, or another alias, is not pinned by it.
            assert adapter_of(gateway.model("claude", role="fold")) == "messages"
        assert adapter_of(gateway.model("claude", role="main", api="responses")) == "responses"
    finally:
        await gateway.close()


async def test_the_harness_runs_a_continuation_on_its_pinned_adapter(info: dict[str, Any]) -> None:
    gateway = ModelGateway(info["url"], KEY, apis={"claude": "messages"})
    try:
        main = Role(gateway.model("claude", role="main"), "claude")
        harness = Harness(cast(WorkerClient, object()), main, gateway=gateway)
        request = RunRequest(project="p", session="s", task="t", operator=True)
        assert harness.role(request) is main
        pinned: dict[ModelRole, ModelProfile] = {"main": ModelProfile(alias="claude", api="chat")}
        continued = harness.role(replace(request, model_profiles=pinned))
        assert continued is not main
        assert adapter_of(continued.model) == "chat"
        same: dict[ModelRole, ModelProfile] = {"main": ModelProfile(alias="claude", api="messages")}
        assert harness.role(replace(request, model_profiles=same)) is main
    finally:
        await gateway.close()


def test_a_confined_control_service_adopts_the_runtime_s_choice() -> None:
    """The callers control makes before the handshake read their profile when they need
    it, so the adapters the runtime chose apply to them; the deployment's own setting
    is kept, and a pinned profile still wins."""

    async def send(_: str) -> None:
        pass

    async def handle(_: object, __: object) -> None:
        pass

    gateway = CallbackGateway(
        Channel(send, handle),
        apis={"named": "chat"},
        default_api="responses",
        reasoning_effort={"reviewer": "medium"},
    )
    main = gateway.model("claude", role="main")
    reviewer = CallbackModel(
        gateway.channel,
        "claude",
        kind="reviewer",
        role="reviewer",
        profile=partial(gateway.profile, "claude", role="reviewer"),
    )
    named = gateway.model("named", role="main")
    # A harness without a delegated model delegates on the main alias, also read late.
    harness = Harness(cast(WorkerClient, object()), Role(main, "claude"), gateway=gateway)
    assert main.requested_profile.api == reviewer.requested_profile.api == "responses"
    gateway.adopt(None)
    assert main.requested_profile.api == "responses"
    gateway.adopt({"claude": "messages", "named": "messages"})
    assert main.requested_profile.api == "messages"
    delegated = harness.delegated.model
    assert isinstance(delegated, CallbackModel)
    assert delegated.requested_profile.api == "messages"
    assert reviewer.requested_profile == ModelProfile(
        alias="claude", api="messages", reasoning_effort="medium"
    )
    assert named.requested_profile.api == "chat"
    with profiles({"main": ModelProfile(alias="claude", api="chat")}):
        assert main.requested_profile.api == "chat"
    fixed = gateway.model("claude", role="main", api="responses")
    assert fixed.requested_profile.api == "responses"


async def test_the_runtime_calls_through_the_adapter_the_attempt_pinned(fake: Fake) -> None:
    """The runtime's own choice is messages, but the attempt pinned chat: the call goes
    to chat completions, and the profile reaches the paid intent unchanged."""
    intents: list[JsonValue] = []

    async def host(kind: Capability, value: JsonValue) -> JsonValue:
        if kind is Capability.MODEL_INTENT:
            intents.append(value)
            return {"identity": "intent-1"}
        assert kind is Capability.MODEL_OBSERVED
        return None

    async def runtime(kind: Capability, value: JsonValue) -> JsonValue:
        raise AssertionError("the runtime is asked nothing here")

    left = Channel(lambda frame: right.receive(frame), host)
    right = Channel(lambda frame: left.receive(frame), runtime)
    gateway = ModelGateway(fake.url, KEY, apis={"claude": "messages"})
    models = RuntimeModels(gateway, frozenset({"claude"}), right)
    try:
        pinned = ModelProfile(alias="claude", api="chat", reasoning_effort="high")
        request = ModelRequest(
            attempt=ATTEMPT,
            role="main",
            kind="main",
            profile=pinned,
            model="claude",
            messages=(),
            tools=(),
        )
        await models.complete(payload(request))
        assert fake.paths == ["/v1/chat/completions"]
        assert fake.bodies[-1]["reasoning_effort"] == "high"
        assert isinstance(intents[0], dict)
        assert intents[0]["profile"] == pinned.model_dump()
    finally:
        await gateway.close()
        await left.close()
        await right.close()


async def test_a_run_on_an_anthropic_alias_goes_through_messages_by_default(
    harness: LoopHarness,
) -> None:
    """The command line names no API: the run service asks the gateway once, which lists
    the alias's provider as Anthropic, so the run's calls go to ``/v1/messages``."""
    harness.gateway.served = {"main": "anthropic/claude-opus-5-5"}
    harness.gateway.providers = {"main": "anthropic"}
    looking = Scripted("Looking around.", calls=[call("shell", script="echo hello")])
    record = await harness.run("run-1", [looking, Scripted("Done.")])
    found = harness.gateway
    assert found.main == []
    assert len(found.claude) == 2
    assert record.final_text == "Done."
    harness.gateway.providers = {"main": "openai"}
    record = await harness.run("run-2", [Scripted("Plain.")])
    assert len(found.claude) == 2  # chat completions, the default
    assert record.final_text == "Plain."
