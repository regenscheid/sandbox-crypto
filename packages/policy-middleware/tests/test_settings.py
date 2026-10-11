# SPDX-License-Identifier: MIT
"""The service settings file."""

from pathlib import Path

import pytest

from sandbox_science.policy_middleware.errors import SettingsError
from sandbox_science.policy_middleware.settings import MAX_PAYLOAD_BYTES, load_settings

TOKENS = """
[tls]
certificate = "tls/cert.pem"
private_key = "tls/key.pem"

[tokens]
gateway_id = "openshell"
public_key = "jwt/public.pem"
"""


DEV = 'state_dir = "s"\nceiling = "c"\nallow_unauthenticated = true\n'


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "middleware.toml"
    path.write_text(text)
    return path


def test_relative_paths_resolve_against_the_settings_file(tmp_path: Path) -> None:
    settings = load_settings(
        write(tmp_path, 'state_dir = "state"\nceiling = "/etc/ceiling.yaml"\n' + TOKENS)
    )
    assert settings.state_dir == tmp_path / "state"
    assert settings.ceiling == Path("/etc/ceiling.yaml")
    assert settings.tls is not None
    assert settings.tls.certificate == tmp_path / "tls" / "cert.pem"
    assert settings.tokens is not None
    assert settings.tokens.public_key == tmp_path / "jwt" / "public.pem"
    assert settings.registration == "sandbox-science-policy"
    assert settings.max_payload_bytes == MAX_PAYLOAD_BYTES


def test_local_development_without_tokens(tmp_path: Path) -> None:
    settings = load_settings(
        write(tmp_path, 'state_dir = "s"\nceiling = "c.yaml"\nallow_unauthenticated = true\n')
    )
    assert settings.tokens is None
    assert settings.tls is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param('state_dir = "s"\nceiling = "c"\n', "configure \\[tokens\\]", id="no-auth"),
        pytest.param(
            DEV + TOKENS,
            "mutually exclusive",
            id="both",
        ),
        pytest.param(
            'state_dir = "s"\nceiling = "c"\n[tokens]\ngateway_id = "g"\npublic_key = "k"\n',
            "only over TLS",
            id="tokens-without-tls",
        ),
        pytest.param(
            DEV + "max_payload_bytes = 4194305\n",
            "max_payload_bytes",
            id="payload-over-4mib",
        ),
        pytest.param(
            DEV + 'registration = "Bad Name"\n',
            "registration",
            id="registration",
        ),
        pytest.param(
            DEV + "unknown = 1\n",
            "unknown",
            id="unknown-key",
        ),
        pytest.param('ceiling = "c"\nallow_unauthenticated = true\n', "state_dir", id="no-state"),
        pytest.param("state_dir = [", "cannot read", id="not-toml"),
    ],
)
def test_invalid_settings_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(write(tmp_path, text))


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(tmp_path / "missing.toml")
