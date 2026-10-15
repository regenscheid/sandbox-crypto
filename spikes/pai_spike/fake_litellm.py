# SPDX-License-Identifier: MIT
"""A fake LiteLLM proxy for the Pydantic AI spike: the three endpoints the harness could
use (OpenAI chat completions, Anthropic messages, OpenAI responses), streaming and not,
with the cost header LiteLLM sets and the errors it sends at a model's context limit.

Every request is kept as it arrived (path, headers, body), so a test can see exactly
what a client sent. Replies come from one script shared by the endpoints.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

Json = dict[str, Any]
COST_HEADER = "x-litellm-response-cost"

CONTEXT_ERROR_CHAT: Json = {
    "error": {
        "message": "litellm.ContextWindowExceededError: litellm.BadRequestError: "
        "This model's maximum context length is 8192 tokens.",
        "type": None,
        "param": None,
        "code": "400",
    }
}
"""LiteLLM proxy's answer at the context limit, as its /chat/completions sends it."""

CONTEXT_ERROR_ANTHROPIC: Json = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "prompt is too long: 210000 tokens > 200000 maximum",
    },
}
"""Anthropic's own answer at the context limit, which LiteLLM's /v1/messages passes on."""


@dataclass
class Reply:
    text: str | None = None
    calls: Sequence[tuple[str, Json]] = ()
    cost: str | None = "0.00125"
    status: int = 200
    error: Json | None = None
    input_tokens: int = 100
    output_tokens: int = 10
    cached_tokens: int = 0
    compaction: str | None = None
    """An Anthropic compaction block's text, or an OpenAI compaction item's content."""
    call_ids: Sequence[str] = ()
    """The tool calls' IDs, when the script chooses them."""


@dataclass
class Seen:
    path: str
    headers: dict[str, str]
    body: Json


