# SPDX-License-Identifier: MIT
"""Small optional S3 boundary; no ETag or object metadata is an integrity proof."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from typing import BinaryIO, NotRequired, Protocol, TypedDict, Unpack, cast

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock

from .artifact_blobs import CHUNK_BYTES, Blob
from .artifact_config import S3Settings


class Body(Protocol):
    def read(self, amt: int) -> bytes: ...
    def close(self) -> None: ...


class Response(TypedDict):
    Body: Body
    ContentLength: int
    ContentRange: NotRequired[str]


class Location(TypedDict):
    Bucket: str
    Key: str


class Put(Location):
    Body: BinaryIO
    ContentLength: int
    IfNoneMatch: str


class Get(Location):
    Range: NotRequired[str]


class Client(Protocol):
    """Only these SDK operations are available to artifact storage."""

    def put_object(self, **kwargs: Unpack[Put]) -> object: ...
    def get_object(self, **kwargs: Unpack[Get]) -> Response: ...
    def head_object(self, **kwargs: Unpack[Location]) -> dict[str, object]: ...
    def close(self) -> None: ...


def connect(
    settings: S3Settings, request_timeout: float
) -> tuple[Client, tuple[type[Exception], ...]]:
    """Load the optional SDK lazily, without injecting credentials or logging them."""
    try:
        boto = importlib.import_module("boto3")
        configuration = importlib.import_module("botocore.config")
        exceptions = importlib.import_module("botocore.exceptions")
    except ImportError:
        raise OSError("artifact S3 backing requires sandbox-science-orchestrator[s3]") from None
    errors = (
        cast(type[Exception], exceptions.BotoCoreError),
        cast(type[Exception], exceptions.ClientError),
    )
    config = cast(Callable[..., object], configuration.Config)(
        signature_version="s3v4",
        connect_timeout=min(5, request_timeout),
        read_timeout=request_timeout,
        retries={"total_max_attempts": 1},
        s3={"addressing_style": settings.addressing_style},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    try:
        client = cast(Callable[..., Client], boto.client)(
            "s3", region_name=settings.region, endpoint_url=settings.endpoint, config=config
        )
    except errors:
        raise OSError("artifact object-store connection failed") from None
    return client, errors


def _precondition(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    metadata = cast(dict[str, object], response).get("ResponseMetadata")
    return (
        isinstance(metadata, dict)
        and cast(dict[str, object], metadata).get("HTTPStatusCode") == 412
    )


class S3Backing:
    """Immutable conditional writes and independently verified, bounded reads."""

    def __init__(
        self,
        settings: S3Settings,
        *,
        clock: Clock = SYSTEM_CLOCK,
        timeout: float = 300,
        request_timeout: float = 15,
    ) -> None:
        self.settings, self.clock = settings, clock
        self.timeout, self.request_timeout = timeout, request_timeout

    @contextmanager
    def _client(self) -> Generator[tuple[Client, tuple[type[Exception], ...]]]:
        client, errors = connect(self.settings, self.request_timeout)
        try:
            yield client, errors
        except errors:
            raise OSError("artifact object-store request failed") from None
        finally:
            client.close()

    def retain(self, blob: Blob, source: BinaryIO) -> None:
        """Never replace an existing key; identical retries verify the existing bytes."""
        with self._client() as (client, errors):
            source.seek(0)
            try:
                client.put_object(
                    Bucket=self.settings.bucket,
                    Key=self.settings.key(blob.sha256),
                    Body=source,
                    ContentLength=blob.size,
                    IfNoneMatch="*",
                )
            except errors as error:
                if not _precondition(error):
                    raise
        self.copy(blob)

    def copy(self, blob: Blob, target: BinaryIO | None = None) -> None:
        """Verify a complete read, optionally spooling it to a caller-owned file."""
        deadline = self.clock.monotonic() + self.timeout
        with self._client() as (client, _):
            response = client.get_object(
                Bucket=self.settings.bucket, Key=self.settings.key(blob.sha256)
            )
            with closing(response["Body"]) as body:
                if response.get("ContentLength") != blob.size:
                    raise ValueError("artifact object-store size differs")
                size, digest = 0, hashlib.sha256()
                while True:
                    if self.clock.monotonic() >= deadline:
                        raise TimeoutError("artifact object-store read exceeded its deadline")
                    chunk = body.read(min(CHUNK_BYTES, blob.size - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > blob.size:
                        raise ValueError("artifact object-store body exceeds pinned size")
                    digest.update(chunk)
                    if target is not None:
                        target.write(chunk)
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError("artifact object-store read exceeded its deadline")
                if (size, digest.hexdigest()) != (blob.size, blob.sha256):
                    raise ValueError("artifact object-store size/hash differs")

    def prefix(self, blob: Blob, limit: int) -> bytes:
        """Read only a provenance prefix, matching the local ingest-verified semantics."""
        amount = min(limit, blob.size)
        deadline = self.clock.monotonic() + self.timeout
        with self._client() as (client, _):
            if not amount:
                if (
                    client.head_object(
                        Bucket=self.settings.bucket, Key=self.settings.key(blob.sha256)
                    ).get("ContentLength")
                    != blob.size
                ):
                    raise ValueError("artifact object-store size differs")
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError("artifact object-store prefix exceeded its deadline")
                return b""
            response = client.get_object(
                Bucket=self.settings.bucket,
                Key=self.settings.key(blob.sha256),
                Range=f"bytes=0-{amount - 1}",
            )
            with closing(response["Body"]) as body:
                if response.get("ContentLength") != amount or response.get("ContentRange") != (
                    f"bytes 0-{amount - 1}/{blob.size}"
                ):
                    raise ValueError("artifact object-store prefix range differs")
                data = body.read(amount + 1)
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError("artifact object-store prefix exceeded its deadline")
                if len(data) != amount:
                    raise ValueError("artifact object-store prefix length differs")
                return data
