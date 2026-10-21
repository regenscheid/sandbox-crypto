#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install and run the isolated OpenShell crypto-base test on Apple Silicon."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

OPEN_SHELL_VERSION = "0.1.5"
INSTALLER = f"https://raw.githubusercontent.com/NVIDIA/OpenShell/v{OPEN_SHELL_VERSION}/install.sh"
INSTALLER_SHA256 = "7bf6706741b8b59c1b61e0c47030f0a34fefba6cee3c74cd52638cbf7cdc3655"
DEFAULT_IMAGE = "ghcr.io/regenscheid/sandbox-crypto/crypto-base:6bb73871"
# OpenShell's VM driver sends its one registry credential to every pull, so the
# bootstrap image is NVIDIA's index mirrored beside crypto-base with its digest kept.
DEFAULT_BOOTSTRAP_IMAGE = (
    "ghcr.io/regenscheid/sandbox-crypto/vm-bootstrap"
    "@sha256:7e0f726d635d440d28a7f18e6498d3bdb76c7038bd4c90a090c21eb7c9f40686"
)
LEGACY_BOOTSTRAP_IMAGE = "nvcr.io/nvidia/base/ubuntu:24.04"
GATEWAY_NAME = "ss-mac-crypto-test"
# OpenShell's supervisor adds its baseline paths only to a sandbox with network rules,
# which the smoke sandbox lacks, so this repeats OpenShell's default policy (as
# `policy get --full` reports it), adds the image's Conda prefix read-only and requires
# Landlock, as compiled policies do.
SMOKE_POLICY = """version: 1
filesystem_policy:
  include_workdir: true
  read_only:
  - /bin
  - /usr
  - /lib
  - /proc
  - /dev/urandom
  - /etc
  - /var/log
  - /opt/sandbox-science
  read_write:
  - /tmp
  - /dev/null
landlock:
  compatibility: hard_requirement
"""
ISOLATED_CLI_KEYS = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "OPENSHELL_LOCAL_TLS_DIR",
    "OPENSHELL_GATEWAY",
)


class SetupError(Exception):
    """An actionable Mac setup or test failure."""


@dataclass(frozen=True)
class Settings:
    """The small user-edited part of the Mac setup."""

    volume: Path
    root: Path
    gateway_port: int
    image: str
    github_user: str
    keychain_service: str
    application_config: Path | None
    bootstrap_image: str = DEFAULT_BOOTSTRAP_IMAGE
    middleware_port: int = 18082
    package_cache_port: int = 18083

    @property
    def gateway_url(self) -> str:
        """The dedicated loopback endpoint for this test."""
        return f"https://127.0.0.1:{self.gateway_port}"


