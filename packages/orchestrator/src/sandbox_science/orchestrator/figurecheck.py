# SPDX-License-Identifier: MIT
"""Bounded checks of charts in a separate, operator-configured Node process (FG-20).

The figure frame's rules and libraries, built from the GUI's lockfile beside the math
checker (``npm run build:math`` writes ``dist-math/figures``), run as the math check
does (``mathcheck``): a fresh process per batch, bounded input, output, time and memory,
the launcher's and bundle's bytes pinned for the batch. A chart arrives with its pinned
data's text; the process reads no other file and reaches no address. No text selects
code, a path or an address.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from sandbox_science.contracts import Contract

from .mathcheck import bounded_file, run_checker

MAX_BATCH: Final = 8
"""Charts one process checks."""
MAX_INPUT: Final = 20 * 1024 * 1024
"""Bytes of one request to the launcher (as ``check-figures.mjs`` bounds it)."""
MAX_OUTPUT: Final = 32_768
"""Bytes of one chart's answer."""
MAX_SOURCE: Final = 65_536
MAX_DATA: Final = 8 * 1024 * 1024
"""A chart's data in all, in bytes (FG-17)."""
MAX_FILES: Final = 16
MAX_BUNDLE: Final = 16 * 1024 * 1024
HEAP: Final = 512
"""MiB of V8 old space: a chart may read 100,000 rows."""
ENGINE: Final = "vega-lite 6.4.3, vega 6.4.0"
"""The frame's versions (FG-15), which the bundle must report."""


class Chart(Contract):
    """One fence's text and its pinned data's text, by the path the chart names."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    source: str = Field(max_length=MAX_SOURCE)
    files: dict[str, str] = Field(default_factory=dict, max_length=MAX_FILES)


class Problem(Contract):
    """An error or a note: its kind, the library's or the rule's message, and its line and
    column in the fence when known (FG-21)."""

    kind: str = Field(pattern=r"^[a-z_]{1,32}$")
    message: str = Field(max_length=1000)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)


class _Answer(Contract):
    protocol: Literal[1]
    id: str
    engine: str
    runtime: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["accepted", "rejected"]
    errors: list[Problem] = Field(max_length=8)
    notes: list[Problem] = Field(max_length=8)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == "rejected") != bool(self.errors):
            raise ValueError("a chart is rejected exactly when it has errors")
        return self


_ANSWERS: Final = TypeAdapter(list[_Answer])


class FigureCheck(Contract):
    """A check's result. ``unavailable`` is inconclusive: never accepted, never rejected."""

    id: str = ""
    """The chart's ID in its batch; a recorded result keeps none."""
    status: Literal["accepted", "rejected", "unavailable"]
    errors: list[Problem] = Field(default_factory=list[Problem])
    notes: list[Problem] = Field(default_factory=list[Problem])
    reason: str | None = None
    """Why the check was unavailable."""
    engine: str | None = None
    runtime: str | None = None


def unavailable(chart: str, reason: str) -> FigureCheck:
    return FigureCheck(id=chart, status="unavailable", reason=reason)


def _size(chart: Chart) -> int:
    return len(chart.model_dump_json().encode())


class FigureChecker:
    """At most two processes; a batch of at most eight charts in one fresh process.

    Paths are trusted service configuration. A batch that misses its deadline, fails or
    answers wrongly is checked again in halves, down to single charts, so a chart that
    does so leaves only itself unavailable."""

    def __init__(self, node: Path, launcher: Path, bundle: Path, *, timeout: float = 20.0) -> None:
        if not 0.05 <= timeout <= 60:
            raise ValueError("checker deadline must be between 0.05 and 60 seconds")
        self.node, self.launcher, self.bundle = node.resolve(), launcher.resolve(), bundle.resolve()
        self.timeout = timeout
        self._slots = asyncio.Semaphore(2)

    def version(self) -> str:
        """The bundle's and launcher's digest, which a result is kept for."""
        digest = hashlib.sha256(bounded_file(self.bundle, MAX_BUNDLE))
        digest.update(bounded_file(self.launcher, 65_536))
        return digest.hexdigest()

    async def check_many(self, charts: list[Chart]) -> list[FigureCheck]:
        """Each chart's result, in input order; duplicate IDs are refused."""
        if len({chart.id for chart in charts}) != len(charts):
            raise ValueError("a figure batch repeats an ID")
        found: dict[str, FigureCheck] = {}
        batches: list[list[Chart]] = [[]]
        size = 2
        for chart in charts:
            data = sum(len(text.encode()) for text in chart.files.values())
            if data > MAX_DATA or _size(chart) + 2 > MAX_INPUT:
                found[chart.id] = unavailable(chart.id, "the chart's data exceed the checker's")
                continue
            if batches[-1] and (len(batches[-1]) == MAX_BATCH or size + _size(chart) > MAX_INPUT):
                batches.append([])
                size = 2
            batches[-1].append(chart)
            size += _size(chart) + 1
        for results in await asyncio.gather(*(self._batch(b) for b in batches if b)):
            found.update((result.id, result) for result in results)
        return [found[chart.id] for chart in charts]

    async def _batch(self, batch: list[Chart]) -> list[FigureCheck]:
        results, failed = await self._attempt(batch)
        if not failed or len(batch) == 1:
            return results
        middle = len(batch) // 2
        first, second = await asyncio.gather(
            self._batch(batch[:middle]), self._batch(batch[middle:])
        )
        return first + second

    async def _attempt(self, batch: list[Chart]) -> tuple[list[FigureCheck], bool]:
        started = False

        def failed(reason: str) -> tuple[list[FigureCheck], bool]:
            return [unavailable(chart.id, reason) for chart in batch], started

        try:
            async with asyncio.timeout(self.timeout), self._slots:
                bundle = bounded_file(self.bundle, MAX_BUNDLE)
                launcher = bounded_file(self.launcher, 65_536)
                started = True
                # The exact bytes for this batch, even during an operator's rollout.
                with tempfile.TemporaryDirectory(prefix="ss-figurecheck-") as directory:
                    pinned = Path(directory)
                    (pinned / "check-figures.mjs").write_bytes(launcher)
                    (pinned / "figurecheck.mjs").write_bytes(bundle)
                    data = b"[" + b",".join(c.model_dump_json().encode() for c in batch) + b"]"
                    output = await run_checker(
                        self.node,
                        pinned / "check-figures.mjs",
                        pinned / "figurecheck.mjs",
                        data,
                        MAX_OUTPUT * len(batch),
                        heap=HEAP,
                    )
            answers = _ANSWERS.validate_json(output)
            if [a.id for a in answers] != [c.id for c in batch] or any(
                a.engine != ENGINE or tuple(map(int, a.runtime.split(".")[:2])) < (22, 12)
                for a in answers
            ):
                raise ValueError("figure checker answered another batch or version")
        except TimeoutError:
            return failed("figure checker deadline exceeded")
        except OSError:
            started = False
            return failed("figure checker unavailable")
        except (ValueError, RecursionError, ExceptionGroup):
            return failed("figure checker unavailable or invalid response")
        return [
            FigureCheck(
                id=a.id,
                status=a.status,
                errors=a.errors,
                notes=a.notes,
                engine=a.engine,
                runtime=a.runtime,
            )
            for a in answers
        ], False


def identity(source: str, data: dict[str, str], version: str) -> str:
    """What a result is kept for: the fence's text, its data files' SHA-256 by path, and
    the checker's code (``FigureChecker.version``)."""
    text = json.dumps([source, data, version], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()
