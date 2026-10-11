# SPDX-License-Identifier: MIT
"""Repairs of math by the reviewer model: the service, its limits, its endpoint, its settings.

The tests under "The original's cases" are adapted from cscience-crypto's
test/unit/math-repair.test.mjs, contributed under the MIT License by its author. No real
model is asked: a scripted model, or a fake gateway, answers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.orchestrator.model import (
    Message,
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolSpec,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace import cli
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.mathrepair import (
    MAX_ACTIVE,
    MAX_OUTPUT_TOKENS,
    MAX_WAITING,
    SYSTEM_PROMPT,
    MathRepairService,
    RepairAnswer,
    RepairRequest,
    RepairRequestError,
    SessionLimitError,
    disabled_reason,
    gateway_service,
    parse_request,
    reviewer_alias,
    sanitize,
)

KEY = "test-secret-gateway-key-0123456789"
TOKEN = "test-secret-login-token-0123456789abcdef"  # noqa: S105 - a test value

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def request(tex: str, display: bool = False, error: str = "e") -> RepairRequest:
    return RepairRequest(tex=tex, display=display, error=error)


@dataclass
class Gateway:
    """A fake model gateway: each request's path, headers and body, and its answers."""

    answer: Callable[[int], Awaitable[web.Response]]
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    url: str = ""


def completion(text: str) -> web.Response:
    return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async def default(_count: int) -> web.Response:
        return completion("x")

    fake = Gateway(default)

    async def completions(incoming: web.Request) -> web.Response:
        fake.calls.append(
            {
                "path": incoming.path,
                "authorization": incoming.headers.get("Authorization"),
                "body": await incoming.json(),
            }
        )
        return await fake.answer(len(fake.calls))

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    async with TestServer(app) as server:
        fake.url = str(server.make_url("/v1"))
        yield fake


