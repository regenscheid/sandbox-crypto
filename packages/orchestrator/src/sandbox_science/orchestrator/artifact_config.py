# SPDX-License-Identifier: MIT
"""Private, immutable destination settings shared by every artifact reader."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from sandbox_science.contracts.model import StrictContract


class S3Settings(StrictContract):
    """Destination only; authentication always uses the SDK's credential chain."""

    bucket: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
    prefix: Annotated[str, Field(max_length=512)] = "sandbox-science"
    region: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")] = "us-east-1"
    endpoint: Annotated[str, Field(max_length=2048)] | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"

    @model_validator(mode="after")
    def _destination(self) -> Self:
        if self.prefix and (
            any(part in ("", ".", "..") for part in self.prefix.split("/"))
            or any(not (c.isascii() and (c.isalnum() or c in "-._/")) for c in self.prefix)
        ):
            raise ValueError("invalid artifact key prefix")
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
                raise ValueError("invalid artifact endpoint")
            _ = url.port  # validate a supplied port before the SDK sees it
        return self

    def key(self, sha256: str) -> str:
        """The caller validates the exact SHA-256 before asking for its key."""
        return "/".join(part for part in (self.prefix, "blobs", sha256[:2], sha256) if part)


def load(root: Path) -> S3Settings | None:
    """Read a bounded owner-only regular file, without creating any directories."""
    try:
        descriptor = os.open(root / "storage.toml", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 8192
        ):
            raise ValueError("artifact backing configuration must be private and bounded")
        return S3Settings.model_validate(tomllib.loads(stream.read(8193).decode("utf-8")))


def install(root: Path, settings: S3Settings) -> None:
    """Install once; a different destination requires an explicit migration."""
    previous = load(root)
    if previous is not None:
        if previous != settings:
            raise ValueError("artifact backing is already configured; migrate before changing it")
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = "".join(
        f"{key} = {json.dumps(value)}\n"
        for key, value in settings.model_dump(exclude_none=True).items()
    ).encode()
    descriptor, name = tempfile.mkstemp(dir=root, prefix=".storage-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, root / "storage.toml")
        except FileExistsError:
            if load(root) != settings:
                raise ValueError(
                    "artifact backing was concurrently configured differently"
                ) from None
        for directory in (root, root.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        os.unlink(name)
