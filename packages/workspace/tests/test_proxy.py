# SPDX-License-Identifier: MIT
"""The workspace behind a TLS-terminating proxy: its public origin, the proxies it trusts
to forward the scheme and host, and the session cookie there; and that without these
settings nothing changes for the loopback server.

Every rule has a negative case: another origin or host, forwarded headers from a peer
that is not trusted, ambiguous forwarded headers, and Authentik's headers, which open no
session.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from multidict import CIMultiDict

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import SESSION_COOKIE, create_app
from sandbox_science.workspace.preview import Capabilities, create_preview_app
from sandbox_science.workspace.proxy import Exposure, PublicOrigin, parse_network

from .route_support import TOKEN, ClientFactory

WORKSPACE = PublicOrigin.parse("https://workspace.research.test")
VIEWER = PublicOrigin.parse("https://preview.research.test")
HOST = "workspace.research.test"
ORIGIN = "https://workspace.research.test"
LOOPBACK_PEERS = (ipaddress.ip_network("127.0.0.0/8"),)
"""The test client's own address, which these tests trust as the proxy."""
OTHER_PEERS = (ipaddress.ip_network("10.42.0.0/16"),)
"""A network the test client is not in."""


async def served(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    exposure: Exposure | None,
    *,
    check_host: bool = False,
) -> TestClient[Any, Any]:
    """The workspace as ``serve`` makes it off loopback: no loopback host check."""
    return await aiohttp_client(
        create_app(
            ProjectStore(state_dir, readonly=True),
            static_dir,
            login_token=TOKEN,
            check_host=check_host,
            exposure=exposure,
        )
    )


async def log_in(client: TestClient[Any, Any], **headers: str) -> tuple[str, str, str]:
    """Log in; the cookie's name and value, and the whole ``Set-Cookie`` header."""
    response = await client.get(
        "/login", params={"token": TOKEN}, headers=headers, allow_redirects=False
    )
    assert response.status == 303, await response.text()
    assert response.headers["Location"] == "/"
    [(name, morsel)] = response.cookies.items()
    return name, morsel.value, response.headers["Set-Cookie"]


# The settings themselves.


@pytest.mark.parametrize(
    ("text", "origin", "hosts"),
    [
        ("https://workspace.research.test", ORIGIN, {HOST, f"{HOST}:443"}),
        ("HTTPS://Workspace.Research.Test", ORIGIN, {HOST, f"{HOST}:443"}),
        ("https://workspace.research.test:443", ORIGIN, {HOST, f"{HOST}:443"}),
        ("https://w.test:8443", "https://w.test:8443", {"w.test:8443"}),
    ],
)
def test_a_public_origin_is_an_https_origin(text: str, origin: str, hosts: set[str]) -> None:
    public = PublicOrigin.parse(text)
    assert public.origin == origin
    assert public.hosts == hosts


@pytest.mark.parametrize(
    "text",
    [
        "http://workspace.research.test",
        "workspace.research.test",
        "https://workspace.research.test/",
        "https://workspace.research.test/app",
        "https://workspace.research.test?x=1",
        "https://workspace.research.test#x",
        "https://user@workspace.research.test",
        "https://*.research.test",
        "https://workspace.research.test:0",
        "https://workspace.research.test:65536",
        "https://[::1]",
        "https://",
        "",
    ],
)
def test_anything_but_an_https_origin_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"origin|port"):
        PublicOrigin.parse(text)


@pytest.mark.parametrize("text", ["10.42.0.1/16", "0.0.0.0/0", "::/0", "traefik", "10.42.0.0/33"])
def test_a_trusted_network_must_be_exact_and_not_everyone(text: str) -> None:
    with pytest.raises(ValueError, match=r"CIDR|every address"):
        parse_network(text)


def test_a_trusted_network_is_parsed() -> None:
    assert parse_network("10.42.0.0/16") == ipaddress.ip_network("10.42.0.0/16")
    assert parse_network("fd00::/8") == ipaddress.ip_network("fd00::/8")


# Behind a public origin.


async def test_the_public_origin_answers_only_to_its_host(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE))
    for host in (HOST, f"{HOST}:443", "WORKSPACE.research.test"):
        assert (await client.get("/", headers={"Host": host})).status == 200, host
    for host in (
        "evil.example",
        f"{HOST}:8443",
        f"127.0.0.1:{client.port}",
        "preview.research.test",
    ):
        response = await client.get("/", headers={"Host": host})
        assert response.status == 403, host
        assert "public origin" in await response.text()