def read_settings(path: Path) -> Settings:
    """Load a local TOML file without accepting ambiguous storage paths."""
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"Cannot read {path}: {exc}") from exc
    allowed = {
        "volume",
        "directory",
        "gateway_port",
        "image",
        "github_user",
        "keychain_service",
        "application_config",
        "bootstrap_image",
        "middleware_port",
        "package_cache_port",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise SetupError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    volume_text = raw.get("volume")
    directory = raw.get("directory", "SandboxScience")
    if not isinstance(volume_text, str) or not volume_text.startswith("/"):
        raise SetupError("volume must be an absolute path to an existing mounted volume")
    if not isinstance(directory, str) or directory in {"", ".", ".."} or "/" in directory:
        raise SetupError("directory must be one simple folder name")
    volume = Path(volume_text)
    if volume_text != str(volume):
        raise SetupError("volume must use a normalized absolute path")
    port = raw.get("gateway_port", 18081)
    middleware_port = raw.get("middleware_port", 18082)
    package_cache_port = raw.get("package_cache_port", 18083)
    for name, value in (
        ("gateway_port", port),
        ("middleware_port", middleware_port),
        ("package_cache_port", package_cache_port),
    ):
        if type(value) is not int or not 1024 <= value <= 65535:
            raise SetupError(f"{name} must be an integer from 1024 to 65535")
    if len({port, middleware_port, package_cache_port}) != 3:
        raise SetupError("gateway_port, middleware_port and package_cache_port must differ")
    image = raw.get("image", DEFAULT_IMAGE)
    user = raw.get("github_user", "regenscheid")
    service = raw.get("keychain_service", "sandbox-science-ghcr")
    bootstrap = raw.get("bootstrap_image", DEFAULT_BOOTSTRAP_IMAGE)
    for name, value in (
        ("image", image),
        ("github_user", user),
        ("keychain_service", service),
        ("bootstrap_image", bootstrap),
    ):
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise SetupError(f"{name} must be a nonempty string without spaces")
    if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", bootstrap):
        raise SetupError("bootstrap_image must be pinned by @sha256 digest")
    if bootstrap.split("/", 1)[0] != image.split("/", 1)[0]:
        raise SetupError(
            "bootstrap_image must use the same registry as image; "
            "OpenShell sends one registry credential to both pulls"
        )
    app_text = raw.get("application_config")
    if app_text is not None and (not isinstance(app_text, str) or not app_text.startswith("/")):
        raise SetupError("application_config must be an absolute path")
    return Settings(
        volume=volume,
        root=volume / directory,
        gateway_port=port,
        image=image,
        github_user=user,
        keychain_service=service,
        application_config=Path(app_text) if app_text else None,
        bootstrap_image=bootstrap,
        middleware_port=middleware_port,
        package_cache_port=package_cache_port,
    )


def require_mac(settings: Settings, *, space: bool = True) -> None:
    """Refuse an absent volume before creating any large state on the system disk."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SetupError("This helper requires an Apple Silicon Mac with a native arm64 terminal")
    if settings.volume == Path("/"):
        raise SetupError("Choose a mounted data volume, not the Mac's system root")
    if not settings.volume.is_dir() or not os.path.ismount(settings.volume):
        raise SetupError(f"Volume is not mounted: {settings.volume}")
    if settings.root.is_symlink():
        raise SetupError(f"Storage directory must not be a symlink: {settings.root}")
    socket_path = settings.root / "vm/run/compute-driver.sock"
    if len(os.fsencode(socket_path)) >= 104:
        raise SetupError(
            f"OpenShell VM socket path is too long for macOS: {socket_path}; "
            "choose a shorter volume or directory name"
        )
    if space and shutil.disk_usage(settings.volume).free < 40 * 1024**3:
        print(
            f"Warning: under 40 GiB free on {settings.volume}; "
            "crypto-base preparation may need more",
            file=sys.stderr,
        )


def require_command(name: str) -> str:
    """Locate a command without modifying the user's normal tool installation."""
    command = shutil.which(name)
    if command is None:
        raise SetupError(f"Missing {name}; install it first, then rerun this command")
    return command


def pinned_version(output: str) -> bool:
    """Match exactly the pinned release, not 0.1.50 or a prerelease of it."""
    pattern = rf"(?<![\w.])v?{re.escape(OPEN_SHELL_VERSION)}(?![\w.+-])"
    return re.search(pattern, output) is not None


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> str:
    """Run a trusted local command and turn a failing exit into a useful error."""
    try:
        result = subprocess.run(  # noqa: S603 - argv is a trusted local executable; no shell.
            args,
            env=env,
            cwd=cwd,
            text=True,
            check=True,
            capture_output=capture,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"{args[0]} failed with exit status {exc.returncode}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"{args[0]} did not answer within {timeout} seconds") from exc
    return result.stdout.strip() if capture else ""


def prepare_dirs(settings: Settings) -> None:
    """Create only the chosen volume's application and OpenShell state."""
    if not settings.volume.is_dir() or not os.path.ismount(settings.volume):
        raise SetupError(f"Volume is not mounted: {settings.volume}")
    paths = [
        settings.root,
        settings.root / "app",
        settings.root / "vm",
        settings.root / "gateway",
        settings.root / "gateway/state",
        settings.root / "gateway/data",
        settings.root / "gateway/cache",
        settings.root / "gateway/config",
        settings.root / "gateway/tls",
        settings.root / "cli-config",
        settings.root / "cache",
    ]
    for path in paths:
        if path.is_symlink():
            raise SetupError(f"State directory must not be a symlink: {path}")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)


def check_vm_socket_storage(settings: Settings) -> None:
    """Verify the chosen volume can host the VM driver's Unix control socket."""
    socket_dir = settings.root / "vm/run"
    socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = socket_dir / f".{secrets.token_hex(8)}"
    bound = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.bind(str(socket_path))
            bound = True
    except OSError as exc:
        raise SetupError(
            f"The VM state volume cannot host a Unix socket at {socket_path}: {exc}. "
            "Choose a local volume that supports Unix sockets (APFS recommended) "
            "for `volume` in the Mac TOML; "
            "preserve the existing SandboxScience directory before changing paths."
        ) from exc
    finally:
        if bound:
            socket_path.unlink()


def gateway_config(
    settings: Settings,
    driver_dir: Path | None = None,
    bootstrap: str | None = None,
    *,
    tls: bool = False,
) -> str:
    """Render the VM config; None and False reproduce only former generated configs.

    With ``tls``, the listener uses the gateway's own certificate and admits only
    clients with a certificate from its CA (the CLI's, which `gateway add --local`
    copies), and the host supervisors reach it over TLS as well.
    """
    tls_dir = settings.root / "gateway/tls"
    driver_location = (
        f"driver_dir = {json.dumps(str(driver_dir))}\n" if driver_dir is not None else ""
    )
    images = (
        f"bootstrap_image = {json.dumps(bootstrap)}\ndefault_image = {json.dumps(bootstrap)}\n"
        if bootstrap is not None
        else f"default_image = {json.dumps(LEGACY_BOOTSTRAP_IMAGE)}\n"
    )
    if tls:
        listener = f"""
[openshell.gateway.tls]
cert_path = {json.dumps(str(tls_dir / "server/tls.crt"))}
key_path = {json.dumps(str(tls_dir / "server/tls.key"))}
client_ca_path = {json.dumps(str(tls_dir / "ca.crt"))}

[openshell.gateway.mtls_auth]
enabled = true
"""
        endpoint = f'grpc_endpoint = "{settings.gateway_url}"\n'
        # The host supervisors' CA for the gateway; v0.1.5 reads it here, not in the driver.
        plaintext = f"guest_tls_ca = {json.dumps(str(tls_dir / 'ca.crt'))}\n"
    else:
        listener = """
[openshell.gateway.auth]
allow_unauthenticated_users = true
"""
        endpoint = f'grpc_endpoint = "http://127.0.0.1:{settings.gateway_port}"\n'
        plaintext = "disable_tls = true\n"
    return f"""[openshell]
version = 2

[openshell.gateway]
compute_driver = "vm"
{plaintext}bind_address = "127.0.0.1:{settings.gateway_port}"
image_preparation_timeout_seconds = 3600
{listener}
[openshell.drivers.vm]
{driver_location}state_dir = {json.dumps(str(settings.root / "vm"))}
{endpoint}{images}vcpus = 4
mem_mib = 8192
overlay_disk_mib = 16384
"""


def config_path(settings: Settings) -> Path:
    """The dedicated gateway configuration path."""
    return settings.root / "gateway/gateway.toml"


def vm_driver_dir(gateway: str) -> Path:
    """Find the VM driver in the same pinned Homebrew installation as the gateway."""
    gateway_path = Path(gateway).resolve()
    driver = gateway_path.parent.parent / "libexec/openshell-driver-vm"
    if not driver.is_file() or not os.access(driver, os.X_OK):
        raise SetupError(
            f"OpenShell VM driver is missing or not executable: {driver}. "
            "Inspect the pinned Homebrew installation with "
            "`brew list nvidia/openshell/openshell`; if incomplete, "
            f"reinstall OpenShell v{OPEN_SHELL_VERSION} with Homebrew before retrying"
        )
    return driver.parent


HOMEBREW_DRIVER_RE: Final = re.compile(r"/opt/homebrew/Cellar/openshell/\d+\.\d+\.\d+/libexec")


def earlier_gateway_configs(settings: Settings, driver_dir: Path, previous: str) -> set[str]:
    """Configs this helper generated before, which it may replace without asking.

    An earlier config may name the VM driver of an earlier Homebrew OpenShell, which an
    upgrade removes; only a driver directory of that exact form is accepted.
    """
    drivers: list[Path | None] = [None, driver_dir]
    try:
        named = tomllib.loads(previous)["openshell"]["drivers"]["vm"].get("driver_dir")
    except (tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError):
        named = None
    if isinstance(named, str) and HOMEBREW_DRIVER_RE.fullmatch(named):
        drivers.append(Path(named))
    return {
        gateway_config(settings, driver, bootstrap, tls=tls)
        for driver in drivers
        for bootstrap in (None, settings.bootstrap_image)
        for tls in (False, True)
        if not (driver is None and bootstrap is not None) and not (bootstrap is None and tls)
    }


def write_gateway_config(settings: Settings, driver_dir: Path) -> None:
    """Write the pinned config, migrating only our exact previous generated text."""
    target = config_path(settings)
    content = gateway_config(settings, driver_dir, settings.bootstrap_image, tls=True)
    if target.parent.is_symlink():
        raise SetupError(f"Gateway directory must not be a symlink: {target.parent}")
    if target.is_symlink():
        raise SetupError(f"Gateway config must not be a symlink: {target}")
    if target.exists():
        previous = target.read_text()
        if previous == content:
            return
        if previous not in earlier_gateway_configs(settings, driver_dir, previous):
            raise SetupError(
                f"Gateway config differs from this setup: {target}; inspect it before changing it"
            )
        # Replace only an exact earlier helper-generated config, which lacked the
        # driver's Homebrew location, the GHCR bootstrap or listener TLS, or named an
        # earlier Homebrew OpenShell's driver. A crash cannot leave half a TOML.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=".gateway-", suffix=".toml", dir=target.parent, delete=False
        ) as stream:
            stream.write(content)
            replacement = Path(stream.name)
        try:
            replacement.chmod(0o600)
            replacement.replace(target)
        finally:
            replacement.unlink(missing_ok=True)
        return
    with target.open("x") as stream:
        stream.write(content)
    target.chmod(0o600)


