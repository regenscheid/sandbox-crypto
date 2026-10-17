# SPDX-License-Identifier: MIT
"""Stop-and-wait request framing; standalone sandbox payload, standard library only."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Awaitable, Callable
from typing import cast

MAX_FRAME = 64 * 1024
CHUNK_BYTES = 32 * 1024
_KEYS = {"transport", "transfer", "id", "seq", "final", "data"}


def frame(transfer: str, request: int, seq: int, final: bool, data: bytes) -> bytes:
    """One bounded frame; ACK identifies the exact same transfer, request and sequence."""
    value = {
        "transport": "chunk-v1",
        "transfer": transfer,
        "id": request,
        "seq": seq,
        "final": final,
        "data": base64.b64encode(data).decode("ascii"),
    }
    line = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    if len(line) > MAX_FRAME:
        raise ValueError("request transport frame is too large")
    return line


def acknowledgement(transfer: str, request: int, seq: int) -> bytes:
    """Receipt means consumed into bounded storage, not execution success."""
    return (
        json.dumps(
            {"type": "transport_ack", "transfer": transfer, "id": request, "seq": seq},
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate request key")
    return value


class Receiver:
    """One connection's bounded, strictly sequential assembly; never dispatch fragments."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.transfer: str | None = None
        self.request = 0
        self.sequence = 0
        self.data = bytearray()

    def consume(self, line: bytes) -> tuple[bytes, bytes | None]:
        """Validate and retain a frame; return ACK and a complete original request if final."""
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise ValueError("invalid request frame size")
        raw: object = json.loads(line, object_pairs_hook=_object)
        if not isinstance(raw, dict):
            raise ValueError("request frame is not an object")
        value = cast(dict[str, object], raw)
        if set(value) != _KEYS or value["transport"] != "chunk-v1":
            raise ValueError("invalid request frame fields")
        transfer, request, seq = value["transfer"], value["id"], value["seq"]
        final, encoded = value["final"], value["data"]
        if (
            not isinstance(transfer, str)
            or re.fullmatch(r"[0-9a-f]{32}", transfer) is None
            or type(request) is not int
            or not 0 < request < 2**63
            or type(seq) is not int
            or not 0 <= seq < 2**31
            or type(final) is not bool
            or not isinstance(encoded, str)
        ):
            raise ValueError("invalid request frame identity")
        if self.transfer is None:
            if seq != 0:
                raise ValueError("request frame sequence must start at zero")
            self.transfer, self.request = transfer, request
        if (transfer, request, seq) != (self.transfer, self.request, self.sequence):
            raise ValueError("request frame identity changed")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid request frame data") from exc
        if not chunk or len(chunk) > CHUNK_BYTES or len(self.data) + len(chunk) > self.maximum:
            raise ValueError("request assembly exceeds its bounds")
        self.data.extend(chunk)
        ack = acknowledgement(transfer, request, seq)
        self.sequence += 1
        if not final:
            return ack, None
        # A request may contain escaped newlines, never multiple protocol lines.
        if not self.data.endswith(b"\n") or b"\n" in self.data[:-1]:
            raise ValueError("invalid original request line")
        original: object = json.loads(self.data, object_pairs_hook=_object)
        if not isinstance(original, dict):
            raise ValueError("original request is not an object")
        message = cast(dict[str, object], original)
        if message.get("id") != request:
            raise ValueError("original request identity differs")
        if type(message.get("id")) is not int or not isinstance(message.get("op"), str):
            raise ValueError("invalid original request")
        result = bytes(self.data)
        self.transfer, self.request, self.sequence = None, 0, 0
        self.data.clear()
        return ack, result


async def receive(
    reader: asyncio.StreamReader,
    receiver: Receiver,
    send: Callable[[bytes], Awaitable[None]],
) -> tuple[bytes, bytes] | None:
    """Read to a validated final frame; its ACK is held until the caller drains it."""
    while line := await reader.readline():
        ack, original = receiver.consume(line)
        if original is not None:
            return original, ack
        await send(ack)
    if receiver.transfer is not None:
        raise ValueError("incomplete request transfer")
    return None
