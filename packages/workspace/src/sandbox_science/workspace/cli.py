# SPDX-License-Identifier: MIT
"""Command line: serve the workspace.

```
sandbox-science-workspace serve --state-dir DIR --static DIR [--listen 127.0.0.1:8200] \\
    [--allow-remote]
```

``--state-dir`` is the harness's state directory, whose records the workspace reads and
never changes. ``--static`` is the web app's build (``web/workspace/dist``). The server
listens on a loopback address unless ``--allow-remote`` allows another one; it speaks
plain HTTP, so on another address the login URL and the session cookie cross the
network unencrypted. At each start it makes a new login token and prints the login URL
once, on standard output.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import secrets
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from aiohttp import web

from sandbox_science.orchestrator.projects import ProjectStore

from .app import create_app

_log = logging.getLogger("sandbox_science.workspace")
DEFAULT_LISTEN: Final = "127.0.0.1:8200"


def parse_listen(text: str) -> tuple[str, int]:
    """``host:port``, with an IPv6 address in brackets; port 0 picks a free port."""
    host, colon, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not colon or not host or not port.isdigit() or int(port) > 65535:
        raise ValueError(f"--listen {text!r} is not host:port")
    return host, int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def base_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


async def serve(app: web.Application, host: str, port: int, login_token: str) -> None:
    """Serve until SIGTERM or SIGINT, after printing the login URL once."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        bound = runner.addresses[0][1] if port == 0 else port
        url = base_url(host, bound)
        print(f"Sandbox Science workspace on {url}/", flush=True)
        print(
            "Log in by opening this URL; it works until the server stops:\n"
            f"    {url}/login?token={login_token}",
            flush=True,
        )
        await stopping.wait()
        _log.info("stopping")
    finally:
        await runner.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-workspace``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("serve", help="serve the workspace GUI and its API")
    command.add_argument(
        "--state-dir", type=Path, required=True, help="the harness's state directory"
    )
    command.add_argument(
        "--static", type=Path, required=True, help="the web app's build (web/workspace/dist)"
    )
    command.add_argument(
        "--listen", default=DEFAULT_LISTEN, help=f"host:port (default {DEFAULT_LISTEN})"
    )
    command.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a listen address that is not loopback (plain HTTP: see the docs)",
    )
    command.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    args = parser.parse_args(argv)
    try:
        host, port = parse_listen(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    loopback = is_loopback(host)
    if not loopback and not args.allow_remote:
        parser.error(
            f"{host} is not a loopback address; --allow-remote accepts connections from "
            "other machines, over plain HTTP"
        )
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not args.state_dir.is_dir():
        print(f"error: there is no state directory {args.state_dir}", file=sys.stderr)
        return 1
    login_token = secrets.token_urlsafe(32)
    try:
        app = create_app(
            ProjectStore(args.state_dir),
            args.static,
            login_token=login_token,
            check_host=loopback,
        )
    except OSError as exc:
        print(f"error: {exc} (build the web app: npm run build)", file=sys.stderr)
        return 1
    if not loopback:
        _log.warning(
            "listening on %s, which is not loopback: the login URL and session cookie travel "
            "unencrypted, and any Host name is accepted",
            host,
        )
    try:
        asyncio.run(serve(app, host, port, login_token))
    except KeyboardInterrupt:  # before the signal handlers are in place
        return 0
    except OSError as exc:
        print(f"error: cannot listen on {args.listen}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
