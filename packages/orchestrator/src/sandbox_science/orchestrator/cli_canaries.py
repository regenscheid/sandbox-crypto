# SPDX-License-Identifier: MIT
"""``sandbox-science-harness mcp-canaries``: the scheduled MCP canaries (mcp_canaries.py),
which the launcher starts as a part when its config's ``[canaries]`` is enabled.

It lists each ``--server`` through the proxy at ``--mcp-proxy`` with the canaries' own
caller token in ``--mcp-token-file`` (the launcher's ``mcp-canary``), records the results
in the state directory's control store and logs each one, every ``--interval`` seconds
until stopped (or once, with ``--once``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import signal
import sys
from pathlib import Path
from typing import Any

from sandbox_science.contracts.logs import configure_logging

from .cli_documents import read_secret
from .control import ControlError, ControlStore, store_path
from .mcp_canaries import INTERVAL, CanaryRunner, CanaryStore, ProxySource

READY = "MCP canaries running"
"""The line the launcher waits for before it counts the part as started."""
MIN_INTERVAL = 60.0


def add_parser(commands: Any) -> None:
    command = commands.add_parser(
        "mcp-canaries",
        help="list each MCP server's tools through the proxy on a schedule, never calling "
        "a tool, and record the results in the control store",
    )
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--mcp-proxy", required=True, help="the MCP proxy's address")
    command.add_argument(
        "--mcp-token-file", type=Path, required=True, help="a caller token for the MCP proxy"
    )
    command.add_argument(
        "--server", action="append", required=True, help="an MCP server to list; repeat for more"
    )
    command.add_argument(
        "--interval",
        type=float,
        default=INTERVAL,
        help=f"seconds between runs (default {INTERVAL:g}, at least {MIN_INTERVAL:g})",
    )
    command.add_argument("--once", action="store_true", help="run once, then exit")


def mcp_canaries(args: argparse.Namespace) -> int:
    if not math.isfinite(args.interval) or args.interval < MIN_INTERVAL:
        print(f"error: --interval is at least {MIN_INTERVAL:g} seconds", file=sys.stderr)
        return 2
    try:
        if args.mcp_token_file.stat().st_mode & 0o077:
            print(f"error: {args.mcp_token_file} is open to other users", file=sys.stderr)
            return 1
        source = ProxySource(args.mcp_proxy, read_secret(args.mcp_token_file))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    configure_logging("mcp-canary")
    control = ControlStore(store_path(args.state_dir))
    try:
        control.initialize()
        runner = CanaryRunner(CanaryStore(control), source, args.server, interval=args.interval)
        return asyncio.run(_run(runner, source, once=args.once))
    except (ControlError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        control.close()


async def _run(runner: CanaryRunner, source: ProxySource, *, once: bool) -> int:
    try:
        if once:
            await runner.run_once()
            return 0
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for number in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(number, stop.set)
        print(f"{READY}: {len(runner.servers)} servers every {runner.interval:g} s", flush=True)
        await runner.run(stop)
        return 0
    finally:
        await source.close()
