# SPDX-License-Identifier: MIT
"""The direct MCP lab's agent side: DirectClient in the agent runtime sandbox (D98, A2).

``direct_mcp.py proofs`` uploads this file into the agent runtime sandbox and runs it with
the image's interpreter through ``openshell sandbox exec``. It speaks to the lab host over
that exec session's standard streams, one JSON object per line:

- ``{"t": "frame", "text": ...}`` both ways: the confinement ``Channel`` frames, over which
  the client's ``ControlRequestCheck`` asks the host's ``confirm_request``;
- ``{"t": "held", "request": N}`` out: a confirmed hold, which the host answers as the
  workspace would; the client calls again every 3 s, as ``Connectors`` does;
- ``{"t": "step", ...}`` out: one sanitized result per step (counts, kinds, names);
- ``{"t": "plan_done"}`` out, after proofs 1 to 4: the host then relays proof 5's
  ``mcp_request`` over the Channel, which the production ``RuntimeConnectorCalls`` answers
  (its binary items over ``publish_mcp_binary``), and sends ``{"t": "finish"}`` in;
- ``{"t": "done"}`` out, last.

It never prints a credential, a placeholder's value, a URL or a response body: only
whether each credential variable holds a placeholder, and lengths, counts and codes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import aiohttp

from sandbox_science.orchestrator.confinement.canaries import DirectSettings
from sandbox_science.orchestrator.confinement.connector_calls import RuntimeConnectorCalls
from sandbox_science.orchestrator.confinement.listings import ControlListingReports
from sandbox_science.orchestrator.confinement.requests import ControlRequestCheck
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.connectors import ConnectorError
from sandbox_science.orchestrator.mcp_direct import _PLACEHOLDER, DirectClient

SERVER = "crypto-research-mcp"
EXCERPT = 160
"""The most characters of a server's error message a step reports (under the lab's 200)."""


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()


class RecordingClient(DirectClient):
    """DirectClient, noting each answer's status, media type, size and the response head's
    encoding and caching fields (never its body). With ``identity`` it asks for an
    unencoded answer (``Accept-Encoding: identity``), a lab diagnosis of listings."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answers: list[dict[str, Any]] = []
        self.heads: list[dict[str, Any]] = []
        self.identity = False
        self.json_only = False
        """A lab diagnosis: ask for a JSON answer alone, which the middleware reads whole."""

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            trace = aiohttp.TraceConfig()

            async def ended(session, context, params) -> None:  # type: ignore[no-untyped-def]
                headers = params.response.headers
                self.heads.append(
                    {
                        "content_encoding": headers.get("Content-Encoding"),
                        "cache_control": headers.get("Cache-Control"),
                        "content_length": "Content-Length" in headers,
                        "transfer_encoding": headers.get("Transfer-Encoding"),
                    }
                )

            trace.on_request_end.append(ended)
            self._http = aiohttp.ClientSession(
                timeout=self._timeout, trust_env=True, trace_configs=[trace]
            )
        return self._http

    def _headers(self, server, session, mcp_session, run):  # type: ignore[no-untyped-def]
        headers = super()._headers(server, session, mcp_session, run)
        if self.identity:
            headers["Accept-Encoding"] = "identity"
        if self.json_only:
            headers["Accept"] = "application/json"
        return headers

    async def _post(self, server, message, session, mcp_session, run):  # type: ignore[no-untyped-def]
        status, kind, body = await super()._post(server, message, session, mcp_session, run)
        params = message.get("params") or {}
        head = self.heads[-1] if self.heads else {}
        self.answers.append(
            {
                "method": message.get("method"),
                "tool": params.get("name") if isinstance(params, dict) else None,
                "status": status,
                "media": kind,
                "bytes": len(body),
                "identity_asked": self.identity,
                "json_only_asked": self.json_only,
                **{key: value for key, value in head.items() if isinstance(value, str | bool)},
            }
        )
        return status, kind, body


def failure(error: BaseException) -> dict[str, Any]:
    if isinstance(error, ConnectorError):
        found: dict[str, Any] = {"error": error.kind, "message_chars": len(str(error))}
        request = error.data.get("request") if isinstance(error.data, dict) else None
        if isinstance(request, int):
            found["request"] = request
        unconfirmed = (
            error.data.get("unconfirmed_request") if isinstance(error.data, dict) else None
        )
        if isinstance(unconfirmed, int):
            found["unconfirmed_request"] = unconfirmed
        return found
    return {"error": "exception", "type": type(error).__name__}