def keychain_token(settings: Settings) -> str | None:
    """Read the package token only into memory, never into the config or logs."""
    result = subprocess.run(  # noqa: S603 - fixed macOS Keychain command.
        [
            require_command("security"),
            "find-generic-password",
            "-a",
            settings.github_user,
            "-s",
            settings.keychain_service,
            "-w",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        token = result.stdout.strip()
        if not token:
            raise SetupError("The GHCR Keychain item has an empty token")
        return token
    if "could not be found" in result.stderr.lower():
        return None
    raise SetupError("Cannot read the GHCR token from Keychain; inspect Keychain access")


def clean_openshell_env() -> dict[str, str]:
    """Discard shell settings that could override the dedicated VM gateway."""
    return {key: value for key, value in os.environ.items() if not key.startswith("OPENSHELL_")}


def store_keychain_token(settings: Settings) -> None:
    """Prompt without echo and add a Keychain item without a token in process argv."""
    token = getpass.getpass("GitHub classic token (read:packages): ").strip()
    if not token:
        raise SetupError("No GitHub token entered")
    service = settings.keychain_service.encode()
    account = settings.github_user.encode()
    secret = ctypes.create_string_buffer(token.encode())
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    add = security.SecKeychainAddGenericPassword
    add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    add.restype = ctypes.c_int32
    status = add(
        None,
        len(service),
        service,
        len(account),
        account,
        len(token.encode()),
        ctypes.cast(secret, ctypes.c_void_p),
        None,
    )
    if status:
        raise SetupError(f"Keychain could not store the token (OSStatus {status})")


def install(settings: Settings) -> None:
    """Install the pinned local runtime and prepare only selected-volume state."""
    require_mac(settings)
    brew = require_command("brew")
    prepare_dirs(settings)
    check_vm_socket_storage(settings)
    if subprocess.run(  # noqa: S603 - brew path came from command lookup.
        [brew, "list", "--versions", "e2fsprogs"], capture_output=True
    ).returncode:
        run([brew, "install", "e2fsprogs"])
    if shutil.which("uv") is None:
        run([brew, "install", "uv"])
    if shutil.which("zig") is None:
        run([brew, "install", "zig"])
    installed = shutil.which("openshell")
    version = run([installed, "--version"], capture=True) if installed else ""
    if installed and not pinned_version(version):
        raise SetupError(
            f"Installed OpenShell is {version}; this helper needs {OPEN_SHELL_VERSION}. "
            f"Upgrade it with OpenShell's installer: curl -LsSf {INSTALLER} | "
            f"OPENSHELL_VERSION=v{OPEN_SHELL_VERSION} sh (it reinstalls the Homebrew "
            "formula and restarts its gateway service); the VM state on the volume is kept"
        )
    if not installed:
        curl = require_command("curl")
        with tempfile.TemporaryDirectory(dir=settings.root / "cache") as directory:
            script = Path(directory) / "install.sh"
            run([curl, "-fsSL", INSTALLER, "-o", str(script)])
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            if digest != INSTALLER_SHA256:
                raise SetupError(f"OpenShell installer checksum mismatch: {digest}")
            env = os.environ.copy()
            env["OPENSHELL_VERSION"] = f"v{OPEN_SHELL_VERSION}"
            run(["sh", str(script)], env=env)
    for command in ("openshell", "openshell-gateway"):
        version = run([require_command(command), "--version"], capture=True)
        if not pinned_version(version):
            raise SetupError(f"{command} must be version {OPEN_SHELL_VERSION}; found {version}")
    driver_dir = vm_driver_dir(require_command("openshell-gateway"))
    write_gateway_config(settings, driver_dir)
    ensure_gateway_signing(settings, require_command("openshell-gateway"))
    e2fs = run([brew, "--prefix", "e2fsprogs"], capture=True)
    preflight_env = clean_openshell_env()
    preflight_env["PATH"] = f"{e2fs}/sbin:{preflight_env.get('PATH', '')}"
    run(
        [
            require_command("openshell-gateway"),
            "config",
            "preflight",
            "--path",
            str(config_path(settings)),
        ],
        env=preflight_env,
    )
    if keychain_token(settings) is None:
        store_keychain_token(settings)
    print(f"Ready. Large VM and gateway state: {settings.root}")


def gateway_state_env(settings: Settings) -> dict[str, str]:
    """Confine gateway tools and their CLI auto-discovery copy to this volume."""
    env = clean_openshell_env()
    gateway = settings.root / "gateway"
    env.update(
        {
            "XDG_CONFIG_HOME": str(gateway / "config"),
            "XDG_STATE_HOME": str(gateway / "state"),
            "XDG_DATA_HOME": str(gateway / "data"),
            "XDG_CACHE_HOME": str(gateway / "cache"),
            "OPENSHELL_LOCAL_TLS_DIR": str(gateway / "tls"),
            "OPENSHELL_DB_URL": f"sqlite:{gateway / 'openshell.db'}?mode=rwc",
        }
    )
    return env


def ensure_gateway_signing(settings: Settings, gateway_binary: str) -> None:
    """Create the VM launch signer once, without touching the normal CLI profile."""
    tls_dir = settings.root / "gateway/tls"
    tls_files = (
        tls_dir / "ca.crt",
        tls_dir / "ca.key",
        tls_dir / "server/tls.crt",
        tls_dir / "server/tls.key",
        tls_dir / "client/tls.crt",
        tls_dir / "client/tls.key",
    )
    jwt_files = (
        tls_dir / "jwt/signing.pem",
        tls_dir / "jwt/public.pem",
        tls_dir / "jwt/kid",
    )
    if any(
        path.is_symlink()
        for path in (
            tls_dir,
            *(path.parent for path in (*tls_files, *jwt_files)),
            *tls_files,
            *jwt_files,
        )
    ):
        raise SetupError(f"Gateway signing state must not contain symlinks: {tls_dir}")
    tls_count = sum(path.is_file() for path in tls_files)
    jwt_count = sum(path.is_file() for path in jwt_files)
    if (tls_count, jwt_count) == (6, 3):
        if not all(path.stat().st_size for path in (*tls_files, *jwt_files)):
            raise SetupError(f"Gateway signing state has an empty file at {tls_dir}")
        return
    if (tls_count, jwt_count) not in {(0, 0), (6, 0)}:
        raise SetupError(f"Partial gateway signing state at {tls_dir}; inspect it before retrying")
    # OpenShell installs individual files, so generate in a disposable sibling
    # first. A cancelled command cannot leave the live signer half written.
    with tempfile.TemporaryDirectory(prefix=".gateway-signing-", dir=tls_dir.parent) as temp:
        staged = Path(temp) / "tls"
        staged.mkdir(mode=0o700)
        if tls_count == 6:
            for source in tls_files:
                destination = staged / source.relative_to(tls_dir)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        run(
            [gateway_binary, "generate-certs", "--output-dir", str(staged)],
            env=gateway_state_env(settings),
            timeout=60,
        )
        if not all(
            (staged / path.relative_to(tls_dir)).is_file()
            and (staged / path.relative_to(tls_dir)).stat().st_size
            for path in (*tls_files, *jwt_files)
        ):
            raise SetupError(f"Gateway did not generate a complete signing bundle in {staged}")
        if tls_count == 0:
            if any(tls_dir.iterdir()):
                raise SetupError(f"Unexpected gateway signing state at {tls_dir}; inspect it")
            tls_dir.rmdir()
            staged.rename(tls_dir)
        else:
            jwt_dir = tls_dir / "jwt"
            if jwt_dir.exists():
                if any(jwt_dir.iterdir()):
                    raise SetupError(f"Unexpected gateway signing state at {jwt_dir}; inspect it")
                jwt_dir.rmdir()
            (staged / "jwt").rename(jwt_dir)
    if not all(path.is_file() and path.stat().st_size for path in (*tls_files, *jwt_files)):
        raise SetupError(f"Gateway did not create a complete signing bundle at {tls_dir}")


def gateway_env(settings: Settings) -> dict[str, str]:
    """Supply private registry auth and selected-volume state to one gateway."""
    token = keychain_token(settings)
    if not token:
        raise SetupError("No GHCR token in Keychain; run install first")
    env = gateway_state_env(settings)
    env["OPENSHELL_REGISTRY_USERNAME"] = settings.github_user
    env["OPENSHELL_REGISTRY_TOKEN"] = token
    brew = require_command("brew")
    e2fs = run([brew, "--prefix", "e2fsprogs"], capture=True)
    env["PATH"] = f"{e2fs}/sbin:{env.get('PATH', '')}"
    return env


def wait_for_gateway(process: subprocess.Popen[bytes], settings: Settings) -> None:
    """Wait for this gateway's loopback listener, never attaching to another one."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError(f"Gateway exited with status {process.returncode}; see its log")
        try:
            with socket.create_connection(("127.0.0.1", settings.gateway_port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise SetupError("Gateway did not open its loopback port within 90 seconds; see its log")


def stop_gateway(process: subprocess.Popen[bytes], *, group: bool = True) -> None:
    """Stop only the process group created for this invocation.

    Without ``group``, the first signal goes to the leader alone: ``uv run`` forwards it to
    its command, which would otherwise receive it twice. The group is killed if it stays.
    """
    if process.poll() is not None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def cli_env(settings: Settings, cli_config: Path) -> dict[str, str]:
    """Keep gateway selection out of the owner's regular CLI settings."""
    env = clean_openshell_env()
    env["XDG_CONFIG_HOME"] = str(cli_config)
    env["XDG_STATE_HOME"] = str(settings.root / "gateway/state")
    env["XDG_DATA_HOME"] = str(settings.root / "gateway/data")
    env["XDG_CACHE_HOME"] = str(settings.root / "gateway/cache")
    env["OPENSHELL_LOCAL_TLS_DIR"] = str(settings.root / "gateway/tls")
    env["OPENSHELL_GATEWAY"] = GATEWAY_NAME
    return env


def effective_app_config(source: Path, contents: str, env: dict[str, str]) -> Path:
    """Give the launcher and every worker the same isolated CLI settings.

    Preserve the source directory because the launcher resolves relative config
    paths against the config file's parent directory.
    """
    assignments = "\n".join(f"{key} = {json.dumps(env[key])}" for key in ISOLATED_CLI_KEYS)
    section = re.search(r"(?m)^[ \t]*\[openshell\.env\][ \t]*(?:#.*)?$", contents)
    if section:
        rendered = contents[: section.end()] + "\n" + assignments + contents[section.end() :]
    else:
        if "env" in tomllib.loads(contents).get("openshell", {}):
            raise SetupError("Write application [openshell.env] as a normal TOML table")
        rendered = contents.rstrip() + "\n\n[openshell.env]\n" + assignments + "\n"
    try:
        parsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"Could not prepare application config: {exc}") from exc
    if any(parsed["openshell"]["env"].get(key) != env[key] for key in ISOLATED_CLI_KEYS):
        raise SetupError("Could not pin application OpenShell settings")
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix=".sandbox-science-mac-effective-",
        suffix=".toml",
        dir=source.parent,
        delete=False,
    ) as stream:
        stream.write(rendered)
        return Path(stream.name)


