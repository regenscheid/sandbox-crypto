# SPDX-License-Identifier: MIT
"""Command line: serve the workspace.

```
sandbox-science-workspace serve --state-dir DIR --static DIR [--listen 127.0.0.1:8200] \\
    [--allow-remote] \\
    [--model-url http://127.0.0.1:4000 --reviewer-model reviewer --api-key-file key]
```

``--state-dir`` is the harness's state directory, whose project and run records the
workspace reads. With a worker connection it also serves live job status and cancellation.
``--static`` is the web app's build (``web/workspace/dist``). The server
listens on a loopback address unless ``--allow-remote`` allows another one; it speaks
plain HTTP, so on another address the login URL and the session cookie cross the
network unencrypted. At each start it makes a new login token and prints the login URL
once, on standard output.

With ``--model-url``, ``--reviewer-model`` and ``--api-key-file``, the page may ask the
reviewer model, through the model gateway, to repair math that KaTeX rejects
(mathrepair.py); the server then contacts that gateway as well as a configured job
worker. Without them,
or with ``--reviewer-model off``, it never does.
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

from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient

from .app import create_app
from .documents import WRITER
from .mathrepair import MathRepairService, gateway_service, reviewer_alias

_log = logging.getLogger("sandbox_science.workspace")
DEFAULT_LISTEN: Final = "127.0.0.1:8200"


def math_repairs(
    model_url: str | None, reviewer: str | None, api_key_file: Path | None
) -> MathRepairService:
    """The repair service the settings describe; ValueError for incomplete settings."""
    if reviewer_alias(reviewer) is None:
        if reviewer is None and (model_url is not None or api_key_file is not None):
            raise ValueError("--model-url and --api-key-file need --reviewer-model")
        return MathRepairService(reviewer=reviewer)
    if model_url is None or api_key_file is None:
        raise ValueError("--reviewer-model needs --model-url and --api-key-file")
    if not model_url.startswith(("http://", "https://")):
        raise ValueError(f"--model-url {model_url!r} is not an http or https URL")
    try:
        api_key = api_key_file.read_text().strip()
        mode = api_key_file.stat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot read the API key file {api_key_file}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise ValueError(f"the API key file {api_key_file} is not text") from None
    if not api_key:
        raise ValueError(f"the API key file {api_key_file} is empty")
    if mode & 0o077:
        _log.warning(
            "the API key file %s can be read by other users (mode %o); chmod 600 it",
            api_key_file,
            mode & 0o777,
        )
    try:
        return gateway_service(model_url, reviewer or "", api_key)
    except ValueError as exc:
        raise ValueError(f"the API key file {api_key_file}: {exc}") from None


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
    parser = argparse.ArgumentParser(prog="sandbox-science-workspace", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "serve", help="serve the workspace GUI and its API", allow_abbrev=False
    )
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
    command.add_argument("--worker", help="worker URL for live jobs and cancellation")
    command.add_argument("--worker-token-file", type=Path, help="worker key (server only)")
    repairs = command.add_argument_group(
        "math repairs", "repairs of LaTeX that KaTeX rejects, by the reviewer model"
    )
    repairs.add_argument(
        "--model-url",
        help="the model gateway's OpenAI-compatible base URL (the LiteLLM proxy), as the "
        "gateway answers it: redirects are not followed",
    )
    repairs.add_argument(
        "--reviewer-model",
        help="the alias of the reviewer model, the only model asked for repairs (off: none)",
    )
    repairs.add_argument(
        "--api-key-file", type=Path, help="a file holding the workspace's key for the gateway"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        host, port = parse_listen(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        repair = math_repairs(args.model_url, args.reviewer_model, args.api_key_file)
    except ValueError as exc:
        parser.error(str(exc))
    loopback = is_loopback(host)
    if not loopback and not args.allow_remote:
        parser.error(
            f"{host} is not a loopback address; --allow-remote accepts connections from "
            "other machines, over plain HTTP"
        )
    if not args.state_dir.is_dir():
        print(f"error: there is no state directory {args.state_dir}", file=sys.stderr)
        return 1
    job_worker = None
    job_state = None
    if args.worker is not None or args.worker_token_file is not None:
        if args.worker is None or args.worker_token_file is None:
            parser.error("--worker and --worker-token-file must be given together")
        if not args.worker.startswith(("http://", "https://")):
            parser.error("--worker must be an http or https URL")
        try:
            token = args.worker_token_file.read_text().strip()
            if not token:
                raise ValueError("worker token file is empty")
            job_worker = WorkerClient(args.worker, token)
            job_state = JobState(args.state_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(f"cannot configure job worker: {exc}")
        except DocumentError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
    login_token = secrets.token_urlsafe(32)
    try:
        app = create_app(
            ProjectStore(args.state_dir),
            args.static,
            login_token=login_token,
            check_host=loopback,
            math_repair=repair,
            job_worker=job_worker,
            job_state=job_state,
        )
    except OSError as exc:
        print(f"error: {exc} (build the web app: npm run build)", file=sys.stderr)
        return 1
    try:
        # The writer brings the control store to the current schema now, so that the
        # read-only views can read it, or the server does not start, saying why.
        app[WRITER].initialize()
    except DocumentError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    if repair.enabled:
        _log.info(
            "math repairs ask the reviewer model %r through the model gateway", repair.reviewer
        )
    else:
        _log.info("math repairs by a model are off")
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
