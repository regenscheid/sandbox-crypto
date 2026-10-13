# SPDX-License-Identifier: MIT
"""Capped read-only provisioning transport using the operator-configured worker.

The ordinary WorkerClient API keeps its existing behavior. These observations copy
the configured target/headers, never follow redirects, and never use active status.
"""

from __future__ import annotations

from typing import Final, TypeVar

import aiohttp
from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

MAX_BYTES: Final = 64 * 1024
MAX_CHUNKS: Final = 256
CALL_TIMEOUT: Final = 5.0
T = TypeVar("T", bound=BaseModel)


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
        try:
            async with self.binding.session.get(
                self.binding.base_url + path,
                params=params,
                headers=dict(self.binding.headers),
                allow_redirects=False,
                auto_decompress=False,
                read_bufsize=8192,
                timeout=aiohttp.ClientTimeout(total=CALL_TIMEOUT),
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
                return model.model_validate_json(payload, strict=True)
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as error:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "worker observation transport is unavailable"
            ) from error
        except (ValidationError, ValueError, RecursionError) as error:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "worker observation has invalid metadata"
            ) from error
