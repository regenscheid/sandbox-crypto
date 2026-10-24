# SPDX-License-Identifier: MIT
"""Command line: run the proxy, bind projects to policy revisions, and make caller tokens.

```
sandbox-science-mcp-proxy serve --settings proxy.toml [--log-level INFO]
sandbox-science-mcp-proxy bind --state-dir DIR --project ID --policy policy.yaml
sandbox-science-mcp-proxy unbind --state-dir DIR --project ID
sandbox-science-mcp-proxy pending --settings proxy.toml [--request SEQ]
sandbox-science-mcp-proxy approve --settings proxy.toml --request SEQ [--scope tool] [--for HOURS]
sandbox-science-mcp-proxy deny --settings proxy.toml --request SEQ
sandbox-science-mcp-proxy new-token
```

``bind`` stores the policy file under its revision and binds the project to
it; the proxy decides the project's next call under that revision.
``new-token`` prints a random token, for the caller, and its ``token_sha256``,
for the settings.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import aiohttp
from aiohttp import web

from sandbox_science.contracts.logs import configure_logging
from sandbox_science.event_store import EventStore, EventStoreError, Record, StoreReader
from sandbox_science.policy import HardFloor, PolicyLoadError, load_ceiling, load_policy
from sandbox_science.policy.approvals import ALL_SCOPES, KINDS
from sandbox_science.policy.approvals import commands as approval_commands
from sandbox_science.policy.loader import read_limited
from sandbox_science.policy.openshell import CompileError, compile_mcp_proxy, load_targets

from .callers import Callers, token_digest
from .errors import McpProxyError, StoreError
from .proxy import McpProxy
from .records import OPERATOR, STORE_TIMEOUT, StoreSink
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
    compile_policy = commands.add_parser(
        "compile-policy",
        help="compile a project policy into the OpenShell policy of the proxy's own sandbox",
    )
    compile_policy.add_argument("--policy", type=Path, required=True, help="the project policy")
    compile_policy.add_argument(
        "--ceiling", type=Path, required=True, help="the pool ceiling, as the proxy's settings"
    )
    compile_policy.add_argument(
        "--targets", type=Path, required=True, help="the deployment targets (mcp_proxy)"
    )
    compile_policy.add_argument("--out", type=Path, required=True)
    compile_policy.add_argument(
        "--admit-escalated",
        action="store_true",
        help="admit the tools the policy escalates by name at the fence, so that approvals "
        "take effect; the proxy still holds each call for approval",
    )
    pending = commands.add_parser("pending", help="list permission requests nobody answered")
    pending.add_argument("--settings", type=Path, required=True)
    pending.add_argument("--request", type=int, help="show this request in full, answered or not")
    for name, text in (("approve", "approve a permission request"), ("deny", "deny one")):
        answer = commands.add_parser(name, help=text)
        answer.add_argument("--settings", type=Path, required=True)
        answer.add_argument("--request", type=int, required=True, help="its sequence number")
        if name == "approve":
            answer.add_argument(
                "--scope",
                choices=ALL_SCOPES,
                help="call (the default); tool: the tool with any arguments",
            )
            answer.add_argument("--for", dest="hours", type=float, default=8.0, help="hours")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "serve":
                configure_logging("mcp-proxy", level=args.log_level)
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
            case "compile-policy":
                return _compile_policy(
                    args.policy, args.ceiling, args.targets, args.out, args.admit_escalated
                )
            case "pending":
                path = load_settings(args.settings).event_store_path
                return approval_commands.pending(_read_records(path, 0), args.request)
            case "approve" | "deny":
                return _answer(
                    load_settings(args.settings).event_store_path,
                    approve=args.command == "approve",
                    seq=args.request,
                    scope=getattr(args, "scope", None),
                    hours=getattr(args, "hours", None),
                )
            case _:
                token = secrets.token_urlsafe(32)
                print(f"token = {token}")
                print(f"token_sha256 = {token_digest(token)}")
                return 0
    except (
        McpProxyError,
        StoreError,
        PolicyLoadError,
        CompileError,
        EventStoreError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _compile_policy(
    policy: Path, ceiling: Path, targets: Path, out: Path, admit_escalated: bool
) -> int:
    """Write the proxy sandbox's OpenShell policy, and print what the compiler found."""
    compiled = compile_mcp_proxy(
        load_policy(policy),
        load_targets(targets).mcp_proxy,
        ceiling=load_ceiling(ceiling),
        admit_escalated=admit_escalated,
    )
    for finding in compiled.findings:
        print(f"{finding.severity.value}: {finding.subject}: {finding.message}")
    out.write_text(compiled.policy.to_yaml())
    print(f"wrote {out}")
    return 0


def _read_records(path: Path, after: int) -> list[Record]:
    """Permission requests and answers after ``after``, read with a connection of this
    thread's own; the store's other records are left in it."""
    with StoreReader(path) as reader:
        return list(reader.records(after=after, kinds=KINDS))


def _answer(path: Path, *, approve: bool, seq: int, scope: str | None, hours: float | None) -> int:
    """Append an approval or a denial of permission request ``seq``."""

    def append(kind: str, payload: dict[str, object]) -> int:
        with EventStore(path) as events:
            return events.append(kind, OPERATOR, payload).seq

    return approval_commands.answer(
        _read_records(path, 0),
        append,
        seq,
        approve=approve,
        scope=scope,
        hours=hours,
        now=datetime.now(UTC),
    )


async def _append_answer(events: EventStore, kind: str, payload: dict[str, object]) -> int:
    """Append an operator caller's approval or denial, as the command line would."""
    return (await events.append_async(kind, OPERATOR, payload)).seq


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
                limit=settings.max_sessions,
                per_caller=settings.max_sessions_per_caller,
                idle=settings.session_idle.total_seconds(),
            ),
            credentials=settings.credentials,
            environ=os.environ,
            requests=partial(_read_records, settings.event_store_path),
            answers=lambda kind, payload: _append_answer(events, kind, payload),
        )
        await asyncio.to_thread(proxy.prime_approvals)
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
