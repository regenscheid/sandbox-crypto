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

OPEN_SHELL_VERSION = "0.1.3"
INSTALLER = "https://raw.githubusercontent.com/NVIDIA/OpenShell/v0.1.3/install.sh"
INSTALLER_SHA256 = "b8daa915e766564f0473b76ed4e012fff9723c1ac98007b3f85ba20f59442d28"
DEFAULT_IMAGE = "ghcr.io/regenscheid/sandbox-crypto/crypto-base:6bb73871"
GATEWAY_NAME = "ss-mac-crypto-test"
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

    @property
    def gateway_url(self) -> str:
        """The dedicated loopback endpoint for this test."""
        return f"http://127.0.0.1:{self.gateway_port}"


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
    if type(port) is not int or not 1024 <= port <= 65535:
        raise SetupError("gateway_port must be an integer from 1024 to 65535")
    image = raw.get("image", DEFAULT_IMAGE)
    user = raw.get("github_user", "regenscheid")
    service = raw.get("keychain_service", "sandbox-science-ghcr")
    for name, value in (("image", image), ("github_user", user), ("keychain_service", service)):
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise SetupError(f"{name} must be a nonempty string without spaces")
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
    """Match exactly v0.1.3, not a later 0.1.30 installation."""
    return re.search(r"(?<![\w.])v?0\.1\.3(?![\w.+-])", output) is not None


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


def gateway_config(settings: Settings, driver_dir: Path | None = None) -> str:
    """Render the VM config; None reproduces only the former generated config."""
    driver_location = (
        f"driver_dir = {json.dumps(str(driver_dir))}\n" if driver_dir is not None else ""
    )
    return f'''[openshell]
version = 2

[openshell.gateway]
compute_driver = "vm"
disable_tls = true
bind_address = "127.0.0.1:{settings.gateway_port}"
image_preparation_timeout_seconds = 3600

[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.drivers.vm]
{driver_location}state_dir = {json.dumps(str(settings.root / "vm"))}
grpc_endpoint = "{settings.gateway_url}"
default_image = "nvcr.io/nvidia/base/ubuntu:24.04"
vcpus = 4
mem_mib = 8192
overlay_disk_mib = 16384
'''


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
            "reinstall OpenShell v0.1.3 with Homebrew before retrying"
        )
    return driver.parent


def write_gateway_config(settings: Settings, driver_dir: Path) -> None:
    """Write the pinned config, migrating only our exact previous generated text."""
    target = config_path(settings)
    content = gateway_config(settings, driver_dir)
    if target.parent.is_symlink():
        raise SetupError(f"Gateway directory must not be a symlink: {target.parent}")
    if target.is_symlink():
        raise SetupError(f"Gateway config must not be a symlink: {target}")
    if target.exists():
        previous = target.read_text()
        if previous == content:
            return
        if previous != gateway_config(settings):
            raise SetupError(
                f"Gateway config differs from this setup: {target}; inspect it before changing it"
            )
        # Preserve an existing exact helper-generated config while adding the
        # driver's explicit Homebrew location. A crash cannot leave half a TOML.
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
    if subprocess.run(  # noqa: S603 - brew path came from command lookup.
        [brew, "list", "--versions", "e2fsprogs"], capture_output=True
    ).returncode:
        run([brew, "install", "e2fsprogs"])
    if shutil.which("uv") is None:
        run([brew, "install", "uv"])
    installed = shutil.which("openshell")
    version = run([installed, "--version"], capture=True) if installed else ""
    if installed and not pinned_version(version):
        raise SetupError(
            f"Installed OpenShell is {version}; use the pinned {OPEN_SHELL_VERSION} release"
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


def gateway_env(settings: Settings) -> dict[str, str]:
    """Supply private registry auth and selected-volume state to one gateway."""
    token = keychain_token(settings)
    if not token:
        raise SetupError("No GHCR token in Keychain; run install first")
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
            "OPENSHELL_REGISTRY_USERNAME": settings.github_user,
            "OPENSHELL_REGISTRY_TOKEN": token,
        }
    )
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


def stop_gateway(process: subprocess.Popen[bytes]) -> None:
    """Stop only the process group created for this invocation."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
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


def run_with_gateway(settings: Settings, *, app: bool) -> None:
    """Run one foreground gateway and either a Sage probe or the application."""
    require_mac(settings, space=not app)
    app_contents = ""
    if not config_path(settings).is_file():
        raise SetupError("Gateway config missing; run install first")
    if config_path(settings).is_symlink():
        raise SetupError("Gateway config must not be a symlink")
    gateway = require_command("openshell-gateway")
    driver_dir = vm_driver_dir(gateway)
    write_gateway_config(settings, driver_dir)
    if app:
        if settings.application_config is None or not settings.application_config.is_file():
            raise SetupError(
                "Set application_config to an existing private TOML file before launch"
            )
        try:
            app_contents = settings.application_config.read_text()
            config = tomllib.loads(app_contents)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SetupError(f"Cannot read application config: {exc}") from exc
        app_state = config.get("state_dir")
        if not isinstance(app_state, str):
            raise SetupError("Application config must set an absolute state_dir")
        if Path(app_state).expanduser().resolve() != (settings.root / "app").resolve():
            raise SetupError(f"Application state_dir must be {settings.root / 'app'}")
        openshell_config = config.get("openshell", {})
        if not isinstance(openshell_config, dict):
            raise SetupError("Application config [openshell] must be a table")
        configured_env = openshell_config.get("env", {})
        if not isinstance(configured_env, dict):
            raise SetupError("Application config [openshell.env] must be a table")
        if any(name in configured_env for name in ISOLATED_CLI_KEYS) or any(
            name.startswith("OPENSHELL_") for name in configured_env
        ):
            raise SetupError(
                "Remove OpenShell XDG/TLS path overrides from the application config; "
                "this helper supplies isolated paths"
            )
        require_command("uv")
    require_command("openshell")
    prepare_dirs(settings)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", settings.gateway_port)) == 0:
            raise SetupError(f"Port {settings.gateway_port} is already in use; choose another port")
    log_path = settings.root / "gateway/gateway.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - pinned local gateway executable.
            [gateway, "--config", str(config_path(settings))],
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
                if app:
                    env["UV_CACHE_DIR"] = str(settings.root / "cache/uv")
                    env["UV_PROJECT_ENVIRONMENT"] = str(settings.root / "cache/venv")
                    env["UV_PYTHON_INSTALL_DIR"] = str(settings.root / "cache/python")
                    print(
                        "Starting Sandbox Science. Ctrl-C stops this launch and its test gateway."
                    )
                    if settings.application_config is None:
                        raise SetupError("Application config missing")
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


def smoke(shell: str, settings: Settings, env: dict[str, str]) -> None:
    """Check the real ARM64 Sage image in a newly named MicroVM sandbox."""
    name = f"ss-mac-crypto-{secrets.token_hex(4)}"
    created = False
    passed = False
    try:
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
