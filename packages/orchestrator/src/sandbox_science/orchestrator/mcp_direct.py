# SPDX-License-Identifier: MIT
"""The MCP client that reaches declared servers straight through OpenShell (D98).

In a sandbox compiled with ``compile_agent_mcp``, the agent runtime's client speaks MCP's
Streamable HTTP transport to each server's own URL, through OpenShell's proxy (its
``HTTPS_PROXY``): OpenShell's native rules allow and deny tools by name, the policy
middleware decides the rest, and OpenShell substitutes the credentials, whose placeholders
the client reads from its environment. It replaced the MCP proxy, a service in a sandbox of
its own that held the servers' credentials and decided each call, which retired on
2026-10-26.

Each request names the session and the run in ``Sandbox-Science-Session`` and
``Sandbox-Science-Run``, which the middleware records. A refusal comes back as OpenShell's
``403`` with a JSON body: ``policy_denied`` from the native rules, or ``middleware_denied``
with the middleware's reason code, which for a held call names its permission request
(``approval_required_r12``). Both become a ``ConnectorError`` with that reason, so
``Connectors`` waits for an approval and calls again.

The server is untrusted, and nothing marks OpenShell's denial as OpenShell's: its body is
fixed JSON (``error``, ``detail``, the policy's and the middleware's names, ``reason_code``)
that any server can write, with no header of its own. So a refusal that names a request is
trusted only when the control side confirms it (``RequestCheck``): the request exists,
stands as the code says, and is this call's, by project, server, method, tool and the
digest of the parameters. Anything else is the server's error (``server_error``), never a
hold: no card, no wait, and the number it named is kept only as ``unconfirmed_request``.

The production check reads the middleware's own records, the way the workspace reaches
them: through the worker, which holds the middleware's event store (``WorkerRequestCheck``,
the worker's ``POST /v1/permission-requests/{seq}/confirm``). That route answers any
client, for its own projects, yes or no about one request, never its record, so the run
service's token gains nothing broader. In the confined runtime, which holds no worker
token, the check is the attempt's ``confirm_request`` capability
(``confinement.requests.ControlRequestCheck``), bound to the attempt's project, which the
host answers with the same worker check.

Every ``tools/list`` page it returns shows only the tools its server's compiled native rules
admit (``DirectServer.tools``), since the middleware cannot rewrite an event-stream listing
OpenShell does not offer it whole; each complete listing, as the server sent it, goes to its
``ListingReporter`` for the trusted side's drift check, and one the control side does not
take is not shown.

In the confined runtime the client runs in the agent runtime's sandbox, beside OpenShell's
proxy and the provider's placeholders: there it makes the agent's connector requests
(``[mcp_direct]``): the host's toolbox admits each call and relays its request
(``confinement.connector_calls``); it lists servers for the control service's canaries and
the workspace's availability checks (``confinement.canaries``), and hands binary results to
the host over ``publish_mcp_binary`` (``confinement.binaries``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import aiohttp
from pydantic import ValidationError

from sandbox_science.contracts.permission_requests import PermissionConfirmation
from sandbox_science.policy import mcp_wire
from sandbox_science.policy.mcp_listings import listing_digest
from sandbox_science.policy.mcp_wire import ListedTool, McpWireError
from sandbox_science.policy.openshell import ToolAdmission

from .connectors import (
    CALL_SECONDS,
    MAX_ANSWER_BYTES,
    PROTOCOL,
    RUN_HEADER,
    SESSION_HEADER,
    ConnectorError,
    clean,
    encode_message,
)
from .worker_client import WorkerClient

_HELD: Final = re.compile(
    r"(approval_required|approval_denied|escalation_timed_out)_r([0-9]{1,18})"
)
_HELD_KINDS: Final = frozenset({"approval_required", "approval_denied", "escalation_timed_out"})
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLACEHOLDER: Final = re.compile(r"openshell:resolve:env:[\x21-\x7e]{1,512}")


@dataclass(frozen=True)
class DirectServer:
    """A declared server: its URL, as the project policy names it, its credential headers,
    each the environment variable that holds an OpenShell placeholder, and the tool names
    its compiled endpoint's native rules admit (``tools``): the only tools a listing shows.
    Without them, a listing shows none."""

    url: str
    credentials: Mapping[str, str] = field(default_factory=dict[str, str])
    tools: ToolAdmission = field(default_factory=ToolAdmission)


MAX_REPORTED_TOOLS: Final = 2048
"""Tools one complete listing may have to be reported; a larger one is refused."""


class ListingReporter(Protocol):
    """Takes a server's complete listing, as the server sent it: each tool's name, digest
    and hint, and the listing's digest. In the confined runtime, the attempt's (or the
    canaries') ``report_mcp_listing`` (``confinement.listings``); it raises when the
    control side does not take the report."""

    async def __call__(self, server: str, tools: Sequence[ListedTool], listing: str) -> None: ...


@dataclass(frozen=True)
class HeldCall:
    """What a held call's permission request must be keyed by, as the middleware keys it:
    the project, the declared server, the method, the tool (empty for a method other than
    ``tools/call``) and the SHA-256 of the parameters MCP defines, as compact JSON."""

    project: str
    server: str
    method: str
    tool: str
    arguments: str

    def fields(self) -> dict[str, str]:
        """The fields of a ``RequestKey.mcp`` they fix; its caller and revision are left out."""
        return {
            "project_id": self.project,
            "server": self.server,
            "method": self.method,
            "tool": self.tool,
            "arguments": self.arguments,
        }


class RequestCheck(Protocol):
    """The control side's word on a refusal that names permission request ``request``
    with ``reason`` (``approval_required``, ``approval_denied`` or
    ``escalation_timed_out``): whether the request exists, stands so and concerns ``call``
    (``Approvals.confirms`` over the permission request records decides it)."""

    async def __call__(self, request: int, reason: str, call: HeldCall) -> bool: ...


class WorkerRequestCheck:
    """The production ``RequestCheck``: the worker's confirmation of one MCP request in the
    policy middleware's event store, asked with this client's own token, for its projects.
    A call the confirmation cannot describe is not confirmed; a worker that cannot answer
    raises, and the client reports the call as unavailable rather than wait."""

    def __init__(self, worker: WorkerClient) -> None:
        self._worker = worker

    async def __call__(self, request: int, reason: str, call: HeldCall) -> bool:
        try:
            asked = PermissionConfirmation.model_validate(
                {
                    "reason": reason,
                    "project_id": call.project,
                    "server": call.server,
                    "method": call.method,
                    "tool": call.tool,
                    "arguments": call.arguments,
                }
            )
        except ValidationError:
            return False
        answer = await self._worker.confirm_permission_request(request, asked)
        return answer.confirmed is True


class DirectClient:
    """One run's MCP connections to declared servers, through OpenShell.

    It keeps one MCP session per server, started on first use and
    shared by concurrent callers, starts it again once if the server forgot it (404),
    sends every message with its keys sorted, so a call made again after its approval is
    the request that was approved, and never follows a redirect.
    """

    def __init__(
        self,
        servers: Mapping[str, DirectServer],
        *,
        project: str,
        check: RequestCheck | None = None,
        run: str | None = None,
        timeout: float = CALL_SECONDS,
        environ: Mapping[str, str] | None = None,
        reporter: ListingReporter | None = None,
    ) -> None:
        self._servers = dict(servers)
        self._project, self._check = project, check
        """The project whose policy the middleware applies, and the check of held calls:
        without one, no refusal that names a request is trusted."""
        self._run = run
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._environ = environ if environ is not None else os.environ
        self._http: aiohttp.ClientSession | None = None
        self._sessions: dict[str, asyncio.Task[str | None]] = {}
        self._ids = 0
        self.reporter = reporter
        """Where each complete listing goes for the drift check; none in a lab or test."""
        self._pages: dict[tuple[str, str], list[ListedTool]] = {}

    async def close(self) -> None:
        for task in self._sessions.values():
            task.cancel()
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        """``method``'s result from ``server``; ``session`` names the asking session and
        ``run`` the run that asks, over the run this client was made for (DA-4)."""
        run = run if run is not None else self._run
        if server not in self._servers:
            raise ConnectorError("unavailable", f"{server} is not a declared server")
        self._ids += 1
        request_id = self._ids
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        for attempt in (1, 2):
            task = self._sessions.get(server)
            if task is None or (task.done() and (task.cancelled() or task.exception())):
                task = self._sessions[server] = asyncio.ensure_future(
                    self._start(server, session, run)
                )
            try:
                mcp_session = await asyncio.shield(task)
            except ConnectorError:
                if self._sessions.get(server) is task:
                    del self._sessions[server]
                raise
            status, kind, body = await self._post(server, message, session, mcp_session, run)
            if status == 404 and mcp_session is not None and attempt == 1:
                if self._sessions.get(server) is task:
                    del self._sessions[server]  # the server forgot it: start again, once
                continue
            try:
                result = _result(status, kind, body, request_id)
            except ConnectorError as error:
                if error.kind in _HELD_KINDS:
                    await self._confirm(error, server, method, params)
                raise
            if method == "tools/list":
                return await self._listed(server, session, "cursor" in params, result)
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    async def _listed(
        self, server: str, session: str, following: bool, result: dict[str, Any]
    ) -> dict[str, Any]:
        """A ``tools/list`` page as the caller sees it: without the tools the compiled native
        rules deny (by name, as OpenShell matches them), so no denied tool reaches the
        model. Once a listing is complete (a first page, then pages to one without a
        cursor), the server's own listing goes to ``reporter`` for the drift check; a
        listing the control side does not take is refused, never passed unchecked."""
        try:
            listed = mcp_wire.listed_tools(result)
            _, cursor = mcp_wire.tool_names(result)
        except McpWireError:
            raise ConnectorError("unavailable", f"{server}'s tools/list is malformed") from None
        key = (server, session)
        earlier = self._pages.pop(key, None)
        pages = earlier if following else []
        if pages is not None:
            pages += listed
            if len(pages) > MAX_REPORTED_TOOLS:
                raise ConnectorError("unavailable", f"{server} lists too many tools to check")
            if cursor is not None:
                self._pages[key] = pages
            elif self.reporter is not None:
                seen = {tool.name: tool for tool in pages}
                try:
                    await self.reporter(server, list(seen.values()), listing_digest(seen))
                except Exception as exc:
                    raise ConnectorError(
                        "unavailable", f"{server}'s listing was not checked ({type(exc).__name__})"
                    ) from None
        admission = self._servers[server].tools
        tools = cast(list[dict[str, Any]], result["tools"])
        return {**result, "tools": [tool for tool in tools if admission.admits(tool["name"])]}

    async def _confirm(
        self, error: ConnectorError, server: str, method: str, params: Mapping[str, object]
    ) -> None:
        """Return if the control side confirms that ``error``'s request is this call's and
        stands as its reason says; raise the server's error otherwise."""
        request = cast(int, error.data["request"])
        tool = params.get("name") if method == "tools/call" else None
        try:
            call = HeldCall(
                self._project,
                server,
                method,
                tool if isinstance(tool, str) else "",
                parameters_digest(params),
            )
        except (TypeError, ValueError, RecursionError):
            raise _unconfirmed(error.kind, request) from None
        if self._check is None:
            raise _unconfirmed(error.kind, request)
        try:
            confirmed = await self._check(request, error.kind, call)
        except Exception as exc:
            raise ConnectorError(
                "unavailable",
                f"permission request {request} could not be checked ({type(exc).__name__})",
            ) from None
        if confirmed is not True:
            raise _unconfirmed(error.kind, request)

    async def _start(self, server: str, session: str, run: str | None) -> str | None:
        """Start an MCP session with ``server``; the server's session ID, if it issued one.
        The client declares no capabilities, which the middleware requires."""
        params = {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "sandbox-science-harness", "version": "0.1"},
        }
        self._ids += 1
        message = {"jsonrpc": "2.0", "id": self._ids, "method": "initialize", "params": params}
        try:
            async with self._client().post(
                self._servers[server].url,
                data=encode_message(message),
                headers=self._headers(server, session, None, run),
                allow_redirects=False,
            ) as response:
                found = response.headers.get("Mcp-Session-Id")
                status, kind, body = response.status, _media(response), await _read(response)
            try:
                _result(status, kind, body, self._ids)
            except ConnectorError as error:
                if error.kind in _HELD_KINDS:  # no request of initialize is ever confirmed
                    raise _unconfirmed(error.kind, cast(int, error.data["request"])) from None
                raise
            async with self._client().post(
                self._servers[server].url,
                data=encode_message({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                headers=self._headers(server, session, found or "", run),
                allow_redirects=False,
            ) as response:
                await _read(response)
                if response.status != 202:
                    raise ConnectorError(
                        "unavailable", f"{server} answered initialized with {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ConnectorError(
                "unavailable", f"{server} cannot be reached: {type(exc).__name__}"
            ) from None
        return found

    async def _post(
        self,
        server: str,
        message: Mapping[str, object],
        session: str,
        mcp_session: str | None,
        run: str | None,
    ) -> tuple[int, str, bytes]:
        try:
            response = await self._client().post(
                self._servers[server].url,
                data=encode_message(message),
                headers=self._headers(server, session, mcp_session or "", run),
                allow_redirects=False,
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # Nothing was answered: the call may or may not have left.
            raise ConnectorError(
                "no_usable_answer", f"{server} gave no answer ({type(exc).__name__})"
            ) from None
        async with response:
            try:
                return response.status, _media(response), await _read(response)
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ConnectorError(
                    "no_usable_answer",
                    f"{server}'s answer could not be read ({type(exc).__name__})",
                ) from None

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            # OpenShell's proxy is the sandbox's HTTPS_PROXY.
            self._http = aiohttp.ClientSession(timeout=self._timeout, trust_env=True)
        return self._http

    def _headers(
        self, server: str, session: str, mcp_session: str | None, run: str | None
    ) -> dict[str, str]:
        """The request's headers. ``mcp_session`` is None for ``initialize`` and empty
        when the server issued no session."""
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            SESSION_HEADER: session,
        }
        if run is not None:
            headers[RUN_HEADER] = run
        if mcp_session is not None:
            headers["MCP-Protocol-Version"] = PROTOCOL
            if mcp_session:
                headers["Mcp-Session-Id"] = mcp_session
        for name, variable in self._servers[server].credentials.items():
            value = self._environ.get(variable, "")
            if not _PLACEHOLDER.fullmatch(value):
                # Only a placeholder leaves: the harness never sends a credential it holds.
                raise ConnectorError("unavailable", f"{variable} holds no OpenShell placeholder")
            headers[name] = value
        return headers


def parameters_digest(params: Mapping[str, object]) -> str:
    """The digest the middleware's key holds for a request's parameters: those MCP defines,
    which are every one but ``_meta`` this client sends, as ``policy.mcp_wire`` encodes them
    (compact, and in the client's sorted order). Raises ValueError for what JSON cannot
    hold."""
    defined = {key: params[key] for key in sorted(params) if key != "_meta"}
    text = json.dumps(
        defined, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unconfirmed(code: str, request: int) -> ConnectorError:
    """A refusal naming a request the control side does not confirm: the server's words."""
    return ConnectorError(
        "server_error",
        f"the MCP server answered 403 as a refusal ({code}) naming permission request "
        f"{request}, which is not this call's (untrusted)",
        {"unconfirmed_request": request},
    )


def _media(response: aiohttp.ClientResponse) -> str:
    return response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


async def _read(response: aiohttp.ClientResponse) -> bytes:
    data = bytearray()
    while chunk := await response.content.read(64 * 1024):
        data += chunk
        if len(data) > MAX_ANSWER_BYTES:
            raise ConnectorError("no_usable_answer", f"the answer exceeds {MAX_ANSWER_BYTES} bytes")
    return bytes(data)


def _messages(kind: str, body: bytes) -> list[object]:
    """The JSON messages of an answer: one JSON body, or an event stream's message events.
    Any other media holds none: the middleware reads listings of these two only."""
    try:
        if kind == "application/json":
            return [json.loads(body)]
        if kind != "text/event-stream":
            return []
        found: list[object] = []
        for block in re.split(r"\r?\n\r?\n", body.decode("utf-8")):
            lines = block.splitlines()
            event = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
            data = [line[5:].removeprefix(" ") for line in lines if line.startswith("data:")]
            if data and event in ("", "message"):
                found.append(json.loads("\n".join(data)))
        return found
    except (ValueError, RecursionError):
        return []


def _result(status: int, kind: str, body: bytes, request_id: int) -> dict[str, Any]:
    """An answer's result, or why there is none as a ``ConnectorError``."""
    if status == 403:
        _refusal(body)
    for value in _messages(kind, body):
        message = cast(dict[str, Any], value) if isinstance(value, dict) else {}
        if message.get("id") != request_id:
            continue  # notifications and server requests in a stream
        error = message.get("error")
        if isinstance(error, dict):
            # A server speaks only for itself: its errors never pass as the platform's.
            said = clean(str(cast(dict[str, Any], error).get("message", "")))[:2000]
            raise ConnectorError(
                "server_error", f"the MCP server answered an error (untrusted): {said}"
            )
        result = message.get("result")
        if status == 200 and isinstance(result, dict):
            return cast(dict[str, Any], result)
    raise ConnectorError(
        "no_usable_answer", f"the server answered {status} without a usable result"
    )


def _refusal(body: bytes) -> None:
    """Raise OpenShell's refusal as its reason's ``ConnectorError``; anything else is the
    server's."""
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        value = None
    fields = cast(dict[str, Any], value) if isinstance(value, dict) else {}
    error, code = fields.get("error"), fields.get("reason_code")
    if error == "policy_denied":
        raise ConnectorError("policy_denied", "the sandbox's rules deny this call")
    if error != "middleware_denied" or not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ConnectorError("no_usable_answer", "the server answered 403")
    held = _HELD.fullmatch(code)
    if held is None:
        raise ConnectorError(code, f"the platform refused the call ({code})")
    reason, request = held.group(1), int(held.group(2))
    messages = {
        "approval_required": "the call waits for approval",
        "approval_denied": "the call was denied",
        "escalation_timed_out": "nobody answered the call's permission request in time",
    }
    raise ConnectorError(
        reason, f"{messages[reason]} (permission request {request})", {"request": request}
    )
