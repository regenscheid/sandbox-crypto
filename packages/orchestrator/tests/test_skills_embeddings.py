# SPDX-License-Identifier: MIT
"""Embeddings (docs/specs/skills.md, SK-29, SK-35, SK-36; SK-F13, SK-F16): the import
embeds each skill once per snapshot and model, and asks the gateway nothing when the
vectors are there; no run waits for the skills' vectors, which are made in the
background; search and discovery find skills by meaning as well as words once they
exist, check every answer against the vectors' model, fall back to words when the
gateway fails, and wait at most their bound on the run's clock. Each call's cost counts
for the project.

The pack is ``ss-test-skills``, written here under the MIT License, in a temporary git
repository. The fake gateway's vectors mark a few word families, so that "how many rows"
means what "counts the lines" does without sharing a word with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.history import body
from sandbox_science.orchestrator.notices import SKILLS, SKILLS_WORDS
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.skills import Entry, SkillStore, from_git, snapshot
from sandbox_science.orchestrator.skills import embeddings as embeddings_module
from sandbox_science.orchestrator.skills.discovery import SkillDiscovery
from sandbox_science.orchestrator.skills.embeddings import (
    EMBEDDED_BODY,
    Embedded,
    Embedder,
    EmbeddingError,
    Paid,
    SkillVectors,
    Space,
)
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.orchestrator.skills.tool import SIMILAR, SkillTool, hybrid
from sandbox_science.testing.clock import FakeClock

from .loop_harness import API_KEY, PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

ALPHA = "---\nname: alpha\ndescription: Says the marker.\n---\nSS-MARKER-ALPHA-1\n"
BETA = "---\nname: beta\ndescription: Counts the lines of the notes.\n---\n" + "Count. " * 400
FAMILIES = (("count", "tally", "how many", "rows", "lines"), ("marker", "say", "phrase"))
EMBEDDING = ("--skills-embedding-model", "embed")


def meaning(text: str) -> list[float]:
    """The fake model's vector: which word families ``text`` holds, and a small constant."""
    lowered = text.lower()
    return [1.0 if any(word in lowered for word in family) else 0.0 for family in FAMILIES] + [0.1]


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "ss-test-skills"
    for path, text in {"skills/alpha/SKILL.md": ALPHA, "skills/beta/SKILL.md": BETA}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "C1"]):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args]
        subprocess.run(git, cwd=root, check=True, capture_output=True)
    return root


def _import(harness: Harness, pack: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    key = harness.tmp / "gateway.key"
    key.write_text(API_KEY)
    argv = [
        "skills", "import", "--state-dir", str(harness.state), "--name", "ss", "--git", str(pack),
        "--embedding-model", "embed", "--model-url", harness.gateway.url,
        "--api-key-file", str(key),
    ]  # fmt: skip
    capsys.readouterr()  # what runs before it printed
    assert cli.main(argv) == 0
    return cast(dict[str, Any], json.loads(capsys.readouterr().out))


def _pin(harness: Harness, pack: Path | None = None) -> None:
    """Pin ``ss`` in the project, importing ``pack`` first without embeddings."""
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        if pack is not None:
            store.add("ss", snapshot(from_git(str(pack))), USER)
        store.pin(PROJECT, "ss", USER)
    finally:
        projects.close()


def _notices(messages: list[dict[str, Any]]) -> list[str]:
    texts = [body(cast(str, m["content"])) for m in messages if m.get("role") == "user"]
    return [text for text in texts if text.startswith((SKILLS.text, SKILLS_WORDS.text))]


def _summary(record: RunRecord) -> dict[str, Any]:
    return cast(dict[str, Any], record.summaries["skills"])


def _searches(record: RunRecord) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], entry["skills"]) for entry in record.tools if "skills" in entry]


