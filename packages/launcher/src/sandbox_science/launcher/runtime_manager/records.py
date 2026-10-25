# SPDX-License-Identifier: MIT
"""What the runtime manager keeps on its PVC (KRB-9), each file mode 600 in a mode-700
directory, replaced in one step and synced:

```
<state_dir>/
    sandbox.json                  the owned sandbox: name, ID, image, revision, placement,
                                  and ``outcome`` when the gateway rolled its create back
    infrastructure-recovery.json  the journal of automatic actions (``RecoveryState``)
    credentials.json              the in-flight credentials request, and whether an exec used it
    request.json, result.json     an operator request and its receipt
    request.claimed.json          the request the serving process has taken and is answering
    request.lock                  held by the one operator request waiting for its receipt
    serve.lock                    held by the one serving process
    status.json                   the serving process: its instance nonce, PID and view
    policy.yaml, runtime.json     the runtime sandbox's policy and settings
```

The runtime's token is never written here: it is held in memory, and in a private
temporary file only while it is uploaded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple, Self, cast

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from sandbox_science.contracts.model import Contract
from sandbox_science.launcher import replacement
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.runtime_owner import Owned
from sandbox_science.launcher.state import write_private

_RECORD_BYTES = 16 * 1024
ROLLED_BACK = "create_rolled_back"
"""``sandbox.json``'s ``outcome`` for a create the gateway rolled back."""
NoteKind = Literal["gateway_unreachable", "control_unreachable", "action_failed", "blocked"]
"""What a status note is about, so that it is cleared when that condition ends."""


class RolledBack(NamedTuple):
    """A create the gateway rolled back: what it said, and when the manager saw it."""

    reason: str | None
    at: datetime


def read_bounded(path: Path, limit: int = 4096) -> bytes | None:
    """The file's bytes, None when it is missing; refused past ``limit``."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise LauncherError(f"{path.name} exceeds its byte bound")
    return data


class Placement(Contract):
    """What the sandbox was created from; a replacement by recovery reuses the placement."""

    image: str = Field(min_length=1, max_length=512)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    driver_config: str = Field(min_length=2, max_length=8192)
    driver_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def of(cls, image: str, revision: str, driver_config: str) -> Placement:
        digest = hashlib.sha256(driver_config.encode()).hexdigest()
        return cls(
            image=image,
            revision=revision,
            driver_config=driver_config,
            driver_config_sha256=digest,
        )


class ManagerOwnership:
    """``runtime_owner``'s record as ``sandbox.json``, with the placement beside today's
    ``{name, id}`` keys, so ``FileOwnership`` reads the same file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.creating: Placement | None = None
        """The placement a create in progress records with the new name."""

    def _document(self) -> dict[str, object] | None:
        data = read_bounded(self.path, _RECORD_BYTES)
        if data is None:
            return None
        value: object = json.loads(data)
        if not isinstance(value, dict):
            raise LauncherError("sandbox.json is malformed")
        return cast(dict[str, object], value)

    def read(self) -> Owned | None:
        document = self._document()
        if document is None:
            return None
        name, identity = document.get("name"), document.get("id")
        if not isinstance(name, str) or not (identity is None or isinstance(identity, str)):
            raise LauncherError("sandbox.json is malformed")
        return Owned(name, identity)

    def placement(self) -> Placement | None:
        document = self._document()
        if document is None or "driver_config" not in document:
            return None
        try:
            return Placement.model_validate(
                {key: document.get(key) for key in Placement.model_fields}
            )
        except ValidationError:
            raise LauncherError("sandbox.json's placement is malformed") from None

    def write(self, owned: Owned) -> None:
        document = self._document()
        if document is not None and document.get("name") == owned.name:
            kept = {key: document[key] for key in Placement.model_fields if key in document}
        else:
            kept = self.creating.model_dump() if self.creating is not None else {}
        record = {"name": owned.name, "id": owned.id, **kept}
        write_private(self.path, json.dumps(record) + "\n")

    def mark_rolled_back(self, reason: str | None, at: datetime) -> None:
        """Record on the recorded sandbox that the gateway rolled its create back."""
        document = self._document()
        if document is None:
            raise LauncherError("no runtime sandbox is recorded to mark")
        marked = document | {"outcome": ROLLED_BACK, "reason": reason, "at": at.isoformat()}
        write_private(self.path, json.dumps(marked) + "\n")

    def rolled_back(self) -> RolledBack | None:
        """The rollback marked on the record, if any."""
        document = self._document()
        if document is None or document.get("outcome") != ROLLED_BACK:
            return None
        reason, at = document.get("reason"), document.get("at")
        try:
            when = datetime.fromisoformat(at) if isinstance(at, str) else None
        except ValueError:
            when = None
        if when is None or when.tzinfo is None or not (reason is None or isinstance(reason, str)):
            raise LauncherError("sandbox.json's rollback is malformed")
        return RolledBack(reason, when)

    def remove(self) -> None:
        self.path.unlink()

    def document(self) -> dict[str, object] | None:
        """The whole record, for status."""
        return self._document()


class CredentialsRecord(Contract):
    """The request ID journaled before control is called, so a crash replays it."""

    request: str = Field(pattern=r"^[0-9a-f]{32}$")
    started: bool = False
    """An exec started with this request's values: a restart asks anew, revoking any
    runtime that may still hold them."""


class Status(Contract):
    """The serving process, as ``request`` finds and ``request status`` shows it."""

    instance: str = Field(pattern=r"^instance-[0-9a-f]{32}$")
    pid: int = Field(gt=0)
    runtime: Literal["starting", "running", "stopped"]
    """``starting`` until the process's first reconcile has finished."""
    sandbox: str | None = Field(default=None, max_length=128)
    control_instance: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=512)
    """The current condition that needs attention, for the operator; None when there is
    none. It is cleared when the condition ends."""
    note_kind: NoteKind | None = None
    note_since: AwareDatetime | None = None
    """When the condition began; it stays while the condition lasts."""

    @model_validator(mode="after")
    def _note(self) -> Self:
        if (self.note is None) != (self.note_kind is None) or (self.note is None) != (
            self.note_since is None
        ):
            raise ValueError("a status note has a kind and a since time, and only a note has")
        return self


class ManagerRequest(replacement.Request):
    """An operator's request to the serving manager, bound to its instance nonce."""

    action: Literal["bootstrap", "replace", "stop", "forget"]
    delete: bool = False

    @model_validator(mode="after")
    def _manager(self) -> Self:
        if self.instance is None:
            raise ValueError("a runtime manager request names the manager's instance")
        if (self.action == "stop") != self.delete:
            raise ValueError("stop is teardown: it needs delete, which nothing else takes")
        return self


Result = replacement.Result
