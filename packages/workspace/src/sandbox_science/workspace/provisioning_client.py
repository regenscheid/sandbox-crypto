# SPDX-License-Identifier: MIT
"""Capped read-only provisioning transport using the operator-configured worker.

The ordinary WorkerClient API keeps its existing behavior. These observations copy
the configured target/headers, never follow redirects, and never use active status.
"""

from __future__ import annotations

import json
from typing import Final, Never, TypeVar

import aiohttp
from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

MAX_BYTES: Final = 64 * 1024
MAX_CHUNKS: Final = 256
CALL_TIMEOUT: Final = 5.0
DIFF_TIMEOUT: Final = 9.0
T = TypeVar("T", bound=BaseModel)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("duplicate observation field")
        value[key] = child
    return value


def _invalid_constant(value: str) -> Never:
    raise ValueError("invalid observation number")


class ProvisionObserver:
    """One exact configured binding and an owned read-only request at a time."""

    def __init__(self, worker: WorkerClient) -> None:
        self.worker = worker
        self.binding = worker.observation_transport()

    def current(self) -> bool:
        """Whether this same client still owns the original configured transport."""
        return self.worker.matches_observation_transport(self.binding)

    async def get(self, path: str, params: dict[str, str], model: type[T]) -> T:
        """Read one bounded strict DTO; request cancellation closes its HTTP owner."""
        return await self._get(path, params, model, CALL_TIMEOUT)

    async def get_diff(self, path: str, params: dict[str, str]) -> LockfileDiffPage:
        """Allow the worker's eight-second comparison budget, with the same byte cap."""
        return await self._get(path, params, LockfileDiffPage, DIFF_TIMEOUT)

    async def _get(self, path: str, params: dict[str, str], model: type[T], timeout: float) -> T:
        try:
            async with self.binding.session.get(
                self.binding.base_url + path,
                params=params,
                headers=dict(self.binding.headers),
                allow_redirects=False,
                auto_decompress=False,
                read_bufsize=8192,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                # A claimed length is only an early refusal, never a substitute
                # for the independent streaming byte and chunk counters.
                if response.content_length is not None and response.content_length > MAX_BYTES:
                    raise WorkerAPIError(
                        ErrorKind.UNAVAILABLE, "worker observation exceeds its bound"
                    )
                payload = bytearray()
                chunks = 0
                async for chunk in response.content.iter_chunked(8192):
                    chunks += 1
                    if chunks > MAX_CHUNKS or len(payload) + len(chunk) > MAX_BYTES:
                        raise WorkerAPIError(
                            ErrorKind.UNAVAILABLE, "worker observation exceeds its bound"
                        )
                    payload.extend(chunk)
                if response.status != 200:
                    kind, message = {
                        404: (
                            ErrorKind.NOT_FOUND,
                            "no retained provisioning observation in this scope",
                        ),
                        409: (
                            ErrorKind.CONFLICT,
                            "the selected provisioning record changed; refresh it",
                        ),
                        422: (
                            ErrorKind.UNSUPPORTED,
                            "the worker does not support provisioning observations",
                        ),
                    }.get(
                        response.status,
                        (ErrorKind.UNAVAILABLE, "worker observation is unavailable"),
                    )
                    raise WorkerAPIError(kind, message, response.status)
                if response.content_type != "application/json" or response.headers.get(
                    "Content-Encoding"
                ):
                    raise WorkerAPIError(
                        ErrorKind.UNAVAILABLE, "worker observation is not plain JSON"
                    )
                # Check raw object uniqueness before Pydantic can normalize an
                # ambiguous action/scope/pin to its last occurrence. Parsing is
                # independently bounded above; retain the original JSON bytes
                # for strict JSON-mode tuple/date/enum validation afterward.
                json.loads(
                    payload.decode("utf-8", "strict"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
                return model.model_validate_json(payload, strict=True)
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as error:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "worker observation transport is unavailable"
            ) from error
        except (ValidationError, ValueError, RecursionError) as error:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "worker observation has invalid metadata"
            ) from error
