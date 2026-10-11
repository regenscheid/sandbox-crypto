# SPDX-License-Identifier: MIT
"""Credential headers come only from OpenShell placeholders."""

import pytest

from sandbox_science.mcp_proxy.credentials import contains_placeholder, credential_headers
from sandbox_science.mcp_proxy.errors import RefusalError
from sandbox_science.policy.model import BearerAuth, HeadersAuth, NoAuth, OAuthAuth

BEARER_PLACEHOLDER = "openshell:resolve:env:v3_CRM_TOKEN"
KEY_PLACEHOLDER = "openshell:resolve:env:v3_EXAMPLE_API_KEY"
VARIABLES = {"crm": "CRM_TOKEN", "example.API_KEY": "EXAMPLE_API_KEY"}
ENVIRON = {"CRM_TOKEN": BEARER_PLACEHOLDER, "EXAMPLE_API_KEY": KEY_PLACEHOLDER}


def test_no_auth_sends_no_credentials() -> None:
    assert credential_headers(NoAuth(mode="none"), VARIABLES, ENVIRON) == ()


def test_bearer_and_oauth_send_the_providers_placeholder() -> None:
    expected = (("Authorization", f"Bearer {BEARER_PLACEHOLDER}"),)
    bearer = BearerAuth(mode="bearer", provider="crm")
    oauth = OAuthAuth(mode="oauth", registration="dcr", provider="crm")
    assert credential_headers(bearer, VARIABLES, ENVIRON) == expected
    assert credential_headers(oauth, VARIABLES, ENVIRON) == expected


def test_header_references_become_placeholders() -> None:
    auth = HeadersAuth(
        mode="headers",
        headers={"X-Api-Key": "${provider:example.API_KEY}", "X-Account": "research"},
    )
    assert credential_headers(auth, VARIABLES, ENVIRON) == (
        ("X-Api-Key", KEY_PLACEHOLDER),
        ("X-Account", "research"),
    )


@pytest.mark.parametrize(
    ("variables", "environ", "message"),
    [
        ({}, ENVIRON, "no \\[credentials\\] entry"),
        (VARIABLES, {}, "is not set"),
        (VARIABLES, {"CRM_TOKEN": "test-secret-raw-token"}, "does not hold"),
        (VARIABLES, {"CRM_TOKEN": "openshell:resolve:env:"}, "does not hold"),
        (VARIABLES, {"CRM_TOKEN": BEARER_PLACEHOLDER + " \r\nX-Injected: 1"}, "does not hold"),
    ],
)
def test_a_missing_or_literal_credential_is_refused(
    variables: dict[str, str], environ: dict[str, str], message: str
) -> None:
    with pytest.raises(RefusalError, match=message) as raised:
        credential_headers(BearerAuth(mode="bearer", provider="crm"), variables, environ)
    assert raised.value.reason_code == "credential_unavailable"


@pytest.mark.parametrize("name", ["Content-Type", "MCP-Protocol-Version", "User-Agent", "Host"])
def test_headers_the_proxy_sets_cannot_be_credentials(name: str) -> None:
    auth = HeadersAuth(mode="headers", headers={name: "value"})
    with pytest.raises(RefusalError, match="set by the proxy"):
        credential_headers(auth, VARIABLES, ENVIRON)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"q":"openshell:resolve:env:v1_X"}', True),
        (b'{"q":"OPENSHELL:RESOLVE:ENV:v1_X"}', True),
        (b'{"q":"openshell resolve"}', False),
        (b'{"q":"openshell:resolved"}', False),
    ],
)
def test_contains_placeholder(body: bytes, expected: bool) -> None:
    assert contains_placeholder(body) is expected


def test_a_literal_placeholder_in_a_policys_headers_is_refused() -> None:
    auth = HeadersAuth(mode="headers", headers={"X-Account": "openshell:resolve:env:v1_OTHER"})
    with pytest.raises(RefusalError, match="literal placeholder"):
        credential_headers(auth, VARIABLES, ENVIRON)