async def test_an_import_embeds_each_skill_once(
    harness: Harness, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SK-29 and SK-F13: name, description and the body's start, once per snapshot and
    model; the import of a snapshot whose vectors are there asks the gateway nothing."""
    harness.gateway.embed = meaning
    first = await asyncio.to_thread(_import, harness, pack, capsys)
    assert first["embeddings"] == {
        "alias": "embed",
        "model": "embed-model-v1",
        "dimensions": 3,
        "computed": 2,
        "reused": 0,
        "cost": 0.002,
    }
    probe, rest = harness.gateway.embedded
    assert probe == ["alpha\nSays the marker.\nSS-MARKER-ALPHA-1\n"]
    assert rest[0].startswith("beta\nCounts the lines of the notes.\nCount. ")
    assert len(rest[0]) == len("beta\nCounts the lines of the notes.\n") + EMBEDDED_BODY
    again = await asyncio.to_thread(_import, harness, pack, capsys)
    assert again["unchanged"] is True
    assert again["embeddings"]["computed"] == 0
    assert again["embeddings"]["reused"] == 2
    assert len(harness.gateway.embedded) == 2  # the gateway was not asked again


async def test_an_import_whose_embedding_fails_is_kept(
    harness: Harness, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The import stands, with a warning; importing again embeds the skills."""
    harness.gateway.embed, harness.gateway.embed_status = meaning, 500
    first = await asyncio.to_thread(_import, harness, pack, capsys)
    assert first["embeddings"]["failed"].startswith("the gateway answered 500")
    assert first["unchanged"] is False
    harness.gateway.embed_status = 200
    again = await asyncio.to_thread(_import, harness, pack, capsys)
    assert again["unchanged"] is True
    assert (again["embeddings"]["computed"], again["embeddings"]["reused"]) == (2, 0)


async def test_no_run_waits_for_the_vectors_and_once_they_exist_meaning_counts(
    harness: Harness, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SK-29, SK-35: a run whose skills have no vectors starts making them in the
    background and compares by words, its record saying why; once they exist, discovery
    and search find ``beta`` for words it does not hold, and the calls count."""
    _pin(harness, pack)
    harness.gateway.embed = meaning
    harness.gateway.embed_hold = asyncio.Event()  # the gateway never answers this run
    try:
        script = [
            Reply(calls=[call("skills", action="search", query="tally the rows")]),
            Reply("Done."),
        ]
        one = await harness.run(
            "run-1", script, options=EMBEDDING, task="How many rows does my file hold?"
        )
    finally:
        harness.gateway.embed_hold.set()
    assert len(harness.gateway.embedded) <= 1  # the background's first ask, never answered
    (found,) = _summary(one)["discoveries"]
    assert (found["scoring"], found["embedding"]) == ("lexical", "pending")
    assert _notices(harness.gateway.main[0]) == []
    (search,) = _searches(one)
    assert (search["scoring"], search["embedding"], search["found"]) == (
        "lexical",
        "pending",
        [],
    )
    assert _summary(one)["embeddings"] == {"alias": "embed", "spaces": [], "missing": "pending"}
    # The vectors made at import, the next run reads them at its start.
    harness.gateway.embed_hold = None
    await asyncio.to_thread(_import, harness, pack, capsys)
    _pin(harness)
    before = len(harness.gateway.embedded)
    script = [
        Reply(calls=[call("skills", action="search", query="tally the rows")]),
        Reply("Done."),
    ]
    two = await harness.run(
        "run-2", script, options=EMBEDDING, task="How many rows does my file hold?"
    )
    assert harness.gateway.embedded[before:] == [
        ["How many rows does my file hold?"],
        ["tally the rows"],
    ]
    (shown,) = _notices(harness.gateway.main[0])
    assert "ss/beta" in shown
    assert "ss/alpha" not in shown
    assert shown.startswith(SKILLS.text)  # meaning was compared: the framing says so
    (found,) = _summary(two)["discoveries"]
    assert found["scoring"] == "hybrid"
    assert [skill["id"] for skill in found["skills"]] == ["ss/beta"]
    answer = json.loads(body(cast(str, two.messages[-2]["content"])))
    assert answer.pop("ranked") == "by words and meaning"
    assert answer["found"] == [
        {"id": "ss/beta", "description": "Counts the lines of the notes.", "meaning": 1.0}
    ]
    (search,) = _searches(two)
    assert (search["scoring"], search["found"]) == ("hybrid", ["ss/beta"])
    assert _summary(two)["embeddings"] == {
        "alias": "embed",
        "spaces": [{"model": "embed-model-v1", "dimensions": 3}],
    }
    projects = ProjectStore(harness.state)
    try:
        costs = RunState(projects.control).model_costs(PROJECT)
    finally:
        projects.close()
    assert costs["embeddings"] == pytest.approx(0.002)  # the two queries of run 2


async def test_a_failing_gateway_or_a_short_message_leaves_words(
    harness: Harness, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Vectors there, but the gateway fails on the run's text: discovery and search still
    match by words, and say why. A message too short to score is not embedded."""
    harness.gateway.embed = meaning
    await asyncio.to_thread(_import, harness, pack, capsys)
    _pin(harness)
    harness.gateway.embed_status = 500
    before = len(harness.gateway.embedded)
    script = [
        Reply(calls=[call("skills", action="search", query="count the lines")]),
        Reply("Done."),
    ]
    record = await harness.run(
        "run-1", script, options=EMBEDDING, task="Count the lines of the notes file."
    )
    (shown,) = _notices(harness.gateway.main[0])
    assert "ss/beta" in shown
    assert shown.startswith(SKILLS_WORDS.text)  # words alone (lab 42): the framing says so
    (found,) = _summary(record)["discoveries"]
    assert found["scoring"] == "lexical"
    assert found["embedding"].startswith("failed: the gateway answered 500")
    answer = json.loads(body(cast(str, record.messages[-2]["content"])))
    assert answer["ranked"].startswith(
        "by words alone: meaning could not be compared (failed: the gateway answered 500"
    )
    (search,) = _searches(record)
    assert search["embedding"].startswith("failed: the gateway answered 500")
    assert search["found"] == ["ss/beta"]
    asked = len(harness.gateway.embedded)
    assert asked - before == 2
    short = await harness.run("run-2", [Reply("Done.")], options=EMBEDDING, task="Thanks!")
    assert len(harness.gateway.embedded) == asked
    assert _summary(short)["discoveries"] == [
        {"answered": "task", "scoring": "lexical", "skills": []}
    ]


def test_fusion_ranks_skills_found_both_ways_first() -> None:
    entries = tuple(
        Entry("p", name, description, "0" * 64, "s", "r")
        for name, description in (
            ("lines", "Counts lines."),
            ("rows", "Tallies rows of tables."),
            ("other", "Draws things."),
        )
    )
    similar = {"p/rows": 0.9, "p/lines": 0.8, "p/other": SIMILAR - 0.01}
    found = hybrid(entries, "count the lines", similar)
    assert [(e.name, matched, meaning) for e, matched, meaning in found] == [
        ("lines", ["count", "line"], 0.8),
        ("rows", [], 0.9),
    ]


class _Peer(Embedder):
    """A gateway as a test plays it: its answers, in order, or one that never comes (a
    ``None``, or none left); from call ``hold`` on, each waits for ``release``. A call it
    answers is paid, as the real one counts it."""

    def __init__(self, *answers: Embedded | Exception | None, hold: int = 10**6) -> None:
        super().__init__("http://127.0.0.1:9", "embed", "test-key")
        self.answers = list(answers)
        self.asked: list[list[str]] = []
        self.hold, self.release = hold, asyncio.Event()
        self.cancelled = 0

    async def embed(self, texts: Sequence[str], paid: Paid | None = None) -> Embedded:
        self.asked.append(list(texts))
        answer = self.answers.pop(0) if self.answers else None
        try:
            if len(self.asked) > self.hold:
                await self.release.wait()
            if answer is None:
                await asyncio.Event().wait()  # never answers
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, Embedded)
        if paid is not None:
            paid.append((answer.space.model, len(texts), answer.cost))
        return Embedded(answer.space, answer.vectors[: len(texts)], answer.cost)


V1, V2 = Space("embed-model-v1", 3), Space("embed-model-v2", 3)


def _space(space: Space, *texts: str, cost: float | None = None) -> Embedded:
    return Embedded(space, [embeddings_module.unit(meaning(text)) for text in texts], cost)


@pytest.fixture
def skills(tmp_path: Path) -> tuple[ProjectStore, SkillStore]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    store = SkillStore(projects.control)
    files = (
        PackFile("alpha/SKILL.md", ALPHA.encode()),
        PackFile("beta/SKILL.md", BETA.encode()),
    )
    digest = hashlib.sha256(b"pack").hexdigest()
    store.add(
        "ss", snapshot(Fetched("archive", "a.tar", "", files, (), archive_sha256=digest)), USER
    )
    store.pin(PROJECT, "ss", USER)
    return projects, store


async def _ready_tool(store: SkillStore, vectors: SkillVectors, clock: FakeClock) -> SkillTool:
    """A run's tool whose skills' vectors, in V1, the store has."""
    (snapshot_id,) = {entry.snapshot for entry in store.effective(PROJECT).entries}
    named = {
        name: embeddings_module.unit(meaning(f"{name} {description}"))
        for name, description in store.skills(snapshot_id)
    }
    store.add_vectors(snapshot_id, *V1, named)
    store.saw_space(snapshot_id, "embed", *V1)
    tool = SkillTool(store, store.effective(PROJECT), role="main", alias="main")
    await tool.use(vectors, PROJECT, clock=clock)
    return tool


async def test_scoring_waits_for_an_embedding_that_never_comes_on_the_clock(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    """SK-F16: with the vectors there and a gateway that never answers the message, the
    call waits the bound on the run's clock, then goes out without a notice; a search
    waits its own bound, then ranks by words."""
    projects, store = skills
    clock = FakeClock()
    peer = _Peer()  # never answers
    vectors = SkillVectors(peer, clock)
    try:
        tool = await _ready_tool(store, vectors, clock)
        found = SkillDiscovery(tool, "What does the alpha marker say?", clock=clock)
        waiting = asyncio.ensure_future(found.notices("start", cast(Any, None)))
        while clock.sleepers == 0:
            await asyncio.sleep(0.01)
        clock.advance(4.9)
        await asyncio.sleep(0.05)
        assert not waiting.done()
        clock.advance(0.1)
        assert await asyncio.wait_for(waiting, 5) == ()
        assert found.records == [{"answered": "task", "scoring": "timed_out", "skills": []}]
        searching = asyncio.ensure_future(
            tool.call({"action": "search", "query": "alpha marker"}, call_id="c", model="m")
        )
        while clock.sleepers == 0:
            await asyncio.sleep(0.01)
        clock.advance(5)
        text, record = await asyncio.wait_for(searching, 5)
        assert json.loads(text)["found"][0]["id"] == "ss/alpha"
        assert (record["skills"]["scoring"], record["skills"]["embedding"]) == (
            "lexical",
            "timed_out",
        )
    finally:
        await vectors.close()
        projects.close()


async def test_an_answer_from_another_model_fails_and_the_vectors_are_made_again(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    """An alias that now names another model answers in another space: the run's
    comparison is an embedding failure, never a score, and the vectors are made again in
    the new space at once, in the background; another process reads them."""
    projects, store = skills
    clock = FakeClock()
    peer = _Peer(
        _space(V2, "how many rows"),
        _space(V2, "says the marker", "counts the lines"),
        _space(V2, "how many rows"),
    )
    vectors = SkillVectors(peer, clock)
    try:
        tool = await _ready_tool(store, vectors, clock)
        snapshot_id = tool.skills.entries[0].snapshot
        with pytest.raises(EmbeddingError, match=r"not in .*embed-model-v1"):
            await tool.similarity("how many rows")
        while vectors.ready({snapshot_id})[1] == "pending":
            await asyncio.sleep(0.01)
        assert [text.partition("\n")[0] for text in peer.asked[1]] == ["alpha", "beta"]
        again = SkillTool(store, store.effective(PROJECT), role="main", alias="main")
        await again.use(vectors, PROJECT, clock=clock)
        similar, why = await again.similarity("how many rows")
        assert why is None
        assert max(similar, key=lambda skill: similar[skill]) == "ss/beta"
        # Another process reads the V2 vectors the store now has, asking nothing.
        fresh = _Peer(_space(V2, "how many rows"))
        other = SkillVectors(fresh, clock)
        later = SkillTool(store, store.effective(PROJECT), role="main", alias="main")
        await later.use(other, PROJECT, clock=clock)
        assert (await later.similarity("how many rows"))[1] is None
        assert fresh.asked == [["how many rows"]]
        await other.close()
    finally:
        await vectors.close()
        projects.close()


async def test_a_pack_left_in_the_old_model_is_made_again_beside_one_in_the_new(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    """A run scores by the packs in the model the gateway answers in now, says the scoring
    was partial, and has the others made again, which the next records say are pending
    and the store then has: a re-import asks nothing (review of #433, A and D)."""
    projects, store = skills
    files = (
        PackFile("gamma/SKILL.md", b"---\nname: gamma\ndescription: Says the phrase.\n---\nG\n"),
        PackFile("delta/SKILL.md", b"---\nname: delta\ndescription: Tallies rows.\n---\nD\n"),
    )
    digest = hashlib.sha256(b"tt").hexdigest()
    store.add(
        "tt", snapshot(Fetched("archive", "t.tar", "", files, (), archive_sha256=digest)), USER
    )
    store.pin(PROJECT, "tt", USER)
    clock = FakeClock()
    peer = _Peer(
        _space(V2, "how many rows"),
        _space(V2, "says the marker", "counts the lines"),
        hold=1,
    )
    vectors = SkillVectors(peer, clock)
    try:
        by_source = {e.source: e.snapshot for e in store.effective(PROJECT).entries}
        for source, space in (("ss", V1), ("tt", V2)):
            named = {
                name: embeddings_module.unit(meaning(f"{name} {description}"))
                for name, description in store.skills(by_source[source])
            }
            store.add_vectors(by_source[source], *space, named)
            store.saw_space(by_source[source], "embed", *space)
        tool = SkillTool(store, store.effective(PROJECT), role="main", alias="main")
        await tool.use(vectors, PROJECT, clock=clock)
        similar, why = await tool.similarity("how many rows")
        assert set(similar) == {"tt/delta", "tt/gamma"}
        assert why is not None
        assert why.startswith("partial: the gateway answered in ('embed-model-v2', 3)")
        assert vectors.ready({by_source["ss"]}) == ({}, "pending")  # being made again
        peer.release.set()
        while vectors.ready({by_source["ss"]})[1] == "pending":
            await asyncio.sleep(0.01)
        assert store.space_for(by_source["ss"], "embed") == ("embed-model-v2", 3, True)
        found = SkillDiscovery(tool, None, clock=clock)
        peer.answers.append(_space(V2, "how many rows does it hold"))
        notice = await found.about(["how many rows does it hold"])
        assert notice is not None
        assert found.records[0]["scoring"] == "hybrid"
    finally:
        await vectors.close()
        projects.close()


async def test_two_starts_make_one_task_and_closing_ends_it(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    """Review of #433, B: starts at once make one task and one call; closing cancels it."""
    projects, store = skills
    peer = _Peer()  # never answers
    vectors = SkillVectors(peer, FakeClock())
    (snapshot_id,) = {entry.snapshot for entry in store.effective(PROJECT).entries}
    try:
        await asyncio.gather(
            vectors.start(store, [snapshot_id], PROJECT),
            vectors.start(store, [snapshot_id], PROJECT),
        )
        while not peer.asked:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert len(peer.asked) == 1
    finally:
        await vectors.close()
        projects.close()
    assert peer.cancelled == 1
    assert vectors.ready([snapshot_id]) == ({}, None)


async def test_a_cancelled_search_cancels_its_embedding(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    """Review of #433, C: the run's cancellation ends the search's embedding at once, and
    an answer that never came costs nothing."""
    projects, store = skills
    clock = FakeClock()
    peer = _Peer()  # never answers
    vectors = SkillVectors(peer, clock)
    try:
        tool = await _ready_tool(store, vectors, clock)
        searching = asyncio.ensure_future(
            tool.call({"action": "search", "query": "alpha marker"}, call_id="c", model="m")
        )
        while not peer.asked:
            await asyncio.sleep(0.01)
        searching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await searching
        await asyncio.sleep(0.05)
        assert peer.cancelled == 1
        assert "embeddings" not in RunState(projects.control).model_costs(PROJECT)
    finally:
        await vectors.close()
        projects.close()


class _SlowCounts(SkillStore):
    """A store slow to count each call, so that a build that published its outcome before
    counting what it paid would show partial costs to a reader of ``ready``."""

    def add_embedding_call(self, project: str, model: str, texts: int, cost: float | None) -> None:
        time.sleep(0.05)
        super().add_embedding_call(project, model, texts, cost)


@pytest.mark.parametrize("outcome", ["failure", "success"])
async def test_a_build_counts_what_was_paid_before_it_says_how_it_went(
    skills: tuple[ProjectStore, SkillStore], outcome: str
) -> None:
    """Review of #433, F, and the race found on babbage: every call the gateway answered
    counts for the project, a build that failed included, and by the time ``ready`` says
    the build ended, failed or not."""
    projects, _ = skills
    store = _SlowCounts(projects.control)
    second = _space(V2 if outcome == "failure" else V1, "beta", cost=0.5)
    peer = _Peer(_space(V1, "alpha", cost=0.25), second)
    vectors = SkillVectors(peer, FakeClock())
    (snapshot_id,) = {entry.snapshot for entry in store.effective(PROJECT).entries}
    try:
        await vectors.start(store, [snapshot_id], PROJECT)
        while vectors.ready([snapshot_id])[1] == "pending":
            await asyncio.sleep(0)
        costs = RunState(projects.control).model_costs(PROJECT)  # at once: no waiting
        assert costs["embeddings"] == pytest.approx(0.75)
        found, why = vectors.ready([snapshot_id])
        if outcome == "failure":
            assert found == {}
            assert why is not None
            assert why.startswith("failed: the gateway answered in ('embed-model-v2', 3)")
        else:
            assert why is None
            assert sorted(found[snapshot_id][1]) == ["alpha", "beta"]
    finally:
        await vectors.close()
        projects.close()


def test_an_alias_the_store_cannot_keep_is_refused_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key"
    key.write_text(API_KEY)
    argv = [
        "skills", "import", "--state-dir", str(tmp_path / "state"), "--name", "ss",
        "--git", str(tmp_path), "--embedding-model", "a" * 201,
        "--model-url", "http://127.0.0.1:9", "--api-key-file", str(key),
    ]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "an alias of 1 to 200 bytes" in capsys.readouterr().err
    run = ["run", "--skills-embedding-model", "a" * 201, "task"]
    with pytest.raises(SystemExit):
        cli.main(run)
    assert "an alias of 1 to 200 bytes" in capsys.readouterr().err


async def test_a_failure_is_tried_again_after_a_backoff_and_one_task_serves_all(
    skills: tuple[ProjectStore, SkillStore],
) -> None:
    projects, store = skills
    clock = FakeClock()
    peer = _Peer(EmbeddingError("down"), EmbeddingError("down again"))
    vectors = SkillVectors(peer, clock)
    (snapshot_id,) = {entry.snapshot for entry in store.effective(PROJECT).entries}
    try:
        await vectors.start(store, [snapshot_id], PROJECT)
        await vectors.start(store, [snapshot_id], PROJECT)  # the same task serves both
        while vectors.ready([snapshot_id])[1] == "pending":
            await asyncio.sleep(0.01)
        assert len(peer.asked) == 1
        assert vectors.ready([snapshot_id]) == ({}, "failed: down")
        await vectors.start(store, [snapshot_id], PROJECT)
        assert len(peer.asked) == 1  # within the backoff, nothing is asked
        clock.advance(embeddings_module.BACKOFF_FIRST)
        await vectors.start(store, [snapshot_id], PROJECT)
        while vectors.ready([snapshot_id])[1] == "pending":
            await asyncio.sleep(0.01)
        assert len(peer.asked) == 2
        clock.advance(embeddings_module.BACKOFF_FIRST)  # the backoff doubled
        await vectors.start(store, [snapshot_id], PROJECT)
        assert len(peer.asked) == 2
    finally:
        await vectors.close()
        projects.close()


@pytest_asyncio.fixture
async def answers() -> AsyncIterator[tuple[str, list[web.Response]]]:
    """A gateway that gives each request the next of its answers."""
    queued: list[web.Response] = []

    async def embeddings(request: web.Request) -> web.Response:
        return queued.pop(0)

    app = web.Application()
    app.router.add_post("/embeddings", embeddings)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/"), queued


def _data(*vectors: list[float], start: int = 0, model: str = "m") -> dict[str, Any]:
    return {
        "model": model,
        "data": [{"index": start + i, "embedding": vector} for i, vector in enumerate(vectors)],
    }


async def test_the_embedder_refuses_answers_that_are_not_embeddings(
    answers: tuple[str, list[web.Response]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, queued = answers
    key = "test-secret-embedder-key-0123456789"
    embedder = Embedder(url, "embed", key)
    try:
        # A key echoed across the cut at 300 characters leaves no part of it behind.
        queued.append(web.Response(status=401, text="x" * 290 + key + " rest"))
        with pytest.raises(EmbeddingError) as refused:
            await embedder.embed(["one"])
        assert key[:8] not in str(refused.value)
        assert "test-secret" not in str(refused.value)
        queued.append(web.json_response(_data([1.0])))
        with pytest.raises(EmbeddingError, match="its indexes"):
            await embedder.embed(["one", "two"])
        queued.append(web.json_response(_data([1.0], [1.0], start=1)))
        with pytest.raises(EmbeddingError, match="its indexes"):
            await embedder.embed(["one", "two"])
        # A key echoed in a field stays out of the message, as every value does.
        queued.append(web.json_response({"model": "m", "data": [{"index": 0, "embedding": [key]}]}))
        with pytest.raises(EmbeddingError) as refused:
            await embedder.embed(["one"])
        assert str(refused.value) == "the gateway's answer is not embeddings (ValueError)"
        queued.append(web.json_response(_data([0.0, 0.0])))
        with pytest.raises(EmbeddingError, match="not embeddings"):
            await embedder.embed(["one"])
        queued.append(web.json_response(_data([1.0], model="m" * 201)))
        with pytest.raises(EmbeddingError, match="over 200 bytes"):
            await embedder.embed(["one"])
        # Batches in two sizes are one answer in two spaces.
        monkeypatch.setattr(embeddings_module, "BATCH", 1)
        queued += [web.json_response(_data([1.0, 0.0])), web.json_response(_data([1.0]))]
        with pytest.raises(EmbeddingError, match="then in"):
            await embedder.embed(["one", "two"])
        queued.append(
            web.json_response(_data([3.0, 4.0]), headers={"x-litellm-response-cost": "0.5"})
        )
        got = await embedder.embed(["one"])
        assert (got.space, got.vectors, got.cost) == (Space("m", 2), [[0.6, 0.8]], 0.5)
    finally:
        await embedder.close()
    empty = Embedder(url, "embed", "")
    try:
        queued.append(web.Response(status=500, text="down"))
        with pytest.raises(EmbeddingError, match="answered 500: down"):
            await empty.embed(["one"])
    finally:
        await empty.close()


async def test_a_large_answer_in_chunks_is_read_to_its_end() -> None:
    """Lab 42, B1: a single read returns only what has arrived, so a real gateway's answer
    of 64 vectors of 1,536, sent in 64 KiB writes, was cut and could not be parsed."""
    vectors = [[((i * 7 + j) % 13 + 1) / 13.0 for j in range(1536)] for i in range(64)]
    body = json.dumps(
        {"model": "m", "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
    ).encode()
    assert len(body) > 16 * 64 * 1024  # many writes, as the gateway sends it

    async def chunked(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        for start in range(0, len(body), 64 * 1024):
            await response.write(body[start : start + 64 * 1024])
            await asyncio.sleep(0.005)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/embeddings", chunked)
    async with TestServer(app) as server:
        embedder = Embedder(str(server.make_url("/")).rstrip("/"), "embed", "test-key")
        try:
            got = await embedder.embed([f"text {i}" for i in range(64)])
        finally:
            await embedder.close()
    assert got.space == Space("m", 1536)
    assert len(got.vectors) == 64


async def test_an_error_page_is_named_by_its_kind_not_quoted(
    answers: tuple[str, list[web.Response]],
) -> None:
    """Lab 42, B7: a proxy's HTML error page (Cloudflare's 524) stays out of the message."""
    url, queued = answers
    page = '<!DOCTYPE html>\n<!--[if lt IE 7]> <html class="no-js"> <![endif]-->' + "x" * 900
    queued.append(web.Response(status=524, text=page, content_type="text/html"))
    embedder = Embedder(url, "embed", "test-key")
    try:
        with pytest.raises(EmbeddingError) as refused:
            await embedder.embed(["one"])
    finally:
        await embedder.close()
    said = f"the gateway answered 524: a text/html page of {len(page):,} characters"
    assert str(refused.value) == said


@pytest.mark.parametrize(
    ("model", "cutoff", "ranked"),
    [
        ("arr-text-embedding-3-small", 0.34, "by words and meaning"),
        ("embed-model-v1", SIMILAR, "by words; meaning compared but none close enough"),
    ],
)
async def test_the_meaning_cutoff_follows_the_embedding_models_family(
    skills: tuple[ProjectStore, SkillStore], model: str, cutoff: float, ranked: str
) -> None:
    """Lab 43, B1: ``text-embedding-3-small`` puts a paraphrase near 0.36, under the 0.45
    that other models keep; its family's cutoff lets it through. When meaning was compared
    and nothing came close, the answer says so rather than "by words and meaning"."""
    projects, store = skills
    space = Space(model, 3)
    (snapshot_id,) = {entry.snapshot for entry in store.effective(PROJECT).entries}
    store.add_vectors(snapshot_id, *space, {"alpha": [0.0, 1.0, 0.0], "beta": [1.0, 0.0, 0.0]})
    store.saw_space(snapshot_id, "embed", *space)
    query = [0.36, 0.0, (1 - 0.36**2) ** 0.5]  # 0.36 from beta, 0 from alpha
    clock = FakeClock()
    peer = _Peer(*(Embedded(space, [query], None) for _ in range(2)))
    vectors = SkillVectors(peer, clock)
    try:
        tool = SkillTool(store, store.effective(PROJECT), role="main", alias="main")
        await tool.use(vectors, PROJECT, clock=clock)
        asked = "how many are there in what was written down"  # no skill's words
        text, record = await tool.call({"action": "search", "query": asked}, call_id="c", model="m")
        answer = json.loads(text)
        assert answer["ranked"] == ranked
        close = 1 if cutoff < 0.36 else 0
        assert [item["id"] for item in answer["found"]] == ["ss/beta"] * close
        searched = record["skills"]
        assert (searched["scoring"], searched["cutoff"], searched["close"]) == (
            "hybrid",
            cutoff,
            close,
        )
        found = SkillDiscovery(tool, None, clock=clock)
        notice = await found.about([asked])
        assert (notice is not None) == bool(close)
        assert (found.records[0]["cutoff"], found.records[0]["close"]) == (cutoff, close)
    finally:
        await vectors.close()
        projects.close()
