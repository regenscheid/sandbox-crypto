# SPDX-License-Identifier: MIT
"""Connector calls on the direct path: admitted by the host, made by the runtime (D98).

With ``[mcp_direct]`` no trusted service can reach an MCP server: the client, OpenShell's
proxy and the provider's placeholders are in the agent runtime's sandbox, whose compiled
``protocol: mcp`` endpoints admit only the runtime's interpreter. The host keeps
everything else of a run's connectors: the tool index, the
``mcp_tools`` loads, the plan fence and the other gates of each call, a server the user
turned off, the wait on a held call's permission request and its card, and the tool
record (``Connectors``, through the host's toolbox and its ``tool`` capability). Only the
request leaves from the runtime: the host's ``Connectors`` on this path has a
``RuntimeMcpClient``, which sends each ``tools/list`` and ``tools/call`` of the attempt to
the runtime over ``mcp_request``, after the toolbox admitted the call.

The runtime (``RuntimeConnectorCalls``) makes it with a ``DirectClient`` of the attempt's:

- its listings show only the tools its server's compiled native rules admit, and each
  complete listing is reported over the attempt's ``report_mcp_listing`` for the drift
  check;
- a refusal that names a permission request is trusted only once the host confirms it over
  the attempt's ``confirm_request`` (which asks the worker), and the host takes a held
  call's failure only for a request it confirmed for the very call it relayed (server,
  method, tool and parameters' digest): the runtime's word alone starts no wait and no card;
- a ``tools/call`` result's images, audio and blobs go to the host over the attempt's
  ``publish_mcp_binary`` under the model's call ID, while the host relays that call, and
  the host's note replaces each item.

OpenShell's native rules and the policy middleware then decide the request itself, as
for any request of a sandbox bound to the project's revision, and the middleware records
it with the project, session and run.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import JsonValue

from sandbox_science.orchestrator.connectors import CALL_SECONDS, ConnectorError
from sandbox_science.orchestrator.mcp_binaries import withheld
from sandbox_science.orchestrator.mcp_direct import DirectClient
from sandbox_science.policy.mcp_answers import binary_contents, withhold_binary

from .binaries import ControlBinaries
from .canaries import DirectSettings
from .listings import ControlListingReports
from .requests import ControlRequestCheck
from .transport import Channel
from .wire import McpAnswer, McpFailure, McpRequest, payload, typed

if TYPE_CHECKING:
    from .host import HostLoops

_log = logging.getLogger(__name__)

HELD: Final = frozenset({"approval_required", "approval_denied", "escalation_timed_out"})
MAX_CLIENTS: Final = 64
"""Attempts whose clients the runtime keeps at once: its active-tree limit."""


class RuntimeMcpClient:
    """The host's ``McpClient`` on the direct path: each request goes to the runtime over
    the attempt's ``mcp_request`` (``HostLoops.relay_mcp``). Its listings carry no class
    the host takes (``classifies`` is false), as a server's own listing does not."""

    classifies = False

    def __init__(self, loops: HostLoops) -> None:
        self._loops = loops

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        return await self._loops.relay_mcp(server, method, params, session=session, run=run)


def failure(answer: McpFailure, confirmed: bool) -> ConnectorError:
    """The client's error as the runtime said it. A held call's names its request only when
    the host ``confirmed`` that request for the attempt; otherwise it is the server's
    error, as an unconfirmed refusal is (``mcp_direct``)."""
    if answer.kind in HELD:
        if answer.request is not None and confirmed:
            return ConnectorError(answer.kind, answer.message, {"request": answer.request})
        unconfirmed = answer.request or answer.unconfirmed_request
        return ConnectorError(
            "server_error",
            "the MCP server answered a refusal naming a permission request the control side "
            "did not confirm (untrusted)",
            {"unconfirmed_request": unconfirmed} if unconfirmed is not None else {},
        )
    data = (
        {"unconfirmed_request": answer.unconfirmed_request}
        if answer.unconfirmed_request is not None
        else {}
    )
    return ConnectorError(answer.kind, answer.message, data)


class RuntimeConnectorCalls:
    """The runtime's side of ``mcp_request``: one ``DirectClient`` per attempt, whose held
    calls are confirmed, listings reported and binary results published over that
    attempt's capabilities."""

    def __init__(self, settings: DirectSettings, channel: Channel) -> None:
        self._settings, self._channel = settings, channel
        self._servers = {name: spec.server() for name, spec in settings.servers.items()}
        self._clients: OrderedDict[str, DirectClient] = OrderedDict()

    def _client(self, attempt: str) -> DirectClient:
        client = self._clients.get(attempt)
        if client is None:
            client = self._clients[attempt] = DirectClient(
                self._servers,
                project=self._settings.project,
                check=ControlRequestCheck(self._channel, attempt),
                timeout=CALL_SECONDS,
                reporter=ControlListingReports(self._channel, attempt),
            )
        self._clients.move_to_end(attempt)
        return client

    async def request(self, value: JsonValue) -> JsonValue:
        asked = typed(McpRequest, value)
        if asked.server not in self._servers:
            return _failed("unavailable", f"{asked.server} is not a declared server")
        while len(self._clients) >= MAX_CLIENTS and asked.attempt not in self._clients:
            _, oldest = self._clients.popitem(last=False)
            await oldest.close()
        client = self._client(asked.attempt)
        try:
            result = await client.request(
                asked.server,
                asked.method,
                cast(dict[str, object], asked.params),
                session=asked.session,
                run=asked.run,
            )
        except ConnectorError as error:
            return payload(McpAnswer(failure=_said(error)))
        if asked.method == "tools/call":
            result = await self._withhold(asked, result)
        return payload(McpAnswer(result=cast(dict[str, JsonValue], result)))

    async def _withhold(self, asked: McpRequest, result: dict[str, Any]) -> dict[str, Any]:
        """``result`` with each binary item replaced by the host's note: kept as an
        artifact over ``publish_mcp_binary`` under the call's ID, or withheld and said."""
        found = binary_contents("tools/call", result)
        if not found:
            return result
        tool = asked.params.get("name")
        sink = (
            ControlBinaries(self._channel, asked.attempt, asked.call)
            if asked.call is not None
            else None
        )
        notes = [
            await sink.keep(asked.server, str(tool), item)
            if sink is not None
            else withheld(item, "no call of the run to keep it for")
            for item in found
        ]
        kept, _ = withhold_binary("tools/call", result, lambda item: notes[item.index])
        return kept

    async def release(self, attempt: str) -> None:
        """Close an ended attempt's client."""
        client = self._clients.pop(attempt, None)
        if client is not None:
            await client.close()

    async def close(self) -> None:
        while self._clients:
            _, client = self._clients.popitem()
            await client.close()


def _said(error: ConnectorError) -> McpFailure:
    request = error.data.get("request")
    unconfirmed = error.data.get("unconfirmed_request")
    return McpFailure(
        kind=error.kind,
        message=error.message[:4096],
        request=request if isinstance(request, int) and request >= 1 else None,
        unconfirmed_request=unconfirmed
        if isinstance(unconfirmed, int) and unconfirmed >= 1
        else None,
    )


def _failed(kind: str, message: str) -> JsonValue:
    return payload(McpAnswer(failure=McpFailure(kind=kind, message=message)))
