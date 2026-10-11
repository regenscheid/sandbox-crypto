# SPDX-License-Identifier: MIT
"""Repairs of LaTeX that KaTeX rejects, by the reviewer model (docs/WORKSPACE.md).

Adapted from cscience-crypto's pkg/platform/lib/math-repair.mjs, contributed under the
MIT License by its author.

The page asks ``POST /api/v0/math-repair`` for an expression that neither KaTeX nor the
page's deterministic repairs could render, with KaTeX's error. The service asks the
reviewer model the server is configured with, through the model gateway, and no other:
never a session's or a subagent's model, which wrote the expression. Each answer is
cleaned of code fences and delimiters and kept, so that an expression costs at most one
request while the server runs (a failure is kept for ``FAILURE_KEPT`` seconds). Limits:

- an expression of at most ``MAX_TEX`` characters, and KaTeX's error cut to ``MAX_ERROR``;
- ``MAX_OUTPUT_TOKENS`` tokens in an answer, which must come within ``TIMEOUT`` seconds;
- ``MAX_ACTIVE`` requests to the model at once and ``MAX_WAITING`` more waiting; beyond
  that, the page is told the server is busy;
- ``CACHE_SIZE`` answers kept;
- ``SESSION_LIMIT`` requests to the model for each browser session in any
  ``SESSION_WINDOW`` seconds, so that a page cannot spend without limit.

The key for the gateway is a secret: it goes only into the gateway's ``Authorization``
header, and never into a log line or an answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Final, cast

from sandbox_science.orchestrator.model import (
    ChatModel,
    Message,
    ModelError,
    OpenAICompatibleModel,
)

_log = logging.getLogger(__name__)

SYSTEM_PROMPT: Final = " ".join(
    (
        "You repair LaTeX math expressions so that KaTeX renders them.",
        "Input: the rendering mode, the KaTeX error, and one expression without delimiters.",
        "Output: only the corrected expression, with no delimiters, no code fences, no commentary.",
        "Rules: keep the mathematical meaning; use only commands KaTeX supports; drop "
        "\\label, \\ref, and document-level commands;",
        "balance braces and \\left/\\right pairs; in inline mode use aligned instead of "
        "align; escape bare %, #, and & used as text.",
        "If the expression cannot be repaired without changing its meaning, respond with "
        "exactly UNFIXABLE.",
    )
)
"""The original's instructions, its author's words, without the KaTeX version it named."""

MAX_TEX: Final = 4000
MAX_ERROR: Final = 500
MAX_OUTPUT_TOKENS: Final = 512
TIMEOUT: Final = 25.0
CACHE_SIZE: Final = 512
FAILURE_KEPT: Final = 600.0
MAX_ACTIVE: Final = 2
MAX_WAITING: Final = 8
SESSION_LIMIT: Final = 60
SESSION_WINDOW: Final = 3600.0
DISABLED: Final = frozenset({"0", "off", "none", "false", "disabled"})
"""Values of the reviewer setting that turn model repairs off."""

_FENCE = re.compile(r"```[A-Za-z]*\s*\n?(.*?)\n?```", re.DOTALL)
_UNFIXABLE = re.compile(r"UNFIXABLE\b", re.IGNORECASE)
_DELIMITERS: Final = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))


class RepairRequestError(ValueError):
    """A request the endpoint refuses."""


class SessionLimitError(Exception):
    """The browser session has asked for as many model repairs as it may for now."""


@dataclass(frozen=True, slots=True)
class RepairRequest:
    tex: str
    display: bool
    error: str
    """KaTeX's error, cut to ``MAX_ERROR`` characters."""


