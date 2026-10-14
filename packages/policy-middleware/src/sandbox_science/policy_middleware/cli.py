# SPDX-License-Identifier: MIT
"""Command line: run the service, and manage the policy store until the orchestrator does.

```
sandbox-science-policy-middleware serve --settings middleware.toml
sandbox-science-policy-middleware store --state-dir DIR --policy policy.yaml
sandbox-science-policy-middleware bind --state-dir DIR --sandbox ID --policy policy.yaml
sandbox-science-policy-middleware unbind --state-dir DIR --sandbox ID
sandbox-science-policy-middleware pending --settings middleware.toml [--request SEQ]
sandbox-science-policy-middleware approve --settings middleware.toml --request SEQ \
    [--scope host] [--for HOURS]
sandbox-science-policy-middleware deny --settings middleware.toml --request SEQ
```

The gateway asks the middleware to validate a compiled sandbox policy before it
creates the sandbox, so store the project policy first; bind the sandbox once
OpenShell has assigned its id.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import FrameType

import grpc

from sandbox_science.event_store import EventStore, EventStoreError, Record, StoreReader
from sandbox_science.policy import HardFloor, PolicyLoadError, load_ceiling
from sandbox_science.policy.approvals import ALL_SCOPES
from sandbox_science.policy.approvals import commands as approval_commands
from sandbox_science.policy.loader import read_limited

from .archives import CacheArchiveLookup
from .auth import (
    Authenticator,
    TokenAuthenticator,
    UnverifiedCallers,
    default_audience,
    load_public_key,
)
from .errors import MiddlewareError, SettingsError, StoreError
from .evaluator import PolicyMiddleware
from .events import OPERATOR, STORE_TIMEOUT, StoreSink
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
    store = commands.add_parser(
        "store", help="store a policy, before creating sandboxes compiled from it"
    )
    store.add_argument("--state-dir", type=Path, required=True)
    store.add_argument("--policy", type=Path, required=True)
    bind = commands.add_parser("bind", help="store a policy and bind a sandbox to it")
    bind.add_argument("--state-dir", type=Path, required=True)
    bind.add_argument("--sandbox", required=True)
    bind.add_argument("--policy", type=Path, required=True)
    unbind = commands.add_parser("unbind", help="remove a sandbox's binding")
    unbind.add_argument("--state-dir", type=Path, required=True)
    unbind.add_argument("--sandbox", required=True)
    pending = commands.add_parser("pending", help="list permission requests nobody answered")
    pending.add_argument("--settings", type=Path, required=True)
    pending.add_argument(
        "--request",
        type=int,
        help="show this request in full, answered or not, with every package file it lists",
    )
    for name, text in (("approve", "approve a permission request"), ("deny", "deny one")):
        answer = commands.add_parser(name, help=text)
        answer.add_argument("--settings", type=Path, required=True)
        answer.add_argument("--request", type=int, required=True, help="its sequence number")
        if name == "approve":
            answer.add_argument(
                "--scope",
                choices=ALL_SCOPES,
                help="request (the default), or host: any path on its host with its method; "
                "a provisioning operation's request takes files, its exact file set",
            )
            answer.add_argument("--for", dest="hours", type=float, default=8.0, help="hours")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "serve":
                logging.basicConfig(
                    level=args.log_level.upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
                return _serve(load_settings(args.settings))
            case "store":
                loaded = PolicyStore(args.state_dir).add_policy(read_limited(args.policy))
                print(f"stored {loaded.policy.metadata.name} {loaded.digest}")
                return 0
            case "bind":
                store = PolicyStore(args.state_dir)
                loaded = store.add_policy(read_limited(args.policy))
                store.bind(args.sandbox, loaded.digest)
                print(f"bound {args.sandbox} to {loaded.policy.metadata.name} {loaded.digest}")
                return 0
            case "unbind":
                removed = PolicyStore(args.state_dir).unbind(args.sandbox)
                print(f"unbound {args.sandbox}" if removed else f"{args.sandbox} was not bound")
                return 0
            case "pending":
                path = load_settings(args.settings).event_store_path
                return approval_commands.pending(_read_records(path, 0), args.request)
            case _:
                path = load_settings(args.settings).event_store_path
                hours = getattr(args, "hours", None)
                return _answer(
                    path, args.command, args.request, getattr(args, "scope", None), hours
                )
    except (MiddlewareError, StoreError, PolicyLoadError, EventStoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _read_records(path: Path, after: int) -> list[Record]:
    """Records after ``after``, read with a connection of this thread's own."""
    with StoreReader(path) as reader:
        return list(reader.records(after=after))


def _answer(path: Path, command: str, seq: int, scope: str | None, hours: float | None) -> int:
    """Append an approval or a denial of permission request ``seq``."""

    def append(kind: str, payload: dict[str, object]) -> int:
        with EventStore(path) as events:
            return events.append(kind, OPERATOR, payload).seq

    return approval_commands.answer(
        _read_records(path, 0),
        append,
        seq,
        approve=command == "approve",
        scope=scope,
        hours=hours,
        now=datetime.now(UTC),
    )


def _serve(settings: Settings) -> int:
    ceiling = load_ceiling(settings.ceiling)
    with EventStore(settings.event_store_path, timeout=STORE_TIMEOUT) as events:
        _log.info("recording escalations, alerts and monitor findings in %s", events.path)
        middleware = PolicyMiddleware(
            PolicyStore(settings.state_dir),
            ceiling=ceiling,
            floor=HardFloor(frozenset(settings.admin_hosts)),
            events=StoreSink(events),
            requests=partial(_read_records, settings.event_store_path),
            archives=(
                CacheArchiveLookup(settings.package_cache_url)
                if settings.package_cache_url is not None
                else None
            ),
        )
        return _run(settings, middleware)


def _run(settings: Settings, middleware: PolicyMiddleware) -> int:
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
