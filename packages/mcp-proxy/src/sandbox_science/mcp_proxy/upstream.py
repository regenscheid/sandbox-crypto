# SPDX-License-Identifier: MIT
"""Requests to MCP servers over Streamable HTTP (MCP 2025-03-26 to 2025-11-25).

Every request is a POST that the proxy builds itself:

- the headers are the proxy's own: ``Content-Type``, ``Accept``,
  ``Accept-Encoding: identity``, ``MCP-Protocol-Version`` once the session
  has a negotiated revision, ``Mcp-Session-Id`` when the server issued one,
  and the server's credential headers, which hold OpenShell placeholders.
  Nothing a caller sends in headers reaches a server;
- the body is the proxy's serialization of the message, and is refused if
  anything in it reads as a credential placeholder: OpenShell substitutes
  placeholders only in headers and forwards bodies unchanged (lab 2), so a
  placeholder in a body is useless at best, and a leak if that changed;
- redirects are not followed and cookies are not kept, so nothing passes
  between sessions or servers that way.

An answer is read with a byte limit while it arrives, as ``application/json``
or as a ``text/event-stream`` that ends with the response. Server requests in
the stream never reach callers: ``ping`` is answered, and every other method
(sampling, elicitation, roots) is refused with an error. The proxy declares no
client capabilities, so a server has no grounds to send those.
``notifications/tools/list_changed`` is noted; other notifications are dropped.

aiohttp connects only to the addresses its resolver returns, so the
``GuardedResolver`` checks every address against the hard floor and the pool
ceiling before a connection is made (docs/POLICY.md, the ``resolved`` parameter).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import socket
import ssl
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from sandbox_science.policy import HardFloor, PoolCeiling

from . import jsonrpc
from .credentials import Headers, contains_placeholder
from .errors import MessageError, RefusalError, SessionGoneError, UpstreamError
from .jsonrpc import JsonObject, Notification, Request, Response
from .reasons import (
    CREDENTIAL_IN_BODY,
    DESTINATION_FORBIDDEN,
    POOL_CEILING_DENIED,
    RESULT_TOO_LARGE,
    UPSTREAM_ERROR,
    UPSTREAM_UNAUTHORIZED,
    UPSTREAM_UNAVAILABLE,
)

USER_AGENT: Final = "sandbox-science-mcp-proxy/0.1"
ACCEPT: Final = "application/json, text/event-stream"
CONNECT_TIMEOUT: Final = 10.0
MAX_SERVER_REQUESTS: Final = 16
"""Server requests one answer may carry before the proxy gives up on it."""
MAX_SESSION_ID: Final = 2048
"""The longest session id the proxy accepts from a server (some issue JWTs)."""
_CHUNK: Final = 64 * 1024
_MAX_ACK_BYTES: Final = 64 * 1024

_log = logging.getLogger(__name__)

AddressCheck = Callable[[str, Sequence[str]], tuple[str, str] | None]
"""Check a host and its addresses; return a reason code and detail to refuse them."""


class DestinationRefusedError(OSError):
    """A resolver refused a server's addresses (aiohttp wraps it in ClientConnectorDNSError)."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


class GuardedResolver(AbstractResolver):
    """Resolve names with another resolver, then refuse addresses the check forbids."""

    def __init__(self, inner: AbstractResolver, check: AddressCheck) -> None:
        self._inner = inner
        self._check = check

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve ``host`` and check every address it resolves to."""
        results = await self._inner.resolve(host, port, family)
        refused = self._check(host, [result["host"] for result in results])
        if refused is not None:
            raise DestinationRefusedError(*refused)
        return results

    async def close(self) -> None:
        """Release the inner resolver."""
        await self._inner.close()


def address_check(floor: HardFloor, ceiling: PoolCeiling) -> AddressCheck:
    """The checks the policy library applies to the resolved addresses of an MCP server."""

    def check(host: str, addresses: Sequence[str]) -> tuple[str, str] | None:
        reason = floor.check_destination(host, addresses)
        if reason is not None:
            return DESTINATION_FORBIDDEN, f"hard floor: {reason}"
        reason = ceiling.check_host(host, addresses)
        if reason is not None:
            return POOL_CEILING_DENIED, reason
        return None

    return check


def client_session(
    resolver: AbstractResolver, *, timeout: float, tls: ssl.SSLContext | bool = True
) -> aiohttp.ClientSession:
    """The HTTP client for all upstream requests. Create it on the running event loop.

    Certificates are verified against the server's declared host; inside an
    OpenShell sandbox the default context trusts the sandbox's CA through the
    environment OpenShell sets. ``trust_env`` is off, so no proxy variable can
    send requests elsewhere, and the address check sees the server's addresses.
    """
    connector = aiohttp.TCPConnector(resolver=resolver, ssl=tls, limit=256, limit_per_host=64)
    return aiohttp.ClientSession(
        connector=connector,
        auto_decompress=False,
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(
            total=timeout, connect=CONNECT_TIMEOUT, sock_connect=CONNECT_TIMEOUT
        ),
    )


@dataclass(frozen=True, slots=True)
class Target:
    """How one call reaches one server."""

    url: str
    credentials: Headers
    session_id: str | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class Exchange:
    """A server's answer to one request."""

    response: Response
    session_id: str | None
    received_bytes: int
    notifications: int = 0
    refused_requests: int = 0
    tools_changed: bool = False


