# SPDX-License-Identifier: MIT
"""The manager's client for control's manager listener: ``/health`` and credential issue.

It speaks HTTPS verified against the control CA, follows no redirect, sends the
manager's bearer token (reread from its file on every call, so the Secret can rotate),
and reads at most a few KiB of any answer. Control chooses every value it issues; the
client only names a request ID, which the manager journals before calling so that a
retry after a crash replays it (KRB-7, docs/specs/orchestrator-confinement.md).
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import stat
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.launcher.config import LauncherError

_ANSWER_BYTES = 4096
_REQUEST = re.compile(r"[0-9a-f]{32}")


class ControlError(LauncherError):
    """Control could not be reached, or answered something the manager cannot use."""


class SupersededError(ControlError):
    """410: the request is older than control's latest, or its values are no longer
    current. Only a new request gets credentials."""


class OccupiedError(ControlError):
    """409: control revoked the token, but a runtime is still connected or draining;
    the same request is retried once it has gone."""


class Issued(Contract):
    """A generation and token control minted for one request."""

    generation: str = Field(pattern=r"^runtime-[0-9a-f]{24}$")
    token: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class Health(Contract):
    """What the manager reads from control's ``/health``."""

    ready: bool
    runtime_connected: bool
    runtime_ready: bool
    runtime_draining: bool
    control_instance: str | None = Field(default=None, min_length=1, max_length=128)

    @property
    def occupied(self) -> bool:
        return self.runtime_connected or self.runtime_draining


def _token(path: Path) -> str:
    try:
        found = path.stat()
        if not stat.S_ISREG(found.st_mode) or found.st_size > 4096:
            raise ControlError("the manager token is not a small regular file")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControlError(f"cannot read the manager token: {exc.strerror}") from None
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ControlError("the manager token has an invalid shape")
    return token


class ControlClient:
    """Control's manager listener at ``url`` (``https://<control Service>:8444``)."""

    def __init__(self, url: str, *, ca_file: Path, token_file: Path, timeout: float = 10) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise LauncherError("control's manager listener needs an https URL")
        self.host, self.port = parsed.hostname, parsed.port
        self.ca_file, self.token_file, self.timeout = ca_file, token_file, timeout

    def _call(self, method: str, path: str, body: bytes | None = None) -> tuple[int, object]:
        tls = ssl.create_default_context(cafile=str(self.ca_file))
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=tls
        )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers |= {
                "Authorization": "Bearer " + _token(self.token_file),
                "Content-Type": "application/json",
            }
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(_ANSWER_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise ControlError(f"control's manager listener is unreachable: {exc}") from None
        finally:
            connection.close()
        if len(data) > _ANSWER_BYTES:
            raise ControlError("control's answer exceeded its byte bound")
        try:
            value: object = json.loads(data) if data else None
        except ValueError:
            value = None
        return response.status, value

    def health(self) -> Health:
        """Control's health on the manager listener."""
        status, value = self._call("GET", "/health")
        if status != 200:
            raise ControlError(f"control's health answered HTTP {status}")
        try:
            return Health.model_validate(_known(value, Health.model_fields))
        except ValidationError:
            raise ControlError("control's health is not one the manager understands") from None

    def issue(self, request: str) -> Issued:
        """The generation and token for ``request``, or the refusal as an exception."""
        if not _REQUEST.fullmatch(request):
            raise ValueError("a credentials request ID is 32 lowercase hex digits")
        body = json.dumps({"request": request}).encode()
        status, value = self._call("POST", "/runtime/credentials", body)
        if status == 200:
            try:
                return Issued.model_validate(value)
            except ValidationError:
                raise ControlError("control issued malformed credentials") from None
        said = _message(value)
        if status == 410:
            raise SupersededError("control superseded the credentials request" + said)
        if status == 409:
            raise OccupiedError("a runtime still holds control's connection" + said)
        if status == 404:
            raise ControlError("control refused the manager's token")
        raise ControlError(f"control answered the credentials request with HTTP {status}" + said)


def _known(value: object, fields: object) -> dict[str, object]:
    """``value``'s keys that ``fields`` names; the health carries others the manager
    does not use."""
    if not isinstance(value, dict):
        return {}
    found = cast(dict[str, object], value)
    return {key: found[key] for key in cast(dict[str, object], fields) if key in found}


def _message(value: object) -> str:
    if isinstance(value, dict):
        message = cast(dict[str, object], value).get("message")
        if isinstance(message, str) and message:
            return ": " + message[:200]
    return ""