# The launch's own platform services: the policy middleware and the package cache, which
# run beside the gateway on the host, as on babbage (scripts/personal_up.sh).

MIDDLEWARE: Final = "sandbox-science-policy"
MIDDLEWARE_TLS: Final = ("ca.pem", "middleware.pem", "middleware-key.pem")
PLACEHOLDER_PROFILES: Final = {"github-token": "examples/openshell/profiles/github-token.yaml"}
"""Provider profiles a launch may register with a placeholder credential, as babbage does
for github-readonly: public repositories work without a real token."""


SHIM_TARGET: Final = "aarch64-linux-gnu.2.28"
"""What the worker's scheduler shim is built for: the Linux ARM64 guest, glibc 2.28 or
newer, not the macOS host (packages/worker/src/sandbox_science/worker/shim)."""


def cross_compiler(settings: Settings) -> Path:
    """The C compiler the worker builds its scheduler shim with, for the Linux guest."""
    return settings.root / "cache/bin/cc-linux-aarch64"


def write_cross_compiler(settings: Settings) -> None:
    """Write the worker's shim compiler: zig's C compiler, targeting the Linux guest.

    The worker builds the shim with the host's ``cc`` and preloads it in the sandbox, so
    on a Mac host that ``cc`` must cross-compile. zig carries the glibc headers and stubs
    a Linux target needs; its caches stay on the selected volume.
    """
    zig = require_command("zig")
    path = cross_compiler(settings)
    if path.parent.is_symlink():
        raise SetupError(f"State directory must not be a symlink: {path.parent}")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    cache = settings.root / "cache/zig"
    write_private(
        path,
        "#!/bin/sh\n"
        "# Written by scripts/mac_local.py: the worker's scheduler shim, for the Linux guest.\n"
        f"export ZIG_GLOBAL_CACHE_DIR={json.dumps(str(cache))}\n"
        f"export ZIG_LOCAL_CACHE_DIR={json.dumps(str(cache))}\n"
        f'exec {json.dumps(zig)} cc -target {SHIM_TARGET} "$@"\n',
    )
    path.chmod(0o700)


