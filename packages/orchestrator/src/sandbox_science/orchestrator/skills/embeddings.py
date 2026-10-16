# SPDX-License-Identifier: MIT
"""Embeddings for skill retrieval (docs/specs/skills.md, SK-29, SK-35, SK-36).

The model gateway embeds each skill's name, description and the first ``EMBEDDED_BODY``
characters of its body (question 14), once per snapshot and model: the vectors are kept
with the snapshot under the model the gateway reported and their dimensions, so a second
import of it computes nothing, and another model's vectors are never compared with this
one's. Each answer is checked against the space, model and dimensions, of the vectors in
use: one from another model is an ``EmbeddingError``, never a score.

No run waits for the skills' vectors. ``SkillVectors`` embeds a snapshot's in the
background, once per snapshot in the process at a time; runs compare by words until the
vectors exist, and a failure is tried again only after a backoff that doubles. A run
embeds the text it searches for or discovers skills by, within a bound of its own, and
compares it with its skills' vectors by cosine similarity. Vectors are unit length, as
float32. Each call's cost, as the gateway reported it, is counted for the project
(RS-6a).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple, cast

import aiohttp

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.model import COST_HEADER, ModelError, gateway_said, read_limited

from .packs import body
from .store import SkillStore

_log = logging.getLogger(__name__)

EMBEDDED_BODY: Final = 1500
QUERY_CHARS: Final = 2000
BATCH: Final = 64
MAX_ANSWER_BYTES: Final = 64 * 1024 * 1024
MAX_MODEL_BYTES: Final = 200
MAX_DIMENSIONS: Final = 16384
BACKOFF_FIRST: Final = 60.0
BACKOFF_MAX: Final = 3600.0


class EmbeddingError(Exception):
    """The gateway's embeddings could not be had, or are not of the vectors in use."""


Paid = list[tuple[str, int, float | None]]
"""Calls the gateway answered, each with its model (the alias when the answer named none),
the texts and the cost it reported: kept as they come, so a later failure loses none."""


class Space(NamedTuple):
    """Where vectors can be compared: the model the gateway reported, and dimensions."""

    model: str
    dimensions: int


@dataclass(frozen=True)
class Embedded:
    space: Space
    vectors: list[list[float]]
    cost: float | None
    """What the calls cost, as the gateway reported it; ``None`` when it priced none."""


class Embedder:
    """The gateway's OpenAI-compatible ``/embeddings`` for one model alias."""

    def __init__(self, base_url: str, alias: str, api_key: str, *, timeout: float = 60.0) -> None:
        self.base_url, self.alias, self._key = base_url.rstrip("/"), alias, api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def embed(self, texts: Sequence[str], paid: Paid | None = None) -> Embedded:
        """Each text's unit vector, in order, all in one space; each call the gateway
        answered goes into ``paid`` as it comes."""
        vectors: list[list[float]] = []
        space: Space | None = None
        costs: list[float] = []
        for start in range(0, len(texts), BATCH):
            got = await self._batch(list(texts[start : start + BATCH]), paid)
            if space is not None and got.space != space:
                raise EmbeddingError(
                    f"the gateway answered in {tuple(space)}, then in {tuple(got.space)}"
                )
            space = got.space
            vectors += got.vectors
            costs += [] if got.cost is None else [got.cost]
        if space is None:
            raise EmbeddingError("nothing to embed")
        return Embedded(space, vectors, sum(costs) if costs else None)

    async def _batch(self, texts: list[str], paid: Paid | None) -> Embedded:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                f"{self.base_url}/embeddings",
                json={"model": self.alias, "input": texts},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                cost = _cost(response.headers.get(COST_HEADER))
                try:  # to its end: a single read returns only what has arrived (lab 42, B1)
                    raw = await read_limited(response, MAX_ANSWER_BYTES)
                except ModelError:
                    if response.status == 200 and paid is not None:  # charged all the same
                        paid.append((self.alias, len(texts), cost))
                    raise EmbeddingError(
                        f"the gateway's answer is larger than {MAX_ANSWER_BYTES:,} bytes"
                    ) from None
                if response.status != 200:
                    said = raw.decode(errors="replace")
                    if self._key:
                        said = said.replace(self._key, "[the key]")  # whole, then cut
                    said = gateway_said(said, response.content_type)
                    raise EmbeddingError(f"the gateway answered {response.status}: {said}")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise EmbeddingError(
                f"the gateway could not be reached ({type(error).__name__})"
            ) from None
        model, indexes = self.alias, None
        try:
            answer = _json(raw)
            model = str(answer.get("model") or self.alias)
            data = cast(list[dict[str, Any]], answer["data"])
            indexes = sorted(int(item["index"]) for item in data)
            ordered = sorted(data, key=lambda item: int(item["index"]))
            vectors = [unit([float(x) for x in item["embedding"]]) for item in ordered]
        except (KeyError, TypeError, ValueError) as error:
            # Its values stay out of the message, which run records keep (a field could
            # echo the key): only what kind of failure it was.
            raise EmbeddingError(
                f"the gateway's answer is not embeddings ({type(error).__name__})"
            ) from None
        finally:
            if paid is not None:  # answered, and so charged, whatever it holds
                shown = model if len(model.encode()) <= MAX_MODEL_BYTES else self.alias
                paid.append((shown, len(texts), cost))
        if indexes != list(range(len(texts))):
            raise EmbeddingError("the gateway's answer is not embeddings (its indexes)")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or not 0 < (size := dimensions.pop()) <= MAX_DIMENSIONS:
            raise EmbeddingError("the gateway's vectors are not of one size the store keeps")
        if len(model.encode()) > MAX_MODEL_BYTES:
            raise EmbeddingError(f"the gateway names its model in over {MAX_MODEL_BYTES} bytes")
        return Embedded(Space(model, size), vectors, cost)