@dataclass
class FakeLiteLLM:
    script: list[Reply] = field(default_factory=list[Reply])
    seen: list[Seen] = field(default_factory=list[Seen])
    url: str = ""
    responder: Callable[[str, Json], Reply] | None = None
    """Answers from the request instead of the script: (path, body) to a reply."""
    _ids: itertools.count[int] = field(default_factory=itertools.count)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        for prefix in ("", "/v1"):
            app.router.add_post(f"{prefix}/chat/completions", self.chat)
            app.router.add_post(f"{prefix}/responses", self.responses)
        app.router.add_post("/v1/messages", self.messages)
        return app

    async def _take(self, request: web.Request) -> tuple[Json, Reply]:
        body = await request.json()
        self.seen.append(
            Seen(request.path, {k.lower(): v for k, v in request.headers.items()}, body)
        )
        if self.responder is not None:
            return body, self.responder(request.path, body)
        reply = self.script.pop(0) if self.script else Reply("(the script has ended)")
        return body, reply

    def _headers(self, reply: Reply) -> dict[str, str]:
        return {} if reply.cost is None else {COST_HEADER: reply.cost}

    def _id(self, kind: str) -> str:
        return f"{kind}_{next(self._ids)}"

    # --- OpenAI chat completions ---------------------------------------------------

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body, reply = await self._take(request)
        if reply.status != 200:
            return web.json_response(reply.error, status=reply.status, headers=self._headers(reply))
        ids = list(reply.call_ids) or [self._id("call") for _ in reply.calls]
        calls = [
            {
                "id": ids[n],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for n, (name, args) in enumerate(reply.calls)
        ]
        usage = {
            "prompt_tokens": reply.input_tokens,
            "completion_tokens": reply.output_tokens,
            "total_tokens": reply.input_tokens + reply.output_tokens,
            "prompt_tokens_details": {"cached_tokens": reply.cached_tokens},
        }
        finish = "tool_calls" if calls else "stop"
        model = f"resolved-{body['model']}"
        if not body.get("stream"):
            message: Json = {"role": "assistant", "content": reply.text}
            if calls:
                message["tool_calls"] = calls
            return web.json_response(
                {
                    "id": self._id("chatcmpl"),
                    "object": "chat.completion",
                    "created": 1760000000,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                },
                headers=self._headers(reply),
            )
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", **self._headers(reply)}
        )
        await response.prepare(request)
        cid = self._id("chatcmpl")

        async def send(delta: Json, finish_reason: str | None = None, **extra: Any) -> None:
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": 1760000000,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                **extra,
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())

        await send({"role": "assistant", "content": ""})
        if reply.text:
            for piece in (reply.text[: len(reply.text) // 2], reply.text[len(reply.text) // 2 :]):
                await send({"content": piece})
        for index, item in enumerate(calls):
            arguments = item["function"]["arguments"]
            await send(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": item["id"],
                            "type": "function",
                            "function": {"name": item["function"]["name"], "arguments": ""},
                        }
                    ]
                }
            )
            await send({"tool_calls": [{"index": index, "function": {"arguments": arguments}}]})
        await send({}, finish)
        if (body.get("stream_options") or {}).get("include_usage"):
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": 1760000000,
                "model": model,
                "choices": [],
                "usage": usage,
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    # --- Anthropic messages --------------------------------------------------------

    async def messages(self, request: web.Request) -> web.StreamResponse:
        body, reply = await self._take(request)
        if reply.status != 200:
            return web.json_response(reply.error, status=reply.status, headers=self._headers(reply))
        content: list[Json] = []
        if reply.compaction is not None:
            content.append({"type": "compaction", "content": reply.compaction})
        if reply.text:
            content.append({"type": "text", "text": reply.text})
        for name, args in reply.calls:
            content.append(
                {"type": "tool_use", "id": self._id("toolu"), "name": name, "input": args}
            )
        usage: Json = {
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cache_read_input_tokens": reply.cached_tokens,
            "cache_creation_input_tokens": 0,
        }
        stop = "tool_use" if reply.calls else "end_turn"
        message: Json = {
            "id": self._id("msg"),
            "type": "message",
            "role": "assistant",
            "model": f"resolved-{body['model']}",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return web.json_response(message, headers=self._headers(reply))
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", **self._headers(reply)}
        )
        await response.prepare(request)

        async def event(kind: str, data: Json) -> None:
            await response.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        await event(
            "message_start",
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
        )
        for index, block in enumerate(content):
            if block["type"] == "text":
                await event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index,
                     "content_block": {"type": "text", "text": ""}},
                )  # fmt: skip
                await event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "text_delta", "text": block["text"]}},
                )  # fmt: skip
            elif block["type"] == "tool_use":
                await event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index,
                     "content_block": {**block, "input": {}}},
                )  # fmt: skip
                await event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "input_json_delta",
                               "partial_json": json.dumps(block["input"])}},
                )  # fmt: skip
            else:
                await event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": block},
                )
            await event("content_block_stop", {"type": "content_block_stop", "index": index})
        await event(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": reply.output_tokens}},
        )  # fmt: skip
        await event("message_stop", {"type": "message_stop"})
        await response.write_eof()
        return response

    # --- OpenAI responses ----------------------------------------------------------

    async def responses(self, request: web.Request) -> web.StreamResponse:
        body, reply = await self._take(request)
        if reply.status != 200:
            return web.json_response(reply.error, status=reply.status, headers=self._headers(reply))
        output: list[Json] = []
        if reply.compaction is not None:
            output.append(
                {"type": "compaction", "id": self._id("cmp"), "encrypted_content": reply.compaction}
            )
        for name, args in reply.calls:
            output.append(
                {
                    "type": "function_call",
                    "id": self._id("fc"),
                    "call_id": self._id("call"),
                    "name": name,
                    "arguments": json.dumps(args),
                    "status": "completed",
                }
            )
        if reply.text:
            output.append(
                {
                    "type": "message",
                    "id": self._id("msg"),
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": reply.text, "annotations": []}],
                }
            )
        data = {
            "id": self._id("resp"),
            "object": "response",
            "created_at": 1760000000,
            "status": "completed",
            "model": f"resolved-{body['model']}",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "total_tokens": reply.input_tokens + reply.output_tokens,
                "input_tokens_details": {"cached_tokens": reply.cached_tokens},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        if not body.get("stream"):
            return web.json_response(data, headers=self._headers(reply))
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", **self._headers(reply)}
        )
        await response.prepare(request)
        number = itertools.count()

        async def event(kind: str, payload: Json) -> None:
            payload = {"type": kind, "sequence_number": next(number), **payload}
            await response.write(f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode())

        await event(
            "response.created", {"response": {**data, "status": "in_progress", "output": []}}
        )
        for index, item in enumerate(output):
            await event("response.output_item.added", {"output_index": index, "item": item})
            await event("response.output_item.done", {"output_index": index, "item": item})
        await event("response.completed", {"response": data})
        await response.write_eof()
        return response