def services_dir(settings: Settings) -> Path:
    """The services' state: certificates, policy store, ceiling, cache and settings."""
    return settings.root / "services"


@dataclass(frozen=True)
class AppSettings:
    """What a launch needs from the private application config."""

    policy: Path
    mcp_proxy_port: int


def expected_app_values(settings: Settings) -> dict[str, str]:
    """The application config values that must name this helper's services."""
    services = services_dir(settings)
    return {
        "state_dir": str(settings.root / "app"),
        "openshell.package_cache": f"http://host.openshell.internal:{settings.package_cache_port}",
        "openshell.cc": str(cross_compiler(settings)),
        "openshell.environments.policy_store": str(services / "middleware/state"),
        "openshell.environments.targets": str(services / "targets.yaml"),
        "openshell.environments.ceiling": str(services / "middleware/pool-ceiling.yaml"),
    }


def check_app_config(settings: Settings, source: Path, config: dict[str, object]) -> AppSettings:
    """Refuse an application config that does not use this helper's state and services."""

    def value(dotted: str) -> object:
        node: object = config
        for key in dotted.split("."):
            node = node.get(key) if isinstance(node, dict) else None
        return node

    def resolved(text: object) -> Path | None:
        if not isinstance(text, str):
            return None
        path = Path(text).expanduser()
        return (path if path.is_absolute() else source.parent / path).resolve()

    wrong = []
    for dotted, wanted in expected_app_values(settings).items():
        found = value(dotted)
        same = (
            found == wanted
            if dotted == "openshell.package_cache"
            else resolved(found) == Path(wanted).resolve()
        )
        if not same:
            wrong.append(f"  {dotted} = {json.dumps(wanted)}")
    if wrong:
        raise SetupError(
            "Application config must use this helper's state and services; set:\n"
            + "\n".join(wrong)
        )
    policy = resolved(value("worker.policy"))
    if policy is None or not policy.is_file():
        raise SetupError("Application config [worker] policy must name an existing policy file")
    listen = value("mcp_proxy.listen")
    port = 18080
    if isinstance(listen, str):
        match = re.fullmatch(r"127\.0\.0\.1:(\d{4,5})", listen)
        if match is None:
            raise SetupError("Application config [mcp_proxy] listen must be 127.0.0.1:<port>")
        port = int(match.group(1))
    return AppSettings(policy=policy, mcp_proxy_port=port)


