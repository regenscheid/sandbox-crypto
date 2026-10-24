# SPDX-License-Identifier: MIT
"""The Mac helper refuses unsafe storage and cleans up uncertain smoke creates."""

from __future__ import annotations

import errno
import importlib.util
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION, pinned_version

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
    # The helper reads the one constant as text, since it runs with a bare python3.
    assert mac.OPEN_SHELL_VERSION == OPENSHELL_VERSION
    assert mac.pinned_version(f"openshell {OPENSHELL_VERSION}")
    assert mac.pinned_version(f"openshell-gateway v{OPENSHELL_VERSION}")
    assert not mac.pinned_version("openshell 0.1.3")
    assert not mac.pinned_version(f"openshell {OPENSHELL_VERSION}0")
    assert not mac.pinned_version(f"openshell {OPENSHELL_VERSION}-pre.1")
    assert mac.INSTALLER.endswith(f"/v{OPENSHELL_VERSION}/install.sh")
    # Both say the same about every output.
    for said in ("openshell 0.1.5", "v0.1.5", "0.1.50", "0.1.5-pre.1", "x", ""):
        assert mac.pinned_version(said) == pinned_version(said)


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


def test_vm_socket_storage_probe_cleans_up_on_supported_volume(tmp_path: Path) -> None:
    # A Unix socket's path holds at most 104 bytes on macOS, and pytest's temporary
    # directory there is longer than that with the probe's name: a short root, as a
    # real volume's is.
    root = Path(tempfile.mkdtemp(prefix="ss-", dir="/tmp"))
    try:
        chosen = replace(settings(tmp_path), root=root)
        mac.check_vm_socket_storage(chosen)
        assert list((root / "vm/run").iterdir()) == []
    finally:
        shutil.rmtree(root)


def test_vm_socket_storage_probe_reports_unsupported_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)

    class UnsupportedSocket:
        def __enter__(self) -> UnsupportedSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _address: str) -> None:
            raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(mac.socket, "socket", lambda *_args: UnsupportedSocket())
    with pytest.raises(mac.SetupError, match=r"cannot host a Unix socket.*Operation not supported"):
        mac.check_vm_socket_storage(chosen)
    assert list((chosen.root / "vm/run").iterdir()) == []


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
    driver_dir = tmp_path / "homebrew/libexec"
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    mac.write_gateway_config(chosen, driver_dir)
    data = tomllib.loads(mac.config_path(chosen).read_text())
    assert data["openshell"]["drivers"]["vm"]["state_dir"] == str(chosen.root / "vm")
    assert data["openshell"]["drivers"]["vm"]["driver_dir"] == str(driver_dir)
    assert data["openshell"]["gateway"]["compute_driver"] == "vm"
    mac.config_path(chosen).write_text("local modification\n")
    with pytest.raises(mac.SetupError, match="differs"):
        mac.write_gateway_config(chosen, driver_dir)
    assert mac.config_path(chosen).read_text() == "local modification\n"


def test_gateway_admits_only_clients_with_its_certificates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    driver_dir = tmp_path / "homebrew/libexec"
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    target = mac.config_path(chosen)
    # A plaintext config from before listener TLS migrates.
    target.write_text(mac.gateway_config(chosen, driver_dir, mac.DEFAULT_BOOTSTRAP_IMAGE))
    mac.write_gateway_config(chosen, driver_dir)
    data = tomllib.loads(target.read_text())
    gateway = data["openshell"]["gateway"]
    tls = chosen.root / "gateway/tls"
    assert "disable_tls" not in gateway
    assert "auth" not in gateway
    assert gateway["tls"] == {
        "cert_path": str(tls / "server/tls.crt"),
        "key_path": str(tls / "server/tls.key"),
        "client_ca_path": str(tls / "ca.crt"),
    }
    assert gateway["mtls_auth"] == {"enabled": True}
    vm = data["openshell"]["drivers"]["vm"]
    assert vm["grpc_endpoint"] == "https://127.0.0.1:18081"
    assert gateway["guest_tls_ca"] == str(tls / "ca.crt")
    assert "guest_tls_ca" not in vm
    assert chosen.gateway_url == "https://127.0.0.1:18081"