async def test_the_session_cookie_is_secure_and_host_prefixed_behind_the_public_origin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE))
    name, value, header = await log_in(client, Host=HOST)
    assert name == f"__Host-{SESSION_COOKIE}-{client.port}"
    assert header == f"{name}={value}; HttpOnly; Path=/; SameSite=Strict; Secure"
    assert "Domain" not in header
    session = {"Host": HOST, "Cookie": f"{name}={value}"}
    assert (await client.get("/api/v0/projects", headers=session)).status == 200
    # The unprefixed name, as a sibling host could set it, opens nothing.
    unprefixed = {"Host": HOST, "Cookie": f"{SESSION_COOKIE}-{client.port}={value}"}
    assert (await client.get("/api/v0/projects", headers=unprefixed)).status == 401
    ended = await client.post("/api/v0/logout", headers={**session, "Origin": ORIGIN})
    assert ended.status == 204
    cleared = ended.headers["Set-Cookie"]
    assert cleared.startswith(f'{name}=""')
    assert "Secure" in cleared
    assert "Path=/" in cleared
    assert (await client.get("/api/v0/projects", headers=session)).status == 401


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://workspace.research.test",  # the scheme the server itself sees
        "https://workspace.research.test:8443",
        "https://preview.research.test",
        "https://evil.example",
        "http://127.0.0.1",
    ],
)
async def test_a_change_needs_exactly_the_public_origin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, origin: str | None
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE))
    name, value, _ = await log_in(client, Host=HOST)
    session = {"Host": HOST, "Cookie": f"{name}={value}"}
    headers = session if origin is None else {**session, "Origin": origin}
    refused = await client.post("/api/v0/logout", headers=headers)
    assert refused.status == 403
    assert "another origin" in await refused.text()
    assert (await client.get("/api/v0/projects", headers=session)).status == 200  # still in
    accepted = await client.post("/api/v0/logout", headers={**session, "Origin": ORIGIN})
    assert accepted.status == 204


async def test_authentik_gates_reachability_only_the_token_opens_a_session(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    # Authentik's forward auth passes its user's identity in headers; the workspace keeps
    # its own login and session, and those headers grant nothing (no SSO in this increment).
    authentik = {
        "Host": HOST,
        "X-authentik-username": "researcher",
        "X-authentik-groups": "sandbox-science",
        "X-authentik-email": "researcher@research.test",
        "Remote-User": "researcher",
    }
    client = await served(
        aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, LOOPBACK_PEERS)
    )
    assert (await client.get("/api/v0/projects", headers=authentik)).status == 401
    wrong = await client.get("/login", params={"token": "test-secret-wrong"}, headers=authentik)
    assert wrong.status == 403
    proxied = {**authentik, "X-Forwarded-Proto": "https", "X-Forwarded-Host": HOST}
    name, value, _ = await log_in(client, **proxied)
    opened = {**proxied, "Cookie": f"{name}={value}"}
    assert (await client.get("/api/v0/projects", headers=opened)).status == 200


# Forwarded headers, only from trusted proxies.


async def test_a_trusted_proxy_forwards_the_scheme_and_host(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, Exposure(trusted=LOOPBACK_PEERS))
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "ws.research.test"}
    name, value, header = await log_in(client, **forwarded)
    assert name == f"{SESSION_COOKIE}-{client.port}"  # no public origin: no prefix
    assert header.endswith("; Secure")  # the browser reached the proxy over HTTPS
    session = {**forwarded, "Cookie": f"{name}={value}"}
    plain = await client.post(
        "/api/v0/logout", headers={**session, "Origin": "http://ws.research.test"}
    )
    assert plain.status == 403
    accepted = await client.post(
        "/api/v0/logout", headers={**session, "Origin": "https://ws.research.test"}
    )
    assert accepted.status == 204


async def test_forwarded_headers_from_a_peer_that_is_not_trusted_are_ignored(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, OTHER_PEERS))
    # X-Forwarded-For names a trusted address; the peer is what counts, and it is not one.
    forged = {
        "Host": "internal.research.test",
        "X-Forwarded-Host": HOST,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "10.42.0.7",
        "X-Real-IP": "10.42.0.7",
    }
    assert (await client.get("/", headers=forged)).status == 403
    loose = await served(aiohttp_client, state_dir, static_dir, Exposure(trusted=OTHER_PEERS))
    name, value, header = await log_in(loose, **{**forged, "Host": f"127.0.0.1:{loose.port}"})
    assert "Secure" not in header  # the forwarded scheme was not believed
    refused = await loose.post(
        "/api/v0/logout",
        headers={**forged, "Cookie": f"{name}={value}", "Origin": ORIGIN, "Host": HOST},
    )
    assert refused.status == 403  # the server's own origin is http://, as it sees it


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-Proto": "https, http"},
        {"X-Forwarded-Host": f"{HOST}, evil.example"},
        {"X-Forwarded-Proto": "gopher"},
        {"X-Forwarded-Host": "evil example"},
        {"X-Forwarded-Host": "evil.example/path"},
    ],
)
async def test_ambiguous_or_malformed_forwarded_headers_are_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, headers: dict[str, str]
) -> None:
    client = await served(
        aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, LOOPBACK_PEERS)
    )
    response = await client.get("/", headers={"Host": HOST, **headers})
    assert response.status == 403
    assert "forwarded headers" in await response.text()