class HeldModel:
    """A reviewer that answers only when the test lets it, counting what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.answering = 0
        self.most_at_once = 0
        self.release = asyncio.Event()

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.asked.append(str(messages[-1]["content"]).rsplit("\n", 1)[-1])
        self.answering += 1
        self.most_at_once = max(self.most_at_once, self.answering)
        try:
            await self.release.wait()
        finally:
            self.answering -= 1
        return ModelTurn(text=self.asked[-1] + "}")


# The original's cases.


def test_the_reviewer_alias_is_the_only_model_setting() -> None:
    # The original took an explicit override, then the reviewer alias, and never the
    # session's or the catalog's model. Here the reviewer alias is the only setting: the
    # workspace knows no other model to ask.
    assert reviewer_alias("fixer") == "fixer"
    assert reviewer_alias("  rev ") == "rev"
    assert reviewer_alias(None) is None
    assert reviewer_alias("") is None
    assert MathRepairService(ScriptedModel([]), reviewer="rev").reviewer == "rev"


@pytest.mark.asyncio
async def test_repairs_say_why_they_are_off() -> None:
    off = await MathRepairService().repair(request("x^"), "s")
    assert off == RepairAnswer(enabled=False, reason="no reviewer model configured")
    assert off.to_json() == {
        "enabled": False,
        "tex": None,
        "reason": "no reviewer model configured",
    }
    unnamed = await MathRepairService(ScriptedModel([]), reviewer=None).repair(request("x^"), "s")
    assert unnamed.reason == "no reviewer model configured"


def test_off_0_none_false_and_disabled_turn_repairs_off() -> None:
    for value in ["off", "0", "none", "FALSE", "disabled"]:
        assert reviewer_alias(value) is None
        assert disabled_reason(value) == "disabled"


def test_answers_lose_fences_and_delimiters_and_unusable_ones_are_dropped() -> None:
    assert sanitize("```latex\n$$x^2$$\n```", "x^") == "x^2"
    assert sanitize("\\( \\alpha \\)", "a") == "\\alpha"
    assert sanitize("UNFIXABLE", "a") is None
    assert sanitize("unfixable: it means nothing", "a") is None
    assert sanitize("   ", "a") is None
    assert sanitize("x^2", "x^2") is None
    assert sanitize("y" * 1000, "x") is None
    assert sanitize(None, "x") is None


@pytest.mark.asyncio
async def test_one_bounded_request_through_the_gateway_and_its_answer_kept(
    gateway: Gateway,
) -> None:
    async def answer(_count: int) -> web.Response:
        return completion("$$\\frac{a}{b}$$")

    gateway.answer = answer
    service = gateway_service(gateway.url, "sandbox-reviewer", KEY)
    try:
        wanted = request("\\frac{a}{b", display=True, error="Expected '}'")
        first = await service.repair(wanted, "s1")
        second = await service.repair(wanted, "s2")
    finally:
        await service.close()
    assert first == RepairAnswer(enabled=True, tex="\\frac{a}{b}", model="sandbox-reviewer")
    assert first.to_json() == {
        "enabled": True,
        "model": "sandbox-reviewer",
        "tex": "\\frac{a}{b}",
        "reason": None,
        "cached": False,
    }
    assert second.cached
    assert second.tex == "\\frac{a}{b}"
    (call,) = gateway.calls
    assert call["path"] == "/v1/chat/completions"
    assert call["authorization"] == f"Bearer {KEY}"
    body = call["body"]
    assert body["model"] == "sandbox-reviewer"
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS == 512
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert re.fullmatch(
        r"Mode: display\nKaTeX error: Expected '\}'\nExpression:\n\\frac\{a\}\{b",
        body["messages"][1]["content"],
    )
    assert "temperature" not in body
    assert "tools" not in body


@pytest.mark.asyncio
async def test_repairs_turned_off_never_reach_the_gateway(gateway: Gateway) -> None:
    service = gateway_service(gateway.url, "off", KEY)
    assert await service.repair(request("x^"), "s") == RepairAnswer(
        enabled=False, reason="disabled"
    )
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_unfixable_answers_and_gateway_failures_are_kept(gateway: Gateway) -> None:
    async def answer(count: int) -> web.Response:
        return completion("UNFIXABLE") if count == 1 else web.Response(status=502, text="down")

    gateway.answer = answer
    service = gateway_service(gateway.url, "rev", KEY)
    try:
        unfixable = await service.repair(request("\\nonsense"), "s")
        failed = await service.repair(request("\\other"), "s")
        again = await service.repair(request("\\other"), "s")
    finally:
        await service.close()
    assert unfixable.tex is None
    assert unfixable.reason == "no usable repair"
    assert failed.tex is None
    assert failed.reason is not None
    assert "HTTP 502" in failed.reason
    assert again.cached
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_expression_share_one_call() -> None:
    model = HeldModel()
    service = MathRepairService(model, reviewer="rev")
    one = asyncio.create_task(service.repair(request("x_i_j"), "s1"))
    two = asyncio.create_task(service.repair(request("x_i_j"), "s2"))
    await asyncio.sleep(0.01)
    model.release.set()
    first, second = await asyncio.gather(one, two)
    assert first.tex == second.tex == "x_i_j}"
    assert model.asked == ["x_i_j"]


def test_invalid_requests_are_refused() -> None:
    with pytest.raises(RepairRequestError, match="must be an object"):
        parse_request(None)
    with pytest.raises(RepairRequestError, match="non-empty tex"):
        parse_request({"tex": "   "})
    with pytest.raises(RepairRequestError, match="exceeds 4000"):
        parse_request({"tex": "x" * 5000})
    wanted = parse_request({"tex": "x^", "display": "true", "error": "e" * 900})
    assert wanted == RepairRequest(tex="x^", display=True, error="e" * 500)
    assert not parse_request({"tex": "x^", "display": 1}).display


# This project's own: limits, failures and secrets.


@pytest.mark.asyncio
async def test_each_session_may_ask_the_model_only_so_often() -> None:
    now = [1000.0]
    service = MathRepairService(
        ScriptedModel([ModelTurn(text=f"a_{n}") for n in range(10)]),
        reviewer="rev",
        session_limit=2,
        session_window=60,
        clock=lambda: now[0],
    )
    assert (await service.repair(request("a"), "page")).tex == "a_0"
    assert (await service.repair(request("b"), "page")).tex == "a_1"
    assert (await service.repair(request("a"), "page")).cached  # answers kept cost nothing
    with pytest.raises(SessionLimitError, match="2 repairs in the last 1 minutes"):
        await service.repair(request("c"), "page")
    assert (await service.repair(request("c"), "another page")).tex == "a_2"
    now[0] += 61
    assert (await service.repair(request("d"), "page")).tex == "a_3"


@pytest.mark.asyncio
async def test_a_busy_server_says_so_and_asks_two_at_a_time() -> None:
    model = HeldModel()
    service = MathRepairService(model, reviewer="rev")
    waiting = [
        asyncio.create_task(service.repair(request(f"x{n}"), "s"))
        for n in range(MAX_ACTIVE + MAX_WAITING)
    ]
    await asyncio.sleep(0.01)
    busy = await service.repair(request("one more"), "s")
    assert busy == RepairAnswer(enabled=True, model="rev", reason="busy")
    model.release.set()
    answers = await asyncio.gather(*waiting)
    assert all(answer.tex is not None for answer in answers)
    assert model.most_at_once == MAX_ACTIVE
    assert len(model.asked) == MAX_ACTIVE + MAX_WAITING
    assert (await service.repair(request("one more"), "s")).tex == "one more}"


@pytest.mark.asyncio
async def test_a_slow_model_times_out_and_failures_are_asked_again_later() -> None:
    now = [0.0]
    model = HeldModel()
    service = MathRepairService(model, reviewer="rev", timeout=0.05, clock=lambda: now[0])
    slow = await service.repair(request("x^"), "s")
    assert slow.reason == "the reviewer model did not answer within 0.05 s"
    assert (await service.repair(request("x^"), "s")).cached
    now[0] += 3601
    model.release.set()
    assert (await service.repair(request("x^"), "s")).tex == "x^}"


@pytest.mark.asyncio
async def test_the_gateway_key_stays_out_of_answers_and_the_log(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    async def answer(count: int) -> web.Response:
        if count == 1:
            return web.Response(status=401, text=f"invalid key {KEY}")
        return web.Response(status=200, text=f"not a completion, key {KEY}")

    gateway.answer = answer
    service = gateway_service(gateway.url, "rev", KEY)
    with caplog.at_level(logging.DEBUG):
        try:
            refused = await service.repair(request("a"), "s")
            garbled = await service.repair(request("b"), "s")
        finally:
            await service.close()
    assert refused.reason == "the model gateway answered HTTP 401"
    assert garbled.reason == "the model gateway failed (the server's log says how)"
    assert KEY not in caplog.text
    assert "[the gateway key]" in caplog.text
    assert KEY not in str(refused.to_json()) + str(garbled.to_json())


@pytest.mark.asyncio
async def test_model_errors_without_a_status() -> None:
    class Unreachable:
        async def complete(
            self, messages: Sequence[Message], tools: Sequence[ToolSpec]
        ) -> ModelTurn:
            raise ModelError("cannot reach the model gateway: refused")

    answer = await MathRepairService(Unreachable(), reviewer="rev").repair(request("a"), "s")
    assert answer.reason == "the model gateway failed (the server's log says how)"


# The endpoint.


@pytest_asyncio.fixture
async def repairing(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> TestClient[Any, Any]:
    service = MathRepairService(
        ScriptedModel([ModelTurn(text="\\frac{1}{2}"), ModelTurn(text="UNFIXABLE")]),
        reviewer="rev",
        session_limit=3,
    )
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, math_repair=service)
    return await aiohttp_client(app)


async def login(client: TestClient[Any, Any]) -> None:
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303


def origin(client: TestClient[Any, Any]) -> dict[str, str]:
    return {"Origin": f"http://127.0.0.1:{client.port}"}


@pytest.mark.asyncio
async def test_the_endpoint_needs_a_session_and_the_servers_origin(
    repairing: TestClient[Any, Any],
) -> None:
    body = {"tex": "\\frac{1", "display": False, "error": "e"}
    assert (
        await repairing.post("/api/v0/math-repair", json=body, headers=origin(repairing))
    ).status == 401
    await login(repairing)
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
        response = await repairing.post("/api/v0/math-repair", json=body, headers=headers)
        assert response.status == 403
    assert (await repairing.get("/api/v0/math-repair")).status == 404


@pytest.mark.asyncio
async def test_the_endpoint_answers_with_the_repair(repairing: TestClient[Any, Any]) -> None:
    await login(repairing)
    body = {"tex": "\\frac{1", "display": False, "error": "Unexpected end of input"}
    response = await repairing.post("/api/v0/math-repair", json=body, headers=origin(repairing))
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == {
        "enabled": True,
        "model": "rev",
        "tex": "\\frac{1}{2}",
        "reason": None,
        "cached": False,
    }
    again = await repairing.post("/api/v0/math-repair", json=body, headers=origin(repairing))
    assert (await again.json())["cached"] is True


@pytest.mark.asyncio
async def test_the_endpoint_refuses_what_it_cannot_read(repairing: TestClient[Any, Any]) -> None:
    await login(repairing)
    cases = [
        (b"{not json", "not JSON"),
        (b"[1, 2]", "must be an object"),
        (b'{"tex": ""}', "non-empty tex"),
        (b'{"tex": "' + b"x" * 4001 + b'"}', "exceeds 4000"),
    ]
    for data, message in cases:
        response = await repairing.post(
            "/api/v0/math-repair",
            data=data,
            headers={**origin(repairing), "Content-Type": "application/json"},
        )
        assert response.status == 400, data[:20]
        error = (await response.json())["error"]
        assert error["kind"] == "invalid_request"
        assert message in error["message"]
    huge = await repairing.post(
        "/api/v0/math-repair", data=b"x" * 70_000, headers=origin(repairing)
    )
    assert huge.status == 413


@pytest.mark.asyncio
async def test_the_endpoint_bounds_each_session(repairing: TestClient[Any, Any]) -> None:
    await login(repairing)
    statuses: list[int] = []
    for n in range(5):
        body = {"tex": f"\\frac{{{n}", "display": False, "error": "e"}
        response = await repairing.post("/api/v0/math-repair", json=body, headers=origin(repairing))
        statuses.append(response.status)
    assert statuses == [200, 200, 200, 429, 429]
    refused = await repairing.post(
        "/api/v0/math-repair", json={"tex": "y", "error": "e"}, headers=origin(repairing)
    )
    error = (await refused.json())["error"]
    assert error["kind"] == "unavailable"
    assert "3 repairs" in error["message"]
    await login(repairing)  # another session, with its own limit
    fresh = await repairing.post(
        "/api/v0/math-repair", json={"tex": "y", "error": "e"}, headers=origin(repairing)
    )
    assert fresh.status == 200


@pytest.mark.asyncio
async def test_without_a_reviewer_the_endpoint_says_so(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await aiohttp_client(
        create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN)
    )
    await login(client)
    response = await client.post(
        "/api/v0/math-repair", json={"tex": "x^", "error": "e"}, headers=origin(client)
    )
    assert response.status == 200
    assert await response.json() == {
        "enabled": False,
        "tex": None,
        "reason": "no reviewer model configured",
    }


# The settings.


def test_the_settings_turn_repairs_on_only_when_complete(tmp_path: Path) -> None:
    key = tmp_path / "gateway.key"
    key.write_text(f"{KEY}\n")
    url = "http://127.0.0.1:4000"
    on = cli.math_repairs(url, "sandbox-reviewer", key)
    assert on.enabled
    assert on.reviewer == "sandbox-reviewer"
    assert not cli.math_repairs(None, None, None).enabled
    assert not cli.math_repairs(url, "off", key).enabled
    for args, message in [
        ((url, None, key), "need --reviewer-model"),
        ((None, None, key), "need --reviewer-model"),
        ((None, "rev", key), "needs --model-url and --api-key-file"),
        ((url, "rev", None), "needs --model-url and --api-key-file"),
        ((url, "rev", tmp_path / "missing.key"), "cannot read the API key file"),
        (("main", "rev", key), "not an http or https URL"),
    ]:
        with pytest.raises(ValueError, match=message):
            cli.math_repairs(*args)
    key.write_text("  \n")
    with pytest.raises(ValueError, match="is empty"):
        cli.math_repairs(url, "rev", key)


def test_flags_must_be_spelled_out(tmp_path: Path, static_dir: Path) -> None:
    # `--model` is the harness's flag for the session's model; here it is no abbreviation.
    with pytest.raises(SystemExit) as refused:
        cli.main(
            ["serve", "--state-dir", str(tmp_path), "--static", str(static_dir),
             "--model", "main"]
        )  # fmt: skip
    assert refused.value.code == 2