def test_vm_driver_is_resolved_beside_the_pinned_homebrew_gateway(tmp_path: Path) -> None:
    prefix = tmp_path / "Cellar/openshell/0.1.5"
    gateway = prefix / "bin/openshell-gateway"
    driver = prefix / "libexec/openshell-driver-vm"
    gateway.parent.mkdir(parents=True)
    driver.parent.mkdir(parents=True)
    gateway.write_text("gateway")
    gateway.chmod(0o755)
    driver.write_text("driver")
    driver.chmod(0o755)
    link = tmp_path / "bin/openshell-gateway"
    link.parent.mkdir()
    link.symlink_to(gateway)
    assert mac.vm_driver_dir(str(link)) == driver.parent
    driver.unlink()
    with pytest.raises(mac.SetupError, match="VM driver is missing"):
        mac.vm_driver_dir(str(link))


def test_exact_legacy_gateway_config_migrates_without_touching_other_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    driver_dir = tmp_path / "homebrew/libexec"
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    target = mac.config_path(chosen)
    target.write_text(mac.gateway_config(chosen))
    target.chmod(0o600)
    mac.write_gateway_config(chosen, driver_dir)
    assert target.read_text() == mac.gateway_config(
        chosen, driver_dir, mac.DEFAULT_BOOTSTRAP_IMAGE, tls=True
    )
    assert target.stat().st_mode & 0o777 == 0o600
    mac.write_gateway_config(chosen, driver_dir)


def test_nvcr_bootstrap_config_migrates_to_the_ghcr_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    driver_dir = tmp_path / "homebrew/libexec"
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    target = mac.config_path(chosen)
    target.write_text(mac.gateway_config(chosen, driver_dir))
    mac.write_gateway_config(chosen, driver_dir)
    vm = tomllib.loads(target.read_text())["openshell"]["drivers"]["vm"]
    assert vm["bootstrap_image"] == vm["default_image"] == mac.DEFAULT_BOOTSTRAP_IMAGE
    assert "nvcr.io" not in target.read_text()


def test_config_for_an_earlier_homebrew_driver_migrates_but_not_another_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    driver_dir = Path("/opt/homebrew/Cellar/openshell/0.1.5/libexec")
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    target = mac.config_path(chosen)
    old_driver = Path("/opt/homebrew/Cellar/openshell/0.1.3/libexec")
    target.write_text(mac.gateway_config(chosen, old_driver, mac.DEFAULT_BOOTSTRAP_IMAGE))
    mac.write_gateway_config(chosen, driver_dir)
    vm = tomllib.loads(target.read_text())["openshell"]["drivers"]["vm"]
    assert vm["driver_dir"] == str(driver_dir)
    elsewhere = Path("/tmp/openshell/libexec")
    target.write_text(mac.gateway_config(chosen, elsewhere, mac.DEFAULT_BOOTSTRAP_IMAGE))
    with pytest.raises(mac.SetupError, match="differs"):
        mac.write_gateway_config(chosen, driver_dir)


def test_bootstrap_mirror_workflow_copies_the_digest_the_helper_boots() -> None:
    workflow = SCRIPT.parents[1] / ".github/workflows/vm-bootstrap-mirror.yml"
    text = workflow.read_text()
    digest = mac.DEFAULT_BOOTSTRAP_IMAGE.split("@", 1)[1]
    assert f"DIGEST: {digest}\n" in text
    assert "--preserve-digests" in text
    assert mac.DEFAULT_BOOTSTRAP_IMAGE.startswith("ghcr.io/regenscheid/sandbox-crypto/")


@pytest.mark.parametrize(
    ("bootstrap", "message"),
    [
        ("ghcr.io/regenscheid/sandbox-crypto/vm-bootstrap:ubuntu-24.04", "pinned by @sha256"),
        (f"nvcr.io/nvidia/base/ubuntu@sha256:{'0' * 64}", "same registry"),
    ],
)
def test_bootstrap_must_be_digest_pinned_on_the_image_registry(
    tmp_path: Path, bootstrap: str, message: str
) -> None:
    config = tmp_path / "mac.toml"
    config.write_text(f'volume = "{tmp_path}"\nbootstrap_image = "{bootstrap}"\n')
    with pytest.raises(mac.SetupError, match=message):
        mac.read_settings(config)


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


