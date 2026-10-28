# SPDX-License-Identifier: MIT
"""Optional credential-chain S3 I/O for immutable checkpoint chunks and manifests."""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import stat
import tomllib
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from types import TracebackType
from typing import (
    Annotated,
    BinaryIO,
    Literal,
    NotRequired,
    Protocol,
    Self,
    TypedDict,
    Unpack,
    cast,
)
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.model import StrictContract

from .checkpoint_stream import CHUNK


class Storage(StrictContract):
    bucket: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
    prefix: Annotated[str, Field(max_length=512)] = "sandbox-science"
    region: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")] = "us-east-1"
    endpoint: Annotated[str, Field(max_length=2048)] | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"

    @model_validator(mode="after")
    def _destination(self) -> Storage:
        if self.prefix and (
            any(p in ("", ".", "..") for p in self.prefix.split("/"))
            or any(not (c.isascii() and (c.isalnum() or c in "-._/")) for c in self.prefix)
        ):
            raise ValueError("invalid checkpoint key prefix")
        if self.endpoint is not None:
            url = urlsplit(self.endpoint)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or url.path not in ("", "/")
                or any(c.isspace() or ord(c) < 32 for c in self.endpoint)
            ):
                raise ValueError("invalid checkpoint endpoint")
            _ = url.port
        return self

    def key(self, kind: Literal["chunks", "manifests"], digest: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid checkpoint object digest")
        return "/".join(p for p in (self.prefix, "workspace-checkpoints", kind, digest) if p)


def load(path: Path) -> Storage:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 8192
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("checkpoint storage settings must be private and bounded")
        try:
            return Storage.model_validate(tomllib.loads(handle.read(8193).decode()))
        except ValueError:
            # A validation error could echo an accidentally supplied credential field.
            raise ValueError("checkpoint storage settings are invalid") from None


def parse(document: str) -> Storage:
    """Destination settings given inline as JSON, as a checkpoint Job's environment carries
    them (no file of its own user's to check); credential fields are refused as in ``load``."""
    if len(document.encode()) > 8192:
        raise ValueError("checkpoint storage settings exceed their bound")
    try:
        return Storage.model_validate_json(document)
    except ValueError:
        raise ValueError("checkpoint storage settings are invalid") from None


class Body(Protocol):
    def read(self, amt: int) -> bytes: ...
    def close(self) -> None: ...


class Object(TypedDict):
    Body: Body
    ContentLength: int
    ETag: NotRequired[str]


class Location(TypedDict):
    Bucket: str
    Key: str


class Put(Location):
    Body: BinaryIO
    ContentLength: int
    IfNoneMatch: str


class CopySource(TypedDict):
    Bucket: str
    Key: str


class Copy(Location):
    CopySource: CopySource
    CopySourceIfMatch: str


class Listing(TypedDict, total=False):
    Bucket: str
    Prefix: str
    MaxKeys: int
    ContinuationToken: str


class Client(Protocol):
    def put_object(self, **kwargs: Unpack[Put]) -> object: ...
    def get_object(self, **kwargs: Unpack[Location]) -> Object: ...
    def copy_object(self, **kwargs: Unpack[Copy]) -> object: ...
    def delete_object(self, **kwargs: Unpack[Location]) -> object: ...
    def list_objects_v2(self, **kwargs: Unpack[Listing]) -> dict[str, object]: ...
    def close(self) -> None: ...


def connect(
    settings: Storage, request_timeout: float
) -> tuple[Client, tuple[type[Exception], ...]]:
    try:
        boto = importlib.import_module("boto3")
        config = importlib.import_module("botocore.config")
        errors = importlib.import_module("botocore.exceptions")
    except ImportError:
        raise OSError("checkpoint transport requires sandbox-science-worker[s3]") from None
    caught = (
        cast(type[Exception], errors.BotoCoreError),
        cast(type[Exception], errors.ClientError),
    )
    configuration = cast(Callable[..., object], config.Config)(
        signature_version="s3v4",
        connect_timeout=min(5, request_timeout),
        read_timeout=request_timeout,
        retries={"total_max_attempts": 1},
        s3={"addressing_style": settings.addressing_style},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    try:
        return cast(Callable[..., Client], boto.client)(
            "s3", region_name=settings.region, endpoint_url=settings.endpoint, config=configuration
        ), caught
    except caught:
        raise OSError("checkpoint object-store connection failed") from None


class Objects:
    """One attempt per PUT; an uncertain write is reconciled through an exact GET."""

    def __init__(
        self, settings: Storage, *, timeout: float = 3600, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        if not 1 <= timeout <= 86400:
            raise ValueError("checkpoint transport timeout must be between 1 and 86400 seconds")
        self.settings, self.clock = settings, clock
        self.deadline = clock.monotonic() + timeout
        self.request_timeout = min(15, timeout)
        self.connection: tuple[Client, tuple[type[Exception], ...]] | None = None
        self.etag: str | None = None
        """The ETag of the object ``copy`` last read: the version whose bytes it hashed."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.connection is not None:
            self.connection[0].close()
            self.connection = None

    def check(self) -> None:
        if self.clock.monotonic() >= self.deadline:
            raise TimeoutError("checkpoint transport exceeded its operation deadline")

    @contextmanager
    def client(self) -> Generator[tuple[Client, tuple[type[Exception], ...]]]:
        self.check()
        if self.connection is None:
            self.connection = connect(self.settings, self.request_timeout)
        client, errors = self.connection
        try:
            yield client, errors
        except errors:
            raise OSError("checkpoint object-store request failed") from None

    def copy(
        self,
        kind: Literal["chunks", "manifests"],
        digest: str,
        bound: int,
        target: BinaryIO | None = None,
        *,
        expected: int | None = None,
    ) -> int:
        self.check()
        with self.client() as (client, _):
            response = client.get_object(
                Bucket=self.settings.bucket, Key=self.settings.key(kind, digest)
            )
            self.etag = response.get("ETag")
            with closing(response["Body"]) as body:
                size = response["ContentLength"]
                if (
                    type(size) is not int
                    or not 0 <= size <= bound
                    or (expected is not None and size != expected)
                ):
                    raise ValueError("checkpoint object-store length differs or exceeds its bound")
                seen, checksum = 0, hashlib.sha256()
                while True:
                    self.check()
                    data = body.read(min(CHUNK, size - seen + 1))
                    if not data:
                        break
                    seen += len(data)
                    if seen > size:
                        raise ValueError("checkpoint object-store body exceeds pinned length")
                    checksum.update(data)
                    if target is not None:
                        target.write(data)
                self.check()
                if seen != size or checksum.hexdigest() != digest:
                    raise ValueError("checkpoint object-store bytes differ from pinned hash/length")
                return size

    def retain(
        self, kind: Literal["chunks", "manifests"], digest: str, size: int, handle: BinaryIO
    ) -> None:
        self.check()
        with self.client() as (client, errors):
            handle.seek(0)
            # A conditional conflict and a lost reply both require the original
            # immutable bytes to verify; neither permits a second blind write.
            with suppress(*errors):
                client.put_object(
                    Bucket=self.settings.bucket,
                    Key=self.settings.key(kind, digest),
                    Body=handle,
                    ContentLength=size,
                    IfNoneMatch="*",
                )
        self.copy(kind, digest, size, expected=size)

    def store(
        self, kind: Literal["chunks", "manifests"], digest: str, size: int, handle: BinaryIO
    ) -> bool:
        """``retain``, unless an object of that name already reads back exactly; whether
        it was written. A retried capture re-sends only what is missing or differs."""
        try:
            self.copy(kind, digest, size, expected=size)
        except (OSError, ValueError):
            self.retain(kind, digest, size, handle)
            return True
        return False

    def verified(self, kind: Literal["chunks", "manifests"], digest: str, size: int) -> bool:
        """Whether an object of that name already reads back with exactly its hash."""
        try:
            self.copy(kind, digest, size, expected=size)
        except (OSError, ValueError):
            return False
        return True

    def copy_in(
        self,
        source: Storage,
        kind: Literal["chunks", "manifests"],
        digest: str,
        size: int,
        etag: str,
    ) -> None:
        """Copy one object from ``source``'s bucket into this one inside the store
        (``CopyObject``: no bytes pass through this process), only if the source is still
        the version whose bytes were hashed (``CopySourceIfMatch`` on its ETag), then read
        the copy back whole and check its hash. The key needs read on the source bucket and
        read and write on this one."""
        if (source.endpoint, source.region) != (self.settings.endpoint, self.settings.region):
            raise ValueError("a checkpoint copy stays inside one object store")
        self.check()
        with self.client() as (client, _):
            client.copy_object(
                Bucket=self.settings.bucket,
                Key=self.settings.key(kind, digest),
                CopySource={"Bucket": source.bucket, "Key": source.key(kind, digest)},
                CopySourceIfMatch=etag,
            )
        self.copy(kind, digest, size, expected=size)

    def delete(self, kind: Literal["chunks", "manifests"], digest: str) -> None:
        self.check()
        with self.client() as (client, _):
            client.delete_object(Bucket=self.settings.bucket, Key=self.settings.key(kind, digest))

    def names(self, kind: Literal["chunks", "manifests"], limit: int) -> list[str]:
        """The digests stored under ``kind``, at most ``limit``; more is an error, not a
        silently shorter list, since a deletion decides by what is absent from it."""
        prefix = self.settings.key(kind, "0" * 64)[:-64]
        found: list[str] = []
        token: str | None = None
        while True:
            self.check()
            with self.client() as (client, _):
                page = (
                    client.list_objects_v2(
                        Bucket=self.settings.bucket,
                        Prefix=prefix,
                        MaxKeys=1000,
                        ContinuationToken=token,
                    )
                    if token is not None
                    else client.list_objects_v2(
                        Bucket=self.settings.bucket, Prefix=prefix, MaxKeys=1000
                    )
                )
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                raise ValueError("checkpoint object listing is malformed")
            for item in cast(list[object], contents):
                key = cast(dict[str, object], item).get("Key") if isinstance(item, dict) else None
                name = key[len(prefix) :] if isinstance(key, str) else ""
                if (
                    not key
                    or not str(key).startswith(prefix)
                    or re.fullmatch(r"[0-9a-f]{64}", name) is None
                ):
                    raise ValueError("checkpoint object listing names a foreign key")
                found.append(name)
                if len(found) > limit:
                    raise ValueError("checkpoint object listing exceeds its bound")
            following = page.get("NextContinuationToken")
            if page.get("IsTruncated") is not True:
                return found
            if not isinstance(following, str) or not following:
                raise ValueError("checkpoint object listing is truncated without a token")
            token = following