def _cost(header: str | None) -> float | None:
    try:
        found = float(header) if header is not None else None
    except ValueError:
        return None
    return found if found is not None and math.isfinite(found) and found >= 0 else None


def _json(raw: bytes) -> dict[str, Any]:
    found = json.loads(raw)
    if not isinstance(found, dict):
        raise ValueError("not an object")
    return cast(dict[str, Any], found)


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if not vector or not math.isfinite(norm) or norm == 0:
        raise ValueError("not a vector")
    return [x / norm for x in vector]


def cosine(a: list[float], b: list[float]) -> float:
    """Of two unit vectors of one space."""
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass(frozen=True)
class Ensured:
    space: Space
    computed: int
    reused: int
    texts: int
    """Texts the gateway embedded: none when the vectors were there."""
    cost: float | None


async def ensure(
    store: SkillStore,
    embedder: Embedder,
    snapshot: str,
    space: Space | None = None,
    paid: Paid | None = None,
) -> Ensured | None:
    """Embed ``snapshot``'s skills that have no vector in ``space``, or else in the space
    the alias answered in last, and the gateway is asked nothing when its vectors there
    cover every skill. Without either, one skill's embedding tells the space. Each
    answer must be in that space. Each call the gateway answered goes into ``paid``,
    whatever happens after it. ``None`` for a snapshot with no skills."""
    found = await threaded(store.skills, snapshot)
    if not found:
        return None
    if space is None and (last := await threaded(store.space_for, snapshot, embedder.alias)):
        space = Space(last[0], last[1])
        if last[2]:
            return Ensured(space, 0, len(found), 0, None)
    texts: dict[str, str] = {}
    for name, description in found:
        content = await threaded(store.file, snapshot, f"{name}/SKILL.md")
        text = body((content or b"").decode(errors="replace"))
        texts[name] = f"{name}\n{description}\n{text[:EMBEDDED_BODY]}"
    named: dict[str, list[float]] = {}
    costs: list[float] = []
    asked = 0
    if space is None:
        first = next(iter(texts))
        probe = await embedder.embed([texts[first]], paid)
        space, named[first], asked = probe.space, probe.vectors[0], 1
        costs += [] if probe.cost is None else [probe.cost]
    have = set(await threaded(store.vectors, snapshot, *space))
    named = {name: vector for name, vector in named.items() if name not in have}
    missing = [name for name in texts if name not in have and name not in named]
    if missing:
        more = await embedder.embed([texts[name] for name in missing], paid)
        if more.space != space:  # the next try makes them in the space it answers in now
            await threaded(store.saw_space, snapshot, embedder.alias, *more.space)
            raise EmbeddingError(f"the gateway answered in {tuple(more.space)}, not {tuple(space)}")
        named |= dict(zip(missing, more.vectors, strict=True))
        costs += [] if more.cost is None else [more.cost]
        asked += len(missing)
    await threaded(store.add_vectors, snapshot, *space, named)
    await threaded(store.saw_space, snapshot, embedder.alias, *space)
    cost = sum(costs) if costs else None
    return Ensured(space, len(named), len(have), asked, cost)


async def count_paid(store: SkillStore, project: str | None, paid: Paid) -> None:
    """Count ``project``'s calls the gateway answered, priced as it reported (RS-6a)."""
    if project is None:
        return
    for model, texts, cost in paid:
        await threaded(store.add_embedding_call, project, model, texts, cost)


