# SPDX-License-Identifier: MIT
"""The images ``view_image`` puts in a run's conversation, and how each model client
sends them (docs/specs/figures.md, FG-36, FG-37).

The conversation holds no image bytes. An image's message is a framed user message that
names the file, its revision and its SHA-256, the line that stands for it everywhere,
with a token under ``IMAGE`` that this process maps to the bytes while the run that
viewed it goes on. A client sends the bytes of the newest ``IN_VIEW`` images whose tokens
it still holds, in its API's form, and the line alone for the rest: so an image stays in
the view until a fold rebuilds the view from the archive, which keeps the line, or the
run ends and forgets its tokens; later runs see the line. The archive never holds bytes.
"""

from __future__ import annotations

import base64
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

IMAGE: Final = "image"
"""The key of an image's token on its message."""
IN_VIEW: Final = 8
"""The images a model call carries at most; older ones go as their line (FG-36)."""
KEPT_BYTES: Final = 128 * 1024 * 1024
"""What the process keeps of live images in all; past it, the oldest go as lines."""

Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Image:
    media_type: str
    data: bytes
    width: int = 0
    height: int = 0

    @property
    def tokens(self) -> int:
        """About what a model counts the image as: a token for every 750 pixels, the
        rule the providers publish, for a budget's estimate before the call reports."""
        return -(-self.width * self.height // 750)

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.base64}"


_lock = threading.Lock()
_live: OrderedDict[str, Image] = OrderedDict()


def keep(image: Image) -> str:
    """A new token for ``image``, live until ``forget``."""
    token = secrets.token_hex(16)
    with _lock:
        _live[token] = image
        while sum(len(kept.data) for kept in _live.values()) > KEPT_BYTES and len(_live) > 1:
            _live.popitem(last=False)
    return token


def forget(tokens: Iterable[str]) -> None:
    """The run that viewed these images ended: they go as their lines from now on."""
    with _lock:
        for token in tokens:
            _live.pop(token, None)


def carried(messages: Sequence[Message]) -> dict[int, Image]:
    """The images the next call carries, by ``id`` of their message: the newest
    ``IN_VIEW`` whose tokens are live."""
    found: dict[int, Image] = {}
    with _lock:
        for message in reversed(messages):
            token = message.get(IMAGE)
            image = _live.get(token) if isinstance(token, str) else None
            if image is not None:
                found[id(message)] = image
                if len(found) == IN_VIEW:
                    break
    return found


def tokens(messages: Sequence[Message]) -> int:
    """About the tokens the images the next call carries count as."""
    return sum(image.tokens for image in carried(messages).values())


def chat(messages: Sequence[Message]) -> list[Message]:
    """The conversation as chat completions take it: an image's message as its line and
    the image (``image_url``), or its line alone; no message keeps the token."""
    images = carried(messages)
    sent: list[Message] = []
    for message in messages:
        if IMAGE not in message:
            sent.append(message)
            continue
        plain = {key: value for key, value in message.items() if key != IMAGE}
        image = images.get(id(message))
        if image is not None:
            plain["content"] = [
                {"type": "text", "text": str(message.get("content") or "")},
                {"type": "image_url", "image_url": {"url": image.data_url}},
            ]
        sent.append(plain)
    return sent
