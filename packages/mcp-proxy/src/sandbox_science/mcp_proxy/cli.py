# SPDX-License-Identifier: MIT
"""Command line: run the proxy, bind projects to policy revisions, and make caller tokens.

```
sandbox-science-mcp-proxy serve --settings proxy.toml [--log-level INFO]
sandbox-science-mcp-proxy bind --state-dir DIR --project ID --policy policy.yaml
sandbox-science-mcp-proxy unbind --state-dir DIR --project ID
sandbox-science-mcp-proxy new-token
```

``bind`` stores the policy file under its revision and binds the project to
it; the proxy decides the project's next call under that revision.
``new-token`` prints a random token, for the caller, and its ``token_sha256``,
for the settings.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

import aiohttp
from aiohttp import web

from sandbox_science.event_store import EventStore, EventStoreError
from sandbox_science.policy import HardFloor, PolicyLoadError, load_ceiling
from sandbox_science.policy.loader import read_limited

from .callers import Callers, token_digest
from .errors import McpProxyError
from .proxy import McpProxy
from .records import STORE_TIMEOUT, StoreSink
from .sessions import Sessions
from .settings import Settings, load_settings
from .store import PolicyStore
from .upstream import GuardedResolver, Upstream, address_check, client_session

_log = logging.getLogger("sandbox_science.mcp_proxy")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-mcp-proxy``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-mcp-proxy")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the MCP proxy")
    serve.add_argument("--settings", type=Path, required=True)
    serve.add_argument("--log-level", default="INFO", type=str.upper, choices=_LOG_LEVELS)
    bind = commands.add_parser("bind", help="store a policy and bind a project to it")
    bind.add_argument("--state-dir", type=Path, required=True)
    bind.add_argument("--project", required=True)
    bind.add_argument("--policy", type=Path, required=True)
    unbind = commands.add_parser("unbind", help="remove a project's binding")
    unbind.add_argument("--state-dir", type=Path, required=True)
    unbind.add_argument("--project", required=True)
    commands.add_parser("new-token", help="print a new caller token and its hash")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "serve":
                logging.basicConfig(
                    level=args.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
                return _serve(load_settings(args.settings))
            case "bind":
                store = PolicyStore(args.state_dir)
                loaded = store.add_policy(read_limited(args.policy))
                store.bind(args.project, loaded.digest)
                print(f"bound {args.project} to {loaded.policy.metadata.name} {loaded.digest}")
                return 0
            case "unbind":
                removed = PolicyStore(args.state_dir).unbind(args.project)
                print(f"unbound {args.project}" if removed else f"{args.project} was not bound")
                return 0
            case _:
                token = secrets.token_urlsafe(32)
                print(f"token = {token}")
                print(f"token_sha256 = {token_digest(token)}")
                return 0
    except (McpProxyError, PolicyLoadError, EventStoreError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _serve(settings: Settings) -> int:
    ceiling = load_ceiling(settings.ceiling)
    floor = HardFloor(frozenset(settings.admin_hosts))
    store = PolicyStore(settings.state_dir)
    events = EventStore(settings.event_store_path, timeout=STORE_TIMEOUT)

    async def application() -> web.Application:
        # The HTTP client belongs to the running event loop.
        http = client_session(
            GuardedResolver(aiohttp.ThreadedResolver(), address_check(floor, ceiling)),
            timeout=settings.upstream_timeout.total_seconds(),
        )
        upstream = Upstream(http)
        proxy = McpProxy(
            store=store,
            callers=Callers.from_settings(settings.callers),
            ceiling=ceiling,
            floor=floor,
            events=StoreSink(events),
            upstream=upstream,
            sessions=Sessions(
                limit=settings.max_sessions, idle=settings.session_idle.total_seconds()
            ),
            credentials=settings.credentials,
            environ=os.environ,
        )
        app = proxy.application(max_body_bytes=settings.max_body_bytes)

        async def close(_: web.Application) -> None:
            await upstream.close()

        app.on_cleanup.append(close)
        return app

    _log.info(
        "serving on %s; recording in %s; policies from %s",
        settings.listen,
        events.path,
        settings.state_dir,
    )
    try:
        web.run_app(
            application(),
            host=settings.listen_host,
            port=settings.listen_port,
            access_log=None,
            print=None,
            shutdown_timeout=10.0,
        )
    finally:
        events.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
