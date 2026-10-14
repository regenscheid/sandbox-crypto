# SPDX-License-Identifier: MIT
"""Login, the API's guard, security headers and the GUI's files.

Every security rule has a negative case: no session, a wrong token, another origin,
another host, and paths that try to leave the build directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import (
    CONTENT_SECURITY_POLICY,
    MAX_SESSIONS,
    SECURITY_HEADERS,
    SESSION_COOKIE,
    Sessions,
    create_app,
)

pytestmark = pytest.mark.asyncio

TOKEN = "test-secret-login-token-0123456789abcdef"
SECRETS = (b"test-secret-outside-the-build", b"test-secret-in-a-dotfile")

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


@pytest_asyncio.fixture
async def client(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> TestClient[Any, Any]:
    return await aiohttp_client(create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN))


def origin(client: TestClient[Any, Any]) -> str:
    return f"http://127.0.0.1:{client.port}"


def cookie(client: TestClient[Any, Any]) -> str:
    """The session cookie's name, which carries the server's port."""
    return f"{SESSION_COOKIE}-{client.port}"


async def login(client: TestClient[Any, Any]) -> None:
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303


async def raw_get(port: int | None, target: str, host: str | None = None) -> tuple[int, bytes]:
    """GET ``target`` exactly as written, so that no client normalizes the path."""
    assert port is not None
    host = f"127.0.0.1:{port}" if host is None else host
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    await writer.wait_closed()
    head, _, body = reply.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


# Login and sessions.


async def test_the_login_token_opens_a_session(client: TestClient[Any, Any]) -> None:
    refused = await client.get("/api/v0/projects")
    assert refused.status == 401
    assert (await refused.json())["error"]["kind"] == "policy_denied"
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    assert response.headers["Location"] == "/"
    assert response.headers["Cache-Control"] == "no-store"
    assert list(response.cookies) == [cookie(client)]
    session = response.cookies[cookie(client)]
    assert session["httponly"] is True
    assert session["samesite"] == "Strict"
    assert session["path"] == "/"
    assert not session["secure"]  # plain HTTP on loopback
    assert TOKEN not in session.value
    assert (await client.get("/api/v0/projects")).status == 200


@pytest.mark.parametrize(
    "token",
    ["", "test-secret-wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "x" * 1000],
    ids=["empty", "wrong", "short", "long", "upper", "huge"],
)
async def test_a_wrong_login_token_opens_nothing(client: TestClient[Any, Any], token: str) -> None:
    response = await client.get("/login", params={"token": token}, allow_redirects=False)
    assert response.status == 403
    assert not response.cookies
    assert (await client.get("/api/v0/projects")).status == 401


async def test_a_session_cookie_must_be_one_the_server_issued(
    client: TestClient[Any, Any],
) -> None:
    for forged in ("", TOKEN, "a" * 43, "x" * 5000):
        response = await client.get("/api/v0/projects", cookies={cookie(client): forged})
        assert response.status == 401, forged


async def test_two_workspaces_keep_their_sessions_apart(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    # A browser sends a host's cookies to every port on it; each server reads its own.
    first = await aiohttp_client(create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN))
    second = await aiohttp_client(
        create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN.replace("login", "other"))
    )
    await login(first)
    issued = first.session.cookie_jar.filter_cookies(first.make_url("/"))[cookie(first)].value
    both = {cookie(first): issued, cookie(second): "test-secret-not-a-session"}
    assert (await second.get("/api/v0/projects", cookies=both)).status == 401
    assert (await first.get("/api/v0/projects", cookies=both)).status == 200


async def test_logging_out_ends_the_session(client: TestClient[Any, Any]) -> None:
    await login(client)
    response = await client.post("/api/v0/logout", headers={"Origin": origin(client)})
    assert response.status == 204
    assert (await client.get("/api/v0/projects")).status == 401


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_sessions_beyond_the_limit_end_the_oldest() -> None:
    sessions = Sessions(TOKEN)
    first = sessions.login(TOKEN)
    assert sessions.valid(first)
    for _ in range(MAX_SESSIONS):
        assert sessions.login(TOKEN) is not None
    assert not sessions.valid(first)
    with pytest.raises(ValueError, match="at least 32"):
        Sessions("test-secret-short")


# Every API route needs the session, whatever the path's spelling.


@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/",
        "/api/v0/projects",
        "/api/v0/projects/p1",
        "/api/v0/projects/p1/sessions/a",
        "/api/v0/nothing",
        "/%61pi/v0/projects",
    ],
)
async def test_the_api_needs_a_session(client: TestClient[Any, Any], path: str) -> None:
    status, body = await raw_get(client.port, path)
    assert status == 401, body
    assert b"p1" not in body


async def test_unknown_api_routes_are_json_errors(client: TestClient[Any, Any]) -> None:
    await login(client)
    response = await client.get("/api/v0/nothing")
    assert response.status == 404
    assert (await response.json())["error"]["kind"] == "not_found"


# Requests that change state must come from the server's own origin; no CORS.


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "null"}, {"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1"}],
)
async def test_state_changes_from_another_origin_are_refused(
    client: TestClient[Any, Any], headers: dict[str, str]
) -> None:
    await login(client)
    response = await client.post("/api/v0/logout", headers=headers)
    assert response.status == 403
    assert (await client.get("/api/v0/projects")).status == 200  # still logged in


async def test_there_is_no_cors(client: TestClient[Any, Any]) -> None:
    await login(client)
    preflight = await client.options(
        "/api/v0/projects",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status == 403
    read = await client.get("/api/v0/projects", headers={"Origin": "http://evil.example"})
    for response in (preflight, read):
        assert not [name for name in response.headers if name.lower().startswith("access-control")]
    wrong_method = await client.put("/api/v0/projects", headers={"Origin": origin(client)})
    assert wrong_method.status == 405


# A loopback server answers only to its own names.


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1", ""])
async def test_other_host_names_are_refused(client: TestClient[Any, Any], host: str) -> None:
    status, _ = await raw_get(client.port, f"/login?token={TOKEN}", host.format(port=client.port))
    assert status in (400, 403)


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "LOCALHOST", "[::1]"])
async def test_the_servers_own_names_are_answered(client: TestClient[Any, Any], name: str) -> None:
    status, _ = await raw_get(client.port, "/", f"{name}:{client.port}")
    assert status == 200


async def test_a_listener_on_another_loopback_address_answers_to_it(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN)
    client = await aiohttp_client(app, server_kwargs={"host": "127.0.0.2"})
    response = await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    assert (await client.get("/api/v0/projects")).status == 200
    other = await client.get("/", headers={"Host": f"127.0.0.3:{client.port}"})
    assert other.status == 403


async def test_without_the_host_check_any_name_is_answered(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    app = create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, check_host=False)
    client = await aiohttp_client(app)
    status, _ = await raw_get(client.port, "/", "babbage.example:8200")
    assert status == 200
    response = await client.post(
        "/api/v0/logout",
        headers={"Host": "babbage.example:8200", "Origin": "http://evil.example"},
    )
    assert response.status == 403


# Security headers on every response.


async def test_every_response_carries_the_security_headers(
    client: TestClient[Any, Any],
) -> None:
    responses = [
        await client.get("/"),
        await client.get("/assets/index-1a2b3c.js"),
        await client.get("/assets/missing.js"),
        await client.get("/api/v0/projects"),
        await client.get("/login", params={"token": "test-secret-wrong"}),
        await client.get("/", headers={"Host": "evil.example"}),
        await client.post("/api/v0/logout"),
        await client.get("/login", params={"token": TOKEN}, allow_redirects=False),
        await client.get("/api/v0/projects"),
        await client.get("/api/v0/projects/missing"),
    ]
    assert [response.status for response in responses] == [
        200, 200, 404, 401, 403, 403, 403, 303, 200, 404,
    ]  # fmt: skip
    for response in responses:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, (response.url, name)


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_policy_allows_no_inline_or_eval_script() -> None:
    parts = (directive.strip().partition(" ") for directive in CONTENT_SECURITY_POLICY.split(";"))
    directives = {name: value for name, _, value in parts}
    assert directives["script-src"] == "'self'"
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["style-src"] == "'self'"
    assert directives["style-src-attr"] == "'unsafe-inline'"
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert CONTENT_SECURITY_POLICY.count("unsafe-inline") == 1


# The GUI's files.


async def test_the_build_is_served_with_its_types(client: TestClient[Any, Any]) -> None:
    index = await client.get("/")
    assert index.headers["Content-Type"] == "text/html; charset=utf-8"
    assert index.headers["Cache-Control"] == "no-cache"
    assert "/assets/index-1a2b3c.js" in await index.text()
    script = await client.get("/assets/index-1a2b3c.js")
    assert script.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert script.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    style = await client.get("/assets/index-1a2b3c.css")
    assert style.headers["Content-Type"] == "text/css; charset=utf-8"
    font = await client.get("/assets/KaTeX_Main-Regular-4d5e6f.woff2")
    assert font.headers["Content-Type"] == "font/woff2"
    assert await font.read() == b"wOF2 font"
    head = await client.head("/")
    assert head.status == 200


@pytest.mark.parametrize("path", ["/projects/p1", "/projects/p1/sessions/a", "/anything/"])
async def test_app_routes_get_the_index(client: TestClient[Any, Any], path: str) -> None:
    response = await client.get(path)
    assert response.status == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert "<title>Workspace</title>" in await response.text()


@pytest.mark.parametrize("path", ["/assets/missing.js", "/favicon.ico", "/tool.exe", "/api"])
async def test_missing_files_and_other_types_are_not_found(
    client: TestClient[Any, Any], path: str
) -> None:
    await login(client)
    response = await client.get(path)
    assert response.status == 404
    assert "<title>" not in await response.text()


@pytest.mark.parametrize(
    "target",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/assets%2f..%2f..%2fsecret.txt",
        "/..%5csecret.txt",
        "/assets/..%5c..%5csecret.txt",
        "/link.txt",
        "/.env",
        "/%2eenv",
        "/assets/../.env",
        "//secret.txt",
        "/index.html%00.js",
        "/./.env",
        "/assets/./../.env",
    ],
)
async def test_paths_cannot_leave_the_build(client: TestClient[Any, Any], target: str) -> None:
    status, body = await raw_get(client.port, target)
    assert status in (400, 404), (target, status, body[:200])
    for secret in SECRETS:
        assert secret not in body


async def test_only_get_and_head_reach_the_files(client: TestClient[Any, Any]) -> None:
    refused = await client.post("/", headers={"Origin": origin(client)})
    assert refused.status == 405
    assert (await client.post("/")).status == 403  # no origin


async def test_an_injected_document_store_must_be_read_only(
    state_dir: Path, static_dir: Path
) -> None:
    writable = DocumentStore(store_path(state_dir))
    with pytest.raises(ValueError, match="read-only"):
        create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, document_store=writable)
    writable.close()
