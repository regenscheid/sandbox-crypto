# SPDX-License-Identifier: MIT
"""One worker for each state directory (decision 9): an exclusive lock on ``worker.lock``.

The worker holds the lock for as long as it runs, so nothing it finds in its state at
startup belongs to a process still running. Operator commands that change that state,
such as clearing a capture lease, take the same lock, so they refuse while a worker
runs.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from .settings import SettingsError


class StateLock:
    """The exclusive lock on a state directory, held until ``release``."""

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "worker.lock"
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise SettingsError(
                f"another worker uses the state directory {state_dir}; one worker runs "
                "for each state directory"
            ) from None
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor: int | None = descriptor

    def release(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)  # closing it releases the lock
            self._descriptor = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
