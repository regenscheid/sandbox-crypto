# SPDX-License-Identifier: MIT
"""``sandbox-science-unit serve`` as a process, for the unit's and the launcher's tests."""

from __future__ import annotations

import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class Unit:
    """``sandbox-science-unit serve`` as a process, its output read as it comes."""

    def __init__(self, config: Path, env: dict[str, str], *options: str) -> None:
        self.config = config
        self.seen: list[str] = []
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sandbox_science.launcher.unit", "serve",
             "--config", str(config), *options],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )  # fmt: skip
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def expect(self, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.seen):
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {text!r} in:\n" + "\n".join(self.seen)) from None
            if line is None:
                raise AssertionError(
                    f"no {text!r}: the unit ended, saying:\n" + "\n".join(self.seen)
                )
            self.seen.append(line)

    def finish(self, timeout: float = 30) -> int:
        status = self.process.wait(timeout)
        while (line := self.lines.get(timeout=timeout)) is not None:
            self.seen.append(line)
        return status

    def stop(self) -> int:
        self.process.send_signal(signal.SIGTERM)
        return self.finish()

    @property
    def output(self) -> str:
        return "\n".join(self.seen)

    def status(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sandbox_science.launcher.unit", "status",
             "--config", str(self.config), *options],
            capture_output=True, text=True, check=False, timeout=60,
        )  # fmt: skip
