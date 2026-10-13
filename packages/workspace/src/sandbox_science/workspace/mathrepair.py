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

from .repair_prompt import RepairProvenance, assemble_repair, provenance_json

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
FAILURE_KEPT: Final = 3600.0
MAX_ACTIVE: Final = 2
MAX_WAITING: Final = 8
SESSION_LIMIT: Final = 60
SESSION_WINDOW: Final = 3600.0
MAX_ANSWER_BYTES: Final = 1024 * 1024
"""The largest answer read from the gateway; 512 tokens need a few kilobytes."""
DISABLED: Final = frozenset({"0", "off", "none", "false", "disabled"})
"""Values of the reviewer setting that turn model repairs off."""

_UNFIXABLE = re.compile(r"UNFIXABLE\b", re.IGNORECASE)
_DELIMITERS: Final = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))
_FENCE_TAGS: Final = frozenset({"latex", "tex", "math", "katex"})
"""Info strings a model may put before the expression on a fence of one line."""
_KEY = re.compile(r"[\x21-\x7e]+")
"""A key: one line of visible ASCII, as an Authorization header can carry it."""


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
    project: str = ""
    macro_context: str = ""
    """Bounded cache identity only; it grants no document/setting access."""


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
    input_provenance: RepairProvenance | None = None
    """Producing host's static input identity; absent remains unknown, never backfilled."""

    def to_json(self) -> dict[str, object]:
        if not self.enabled:
            return {"enabled": False, "tex": None, "reason": self.reason}
        answer: dict[str, object] = {
            "enabled": True,
            "model": self.model,
            "tex": self.tex,
            "reason": self.reason,
            "cached": self.cached,
        }
        if self.input_provenance is not None:
            answer["input_provenance"] = provenance_json(self.input_provenance)
        return answer


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
    project, context = fields.get("project", ""), fields.get("macro_context", "")
    if (project or context) and (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
        or not isinstance(context, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", context) is None
    ):
        raise RepairRequestError("invalid project macro cache context")
    if not isinstance(project, str) or not isinstance(context, str):
        raise RepairRequestError("invalid project macro cache context")
    return RepairRequest(
        tex=tex,
        display=display is True or display == "true",
        error=error[:MAX_ERROR] if isinstance(error, str) else "",
        project=project,
        macro_context=context,
    )


def _unfenced(value: str) -> str:
    """``value`` out of its code fence, if it is one: the original's pattern, without a
    regular expression, whose backtracking took a second on a fence and 12,000 spaces.

    An info string such as ``latex`` is dropped: a word on the opening fence's own line,
    and on a fence of one line (```` ```latex x^2``` ````) a known one before the
    expression. The original's pattern dropped any letters there, so ```` ```x^2``` ````
    lost its ``x``.
    """
    if len(value) < 6 or not value.startswith("```") or not value.endswith("```"):
        return value
    inner = value[3:-3]
    first, newline, rest = inner.partition("\n")
    if newline:
        tag = first.strip()
        if not tag or (tag.isascii() and tag.isalpha()):
            inner = rest
    else:
        words = inner.split(None, 1)
        if words and words[0].lower() in _FENCE_TAGS:
            inner = words[1] if len(words) == 2 else ""
    return inner.strip()


def sanitize(text: str | None, original: str) -> str | None:
    """The model's answer as one expression, or None when it offers no usable repair."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if len(value) > len(original) * 3 + 300:  # beyond any repair, fences and delimiters
        return None
    value = _unfenced(value)
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
    scope = (request.project + "\0" + request.macro_context).encode()
    return hashlib.sha256(scope + b"\0" + mode + b"\0" + tex).hexdigest()


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
        """The model's answer; any failure is an answer too, kept for ``FAILURE_KEPT``."""
        try:
            wanted = assemble_repair(SYSTEM_PROMPT, request.tex, request.display, request.error)
            messages = _messages(request)
            wanted.verify(messages, ())
        except Exception:
            # No gateway boundary was entered. Never echo private input or metadata errors.
            return RepairAnswer(
                enabled=True, model=self.reviewer, reason="repair input metadata unavailable"
            )
        try:
            async with asyncio.timeout(self._timeout):
                turn = await model.complete(messages, ())
        except TimeoutError:
            reason = f"the reviewer model did not answer within {self._timeout:g} s"
        except Exception as error:  # a gateway error, or anything else: never a 500 to retry
            # Only the status or the kind of failure: the gateway's text can echo the key.
            _log.warning("a math repair failed: %s", self._scrub(_brief(error)))
            if isinstance(error, ModelError) and error.status is not None:
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
                input_provenance=wanted.provenance,
            )
        return RepairAnswer(
            enabled=True, model=self.reviewer, reason=reason, input_provenance=wanted.provenance
        )

    def _finished(self, key: str, task: asyncio.Task[RepairAnswer]) -> None:
        if self._in_flight.get(key) is task:
            del self._in_flight[key]
        if not task.cancelled() and (error := task.exception()) is not None:
            _log.error("a math repair failed: %s", self._scrub(_brief(error)))

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


def _brief(error: BaseException) -> str:
    """A failure in a few words, without the gateway's text: its status, or its kind."""
    if isinstance(error, ModelError):
        if error.status is not None:
            return f"the model gateway answered HTTP {error.status}"
        cause = error.__cause__
        kind = str(error).split(":", 1)[0]
        return f"{kind} ({type(cause).__name__})" if cause is not None else kind
    return type(error).__name__


def gateway_service(base_url: str, reviewer: str, api_key: str) -> MathRepairService:
    """Repairs by the model the alias ``reviewer`` names at the model gateway.

    ValueError when ``api_key`` is not one line of visible ASCII, which every request's
    Authorization header would then refuse.
    """
    alias = reviewer_alias(reviewer)
    if alias is None:
        return MathRepairService(reviewer=reviewer)
    if not _KEY.fullmatch(api_key):
        raise ValueError("the gateway key must be one line of visible ASCII characters")
    model = OpenAICompatibleModel(
        base_url,
        alias,
        api_key,
        timeout=TIMEOUT,
        max_tokens=MAX_OUTPUT_TOKENS,
        max_response_bytes=MAX_ANSWER_BYTES,
    )
    return MathRepairService(model, reviewer=alias, secrets=(api_key,), close=model.close)