def content_summary(result: dict[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    items = content if isinstance(content, list) else []
    return {
        "is_error": bool(result.get("isError")),
        "content_items": len(items),
        "content_types": sorted(
            {str(item.get("type")) for item in items if isinstance(item, dict)}
        ),
        "text_chars": sum(
            len(item.get("text", ""))
            for item in items
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ),
    }


def listing_summary(tools: list[dict[str, Any]], escalated: str, denied: str) -> dict[str, Any]:
    by_server: dict[str, int] = {}
    for tool in tools:
        name = str(tool.get("name", ""))
        head, sep, _ = name.partition("-mcp_")
        key = head + "-mcp" if sep else name.partition("_")[0]
        by_server[key] = by_server.get(key, 0) + 1
    names = {str(tool.get("name")) for tool in tools}
    marked = [
        tool
        for tool in tools
        if isinstance(tool.get("_meta"), dict) and "sandbox-science/effect" in tool["_meta"]
    ]
    escalates = [
        str(tool.get("name"))
        for tool in tools
        if isinstance(tool.get("_meta"), dict) and tool["_meta"].get("sandbox-science/escalates")
    ]
    effects: dict[str, int] = {}
    for tool in marked:
        effect = str(tool["_meta"]["sandbox-science/effect"])
        effects[effect] = effects.get(effect, 0) + 1
    return {
        "tools": len(tools),
        "by_server": dict(sorted(by_server.items())),
        "denied_listed": denied in names,
        "escalated_listed": escalated in names,
        "escalated_marked": escalated in escalates,
        "marked_with_effect": len(marked),
        "effects": dict(sorted(effects.items())),
        "escalating_tools": len(escalates),
    }


async def call(client: RecordingClient, args: argparse.Namespace, tool: str, arguments: dict):
    return await client.request(
        SERVER, "tools/call", {"name": tool, "arguments": arguments}, session=args.session
    )


async def listing(client: RecordingClient, args: argparse.Namespace, step: str) -> dict:
    started = time.monotonic()
    try:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while pages < 5:
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = await client.request(SERVER, "tools/list", params, session=args.session)
            pages += 1
            tools += [tool for tool in result.get("tools", []) if isinstance(tool, dict)]
            cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
            if cursor is None:
                break
        found = {
            "t": "step",
            "step": step,
            "ok": True,
            "pages": pages,
            "more": cursor is not None,
            "seconds": round(time.monotonic() - started, 3),
            **listing_summary(tools, args.escalated, args.denied),
        }
    except Exception as error:
        found = {"t": "step", "step": step, "ok": False, **failure(error)}
    emit(found)
    return found


async def run_plan(client: RecordingClient, args: argparse.Namespace) -> None:
    # Proof 4: what the runtime's processes hold.
    emit(
        {
            "t": "step",
            "step": "environment",
            "variables": {
                variable: {
                    "present": variable in os.environ,
                    "placeholder": bool(_PLACEHOLDER.fullmatch(os.environ.get(variable, ""))),
                }
                for variable in args.credentials
            },
            "proxy_set": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
        }
    )
    # Proof 3: the listing, read whole by the middleware and rewritten.
    found = await listing(client, args, "listing")
    if args.identity_fallback and found.get("error") == "tool_listing_unreadable":
        # Diagnosis: the same listing, asking the server for an unencoded answer.
        client.identity = True
        await listing(client, args, "listing_identity")
        client.identity = False
    if args.json_listing:
        # Proof 3's JSON half: the same listing, asking for a JSON answer, which the
        # middleware reads whole and rewrites (a server may answer an event stream anyway).
        client.json_only = True
        await listing(client, args, "listing_json")
        client.json_only = False
    # Proof 1: a denied tool, refused by OpenShell's native rules.
    try:
        await call(client, args, args.denied, {})
        emit({"t": "step", "step": "denied", "ok": False, "error": "answered"})
    except Exception as error:
        found = failure(error)
        emit({"t": "step", "step": "denied", "ok": found.get("error") == "policy_denied", **found})
    # Proofs 1 and 4: allowed reads, one per server, admitted by name and sent upstream.
    for tool, arguments in json.loads(args.reads):
        started = time.monotonic()
        try:
            result = await call(client, args, tool, arguments)
            emit(
                {
                    "t": "step",
                    "step": "read",
                    "tool": tool,
                    "ok": not result.get("isError"),
                    "seconds": round(time.monotonic() - started, 3),
                    **content_summary(result),
                }
            )
        except Exception as error:
            emit({"t": "step", "step": "read", "tool": tool, "ok": False, **failure(error)})
    # Proof 2: the escalated tool, held, confirmed, answered and retried.
    started = time.monotonic()
    first: dict[str, Any] | None = None
    attempts = 0
    passed: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    while time.monotonic() - started < args.hold_seconds:
        attempts += 1
        try:
            result = await call(client, args, args.escalated, {})
            passed = content_summary(result)
            break
        except Exception as error:
            last = failure(error)
            if first is None:
                first = {**last, "at": time.time()}
                if last.get("error") == "approval_required" and "request" in last:
                    emit({"t": "held", "request": last["request"], "at": time.time()})
                else:
                    break
            elif last.get("error") != "approval_required":
                break
            await asyncio.sleep(3)
    emit(
        {
            "t": "step",
            "step": "escalated",
            "tool": args.escalated,
            "ok": passed is not None and not passed["is_error"],
            "first": first,
            "attempts": attempts,
            "seconds": round(time.monotonic() - started, 3),
            "result": passed,
            "last_refusal": last if passed is None else None,
        }
    )
    # Proof 3: a non-read call held by the reported listing's drift until the user re-accepts
    # the listing (the host does, as the workspace's review would); its own request is never
    # answered. Called without its required arguments, so nothing is written upstream.
    started = time.monotonic()
    first = None
    attempts = 0
    refusals_after_acceptance = 0
    outcome: dict[str, Any] | None = None
    while time.monotonic() - started < args.hold_seconds:
        attempts += 1
        accepted_before = args.accepted.is_set()
        try:
            result = await call(client, args, args.drift_tool, {})
            outcome = {"form": "tool_result", **content_summary(result)}
            break
        except Exception as error:
            last = failure(error)
            if last.get("error") != "approval_required":
                outcome = {"form": "refused", **last}
                break
            if accepted_before:
                refusals_after_acceptance += 1
            if first is None:
                first = {**last, "at": time.time()}
                if "request" in last:
                    emit({"t": "drift_held", "request": last["request"], "at": time.time()})
                else:
                    break
            await asyncio.sleep(3)
    emit(
        {
            "t": "step",
            "step": "drift_hold",
            "tool": args.drift_tool,
            "first": first,
            "attempts": attempts,
            "refusals_after_acceptance": refusals_after_acceptance,
            "accepted": args.accepted.is_set(),
            "outcome": outcome,
            "seconds": round(time.monotonic() - started, 3),
        }
    )
    # Proof 3: an error answer passes back to the client.
    if args.skip_error_answer:
        emit({"t": "step", "step": "error_answer", "skipped": True})
        emit({"t": "step", "step": "answers", "answers": client.answers})
        return
    try:
        result = await call(client, args, args.invalid_tool, {})
        emit(
            {
                "t": "step",
                "step": "error_answer",
                "tool": args.invalid_tool,
                "form": "tool_result",
                "ok": bool(result.get("isError")),
                **content_summary(result),
            }
        )
    except ConnectorError as error:
        said = str(error)
        excerpt = said[:EXCERPT] if "://" not in said else ""
        emit(
            {
                "t": "step",
                "step": "error_answer",
                "tool": args.invalid_tool,
                "form": "jsonrpc_error",
                "ok": error.kind == "server_error",
                "error": error.kind,
                "excerpt": excerpt,
            }
        )
    except Exception as error:
        emit({"t": "step", "step": "error_answer", "ok": False, **failure(error)})
    emit({"t": "step", "step": "answers", "answers": client.answers})


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mcp", required=True, help="runtime.json's mcp, as the launcher compiles it"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--escalated", required=True)
    parser.add_argument("--denied", required=True)
    parser.add_argument("--invalid-tool", required=True)
    parser.add_argument("--reads", required=True, help="JSON list of [tool, arguments]")
    parser.add_argument("--hold-seconds", type=float, default=150.0)
    parser.add_argument("--skip-error-answer", action="store_true")
    parser.add_argument("--no-identity-fallback", dest="identity_fallback", action="store_false")
    parser.add_argument("--drift-tool", required=True)
    parser.add_argument("--no-json-listing", dest="json_listing", action="store_false")
    args = parser.parse_args()

    async def send(text: str) -> None:
        emit({"t": "frame", "text": text})

    settings = DirectSettings.model_validate_json(args.mcp)
    args.credentials = sorted(set(settings.servers[SERVER].credentials.values()))
    finished = asyncio.Event()
    args.accepted = asyncio.Event()

    async def handle(kind: Capability, value: Any) -> Any:
        # The runtime's side of mcp_request, as the confined runtime serves it (proof 5).
        if kind is Capability.MCP_REQUEST:
            return await calls.request(value)
        raise TransportError("denied", "the lab's agent offers no other capability")

    channel = Channel(send, handle)
    calls = RuntimeConnectorCalls(settings, channel)
    loop = asyncio.get_running_loop()

    async def read() -> None:
        while line := await loop.run_in_executor(None, sys.stdin.readline):
            message = json.loads(line)
            if message.get("t") == "frame":
                await channel.receive(message["text"])
            elif message.get("t") == "finish":
                finished.set()
            elif message.get("t") == "accepted":
                args.accepted.set()

    reader = asyncio.ensure_future(read())
    client = RecordingClient(
        {SERVER: settings.servers[SERVER].server()},
        project=args.project,
        check=ControlRequestCheck(channel, args.attempt),
        run=args.run,
        reporter=ControlListingReports(channel, args.attempt),
    )
    try:
        await run_plan(client, args)
        emit({"t": "plan_done"})
        await asyncio.wait_for(finished.wait(), timeout=660)
    finally:
        await calls.close()
        await client.close()
        emit({"t": "done"})
        reader.cancel()
        os._exit(0)  # the stdin reader thread may still block on readline


if __name__ == "__main__":
    asyncio.run(main())