def test_gateway_signing_is_generated_in_isolated_state_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "personal-config"))
    monkeypatch.setenv("OPENSHELL_LOCAL_TLS_DIR", str(tmp_path / "personal-tls"))
    mac.prepare_dirs(chosen)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, env: dict[str, str], **_kwargs: object) -> str:
        calls.append(args)
        assert env["XDG_CONFIG_HOME"] == str(chosen.root / "gateway/config")
        assert env["OPENSHELL_LOCAL_TLS_DIR"] == str(chosen.root / "gateway/tls")
        tls = Path(args[-1])
        for name in (
            "ca.crt",
            "ca.key",
            "server/tls.crt",
            "server/tls.key",
            "client/tls.crt",
            "client/tls.key",
            "jwt/signing.pem",
            "jwt/public.pem",
            "jwt/kid",
        ):
            path = tls / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("test certificate")
        return ""

    monkeypatch.setattr(mac, "run", fake_run)
    mac.ensure_gateway_signing(chosen, "openshell-gateway")
    mac.ensure_gateway_signing(chosen, "openshell-gateway")
    assert len(calls) == 1
    assert calls[0][:3] == ["openshell-gateway", "generate-certs", "--output-dir"]
    assert Path(calls[0][-1]).parent.parent == chosen.root / "gateway"
    assert (chosen.root / "gateway/tls/jwt/signing.pem").is_file()
    assert not (tmp_path / "personal-config").exists()
    assert not (tmp_path / "personal-tls").exists()


def test_interrupted_gateway_signing_keeps_live_state_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)

    def interrupted(args: list[str], **_kwargs: object) -> str:
        staged = Path(args[-1])
        (staged / "ca.crt").write_text("partial")
        raise mac.SetupError("generation interrupted")

    monkeypatch.setattr(mac, "run", interrupted)
    with pytest.raises(mac.SetupError, match="generation interrupted"):
        mac.ensure_gateway_signing(chosen, "openshell-gateway")
    assert list((chosen.root / "gateway/tls").iterdir()) == []
    assert not list((chosen.root / "gateway").glob(".gateway-signing-*"))


def test_gateway_signing_refuses_partial_or_symlinked_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    tls = chosen.root / "gateway/tls"
    (tls / "jwt").mkdir()
    (tls / "jwt/signing.pem").write_text("test key")
    with pytest.raises(mac.SetupError, match="Partial gateway signing state"):
        mac.ensure_gateway_signing(chosen, "openshell-gateway")
    (tls / "jwt/signing.pem").unlink()
    (tls / "jwt").rmdir()
    (tls / "jwt").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(mac.SetupError, match="must not contain symlinks"):
        mac.ensure_gateway_signing(chosen, "openshell-gateway")


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
    assert len(calls[1][-1]) <= 19


def test_smoke_sandbox_policy_adds_the_image_prefix_to_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = settings(tmp_path)
    policies: list[str] = []

    def fake_run(args: list[str], **_kwargs: object) -> str:
        if args[1:3] == ["sandbox", "create"]:
            policies.append(Path(args[args.index("--policy") + 1]).read_text())
        if args[-2:] == ["/usr/bin/uname", "-m"]:
            return "aarch64"
        if "/opt/sandbox-science/bin/sage" in args:
            return "1"
        return ""

    monkeypatch.setattr(mac, "run", fake_run)
    mac.smoke("openshell", chosen, {})
    assert policies == [mac.SMOKE_POLICY]
    filesystem = yaml.safe_load(mac.SMOKE_POLICY)["filesystem_policy"]
    assert {"/usr", "/lib", "/etc", "/opt/sandbox-science"} <= set(filesystem["read_only"])
    assert "/opt/sandbox-science" not in filesystem["read_write"]


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


def app_config(chosen: Any, **changes: str) -> dict[str, Any]:
    values = mac.expected_app_values(chosen)
    values.update(changes)
    environments = {
        key: values[f"openshell.environments.{key}"]
        for key in ("policy_store", "targets", "ceiling")
    }
    return {
        "state_dir": values["state_dir"],
        "worker": {"policy": "policy.yaml"},
        "openshell": {
            "package_cache": values["openshell.package_cache"],
            "cc": values["openshell.cc"],
            "environments": environments,
        },
    }