@dataclass(frozen=True, slots=True)
class RepairAnswer:
    enabled: bool
    """False when there is no reviewer model to ask; ``reason`` says why."""
    tex: str | None = None
    """The repaired expression, or None."""
    model: str | None = None
    reason: str | None = None
    """Why there is no repair."""
    cached: bool = False

    def to_json(self) -> dict[str, object]:
        if not self.enabled:
            return {"enabled": False, "tex": None, "reason": self.reason}
        return {
            "enabled": True,
            "model": self.model,
            "tex": self.tex,
            "reason": self.reason,
            "cached": self.cached,
        }


def parse_request(payload: object) -> RepairRequest:
    """The request in a body the page sent, or RepairRequestError."""
    if not isinstance(payload, dict):
        raise RepairRequestError("a math repair request must be an object")
    fields = cast(dict[str, object], payload)
    tex = fields.get("tex")
    if not isinstance(tex, str) or not tex.strip():
        raise RepairRequestError("a math repair request needs a non-empty tex string")
    if len(tex) > MAX_TEX:
        raise RepairRequestError(f"the expression exceeds {MAX_TEX} characters")
    error = fields.get("error")
    display = fields.get("display")
    return RepairRequest(
        tex=tex,
        display=display is True or display == "true",
        error=error[:MAX_ERROR] if isinstance(error, str) else "",
    )


def sanitize(text: str | None, original: str) -> str | None:
    """The model's answer as one expression, or None when it offers no usable repair."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if len(value) > len(original) * 3 + 300:  # beyond any repair, fences and delimiters
        return None
    fence = _FENCE.fullmatch(value)
    if fence:
        value = fence.group(1).strip()
    if _UNFIXABLE.match(value):
        return None
    for opening, closing in _DELIMITERS:
        if value.startswith(opening) and value.endswith(closing):
            inner = value[len(opening) :].lstrip()
            if inner.endswith(closing):
                inner = inner[: len(inner) - len(closing)]
            value = inner.strip()
            break
    if not value or len(value) > len(original) * 3 + 200 or value == original.strip():
        return None
    return value


def reviewer_alias(setting: str | None) -> str | None:
    """The reviewer model's alias, from its setting; None when unset or turned off."""
    alias = (setting or "").strip()
    if not alias or alias.lower() in DISABLED:
        return None
    return alias


def disabled_reason(setting: str | None) -> str:
    """Why model repairs are off, for the page."""
    alias = (setting or "").strip()
    return "disabled" if alias and alias.lower() in DISABLED else "no reviewer model configured"


def _cache_key(request: RepairRequest) -> str:
    mode = b"display" if request.display else b"inline"
    tex = request.tex.encode("utf-8", "surrogatepass")
    return hashlib.sha256(mode + b"\0" + tex).hexdigest()


def _messages(request: RepairRequest) -> list[Message]:
    mode = "display" if request.display else "inline"
    question = f"Mode: {mode}\nKaTeX error: {request.error or 'unknown'}\nExpression:\n"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question + request.tex},
    ]