def ensure_middleware_tls(settings: Settings) -> None:
    """Make the middleware's own CA and loopback certificate once, and keep them."""
    tls = services_dir(settings) / "middleware/tls"
    present = [name for name in MIDDLEWARE_TLS if (tls / name).exists()]
    if len(present) == len(MIDDLEWARE_TLS):
        return
    if present or (tls.exists() and any(tls.iterdir())):
        raise SetupError(
            f"Partial middleware certificates in {tls}; inspect them before changing files"
        )
    openssl = require_command("openssl")
    staged = Path(tempfile.mkdtemp(prefix=".middleware-tls-", dir=tls.parent))
    try:
        key, ca, csr = staged / "ca-key.pem", staged / "ca.pem", staged / "middleware.csr"
        extensions = staged / "server-ext.cnf"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=IP:127.0.0.1,DNS:localhost\n"
        )
        curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"]
        ca_subject = ["-subj", "/CN=sandbox-science Mac middleware CA"]
        ca_extensions = [
            *("-addext", "basicConstraints=critical,CA:TRUE,pathlen:0"),
            *("-addext", "keyUsage=critical,keyCertSign,cRLSign"),
        ]
        run(
            [
                openssl,
                "req",
                "-x509",
                *curve,
                "-days",
                "3650",
                *ca_subject,
                *ca_extensions,
                "-keyout",
                str(key),
                "-out",
                str(ca),
            ],
            capture=True,
        )
        run(
            [
                openssl,
                "req",
                "-new",
                *curve,
                "-subj",
                f"/CN={MIDDLEWARE}",
                "-keyout",
                str(staged / "middleware-key.pem"),
                "-out",
                str(csr),
            ],
            capture=True,
        )
        signing = ["-CA", str(ca), "-CAkey", str(key), "-CAcreateserial", "-days", "3650"]
        run(
            [
                openssl,
                "x509",
                "-req",
                "-in",
                str(csr),
                *signing,
                "-sha256",
                "-extfile",
                str(extensions),
                "-out",
                str(staged / "middleware.pem"),
            ],
            capture=True,
        )
        for path in staged.iterdir():
            path.chmod(0o600 if path.name.endswith("key.pem") else 0o644)
        staged.chmod(0o700)
        if tls.exists():
            tls.rmdir()
        staged.replace(tls)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def write_private(path: Path, text: str) -> None:
    """Replace a helper-owned settings file atomically, readable by the owner only."""
    if path.is_symlink():
        raise SetupError(f"Settings file must not be a symlink: {path}")
    with tempfile.NamedTemporaryFile(
        mode="w", prefix=f".{path.name}-", dir=path.parent, delete=False
    ) as stream:
        stream.write(text)
        staged = Path(stream.name)
    try:
        staged.chmod(0o600)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def launch_gateway_config(settings: Settings, driver_dir: Path) -> str:
    """The gateway config with the policy middleware registered, for launches only.

    The gateway calls the middleware when it starts and refuses to start without it, so
    smoke keeps the config without this registration.
    """
    ca = services_dir(settings) / "middleware/tls/ca.pem"
    return (
        gateway_config(settings, driver_dir, settings.bootstrap_image, tls=True)
        + f"""
[[openshell.supervisor.middleware]]
name = "{MIDDLEWARE}"
grpc_endpoint = "https://127.0.0.1:{settings.middleware_port}"
tls_ca_cert_path = {json.dumps(str(ca))}
max_payload_bytes = 4194304
timeout = "500ms"
"""
    )


