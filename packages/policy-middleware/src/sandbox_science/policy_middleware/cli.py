# SPDX-License-Identifier: MIT
"""Command line: run the service, and manage the policy store until the orchestrator does.

```
sandbox-science-policy-middleware serve --settings middleware.toml
sandbox-science-policy-middleware bind --state-dir DIR --sandbox ID --policy policy.yaml
sandbox-science-policy-middleware unbind --state-dir DIR --sandbox ID
```
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import FrameType

import grpc

from sandbox_science.policy import HardFloor, PolicyLoadError, load_ceiling
from sandbox_science.policy.loader import read_limited

from .auth import (
    Authenticator,
    TokenAuthenticator,
    UnverifiedCallers,
    default_audience,
    load_public_key,
)
from .errors import MiddlewareError, SettingsError
from .evaluator import PolicyMiddleware
from .events import JsonLinesEventLog
from .service import MiddlewareService, build_server
from .settings import Settings, load_settings
from .store import PolicyStore

_log = logging.getLogger("sandbox_science.policy_middleware")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-policy-middleware``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-policy-middleware")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the middleware service")
    serve.add_argument("--settings", type=Path, required=True)
    serve.add_argument("--log-level", default="INFO")
    bind = commands.add_parser("bind", help="store a policy and bind a sandbox to it")
    bind.add_argument("--state-dir", type=Path, required=True)
    bind.add_argument("--sandbox", required=True)
    bind.add_argument("--policy", type=Path, required=True)
    unbind = commands.add_parser("unbind", help="remove a sandbox's binding")
    unbind.add_argument("--state-dir", type=Path, required=True)
    unbind.add_argument("--sandbox", required=True)
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "serve":
                logging.basicConfig(
                    level=args.log_level.upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
                return _serve(load_settings(args.settings))
            case "bind":
                store = PolicyStore(args.state_dir)
                loaded = store.add_policy(read_limited(args.policy))
                store.bind(args.sandbox, loaded.digest)
                print(f"bound {args.sandbox} to {loaded.policy.metadata.name} {loaded.digest}")
                return 0
            case _:
                removed = PolicyStore(args.state_dir).unbind(args.sandbox)
                print(f"unbound {args.sandbox}" if removed else f"{args.sandbox} was not bound")
                return 0
    except (MiddlewareError, PolicyLoadError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _serve(settings: Settings) -> int:
    ceiling = load_ceiling(settings.ceiling)
    middleware = PolicyMiddleware(
        PolicyStore(settings.state_dir),
        ceiling=ceiling,
        floor=HardFloor(frozenset(settings.admin_hosts)),
        events=JsonLinesEventLog(settings.state_dir / "events.jsonl"),
    )
    authenticator: Authenticator
    audience = ""
    if settings.tokens is not None:
        audience = settings.tokens.audience or default_audience(settings.registration)
        authenticator = TokenAuthenticator(
            public_key=load_public_key(settings.tokens.public_key),
            gateway_id=settings.tokens.gateway_id,
            audience=audience,
            leeway=timedelta(seconds=settings.tokens.leeway_seconds),
        )
    else:
        _log.warning("accepting unauthenticated calls (allow_unauthenticated = true)")
        authenticator = UnverifiedCallers()
    credentials = None
    if settings.tls is not None:
        try:
            credentials = grpc.ssl_server_credentials(
                [(settings.tls.private_key.read_bytes(), settings.tls.certificate.read_bytes())]
            )
        except OSError as exc:
            raise SettingsError(f"cannot read the TLS certificate or key: {exc}") from exc
    service = MiddlewareService(
        middleware,
        authenticator,
        max_payload_bytes=settings.max_payload_bytes,
        expected_audience=audience,
    )
    server, port = build_server(
        service, listen=settings.listen, workers=settings.workers, credentials=credentials
    )
    stopped = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        del frame
        _log.info("received signal %d, stopping", signum)
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    _log.info(
        "serving %s on %s (port %d, %s)",
        settings.registration,
        settings.listen,
        port,
        "TLS" if credentials is not None else "plaintext",
    )
    stopped.wait()
    server.stop(grace=5).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
