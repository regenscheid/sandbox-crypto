# SPDX-License-Identifier: MIT
"""The Mac helper refuses unsafe storage and cleans up uncertain smoke creates."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/mac_local.py"
SPEC = importlib.util.spec_from_file_location("mac_local_helper", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
mac = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mac
SPEC.loader.exec_module(mac)


def settings(tmp_path: Path) -> Any:
    return mac.Settings(
        volume=tmp_path,
        root=tmp_path / "SandboxScience",
        gateway_port=18081,
        image=mac.DEFAULT_IMAGE,
        github_user="example",
        keychain_service="test-secret-ghcr",
        application_config=None,
    )


def test_config_rejects_a_path_that_could_escape_the_chosen_volume(tmp_path: Path) -> None:
    config = tmp_path / "mac.toml"
    config.write_text(f'volume = "{tmp_path}"\ndirectory = "../outside"\n')
    with pytest.raises(mac.SetupError, match="one simple folder"):
        mac.read_settings(config)


def test_sample_config_selects_the_published_crypto_base_image() -> None:
    sample = SCRIPT.parents[1] / "examples/mac-local.toml"
    chosen = mac.read_settings(sample)
    assert chosen.image == mac.DEFAULT_IMAGE
    assert chosen.root == Path("/Volumes/Research/SandboxScience")


def test_pinned_version_does_not_accept_a_later_patch_number() -> None:
    assert mac.pinned_version("openshell 0.1.3")
    assert not mac.pinned_version("openshell 0.1.30")
    assert not mac.pinned_version("openshell 0.1.3-pre.1")


def test_unmounted_volume_refuses_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: False)
    with pytest.raises(mac.SetupError, match="not mounted"):
        mac.require_mac(chosen)
    with pytest.raises(mac.SetupError, match="not mounted"):
        mac.prepare_dirs(chosen)
    assert not chosen.root.exists()


def test_long_vm_socket_path_is_rejected_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = mac.Settings(
        volume=tmp_path,
        root=tmp_path / ("very-long-research-directory-" * 4),
        gateway_port=18081,
        image=mac.DEFAULT_IMAGE,
        github_user="example",
        keychain_service="test-secret-ghcr",
        application_config=None,
    )
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    with pytest.raises(mac.SetupError, match="too long"):
        mac.require_mac(chosen)
    assert not chosen.root.exists()


def test_system_root_cannot_be_the_large_state_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = settings(tmp_path)
    chosen = mac.Settings(
        volume=Path("/"),
        root=Path("/SandboxScience"),
        gateway_port=chosen.gateway_port,
        image=chosen.image,
        github_user=chosen.github_user,
        keychain_service=chosen.keychain_service,
        application_config=None,
    )
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    with pytest.raises(mac.SetupError, match="system root"):
        mac.require_mac(chosen)


def test_gateway_config_keeps_image_state_on_volume_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    mac.write_gateway_config(chosen)
    data = tomllib.loads(mac.config_path(chosen).read_text())
    assert data["openshell"]["drivers"]["vm"]["state_dir"] == str(chosen.root / "vm")
    assert data["openshell"]["gateway"]["compute_driver"] == "vm"
    mac.config_path(chosen).write_text("local modification\n")
    with pytest.raises(mac.SetupError, match="differs"):
        mac.write_gateway_config(chosen)
    assert mac.config_path(chosen).read_text() == "local modification\n"


def test_cli_is_pinned_to_the_dedicated_gateway(tmp_path: Path) -> None:
    chosen = settings(tmp_path)
    isolated = tmp_path / "cli-config/run"
    env = mac.cli_env(chosen, isolated)
    assert env["XDG_CONFIG_HOME"] == str(isolated)
    assert env["OPENSHELL_GATEWAY"] == mac.GATEWAY_NAME


def test_ambient_openshell_settings_cannot_change_the_gateway_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setenv("OPENSHELL_COMPUTE_DRIVER", "docker")
    monkeypatch.setenv("OPENSHELL_GATEWAY_CONFIG", "/elsewhere/gateway.toml")
    monkeypatch.setattr(mac, "keychain_token", lambda _settings: "secret")
    monkeypatch.setattr(mac, "require_command", lambda _name: "brew")
    monkeypatch.setattr(mac, "run", lambda _args, **_kwargs: "/opt/homebrew/opt/e2fsprogs")
    for env in (mac.gateway_env(chosen), mac.cli_env(chosen, tmp_path / "cli")):
        assert "OPENSHELL_COMPUTE_DRIVER" not in env
        assert "OPENSHELL_GATEWAY_CONFIG" not in env


@pytest.mark.parametrize("existing_env", [False, True])
def test_effective_application_config_pins_child_gateway_selection(
    tmp_path: Path, existing_env: bool
) -> None:
    chosen = settings(tmp_path)
    source = tmp_path / "private.toml"
    original = "[openshell]\ncli = 'openshell'\n"
    if existing_env:
        original += "[openshell.env]\nOTHER = 'kept'\n"
    source.write_text(original)
    env = mac.cli_env(chosen, tmp_path / "cli")
    effective = mac.effective_app_config(source, original, env)
    try:
        parsed = tomllib.loads(effective.read_text())
        assert effective.parent == source.parent
        assert parsed["openshell"]["env"]["OPENSHELL_GATEWAY"] == mac.GATEWAY_NAME
        assert parsed["openshell"]["env"]["XDG_CONFIG_HOME"] == str(tmp_path / "cli")
        if existing_env:
            assert parsed["openshell"]["env"]["OTHER"] == "kept"
        assert source.read_text() == original
    finally:
        effective.unlink()


def test_token_is_stored_without_putting_it_in_a_subprocess_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)

    class AddPassword:
        def __call__(
            self,
            _keychain: object,
            service_size: int,
            service: bytes,
            account_size: int,
            account: bytes,
            size: int,
            secret: object,
            _item: object,
        ) -> int:
            assert service[:service_size] == chosen.keychain_service.encode()
            assert account[:account_size] == chosen.github_user.encode()
            assert mac.ctypes.string_at(secret, size) == b"private-token"
            return 0

    class Security:
        SecKeychainAddGenericPassword = AddPassword()

    monkeypatch.setattr(mac.getpass, "getpass", lambda _prompt: "private-token")
    monkeypatch.setattr(mac.ctypes, "CDLL", lambda _framework: Security())
    mac.store_keychain_token(chosen)


def test_failed_sandbox_create_still_attempts_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = settings(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        if args[1:3] == ["sandbox", "create"]:
            raise mac.SetupError("create had an uncertain result")
        return ""

    monkeypatch.setattr(mac, "run", fake_run)
    with pytest.raises(mac.SetupError, match="uncertain"):
        mac.smoke("openshell", chosen, {})
    assert calls[0][1:3] == ["sandbox", "create"]
    assert calls[1][1:3] == ["sandbox", "delete"]
    assert calls[0][calls[0].index("--name") + 1] == calls[1][-1]


def test_successful_sage_check_requires_sandbox_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = settings(tmp_path)

    def fake_run(args: list[str], **_kwargs: object) -> str:
        if args[1:3] == ["sandbox", "delete"]:
            raise mac.SetupError("delete failed")
        if args[-2:] == ["/usr/bin/uname", "-m"]:
            return "aarch64"
        if "/opt/sandbox-science/bin/sage" in args:
            return "1"
        return ""

    monkeypatch.setattr(mac, "run", fake_run)
    with pytest.raises(mac.SetupError, match="Could not delete"):
        mac.smoke("openshell", chosen, {})