def test_launch_requires_the_helpers_state_and_services(tmp_path: Path) -> None:
    chosen = settings(tmp_path)
    source = tmp_path / "private/sandbox-science.toml"
    source.parent.mkdir()
    (source.parent / "policy.yaml").write_text("kind: ProjectPolicy\n")
    app = mac.check_app_config(chosen, source, app_config(chosen))
    assert app.policy == (source.parent / "policy.yaml").resolve()
    assert app.mcp_proxy_port == 18080
    wrong = app_config(chosen, **{"openshell.package_cache": "http://host.openshell.internal:8090"})
    with pytest.raises(
        mac.SetupError, match=re.escape('package_cache = "http://host.openshell.internal:18083"')
    ):
        mac.check_app_config(chosen, source, wrong)
    proxied = app_config(chosen)
    proxied["mcp_proxy"] = {"listen": "0.0.0.0:18080"}
    with pytest.raises(mac.SetupError, match=re.escape("listen must be 127.0.0.1")):
        mac.check_app_config(chosen, source, proxied)


def test_only_a_launch_registers_the_middleware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    driver_dir = tmp_path / "homebrew/libexec"
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    app = mac.AppSettings(policy=tmp_path / "policy.yaml", mcp_proxy_port=18090)
    launch = mac.write_service_settings(chosen, app, driver_dir)
    registered = tomllib.loads(launch.read_text())["openshell"]["supervisor"]["middleware"]
    services = mac.services_dir(chosen)
    assert registered == [
        {
            "name": "sandbox-science-policy",
            "grpc_endpoint": "https://127.0.0.1:18082",
            "tls_ca_cert_path": str(services / "middleware/tls/ca.pem"),
            "max_payload_bytes": 4194304,
            "timeout": "500ms",
        }
    ]
    mac.write_gateway_config(chosen, driver_dir)
    assert "supervisor" not in tomllib.loads(mac.config_path(chosen).read_text())["openshell"]
    targets = yaml.safe_load((services / "targets.yaml").read_text())
    assert targets["sandbox"]["package_cache"] == {"host": "host.openshell.internal", "port": 18083}
    assert targets["sandbox"]["mcp_proxy"]["port"] == 18090
    middleware = tomllib.loads((services / "middleware/middleware.toml").read_text())
    assert middleware["listen"] == "127.0.0.1:18082"
    assert middleware["tokens"]["public_key"] == str(chosen.root / "gateway/tls/jwt/public.pem")
    for path in (launch, services / "targets.yaml", services / "middleware/middleware.toml"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert (services / "middleware/tls/middleware-key.pem").stat().st_mode & 0o777 == 0o600
    # The certificates are made once and kept.
    certificate = (services / "middleware/tls/middleware.pem").read_bytes()
    mac.write_service_settings(chosen, app, driver_dir)
    assert (services / "middleware/tls/middleware.pem").read_bytes() == certificate


def test_partial_middleware_certificates_are_left_for_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    tls = mac.services_dir(chosen) / "middleware/tls"
    tls.mkdir(parents=True)
    (tls / "ca.pem").write_text("an earlier CA\n")
    with pytest.raises(mac.SetupError, match="Partial middleware certificates"):
        mac.ensure_middleware_tls(chosen)
    assert (tls / "ca.pem").read_text() == "an earlier CA\n"


def test_service_ports_must_differ(tmp_path: Path) -> None:
    config = tmp_path / "mac.toml"
    config.write_text(f'volume = "{tmp_path}"\nmiddleware_port = 18081\n')
    with pytest.raises(mac.SetupError, match="must differ"):
        mac.read_settings(config)


def test_the_worker_cross_compiles_its_shim_for_the_linux_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path)
    monkeypatch.setattr(mac, "require_command", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(mac.os.path, "ismount", lambda _path: True)
    mac.prepare_dirs(chosen)
    mac.write_cross_compiler(chosen)
    wrapper = mac.cross_compiler(chosen)
    text = wrapper.read_text()
    assert wrapper.stat().st_mode & 0o777 == 0o700
    assert 'exec "/opt/homebrew/bin/zig" cc -target aarch64-linux-gnu.2.28 "$@"' in text
    assert f'ZIG_GLOBAL_CACHE_DIR="{chosen.root / "cache/zig"}"' in text
    source = tmp_path / "private/sandbox-science.toml"
    source.parent.mkdir()
    (source.parent / "policy.yaml").write_text("kind: ProjectPolicy\n")
    config = app_config(chosen)
    config["openshell"]["cc"] = "cc"
    with pytest.raises(mac.SetupError, match=re.escape(f'openshell.cc = "{wrapper}"')):
        mac.check_app_config(chosen, source, config)
