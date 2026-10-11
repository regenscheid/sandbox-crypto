# SPDX-License-Identifier: MIT
"""The proxy's settings file."""

from datetime import timedelta
from pathlib import Path

import pytest

from sandbox_science.mcp_proxy.errors import SettingsError
from sandbox_science.mcp_proxy.settings import load_settings

HASH_A = "a" * 64
HASH_B = "b" * 64

SETTINGS = f"""\
listen = "127.0.0.1:8180"
state_dir = "state"
ceiling = "pool-ceiling.yaml"
admin_hosts = ["gateway.sandbox-science.internal"]
max_body_bytes = "512Ki"
max_sessions = 32
max_sessions_per_caller = 4
session_idle = "30m"
upstream_timeout = "2m"

[credentials]
crypto-research-mcp = "CRYPTO_RESEARCH_MCP_TOKEN"
"example-header-server.API_KEY" = "EXAMPLE_API_KEY"

[[callers]]
name = "orchestrator"
token_sha256 = "{HASH_A}"
kind = "orchestrator"
project = "crypto"

[[callers]]
name = "crypto-default"
token_sha256 = "{HASH_B}"
kind = "worker"
project = "crypto"
container = "ss-crypto-default-1a2b3c"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "proxy.toml"
    path.write_text(text)
    return path


def test_settings_are_read_and_paths_resolved(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, SETTINGS))
    assert (settings.listen_host, settings.listen_port) == ("127.0.0.1", 8180)
    assert settings.state_dir == tmp_path / "state"
    assert settings.ceiling == tmp_path / "pool-ceiling.yaml"
    assert settings.event_store_path == tmp_path / "state" / "events.db"
    assert settings.max_body_bytes == 512 * 1024
    assert (settings.max_sessions, settings.session_idle) == (32, timedelta(minutes=30))
    assert settings.max_sessions_per_caller == 4
    assert settings.upstream_timeout == timedelta(minutes=2)
    assert settings.credentials == {
        "crypto-research-mcp": "CRYPTO_RESEARCH_MCP_TOKEN",
        "example-header-server.API_KEY": "EXAMPLE_API_KEY",
    }
    orchestrator, worker = settings.callers
    assert (orchestrator.kind, orchestrator.container) == ("orchestrator", None)
    assert (worker.container, worker.session) == ("ss-crypto-default-1a2b3c", None)


def test_defaults(tmp_path: Path) -> None:
    text = f"""\
state_dir = "/var/lib/sandbox-science"
ceiling = "/etc/sandbox-science/pool-ceiling.yaml"
event_store = "events/mcp.db"

[[callers]]
name = "orchestrator"
token_sha256 = "{HASH_A}"
kind = "orchestrator"
project = "crypto"
"""
    settings = load_settings(write(tmp_path, text))
    assert settings.listen == "127.0.0.1:8080"
    assert settings.state_dir == Path("/var/lib/sandbox-science")
    assert settings.event_store_path == tmp_path / "events" / "mcp.db"
    assert settings.max_body_bytes == 1024 * 1024
    assert settings.credentials == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('token_sha256 = "' + HASH_B, 'token_sha256 = "' + HASH_A), "own token"),
        (('name = "crypto-default"', 'name = "orchestrator"'), "names must be unique"),
        (('token_sha256 = "' + HASH_A, 'token_sha256 = "' + "A" * 64), "token_sha256"),
        (('container = "ss-crypto-default-1a2b3c"\n', ""), "names its container"),
        (('project = "crypto"\n\n', 'project = "crypto"\ncontainer = "x"\n\n'), "no container"),
        (('kind = "worker"', 'kind = "admin"'), "kind"),
        (('project = "crypto"\n\n', 'project = "../crypto"\n\n'), "project"),
        (('crypto-research-mcp = "', 'Crypto = "'), "credentials"),
        (('"example-header-server.API_KEY"', '"example-header-server.api_key"'), "credentials"),
        (('= "EXAMPLE_API_KEY"', '= "EXAMPLE-API-KEY"'), "credentials"),
        (('listen = "127.0.0.1:8180"', 'listen = "127.0.0.1"'), "listen"),
        (('listen = "127.0.0.1:8180"', 'listen = "::1:8180"'), "listen"),
        (('max_body_bytes = "512Ki"', 'max_body_bytes = "64Mi"'), "max_body_bytes"),
        (("max_sessions = 32", "max_sessions = 0"), "max_sessions"),
        (("max_sessions_per_caller = 4", "max_sessions_per_caller = 32"), "smaller than"),
        (('session_idle = "30m"', 'session_idle = "0s"'), "session_idle"),
        (('upstream_timeout = "2m"', 'unknown = 1\nupstream_timeout = "2m"'), "unknown"),
    ],
)
def test_invalid_settings_are_refused(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    old, new = change
    assert old in SETTINGS
    with pytest.raises(SettingsError, match=message):
        load_settings(write(tmp_path, SETTINGS.replace(old, new, 1)))


def test_a_proxy_needs_callers(tmp_path: Path) -> None:
    text = 'state_dir = "s"\nceiling = "c.yaml"\n'
    with pytest.raises(SettingsError, match="callers"):
        load_settings(write(tmp_path, text))


def test_unreadable_settings_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(tmp_path / "missing.toml")
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(write(tmp_path, "listen = \n"))
