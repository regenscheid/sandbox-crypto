# SPDX-License-Identifier: MIT
"""Our LiteLLM gateway under Pydantic AI: an HTTP tap that keeps what each model call
sent and the cost LiteLLM reported for it, and a wrapper model that hands both to the run.

Pydantic AI exposes neither the request body nor the response headers of a successful
call, so they are read where the HTTP client sees them: httpx2 event hooks. A context
variable, set around each model request by a wrapper model, ties the exchange to the call.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

COST_HEADER = "x-litellm-response-cost"


@dataclass
class Exchange:
    """One HTTP exchange with the gateway, as the client sent and received it."""

    method: str
    url: str
    body: dict[str, Any] | None
    status: int | None = None
    cost: float | None = None


@dataclass
class Slot:
    exchanges: list[Exchange] = field(default_factory=list[Exchange])


_current: contextvars.ContextVar[Slot | None] = contextvars.ContextVar("gateway_slot", default=None)


def _cost(value: str | None) -> float | None:
    try:
        found = float(value) if value is not None else None
    except ValueError:
        return None
    return found if found is not None and 0 <= found < float("inf") else None


async def _on_request(request: httpx2.Request) -> None:
    slot = _current.get()
    if slot is None:
        return
    try:
        body = json.loads(request.content) if request.content else None
    except ValueError:
        body = None
    slot.exchanges.append(Exchange(request.method, str(request.url), body))


async def _on_response(response: httpx2.Response) -> None:
    slot = _current.get()
    if slot is None or not slot.exchanges:
        return
    slot.exchanges[-1].status = response.status_code
    slot.exchanges[-1].cost = _cost(response.headers.get(COST_HEADER))


def gateway_client(**kwargs: Any) -> httpx2.AsyncClient:
    """An HTTP client for the provider SDKs that taps every exchange."""
    return httpx2.AsyncClient(
        event_hooks={"request": [_on_request], "response": [_on_response]}, **kwargs
    )


@dataclass
class CallRecord:
    """What one model call sent, exactly, and what the gateway said it cost."""

    sent: list[dict[str, Any] | None]
    """Each HTTP attempt's body, retries included."""
    statuses: list[int | None]
    cost: float | None
    model_name: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


class GatewayRecorder:
    """The ``CallRecord`` of each model call, in order."""

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    def wrap(self, model: Model) -> GatewayModel:
        return GatewayModel(model, self)

    def record(self, slot: Slot, response: ModelResponse | None) -> float | None:
        costs = [e.cost for e in slot.exchanges if e.cost is not None]
        cost = costs[-1] if costs else None
        usage = response.usage if response is not None else None
        self.calls.append(
            CallRecord(
                sent=[e.body for e in slot.exchanges],
                statuses=[e.status for e in slot.exchanges],
                cost=cost,
                model_name=response.model_name if response is not None else None,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cache_read_tokens=usage.cache_read_tokens if usage else 0,
            )
        )
        return cost


class GatewayModel(WrapperModel):
    """A model whose every call leaves a ``CallRecord``, with the gateway's cost also put in
    the response's ``provider_details``, where it persists with the message history.

    It wraps the model, not the request hook: a streamed request is opened in the task
    that consumes the stream, not in the one ``wrap_model_request`` runs in (Pydantic AI
    2.56, ``_agent_graph.model_request_stream``), so only the model sees the HTTP call.
    """

    def __init__(self, wrapped: Model, recorder: GatewayRecorder) -> None:
        super().__init__(wrapped)
        self.recorder = recorder

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        slot = Slot()
        token = _current.set(slot)
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except Exception:
            self.recorder.record(slot, None)
            raise
        finally:
            _current.reset(token)
        cost = self.recorder.record(slot, response)
        details = {**(response.provider_details or {}), "litellm_cost": cost}
        return replace(response, provider_details=details)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        slot = Slot()
        token = _current.set(slot)
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                _current.reset(token)
                token = None
                yield stream
        finally:
            if token is not None:
                _current.reset(token)
        cost = self.recorder.record(slot, stream.get())
        stream.provider_details = {**(stream.provider_details or {}), "litellm_cost": cost}
