# SPDX-License-Identifier: MIT
"""The runtime manager's settings and its credentials client against a fake control."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pydantic import ValidationError

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.runtime_binding import WorkerBinding
from sandbox_science.launcher.runtime_manager import RuntimeManagerSettings, load
from sandbox_science.launcher.runtime_manager.control import (
    ControlClient,
    ControlError,
    OccupiedError,
    SupersededError,
)
from sandbox_science.launcher.sandboxes import WorkerApi

from .manager_support import (
    MANAGER_TOKEN,
    REVISION,
    FakeControl,
    LocalTls,
    localhost_tls,
    private,
    settings_document,
)

MANAGER_URL = "https://control.example.test:8444"
PROTOCOL = {PROTOCOL_HEADER: str(PROTOCOL_VERSION)}


def document(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    found = settings_document(tmp_path, manager_url=MANAGER_URL, ca=tmp_path / "ca.pem")
    found["control_url"] = "https://control.example.test:8443"
    return found | changes


def test_valid_settings_load_from_their_file(tmp_path: Path) -> None:
    path = private(tmp_path / "runtime-manager.json", json.dumps(document(tmp_path)))
    settings = load(path)
    placement = json.loads(settings.driver_config())
    assert placement["kubernetes"]["pod"] == {
        "node_selector": {"kubernetes.io/arch": "amd64"},
        "runtime_class_name": "kata",
    }
    assert settings.recover_infrastructure is False
    path.write_text(json.dumps(document(tmp_path, unexpected=True)))
    with pytest.raises(LauncherError, match="invalid runtime manager settings"):
        load(path)


@pytest.mark.parametrize(
    ("runtime", "refusal"),
    [
        ({"runtime_class_name": None}, "Kata RuntimeClass"),
        ({"runtime_class_name": "runc"}, "Kata RuntimeClass"),
        ({"runtime_class_name": "gvisor"}, "Kata RuntimeClass"),
        ({"node_selector": {"kubernetes.io/arch": "arm64"}}, "amd64"),
    ],
)
def test_only_a_kata_amd64_placement_is_accepted(
    tmp_path: Path, runtime: dict[str, Any], refusal: str
) -> None:
    profile = document(tmp_path)["runtime"] | runtime
    with pytest.raises(ValidationError, match=refusal):
        RuntimeManagerSettings.model_validate(document(tmp_path, runtime=profile))
    qemu = document(tmp_path)["runtime"] | {"runtime_class_name": "kata-qemu"}
    RuntimeManagerSettings.model_validate(document(tmp_path, runtime=qemu))


@pytest.mark.parametrize(
    "changes",
    [
        {"control_url": "http://control.example.test:8443"},
        {"control_url": "https://control.example.test"},
        {"control_url": "https://control.example.test:8443/control"},
        {"control_url": "https://user:pw@control.example.test:8443"},
        {"control_url": "https://elsewhere.example.test:8443"},
        {"manager_url": "https://control.example.test:8443"},
        {"image": "registry.example.test/orchestrator:latest"},
        {"revision": "main"},
    ],
)
def test_control_urls_name_two_ports_of_one_service_and_the_image_is_pinned(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        RuntimeManagerSettings.model_validate(document(tmp_path, **changes))


def test_runtime_settings_derive_from_controls_model_part(tmp_path: Path) -> None:
    settings = RuntimeManagerSettings.model_validate(document(tmp_path))
    models = settings.models()
    runtime = json.loads(settings.runtime_json(models, "runtime-" + "1" * 24))
    assert runtime["control_url"] == "https://control.example.test:8443/control"
    assert runtime["control_ca_file"] == "/sandbox/runtime/control-ca.crt"
    assert runtime["token_file"] == "/sandbox/runtime/runtime.token"
    assert runtime["model_key_file"] == "/sandbox/runtime/model.key"
    assert runtime["models"] == ["main", "review"]
    assert runtime["generation"] == "runtime-" + "1" * 24
    copy = json.loads(Path(settings.control_models_file).read_text())
    Path(settings.control_models_file).write_text(
        json.dumps(copy | {"image_revision": "b" * 40, "state_dir": "/ignored"})
    )
    with pytest.raises(LauncherError, match="revision differs"):
        settings.models()
    assert copy["image_revision"] == REVISION


@pytest.fixture
def tls(tmp_path: Path) -> LocalTls:
    return localhost_tls(tmp_path / "tls")


def client(tmp_path: Path, url: str, tls: LocalTls, token: str = MANAGER_TOKEN) -> ControlClient:
    return ControlClient(
        url,
        ca_file=tls.certificate,
        token_file=private(tmp_path / "token" / "manager.token", token),
    )


async def test_issue_replays_the_latest_and_refuses_older_requests(
    tmp_path: Path, tls: LocalTls
) -> None:
    control = FakeControl(tmp_path)
    async with control.serving(tls) as url:
        manager = client(tmp_path, url, tls)
        health = await asyncio.to_thread(manager.health)
        assert health.control_instance == control.instance
        assert not health.occupied
        first = await asyncio.to_thread(manager.issue, "a" * 32)
        assert first.generation.startswith("runtime-")
        assert await asyncio.to_thread(manager.issue, "a" * 32) == first
        second = await asyncio.to_thread(manager.issue, "b" * 32)
        assert second != first
        with pytest.raises(SupersededError, match="superseded"):
            await asyncio.to_thread(manager.issue, "a" * 32)
        control.occupied = True
        with pytest.raises(OccupiedError):
            await asyncio.to_thread(manager.issue, "c" * 32)
        control.occupied = False
        assert (await asyncio.to_thread(manager.issue, "c" * 32)).generation != second.generation
        with pytest.raises(ValueError, match="32 lowercase hex"):
            manager.issue("C" * 32)


async def test_a_wrong_token_or_an_unknown_ca_is_refused(tmp_path: Path, tls: LocalTls) -> None:
    control = FakeControl(tmp_path)
    async with control.serving(tls) as url:
        wrong = client(tmp_path, url, tls, token="test-secret-not-the-manager-token-0123")
        with pytest.raises(ControlError, match="refused the manager's token"):
            await asyncio.to_thread(wrong.issue, "a" * 32)
        other = localhost_tls(tmp_path / "other")
        unverified = client(tmp_path, url, other)
        with pytest.raises(ControlError, match="unreachable"):
            await asyncio.to_thread(unverified.health)
        short = client(tmp_path, url, tls, token="short")
        with pytest.raises(ControlError, match="invalid shape"):
            await asyncio.to_thread(short.issue, "a" * 32)
    assert control.asked == []


@pytest.mark.parametrize(
    ("answer", "refusal"),
    [
        (lambda: web.Response(body=b"x" * 8192, content_type="application/json"), "byte bound"),
        (lambda: web.json_response({"generation": "runtime-1", "token": "t"}), "malformed"),
        (lambda: web.json_response({"error": "unavailable"}, status=503), "HTTP 503"),
    ],
)
async def test_answers_are_bounded_and_checked(
    tmp_path: Path, tls: LocalTls, answer: Callable[[], web.Response], refusal: str
) -> None:
    async def respond(request: web.Request) -> web.Response:
        return answer()

    app = web.Application()
    app.router.add_post("/runtime/credentials", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls.server()).start()
    try:
        manager = client(tmp_path, f"https://localhost:{runner.addresses[0][1]}", tls)
        with pytest.raises(ControlError, match=refusal):
            await asyncio.to_thread(manager.issue, "a" * 32)
    finally:
        await runner.cleanup()


def test_the_cli_finds_its_gateway_profile_where_the_bundle_mounts_it(tmp_path: Path) -> None:
    settings = RuntimeManagerSettings.model_validate(document(tmp_path))
    assert settings.openshell.env == {"XDG_CONFIG_HOME": "/private/xdg"}
    given = {"cli": "openshell", "env": {"OPENSHELL_GATEWAY": "g"}}
    settings = RuntimeManagerSettings.model_validate(document(tmp_path, openshell=given))
    assert settings.openshell.env == {"XDG_CONFIG_HOME": "/private/xdg", "OPENSHELL_GATEWAY": "g"}
    given = {"env": {"XDG_CONFIG_HOME": "/elsewhere"}}
    settings = RuntimeManagerSettings.model_validate(document(tmp_path, openshell=given))
    assert settings.openshell.env == {"XDG_CONFIG_HOME": "/elsewhere"}  # explicit wins


def test_the_worker_link_is_optional_and_a_verified_origin(tmp_path: Path) -> None:
    assert RuntimeManagerSettings.model_validate(document(tmp_path)).worker is None
    link = {
        "url": "https://worker.example.test:8100",
        "ca_file": str(tmp_path / "ca.pem"),
        "token_file": str(tmp_path / "worker.token"),
        "project": "crypto",
    }
    settings = RuntimeManagerSettings.model_validate(document(tmp_path, worker=link))
    assert settings.worker is not None
    assert settings.worker.project == "crypto"
    for bad in ({"url": "http://worker.example.test:8100"}, {"project": "../x"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            RuntimeManagerSettings.model_validate(document(tmp_path, worker={**link, **bad}))


async def test_the_runtime_sandbox_is_bound_and_unbound_through_the_worker(
    tmp_path: Path, tls: LocalTls
) -> None:
    seen: list[tuple[str, str, str | None, dict[str, Any] | None]] = []

    async def bind(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append((request.method, request.path, request.headers.get("Authorization"), body))
        return web.json_response({**body, "policy": "sha256:" + "e" * 64}, headers=PROTOCOL)

    async def unbind(request: web.Request) -> web.Response:
        seen.append((request.method, request.path, request.headers.get("Authorization"), None))
        found = {"sandbox_id": request.match_info["id"], "unbound": True}
        return web.json_response(found, headers=PROTOCOL)

    app = web.Application()
    app.router.add_post("/v1/runtime-sandboxes", bind)
    app.router.add_delete("/v1/runtime-sandboxes/{id}", unbind)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls.server()).start()
    try:
        token = private(tmp_path / "worker.token", "test-secret-worker-launcher-token-0123\n")
        api = WorkerApi(f"https://localhost:{runner.addresses[0][1]}", token, tls.certificate)
        said: list[str] = []
        binding = WorkerBinding(api, "crypto", said.append)
        await binding.bind("sbx-1")
        await binding.unbind("sbx-1")
        bearer = "Bearer test-secret-worker-launcher-token-0123"
        assert seen == [
            (
                "POST",
                "/v1/runtime-sandboxes",
                bearer,
                {"project_id": "crypto", "sandbox_id": "sbx-1"},
            ),
            ("DELETE", "/v1/runtime-sandboxes/sbx-1", bearer, None),
        ]
        assert said == ["bound runtime sandbox sbx-1 to sha256:" + "e" * 64]
    finally:
        await runner.cleanup()
    # With the worker gone, a bind is an error that names the consequence, an unbind a note.
    with pytest.raises(LauncherError, match=r"not bound in the policy store.*sandbox_unbound"):
        await binding.bind("sbx-2")
    await binding.unbind("sbx-2")
    assert said[-1].startswith("the binding of runtime sandbox sbx-2 remains:")