def write_service_settings(settings: Settings, app: AppSettings, driver_dir: Path) -> Path:
    """Write the services' settings and targets; return the launch gateway config."""
    services = services_dir(settings)
    middleware = services / "middleware"
    for path in (services, middleware, middleware / "state", services / "package-cache"):
        if path.is_symlink():
            raise SetupError(f"State directory must not be a symlink: {path}")
        path.mkdir(mode=0o700, exist_ok=True)
    ensure_middleware_tls(settings)
    ceiling = middleware / "pool-ceiling.yaml"
    if not ceiling.exists():
        write_private(ceiling, "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
    jwt = settings.root / "gateway/tls/jwt/public.pem"
    write_private(
        middleware / "middleware.toml",
        f"""registration = "{MIDDLEWARE}"
listen = "127.0.0.1:{settings.middleware_port}"
state_dir = {json.dumps(str(middleware / "state"))}
ceiling = {json.dumps(str(ceiling))}

[tls]
certificate = {json.dumps(str(middleware / "tls/middleware.pem"))}
private_key = {json.dumps(str(middleware / "tls/middleware-key.pem"))}

[tokens]
gateway_id = "openshell"
public_key = {json.dumps(str(jwt))}
""",
    )
    write_private(
        services / "package-cache.toml",
        f"""listen = "127.0.0.1:{settings.package_cache_port}"
public_url = "http://host.openshell.internal:{settings.package_cache_port}"
cache_dir = {json.dumps(str(services / "package-cache"))}
max_size = "20Gi"

[pypi]
[conda]
[cargo]
[go]
[npm]
""",
    )
    write_private(
        services / "targets.yaml",
        f"""apiVersion: sandbox-science/v0alpha1
kind: OpenShellTargets
sandbox:
  middleware: {MIDDLEWARE}
  package_cache: {{host: host.openshell.internal, port: {settings.package_cache_port}}}
  mcp_proxy: {{host: host.openshell.internal, port: {app.mcp_proxy_port}}}
  binaries: [/**]
  filesystem: {{read_only: [/var/lib/texmf], read_write: [/opt/sandbox-science]}}
mcp_proxy:
  binaries: [/opt/sandbox-science/bin/python*]
  versions: ["2025-11-25"]
  filesystem: {{read_only: [/opt/sandbox-science]}}
""",
    )
    launch = settings.root / "gateway/gateway-launch.toml"
    write_private(launch, launch_gateway_config(settings, driver_dir))
    return launch


def start_service(args: list[str], log: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Start one service in its own process group, its output appended to its log."""
    with log.open("ab") as stream:
        return subprocess.Popen(  # noqa: S603 - the repository's own service commands.
            args,
            env=env,
            cwd=Path(__file__).resolve().parent.parent,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wait_for_port(process: subprocess.Popen[bytes], port: int, name: str, log: Path) -> None:
    """Wait until a service started here listens on its loopback port."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SetupError(f"The {name} exited with status {process.returncode}; see {log}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise SetupError(f"The {name} did not listen on 127.0.0.1:{port} within 60 s; see {log}")


def policy_providers(policy: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    """The providers the project policy binds, as (name, profile), read by its library."""
    code = (
        "import json, sys\n"
        "from sandbox_science.policy import load_policy\n"
        "loaded = load_policy(sys.argv[1])\n"
        "print(json.dumps([[p.name, p.profile] for p in loaded.policy.credentials.providers]))\n"
    )
    text = run(
        ["uv", "run", "python", "-I", "-c", code, str(policy)],
        env=env,
        capture=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    return [(str(name), str(profile)) for name, profile in json.loads(text)]


def ensure_providers(shell: str, policy: Path, env: dict[str, str]) -> None:
    """Register placeholder providers the policy needs; ask for any other."""
    wanted = policy_providers(policy, env)
    if not wanted:
        return
    listing = json.loads(run([shell, "provider", "list", "-o", "json"], env=env, capture=True))
    entries = listing.get("providers", listing) if isinstance(listing, dict) else listing
    registered = {str(entry.get("name")) for entry in entries if isinstance(entry, dict)}
    missing = [(name, profile) for name, profile in wanted if name not in registered]
    other = [name for name, profile in missing if profile not in PLACEHOLDER_PROFILES]
    if other:
        raise SetupError(
            "Register these providers with this gateway first (docs/MCP-CONNECTORS.md): "
            + ", ".join(other)
        )
    repository = Path(__file__).resolve().parent.parent
    for name, profile in missing:
        profiles = run([shell, "provider", "profile", "list", "-o", "json"], env=env, capture=True)
        if f'"{profile}"' not in profiles:
            source = str(repository / PLACEHOLDER_PROFILES[profile])
            run([shell, "provider", "profile", "import", "-f", source], env=env)
        placeholder = dict(env, GITHUB_TOKEN=f"placeholder-not-a-token-{secrets.token_hex(8)}")
        create = ["provider", "create", "--name", name, "--type", profile]
        run([shell, *create, "--credential", "GITHUB_TOKEN"], env=placeholder)
        print(f"Registered {name} with a placeholder credential; see docs/PERSONAL-BABBAGE.md")


def uv_env(settings: Settings, env: dict[str, str]) -> dict[str, str]:
    """Keep the application's Python, packages and uv cache on the selected volume."""
    env = dict(env)
    env["UV_CACHE_DIR"] = str(settings.root / "cache/uv")
    env["UV_PROJECT_ENVIRONMENT"] = str(settings.root / "cache/venv")
    env["UV_PYTHON_INSTALL_DIR"] = str(settings.root / "cache/python")
    # uv trusts its bundled roots by default; a managed Mac whose network inspects TLS adds
    # its CA to the system store instead. The owner's own uv TLS setting takes precedence.
    if not any(name in env for name in ("UV_SYSTEM_CERTS", "UV_NATIVE_TLS", "SSL_CERT_FILE")):
        env["UV_SYSTEM_CERTS"] = "1"
    return env


def read_app_config(settings: Settings) -> tuple[str, AppSettings]:
    """Read and check the private application config a launch runs."""
    if settings.application_config is None or not settings.application_config.is_file():
        raise SetupError("Set application_config to an existing private TOML file before launch")
    try:
        contents = settings.application_config.read_text()
        config = tomllib.loads(contents)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"Cannot read application config: {exc}") from exc
    openshell_config = config.get("openshell", {})
    if not isinstance(openshell_config, dict):
        raise SetupError("Application config [openshell] must be a table")
    configured_env = cast(dict[str, object], openshell_config).get("env", {})
    if not isinstance(configured_env, dict):
        raise SetupError("Application config [openshell.env] must be a table")
    if any(name in configured_env for name in ISOLATED_CLI_KEYS) or any(
        str(name).startswith("OPENSHELL_") for name in configured_env
    ):
        raise SetupError(
            "Remove OpenShell XDG/TLS path overrides from the application config; "
            "this helper supplies isolated paths"
        )
    return contents, check_app_config(settings, settings.application_config, config)


def run_with_gateway(settings: Settings, *, app: bool) -> None:
    """Run one foreground gateway and either a Sage probe or the application.

    A launch also runs the policy middleware and the package cache, which the gateway
    and the research sandboxes use, and stops them after the gateway.
    """
    require_mac(settings, space=not app)
    if not config_path(settings).is_file():
        raise SetupError("Gateway config missing; run install first")
    if config_path(settings).is_symlink():
        raise SetupError("Gateway config must not be a symlink")
    gateway = require_command("openshell-gateway")
    driver_dir = vm_driver_dir(gateway)
    write_gateway_config(settings, driver_dir)
    app_contents, app_settings = read_app_config(settings) if app else ("", None)
    if app:
        require_command("uv")
    require_command("openshell")
    prepare_dirs(settings)
    check_vm_socket_storage(settings)
    ensure_gateway_signing(settings, gateway)
    ports = [settings.gateway_port]
    if app:
        ports += [settings.middleware_port, settings.package_cache_port]
    for port in ports:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SetupError(f"Port {port} is already in use; choose another port")
    gateway_file = config_path(settings)
    services: list[subprocess.Popen[bytes]] = []
    try:
        if app_settings is not None:
            write_cross_compiler(settings)
            gateway_file = write_service_settings(settings, app_settings, driver_dir)
            start_services(settings, app_settings, services)
        log_path = settings.root / "gateway/gateway.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(  # noqa: S603 - pinned local gateway executable.
                [gateway, "--config", str(gateway_file)],
                env=gateway_env(settings),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            wait_for_gateway(process, settings)
            # A new CLI profile each run avoids changing the user's existing default.
            with tempfile.TemporaryDirectory(dir=settings.root / "cli-config") as cli_dir:
                env = cli_env(settings, Path(cli_dir))
                shell = require_command("openshell")
                run(
                    [
                        shell,
                        "gateway",
                        "add",
                        settings.gateway_url,
                        "--local",
                        "--name",
                        GATEWAY_NAME,
                    ],
                    env=env,
                    timeout=30,
                )
                deadline = time.monotonic() + 30
                while True:
                    try:
                        run([shell, "status"], env=env, capture=True, timeout=10)
                        break
                    except SetupError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise SetupError(
                                f"Gateway did not become ready; inspect {log_path}"
                            ) from None
                        time.sleep(0.5)
                if app_settings is not None and settings.application_config is not None:
                    env = uv_env(settings, env)
                    ensure_providers(shell, app_settings.policy, env)
                    print("Starting Sandbox Science. Ctrl-C stops this launch and its services.")
                    effective = effective_app_config(settings.application_config, app_contents, env)
                    try:
                        run(
                            ["uv", "run", "sandbox-science", "up", "--config", str(effective)],
                            env=env,
                            cwd=Path(__file__).resolve().parent.parent,
                        )
                    finally:
                        effective.unlink(missing_ok=True)
                else:
                    smoke(shell, settings, env)
        finally:
            try:
                stop_gateway(process)
            finally:
                print(f"Gateway log: {log_path}")
    finally:
        for service in reversed(services):
            stop_gateway(service, group=False)


def start_services(
    settings: Settings, app: AppSettings, started: list[subprocess.Popen[bytes]]
) -> None:
    """Store the project policy, then start the package cache and the policy middleware."""
    services = services_dir(settings)
    env = uv_env(settings, clean_openshell_env())
    middleware = services / "middleware"
    run(
        [
            "uv",
            "run",
            "sandbox-science-policy-middleware",
            "store",
            "--state-dir",
            str(middleware / "state"),
            "--policy",
            str(app.policy),
        ],
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
    )
    for name, command, settings_file, port in (
        (
            "package cache",
            ["sandbox-science-package-cache", "serve", "--log-level", "info"],
            services / "package-cache.toml",
            settings.package_cache_port,
        ),
        (
            "policy middleware",
            ["sandbox-science-policy-middleware", "serve", "--log-level", "INFO"],
            middleware / "middleware.toml",
            settings.middleware_port,
        ),
    ):
        log = services / f"{name.replace(' ', '-')}.log"
        process = start_service(
            ["uv", "run", *command[:2], "--settings", str(settings_file), *command[2:]], log, env
        )
        started.append(process)
        wait_for_port(process, port, name, log)
        print(f"The {name} listens on 127.0.0.1:{port}; its log is {log}")


def smoke(shell: str, settings: Settings, env: dict[str, str]) -> None:
    """Check the real ARM64 Sage image in a newly named MicroVM sandbox."""
    name = f"ss-mac-{secrets.token_hex(4)}"
    created = False
    passed = False
    try:
        with tempfile.TemporaryDirectory() as policy_dir:
            policy = Path(policy_dir) / "smoke-policy.yaml"
            policy.write_text(SMOKE_POLICY)
            # Record the intent before dispatch so an uncertain create is cleaned up.
            created = True
            run(
                [
                    shell,
                    "sandbox",
                    "create",
                    "--name",
                    name,
                    "--from",
                    settings.image,
                    "--policy",
                    str(policy),
                    "--detach",
                    "--",
                    "/usr/bin/sleep",
                    "infinity",
                ],
                env=env,
                timeout=3900,
            )
        arch = run(
            [
                shell,
                "sandbox",
                "exec",
                "-n",
                name,
                "--no-tty",
                "--no-login-shell",
                "--",
                "/usr/bin/uname",
                "-m",
            ],
            env=env,
            capture=True,
            timeout=180,
        )
        if arch != "aarch64":
            raise SetupError(f"Expected aarch64 inside the VM, got {arch!r}")
        answer = run(
            [
                shell,
                "sandbox",
                "exec",
                "-n",
                name,
                "--no-tty",
                "--no-login-shell",
                "--",
                "/opt/sandbox-science/bin/sage",
                "-c",
                "print(GF(17)(3)^16)",
            ],
            env=env,
            capture=True,
            timeout=180,
        )
        if answer != "1":
            raise SetupError(f"Sage returned {answer!r}, expected '1'")
        passed = True
    finally:
        if created:
            try:
                run([shell, "sandbox", "delete", name], env=env, timeout=120)
            except SetupError as exc:
                if passed:
                    raise SetupError(f"Could not delete test sandbox {name}: {exc}") from exc
                print(f"Could not delete test sandbox {name}: {exc}", file=sys.stderr)
    print(f"PASS: {settings.image} ran Sage on {arch} in the OpenShell VM driver")
    print(f"Cached image and overlays: {settings.root / 'vm'}")


def main() -> int:
    """Dispatch a single config-driven Mac setup action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / "sandbox-science-mac.toml",
        help="private mac-local.toml path (default: ~/sandbox-science-mac.toml)",
    )
    parser.add_argument("action", choices=("install", "smoke", "launch"))
    args = parser.parse_args()
    try:
        settings = read_settings(args.config)
        if args.action == "install":
            install(settings)
        else:
            run_with_gateway(settings, app=args.action == "launch")
    except KeyboardInterrupt:
        print("Stopped by user.", file=sys.stderr)
        return 130
    except (SetupError, OSError) as exc:
        print(f"Mac setup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