class MathRepairService:
    """Repairs by one reviewer model, for every browser session of one server.

    ``model`` answers as the reviewer model whose alias ``reviewer`` names. Without both,
    model repairs are off, and each answer says why. ``secrets`` are kept out of the log.
    """

    def __init__(
        self,
        model: ChatModel | None = None,
        *,
        reviewer: str | None = None,
        secrets: tuple[str, ...] = (),
        close: Callable[[], Awaitable[None]] | None = None,
        timeout: float = TIMEOUT,
        session_limit: int = SESSION_LIMIT,
        session_window: float = SESSION_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reviewer = reviewer_alias(reviewer)
        self._model = model if self.reviewer is not None else None
        self._off = disabled_reason(reviewer)
        self._secrets = tuple(secret for secret in secrets if secret)
        self._close = close
        self._timeout = timeout
        self._session_limit = session_limit
        self._session_window = session_window
        self._clock = clock
        self._cache: OrderedDict[str, tuple[RepairAnswer, float | None]] = OrderedDict()
        self._in_flight: dict[str, asyncio.Task[RepairAnswer]] = {}
        self._slots = asyncio.Semaphore(MAX_ACTIVE)
        self._sessions: dict[str, deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._model is not None

    async def repair(self, request: RepairRequest, session: str) -> RepairAnswer:
        """The reviewer model's repair of ``request``, for the browser session ``session``.

        Raises SessionLimitError when the session may not ask the model now.
        """
        if self._model is None:
            return RepairAnswer(enabled=False, reason=self._off)
        key = _cache_key(request)
        cached = self._cached(key)
        if cached is not None:
            return replace(cached, cached=True)
        task = self._in_flight.get(key)
        if task is None:
            if len(self._in_flight) >= MAX_ACTIVE + MAX_WAITING:
                return RepairAnswer(enabled=True, model=self.reviewer, reason="busy")
            self._charge(session)
            task = asyncio.create_task(self._ask(key, request, self._model))
            self._in_flight[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        # A page that goes away does not cancel an answer other pages may be waiting for.
        return await asyncio.shield(task)

    def _cached(self, key: str) -> RepairAnswer | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        answer, expires = entry
        if expires is not None and expires <= self._clock():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return answer

    def _charge(self, session: str) -> None:
        now = self._clock()
        since = now - self._session_window
        if len(self._sessions) > 128:
            for stale in [
                name for name, calls in self._sessions.items() if not calls or calls[-1] <= since
            ]:
                del self._sessions[stale]
        calls = self._sessions.setdefault(session, deque())
        while calls and calls[0] <= since:
            calls.popleft()
        if len(calls) >= self._session_limit:
            minutes = self._session_window / 60
            raise SessionLimitError(
                f"this session has asked the reviewer model for {self._session_limit} repairs "
                f"in the last {minutes:g} minutes, as many as it may"
            )
        calls.append(now)

    async def _ask(self, key: str, request: RepairRequest, model: ChatModel) -> RepairAnswer:
        async with self._slots:
            answer = await self._call(request, model)
        kept = (
            None if answer.tex is not None or answer.reason == "no usable repair" else FAILURE_KEPT
        )
        self._cache[key] = (answer, None if kept is None else self._clock() + kept)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return answer

    async def _call(self, request: RepairRequest, model: ChatModel) -> RepairAnswer:
        try:
            async with asyncio.timeout(self._timeout):
                turn = await model.complete(_messages(request), ())
        except TimeoutError:
            reason = f"the reviewer model did not answer within {self._timeout:g} s"
        except ModelError as error:
            _log.warning("a math repair failed: %s", self._scrub(str(error)))
            if error.status is not None:
                reason = f"the model gateway answered HTTP {error.status}"
            else:
                reason = "the model gateway failed (the server's log says how)"
        else:
            repaired = sanitize(turn.text, request.tex)
            return RepairAnswer(
                enabled=True,
                tex=repaired,
                model=self.reviewer,
                reason=None if repaired else "no usable repair",
            )
        return RepairAnswer(enabled=True, model=self.reviewer, reason=reason)

    def _finished(self, key: str, task: asyncio.Task[RepairAnswer]) -> None:
        if self._in_flight.get(key) is task:
            del self._in_flight[key]
        if not task.cancelled() and (error := task.exception()) is not None:
            _log.error("a math repair failed: %s", self._scrub(repr(error)))

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[the gateway key]")
        return text

    async def close(self) -> None:
        """Stop the requests in flight, and close the gateway's connections."""
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._close is not None:
            await self._close()


def gateway_service(base_url: str, reviewer: str, api_key: str) -> MathRepairService:
    """Repairs by the model the alias ``reviewer`` names at the model gateway."""
    alias = reviewer_alias(reviewer)
    if alias is None:
        return MathRepairService(reviewer=reviewer)
    model = OpenAICompatibleModel(
        base_url, alias, api_key, timeout=TIMEOUT, max_tokens=MAX_OUTPUT_TOKENS
    )
    return MathRepairService(model, reviewer=alias, secrets=(api_key,), close=model.close)