async def test_a_repeated_forwarded_header_is_refused(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(
        aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, LOOPBACK_PEERS)
    )
    repeated = CIMultiDict(
        [("Host", HOST), ("X-Forwarded-Host", HOST), ("X-Forwarded-Host", "evil.example")]
    )
    response = await client.get("/", headers=repeated)
    assert response.status == 403


async def test_behind_the_public_origin_a_trusted_proxy_must_have_seen_https(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await served(
        aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, LOOPBACK_PEERS)
    )
    internal = {"Host": "research-workspace:8200", "X-Forwarded-Host": HOST}
    assert (await client.get("/", headers={**internal, "X-Forwarded-Proto": "https"})).status == 200
    assert (await client.get("/", headers=internal)).status == 200  # no scheme forwarded
    plain = await client.get("/", headers={**internal, "X-Forwarded-Proto": "http"})
    assert plain.status == 403
    other = {"Host": HOST, "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"}
    assert (await client.get("/", headers=other)).status == 403


async def test_the_forwarded_header_is_never_read(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    # Traefik replaces a client's X-Forwarded-* but passes Forwarded through.
    client = await served(
        aiohttp_client, state_dir, static_dir, Exposure(WORKSPACE, LOOPBACK_PEERS)
    )
    forged = {"Host": "evil.example", "Forwarded": f'proto=https;host="{HOST}"'}
    assert (await client.get("/", headers=forged)).status == 403
    loose = await served(aiohttp_client, state_dir, static_dir, Exposure(trusted=LOOPBACK_PEERS))
    _, _, header = await log_in(loose, Forwarded="proto=https")
    assert "Secure" not in header


# Without the settings, nothing changes.


@pytest.mark.parametrize("exposure", [None, Exposure()], ids=["absent", "empty"])
async def test_without_the_settings_the_loopback_server_is_unchanged(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, exposure: Exposure | None
) -> None:
    client = await served(aiohttp_client, state_dir, static_dir, exposure, check_host=True)
    own = f"127.0.0.1:{client.port}"
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": HOST, "Forwarded": "proto=https"}
    name, value, header = await log_in(client, **forwarded)
    assert name == f"{SESSION_COOKIE}-{client.port}"
    assert header == f"{name}={value}; HttpOnly; Path=/; SameSite=Strict"
    session = {"Cookie": f"{name}={value}"}
    # Forwarded headers name nothing: the loopback names are still the only ones.
    assert (await client.get("/", headers={"Host": HOST, **forwarded})).status == 403
    assert (await client.get("/", headers={"Host": own, **forwarded})).status == 200
    for origin in (ORIGIN, f"https://{own}"):
        refused = await client.post(
            "/api/v0/logout", headers={**session, **forwarded, "Origin": origin}
        )
        assert refused.status == 403, origin
    ended = await client.post("/api/v0/logout", headers={**session, "Origin": f"http://{own}"})
    assert ended.status == 204
    assert (
        ended.headers["Set-Cookie"]
        == f'{name}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/'
    )


# The preview origin behind its public origin.


async def test_the_preview_origin_answers_only_to_its_public_host(
    aiohttp_client: ClientFactory, state_dir: Path, tmp_path: Path
) -> None:
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    (viewer / "index.html").write_text("<!doctype html><title>Viewer</title>")
    documents = DocumentStore(store_path(state_dir), producer="workspace", readonly=True)
    try:
        app = create_preview_app(
            documents,
            Capabilities(),
            workspace_origin=lambda: WORKSPACE.origin,
            viewer_dir=viewer,
            check_host=Exposure(VIEWER, LOOPBACK_PEERS).host_check,
        )
        client = await aiohttp_client(app)
        page = await client.get("/viewer/", headers={"Host": "preview.research.test"})
        assert page.status == 200
        assert f"frame-ancestors {ORIGIN}" in page.headers["Content-Security-Policy"]
        proxied = {"Host": "research-preview:8201", "X-Forwarded-Host": "preview.research.test"}
        assert (await client.get("/viewer/", headers=proxied)).status == 200
        for headers in (
            {"Host": HOST},
            {"Host": f"127.0.0.1:{client.port}"},
            {**proxied, "X-Forwarded-Proto": "http"},
            {**proxied, "X-Forwarded-Host": "a.test, preview.research.test"},
        ):
            assert (await client.get("/viewer/", headers=headers)).status == 403, headers
    finally:
        documents.close()
