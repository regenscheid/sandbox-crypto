# SPDX-License-Identifier: MIT
"""The confined runtime's check of a refusal that names a permission request (D98): the
``confirm_request`` capability, bound to the attempt's project on the host."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.requests import ControlRequestCheck
from sandbox_science.orchestrator.confinement.transport import (
    Capability,
    Channel,
    TransportError,
)
from sandbox_science.orchestrator.mcp_direct import HeldCall

ATTEMPT = "cap-" + "a" * 48
DIGEST = "b" * 64


@dataclass
class Recorded:
    """A host-side check that says yes to request 12 and remembers what it was asked."""

    asked: list[tuple[int, str, HeldCall]] = field(default_factory=list[tuple[int, str, HeldCall]])

    async def __call__(self, request: int, reason: str, call: HeldCall) -> bool:
        self.asked.append((request, reason, call))
        return request == 12


@pytest.fixture
async def linked() -> AsyncIterator[tuple[HostLoops, Channel, Recorded]]:
    recorded = Recorded()
    host: HostLoops | None = None

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        assert host is not None
        return await host.capability(kind, value)

    async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture refused capability")

    host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
    vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
    host = HostLoops(host_channel, frozenset({"crypto"}), requests=recorded)
    host.attempts[ATTEMPT] = cast(
        Any, SimpleNamespace(owner=SimpleNamespace(project="crypto"), confirmed={})
    )
    try:
        yield host, vm_channel, recorded
    finally:
        await vm_channel.close()
        await host_channel.close()


async def test_the_host_answers_for_the_attempts_project_only(
    linked: tuple[HostLoops, Channel, Recorded],
) -> None:
    host, vm, recorded = linked
    check = ControlRequestCheck(vm, ATTEMPT)
    # The runtime's word on the project is not sent: the attempt's is used.
    call = HeldCall("another-project", "papers", "tools/call", "send", DIGEST)
    assert await check(12, "approval_required", call) is True
    assert await check(13, "approval_required", call) is False
    assert [(request, held.project) for request, _, held in recorded.asked] == [
        (12, "crypto"),
        (13, "crypto"),
    ]
    # The host remembers what it confirmed: only those requests' holds are taken from the
    # runtime's relayed answers (connector_calls).
    # Each with the call it was confirmed for, under the attempt's project.
    assert host.attempts[ATTEMPT].confirmed == {
        12: HeldCall("crypto", "papers", "tools/call", "send", DIGEST)
    }


async def test_a_malformed_call_is_not_confirmed_and_never_asked(
    linked: tuple[HostLoops, Channel, Recorded],
) -> None:
    _, vm, recorded = linked
    check = ControlRequestCheck(vm, ATTEMPT)
    for reason, digest in (("approval_required", "not-a-digest"), ("other", DIGEST)):
        call = HeldCall("crypto", "papers", "tools/call", "send", digest)
        assert await check(12, reason, call) is False
    assert recorded.asked == []


async def test_an_unknown_attempt_or_a_host_without_a_check_is_refused(
    linked: tuple[HostLoops, Channel, Recorded],
) -> None:
    host, vm, _ = linked
    call = HeldCall("crypto", "papers", "tools/call", "send", DIGEST)
    with pytest.raises(TransportError):
        await ControlRequestCheck(vm, "cap-" + "c" * 48)(12, "approval_required", call)
    host.requests = None
    with pytest.raises(TransportError):
        await ControlRequestCheck(vm, ATTEMPT)(12, "approval_required", call)
