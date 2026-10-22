# SPDX-License-Identifier: MIT
"""Read-only provisioning observations from the operator-configured worker.

The observations use the WorkerClient's address, token and HTTP session, never follow
redirects, and never use active status. An answer is read up to 64 KiB and validated
against its contract model.
"""

from __future__ import annotations

from typing import Final, TypeVar

import aiohttp
from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.orchestrator.worker_client import (
    PROTOCOL,
    WorkerAPIError,
    WorkerClient,
    check_protocol,
)

from .context import TIMEOUTS, Timeouts

MAX_BYTES: Final = 64 * 1024
T = TypeVar("T", bound=BaseModel)


class ProvisionObserver:
    """Read-only requests through the configured worker's address, token and session."""

    def __init__(self, worker: WorkerClient, timeouts: Timeouts = TIMEOUTS) -> None:
        self.worker = worker
        self.timeouts = timeouts
        self.binding = worker.observation_transport()

    async def get(self, path: str, params: dict[str, str], model: type[T]) -> T:
        """Read one bounded strict DTO; request cancellation closes its HTTP owner."""
        return await self._get(path, params, model, self.timeouts.worker_call)

    async def get_diff(self, path: str, params: dict[str, str]) -> LockfileDiffPage:
        """Allow the worker's eight-second comparison budget, with the same byte cap."""
        return await self._get(path, params, LockfileDiffPage, self.timeouts.worker_comparison)

    async def get_packages(self, path: str, params: dict[str, str]) -> RevisionPackagePage:
        """Use comparison's bounded budget for the same shared worker read lane."""
        return await self._get(path, params, RevisionPackagePage, self.timeouts.worker_comparison)

    async def _get(self, path: str, params: dict[str, str], model: type[T], timeout: float) -> T:
        try:
            async with self.binding.session.get(
                self.binding.base_url + path,
                params=params,
                headers={**dict(self.binding.headers), **PROTOCOL},
                allow_redirects=False,
                auto_decompress=False,
                read_bufsize=8192,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                await check_protocol(response)
                payload = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    if len(payload) + len(chunk) > MAX_BYTES:
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
