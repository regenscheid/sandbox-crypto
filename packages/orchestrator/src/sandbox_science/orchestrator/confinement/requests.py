# SPDX-License-Identifier: MIT
"""The confined runtime's check of a refusal that names a permission request (D98).

An MCP client that reaches servers straight through OpenShell (``mcp_direct``) trusts a
refusal naming a request only when the control side confirms it, since a server can
write the same body. The runtime holds no worker token, so it asks the host over the
attempt's ``confirm_request`` capability: one request number, the refusal's reason and
the call's server, method, tool and parameters' digest. The host binds the project to the
attempt's own and answers yes or no from the policy middleware's records, through the
worker (``mcp_direct.WorkerRequestCheck``): the capability reads nothing else.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator.mcp_direct import HeldCall, RequestCheck

from .transport import Capability, Channel, TransportError
from .wire import RequestConfirmation, RequestConfirmed, payload, typed

Reason = Literal["approval_required", "approval_denied", "escalation_timed_out"]


class ControlRequestCheck:
    """A ``RequestCheck`` that asks the host for one attempt. The call's project is not
    sent: the host answers only for the attempt's."""

    def __init__(self, channel: Channel, attempt: str) -> None:
        self._channel, self._attempt = channel, attempt

    async def __call__(self, request: int, reason: str, call: HeldCall) -> bool:
        try:
            asked = RequestConfirmation(
                attempt=self._attempt,
                request=request,
                reason=cast(Reason, reason),
                server=call.server,
                method=call.method,
                tool=call.tool,
                arguments=call.arguments,
            )
        except ValidationError:
            return False
        reply = typed(
            RequestConfirmed,
            await self._channel.request(Capability.CONFIRM_REQUEST, payload(asked)),
        )
        return reply.confirmed


async def confirm(check: RequestCheck | None, project: str, value: JsonValue) -> JsonValue:
    """The host's answer to one ``confirm_request`` of an attempt of ``project``."""
    if check is None:
        raise TransportError("denied", "this host checks no permission requests")
    asked = typed(RequestConfirmation, value)
    call = HeldCall(project, asked.server, asked.method, asked.tool, asked.arguments)
    confirmed = await check(asked.request, asked.reason, call) is True
    return payload(RequestConfirmed(confirmed=confirmed))
