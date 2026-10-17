# SPDX-License-Identifier: MIT
"""Explicit operator-to-launcher runtime replacement, through its private state directory."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import secrets
import signal
import time
from pathlib import Path

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract, Counter

from . import instance, orchestrator
from .config import LauncherConfig, LauncherError
from .state import Layout, check_private, write_private
from .supervise import Supervisor, start_time


class Request(Contract):
    nonce: str = Field(pattern=r"^replacement-[0-9a-f]{32}$")
    pid: Counter
    started: Counter
    boot: str = Field(min_length=1, max_length=128)


class Result(Contract):
    nonce: str = Field(pattern=r"^replacement-[0-9a-f]{32}$")
    error: str | None = Field(default=None, max_length=512)


def path(layout: Layout, name: str) -> Path:
    return layout.root / "orchestrator" / ("replacement-" + name)


def read_request(layout: Layout) -> Request:
    request = path(layout, "request")
    if request.stat().st_size > 4096:
        raise LauncherError("runtime replacement request exceeds its byte bound")
    return Request.model_validate_json(request.read_bytes())


def request(config: LauncherConfig) -> int:
    """Signal only the identified owning launcher and wait for this request's receipt."""
    layout = Layout(config.state_dir)
    check_private(layout.root)
    found = instance.read(layout)
    if (
        not instance.running(layout)
        or found is None
        or not instance.launcher_alive(found)
        or found.purpose != "up"
        or not found.replacement
    ):
        raise LauncherError("runtime replacement requires the running owning launcher")
    descriptor = os.open(path(layout, "lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LauncherError("another runtime replacement request is still waiting") from None
        selected = Request(
            nonce="replacement-" + secrets.token_hex(16),
            pid=found.pid,
            started=found.started,
            boot=found.boot,
        )
        write_private(path(layout, "request"), selected.model_dump_json() + "\n")
        os.kill(found.pid, signal.SIGUSR2)
        deadline = time.monotonic() + config.start_timeout + 2 * config.stop_timeout + 10
        while instance.launcher_alive(found):
            result = path(layout, "result")
            try:
                if result.stat().st_size > 4096:
                    raise LauncherError("runtime replacement receipt exceeds its byte bound")
                receipt = Result.model_validate_json(result.read_bytes())
            except FileNotFoundError:
                receipt = None
            if receipt is not None and receipt.nonce == selected.nonce:
                if receipt.error is not None:
                    raise LauncherError(receipt.error)
                print("orchestrator replaced; explicitly Resume unfinished work in the workspace")
                return 0
            if time.monotonic() >= deadline:
                raise LauncherError(
                    "runtime replacement outcome is unknown; inspect status and logs"
                )
            time.sleep(0.1)
        raise LauncherError("the owning launcher stopped before replacement was acknowledged")
    finally:
        os.close(descriptor)


async def watch(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, requested: asyncio.Event
) -> str | None:
    """Keep required services alive while explicit requests replace only the agent VM."""
    while True:
        watching = asyncio.create_task(supervisor.watch())
        replacing = asyncio.create_task(requested.wait())
        try:
            done, _ = await asyncio.wait((watching, replacing), return_when=asyncio.FIRST_COMPLETED)
            if watching in done:
                return await watching
        finally:
            for task in (watching, replacing):
                task.cancel()
            await asyncio.gather(watching, replacing, return_exceptions=True)
        requested.clear()
        selected: Request | None = None
        error: str | None = None
        try:
            selected = read_request(layout)
            if (selected.pid, selected.started, selected.boot) != (
                os.getpid(),
                start_time(os.getpid()),
                instance.boot_id(),
            ):
                raise LauncherError("runtime replacement belongs to another launcher generation")
            print("replacing the orchestrator; control and workspace remain available", flush=True)
            await orchestrator.replace(config, layout, supervisor)
        except (LauncherError, OSError, ValueError, ValidationError) as exc:
            error = str(exc)[:512]
            print("runtime replacement failed: " + error, flush=True)
        if selected is not None:
            write_private(
                path(layout, "result"),
                Result(nonce=selected.nonce, error=error).model_dump_json() + "\n",
            )
            with contextlib.suppress(FileNotFoundError):
                if read_request(layout).nonce == selected.nonce:
                    path(layout, "request").unlink()