class SkillVectors:
    """The process's skill vectors for one embedder: made in the background, one task per
    snapshot at a time, so that no run waits for the gateway; a failure is tried again
    only after a backoff that doubles, from ``BACKOFF_FIRST`` to ``BACKOFF_MAX``
    seconds."""

    def __init__(self, embedder: Embedder, clock: Clock = SYSTEM_CLOCK) -> None:
        self.embedder, self.clock = embedder, clock
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, tuple[Space, dict[str, list[float]]]] = {}
        self._failed: dict[str, tuple[float, float, str]] = {}
        """Each snapshot that failed: when it may be tried again, the delay, and why."""
        self._hints: dict[str, Space] = {}
        """The space the gateway answered a run in, where it was not the vectors'."""

    async def start(self, store: SkillStore, snapshots: Iterable[str], project: str) -> None:
        """Have the vectors of ``snapshots``: those the store has in the space the alias
        answered in last are read before this returns, and the others are made in the
        background, where nothing waits for them; the calls' cost is ``project``'s. Each
        snapshot's task is registered before anything is awaited, so that two starts
        never make two."""
        read: list[asyncio.Future[None]] = []
        for snapshot in dict.fromkeys(snapshots):
            if snapshot in self._ready or snapshot in self._tasks:
                continue
            failed = self._failed.get(snapshot)
            if failed is not None and self.clock.monotonic() < failed[0]:
                continue
            settled = asyncio.get_running_loop().create_future()
            self._spawn(store, snapshot, project, settled)
            read.append(settled)
        for settled in read:
            await settled

    def _spawn(
        self,
        store: SkillStore,
        snapshot: str,
        project: str,
        settled: asyncio.Future[None] | None = None,
    ) -> None:
        self._tasks[snapshot] = asyncio.create_task(self._make(store, snapshot, project, settled))

    async def _make(
        self,
        store: SkillStore,
        snapshot: str,
        project: str,
        settled: asyncio.Future[None] | None,
    ) -> None:
        """Read the snapshot's vectors, or, settling ``settled`` first so that the run
        goes on, have the gateway make them. The calls the gateway answered are counted
        before the result or the failure is published, so that a reader who sees
        ``ready`` change sees every cost (RS-6a)."""
        hint = self._hints.pop(snapshot, None)
        paid: Paid = []
        made: tuple[Space, dict[str, list[float]]] | None = None
        failure: Exception | None = None
        try:
            if hint is None:
                last = await threaded(store.space_for, snapshot, self.embedder.alias)
                if last is not None and last[2]:
                    space = Space(last[0], last[1])
                    made = (space, await threaded(store.vectors, snapshot, *space))
            if made is None:
                _settle(settled)
                found = await ensure(store, self.embedder, snapshot, hint, paid)
                if found is None:
                    made = (Space("", 0), {})
                else:
                    made = (found.space, await threaded(store.vectors, snapshot, *found.space))
        except Exception as error:  # remembered, and tried again later
            failure = error
        finally:
            try:
                await count_paid(store, project, paid)  # what was paid, the build failed or not
            except Exception:
                _log.exception("counting the embedding calls of %s failed", snapshot)
            if failure is not None:
                self._failed_with(snapshot, hint, failure)
            elif made is not None:
                self._ready[snapshot] = made
                self._failed.pop(snapshot, None)
            _settle(settled)
            if self._tasks.get(snapshot) is asyncio.current_task():
                del self._tasks[snapshot]

    def _failed_with(self, snapshot: str, hint: Space | None, error: Exception) -> None:
        """Remember a build that failed, to be tried again after a backoff that doubles."""
        failed = self._failed.get(snapshot)
        delay = min(BACKOFF_MAX, failed[1] * 2) if failed is not None else BACKOFF_FIRST
        reason = str(error) or type(error).__name__
        if hint is not None:
            self._hints[snapshot] = hint
        self._failed[snapshot] = (self.clock.monotonic() + delay, delay, reason)
        _log.warning(
            "embedding the skills of %s failed, again in %g s: %s", snapshot, delay, reason
        )

    def ready(
        self, snapshots: Iterable[str]
    ) -> tuple[dict[str, tuple[Space, dict[str, list[float]]]], str | None]:
        """The vectors of ``snapshots`` made so far, and why some are missing: ``pending``
        while they are made, or the failure, until it is tried again."""
        snapshots = list(snapshots)
        found = {s: self._ready[s] for s in snapshots if s in self._ready}
        reasons = [
            "pending" if s in self._tasks else f"failed: {self._failed[s][2]}"
            for s in snapshots
            if s not in found and (s in self._tasks or s in self._failed)
        ]
        return found, (reasons[0] if reasons else None)

    async def moved(
        self, store: SkillStore, snapshots: Iterable[str], space: Space, project: str
    ) -> None:
        """The gateway answers in ``space`` now, not in the vectors' space: they are made
        again in it, from now on in the background, and a run in another process starts
        them too."""
        for snapshot in snapshots:
            self._ready.pop(snapshot, None)
            self._failed.pop(snapshot, None)
            self._hints[snapshot] = space
            await threaded(store.saw_space, snapshot, self.embedder.alias, *space)
            if snapshot not in self._tasks:
                self._spawn(store, snapshot, project)

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.embedder.close()


def _settle(settled: asyncio.Future[None] | None) -> None:
    if settled is not None and not settled.done():
        settled.set_result(None)