class Upstream:
    """Sends requests to servers and reads their answers within limits."""

    def __init__(self, http: aiohttp.ClientSession) -> None:
        self._http = http
        self._replies: set[asyncio.Task[None]] = set()

    async def request(
        self, target: Target, body: bytes, *, request_id: int, limit: int
    ) -> Exchange:
        """Send one request (``body``, already serialized) and return the server's response.

        Raises RefusalError when nothing was sent, SessionGoneError when the server no
        longer knows the session, and UpstreamError for any other failure.
        """
        async with self._post(target, body) as response:
            session_id = _session_id(response)
            match response.content_type:
                case "application/json":
                    data = await _read(response, limit)
                    try:
                        message = jsonrpc.parse_message(jsonrpc.loads(data), strict=False)
                    except MessageError as exc:
                        raise UpstreamError(UPSTREAM_ERROR, f"malformed answer: {exc}") from exc
                    return Exchange(_response_to(message, request_id), session_id, len(data))
                case "text/event-stream":
                    return await self._stream(target, response, request_id, limit, session_id)
                case other:
                    raise UpstreamError(UPSTREAM_ERROR, f"an answer of type {other}")

    async def send(self, target: Target, message: JsonObject) -> None:
        """Send a notification or a response; the server acknowledges it with 202."""
        async with self._post(target, jsonrpc.dumps(message)) as response:
            await _read(response, _MAX_ACK_BYTES)

    async def close(self) -> None:
        """Wait briefly for replies to server requests, then close the HTTP client."""
        if self._replies:
            await asyncio.wait(self._replies, timeout=5)
        for task in self._replies:
            task.cancel()
        await self._http.close()

    @contextlib.asynccontextmanager
    async def _post(
        self, target: Target, body: bytes
    ) -> AsyncGenerator[aiohttp.ClientResponse, None]:
        """POST a body and map transport failures to RefusalError and UpstreamError."""
        if contains_placeholder(body):
            raise RefusalError(
                CREDENTIAL_IN_BODY, "the request body contains a credential placeholder"
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        if target.version is not None:
            headers["MCP-Protocol-Version"] = target.version
        if target.session_id is not None:
            headers["Mcp-Session-Id"] = target.session_id
        headers.update(target.credentials)
        try:
            async with self._http.post(
                target.url, data=body, headers=headers, allow_redirects=False
            ) as response:
                _check_status(target, response.status)
                yield response
        except aiohttp.ClientConnectorDNSError as exc:
            if isinstance(exc.os_error, DestinationRefusedError):
                raise RefusalError(exc.os_error.reason_code, str(exc.os_error)) from exc
            raise UpstreamError(UPSTREAM_UNAVAILABLE, str(exc)) from exc
        except TimeoutError as exc:
            raise UpstreamError(UPSTREAM_ERROR, "the server did not answer in time") from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(UPSTREAM_UNAVAILABLE, str(exc)) from exc

    async def _stream(
        self,
        target: Target,
        response: aiohttp.ClientResponse,
        request_id: int,
        limit: int,
        session_id: str | None,
    ) -> Exchange:
        _check_encoding(response)
        if target.session_id is None and session_id is not None:
            # Replies to requests in an initialize answer belong to the new session.
            target = Target(target.url, target.credentials, session_id, target.version)
        parser = jsonrpc.EventStreamParser()
        stream = _Stream()
        received = 0
        final: Response | None = None
        async for chunk in response.content.iter_chunked(_CHUNK):
            received += len(chunk)
            if received > limit:
                raise UpstreamError(RESULT_TOO_LARGE, f"the answer exceeds {limit} bytes")
            final = self._take(target, stream, _events(functools.partial(parser.feed, chunk)))
            if final is not None:
                response.close()  # the rest of the stream is not needed
                break
        else:
            final = self._take(target, stream, _events(parser.finish))
        if final is None:
            raise UpstreamError(UPSTREAM_ERROR, "the event stream ended without a response")
        return Exchange(
            _response_to(final, request_id),
            session_id,
            received,
            stream.notifications,
            stream.refused,
            stream.tools_changed,
        )

    def _take(
        self, target: Target, stream: _Stream, events: list[jsonrpc.Event]
    ) -> Response | None:
        """Handle a stream's events; return the response once it has arrived."""
        for event in events:
            if event.type != "message" or not event.data:
                continue  # other event types, and priming events without data
            for message in _messages(event):
                final = self._handle_message(target, stream, message)
                if final is not None:
                    return final
        return None

    def _handle_message(
        self, target: Target, stream: _Stream, message: jsonrpc.Message
    ) -> Response | None:
        """Note or answer one message from a stream; return it if it is the response."""
        match message:
            case Response():
                return message
            case Request():
                stream.requests += 1
                if stream.requests > MAX_SERVER_REQUESTS:
                    raise UpstreamError(UPSTREAM_ERROR, "too many server requests")
                if message.method != "ping":
                    stream.refused += 1
                self._reply(target, message)
            case Notification():
                stream.notifications += 1
                if message.method == "notifications/tools/list_changed":
                    stream.tools_changed = True
        return None

    def _reply(self, target: Target, message: Request) -> None:
        """Answer a server request in the background, without passing it to the caller."""
        if message.method == "ping":
            reply = jsonrpc.result(message.id, {})
        else:
            reply = jsonrpc.error(
                message.id, -32601, f"{message.method} is not available through the MCP proxy"
            )
        task = asyncio.create_task(self._send_quietly(target, reply))
        self._replies.add(task)
        task.add_done_callback(self._replies.discard)

    async def _send_quietly(self, target: Target, message: JsonObject) -> None:
        try:
            await self.send(target, message)
        except (RefusalError, UpstreamError, MessageError) as exc:
            _log.warning("could not answer a server request: %s", exc)


@dataclass
class _Stream:
    """What one event stream carried besides its response."""

    notifications: int = 0
    requests: int = 0
    refused: int = 0
    tools_changed: bool = False


def _messages(event: jsonrpc.Event) -> list[jsonrpc.Message]:
    """The messages in one event; MCP 2025-03-26 lets a server batch several in one."""
    try:
        value = jsonrpc.loads(event.data.encode("utf-8"))
        items = cast("list[object]", value) if isinstance(value, list) else [value]
        return [jsonrpc.parse_message(item, strict=False) for item in items]
    except MessageError as exc:
        raise UpstreamError(UPSTREAM_ERROR, f"malformed event: {exc}") from exc


def _events(parse: Callable[[], list[jsonrpc.Event]]) -> list[jsonrpc.Event]:
    try:
        return parse()
    except MessageError as exc:
        raise UpstreamError(UPSTREAM_ERROR, str(exc)) from exc


def _check_status(target: Target, status: int) -> None:
    if 200 <= status < 300:
        return
    if status == 404 and target.session_id is not None:
        raise SessionGoneError(UPSTREAM_ERROR, "the server ended the session", status=status)
    if status == 401:
        raise UpstreamError(UPSTREAM_UNAUTHORIZED, "the server answered 401", status=status)
    raise UpstreamError(UPSTREAM_ERROR, f"the server answered {status}", status=status)


def _response_to(message: jsonrpc.Message, request_id: int) -> Response:
    if not isinstance(message, Response):
        raise UpstreamError(UPSTREAM_ERROR, "the answer is not a JSON-RPC response")
    if message.id != request_id:
        raise UpstreamError(UPSTREAM_ERROR, f"the answer is for request {message.id!r}")
    return message


def _session_id(response: aiohttp.ClientResponse) -> str | None:
    values = response.headers.getall("Mcp-Session-Id", [])
    if not values:
        return None
    value = values[0]
    visible = all(0x21 <= ord(char) <= 0x7E for char in value)
    if len(values) > 1 or not 0 < len(value) <= MAX_SESSION_ID or not visible:
        raise UpstreamError(UPSTREAM_ERROR, "a malformed Mcp-Session-Id")
    if contains_placeholder(value.encode("ascii")):
        # The proxy sends the id back in a header, where OpenShell would substitute it.
        raise UpstreamError(UPSTREAM_ERROR, "an Mcp-Session-Id that reads as a placeholder")
    return value


def _check_encoding(response: aiohttp.ClientResponse) -> None:
    # The proxy asks for identity encoding and does not decompress, so a
    # compressed body cannot expand past the limit after it was counted.
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding != "identity":
        raise UpstreamError(UPSTREAM_ERROR, f"an answer with Content-Encoding {encoding}")


async def _read(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read a whole body, failing as soon as it exceeds ``limit`` bytes."""
    _check_encoding(response)
    length = response.content_length
    if length is not None and length > limit:
        raise UpstreamError(RESULT_TOO_LARGE, f"the answer is {length} bytes; at most {limit}")
    data = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        data += chunk
        if len(data) > limit:
            raise UpstreamError(RESULT_TOO_LARGE, f"the answer exceeds {limit} bytes")
    return bytes(data)
