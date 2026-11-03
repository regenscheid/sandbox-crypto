# SPDX-License-Identifier: MIT
"""The context-management lab's driver (runbook: ../cm-lab-round-2.md).

```
D=".venv/bin/python -I .agents/labs/cm_drive.py"
$D sources OUT
$D count --config CFG --lab DIR --alias ALIAS
$D preflight --config CFG --claude ALIAS [--lab DIR --total USD --elsewhere USD --seed-reused]
$D plan --lab DIR
$D seed --config CFG --lab DIR --alias ALIAS [--budget USD]
$D forecast --lab DIR [--alias ALIAS]
$D arm --config CFG --lab DIR --arm NAME --alias ALIAS [--budget USD]
$D export --lab DIR --arm NAME
```

It drives the launcher's
instance, as the config describes it: each run is ``sandbox-science-harness run`` with
the run service's own options (``launcher.parts.harness_argv``), so a run here is what
the GUI's would be, with the lab's ceiling, fold writer and model added.

- ``seed`` writes the sources into the seed session's workspace, then runs the seed's
  steps with folding off, in the launcher's own state directory. After each run it
  keeps a snapshot: a backup of the control store, and the workspace's files by hash.
  It stops once the view passes ``--target`` tokens.
- ``count`` counts each seed snapshot's view as a Claude alias would be sent it, with the
  main run's tool schemas, by the gateway's ``count_tokens`` (free): the seed was
  recorded on GPT, whose tokenizer counts about two thirds as many.
- ``preflight`` checks the aliases, routes and prices against the gateway, and prints the
  round's plan; ``plan`` prints the plan offline, from the counts and Opus 4.8's prices
  as read on 2026-11-02 (``OPUS_4_8``), with no gateway call at all.
- ``arm`` copies the snapshot whose view is just under the arm's trigger into a state
  directory of its own, gives the session a new environment and its workspace's files,
  sets the project's delegation settings, then runs the live steps there. Each arm is a
  store of its own, since the ledger is a project's: arms in one store would share it.
- ``export`` writes the arm's session as one JSON bundle, which ``score`` reads.

Every step is resumable: a run whose transcript is kept is not run again.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import math
import secrets
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import aiohttp

from sandbox_science.contracts import (
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    ListFilesRequest,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.launcher import parts
from sandbox_science.launcher.config import LauncherConfig, load_config
from sandbox_science.launcher.state import Layout
from sandbox_science.orchestrator.context.history.geometry import ceiling as capped
from sandbox_science.orchestrator.context.history.geometry import geometry
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.control.store import store_path
from sandbox_science.orchestrator.messages import MAX_TOKENS
from sandbox_science.orchestrator.model import ModelGateway, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore, ensure_environment, environment_id
from sandbox_science.orchestrator.questions import Answer, Question, Questions
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from . import scenario, sources

FILE_NAME = "control.sqlite3"
SKIPPED_DIRS = (".", "_")
"""Workspace directories a snapshot leaves out: hidden ones and caches."""
MAX_FILE = 16 * 1024 * 1024
CHUNK = 1024 * 1024
TOKENS_PER_PAGE = 1_300
"""About what a page of Source D costs as ``pdf_pages`` shows it (numbered lines)."""
TOKENS_PER_RUN = 4_000
"""About what a run without bulk adds: its message, calls, results and answer."""


# The run command.


def run_argv(serve: Sequence[str]) -> list[str]:
    """The run service's command as a ``run`` command: what only ``serve`` takes, and the
    state directory, which each run names, left out."""
    argv = list(serve)
    argv[argv.index("serve")] = "run"
    out: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item in ("--run-share", "--poll", "--state-dir"):
            skip = True
            continue
        out.append(item)
    return out


def step_argv(
    base: Sequence[str],
    *,
    state_dir: Path,
    project: str,
    session: str,
    alias: str,
    ceiling: int,
    transcript: Path,
    task: str,
    folding_off: bool = False,
    skeleton_only: bool = False,
    compaction: bool = False,
    extra: Sequence[str] = (),
) -> list[str]:
    """One step's run: on ``alias``, under ``ceiling``; ``folding_off`` for the seed;
    ``compaction``, Anthropic's server-side compaction in place of folds, at the trigger
    the ceiling gives (CM-38b); ``extra``, the arm's further options, such as the lab's
    small shares. A delegated run calls ``alias`` too, when the config names a delegated
    model (L16's child), and is in its root's context mode."""
    argv = list(base)
    argv[argv.index("--model") + 1] = alias
    if "--delegated-model" in argv:
        argv[argv.index("--delegated-model") + 1] = alias
    argv += ["--state-dir", str(state_dir), "--project", project, "--session", session]
    argv += ["--transcript", str(transcript), "--context-ceiling", str(ceiling)]
    if folding_off:
        argv += ["--input-limit", str(scenario.FOLDING_OFF)]
    if skeleton_only:
        argv.append("--skeleton-only-folds")
    if compaction:
        argv += ["--context-mode", "provider_compaction"]
    return [*argv, *extra, task]


def recovered(store: ProjectStore, project: str, session: str, task: str, transcript: Path) -> bool:
    """Whether ``transcript`` is kept: as the run wrote it, or written now from the run's
    record in the store, when the session's last main run was ``task``'s but its
    transcript could not be written. Running the step again would put its message in the
    session twice."""
    if transcript.exists():
        return True
    runs = [entry for entry in store.runs(project, session) if entry.role == "main"]
    if not runs:
        return False
    record = store.run(project, session, runs[-1].name)
    if record.task != task:
        return False
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(record.to_json())
    return True


def delegation_argv(base: Sequence[str], state_dir: Path, project: str) -> list[str]:
    """The project's delegation settings for L16, set on an arm's own store."""
    return [
        base[0], "project", "delegation", "--state-dir", str(state_dir), "--project", project,
        *scenario.DELEGATION,
    ]  # fmt: skip


def keep_delegated(store: ProjectStore, project: str, session: str, home: Path) -> list[Path]:
    """The session's delegated runs' records, kept beside the arm's transcripts, so that
    what a child spent counts toward its arm's cap and the round's (``spent``)."""
    kept = []
    for entry in store.runs(project, session):
        if entry.role == "main":
            continue
        path = home / "runs" / "delegated" / f"{entry.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(store.run(project, session, entry.name).to_json())
        kept.append(path)
    return kept


def _run(argv: list[str], log: Path, transcript: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        return subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, check=False).returncode  # noqa: S603


# The scripted user's answers.

ANSWER_POLL = 10.0
"""Seconds between the driver's looks at a running step's open questions."""


def due(questions: Sequence[Question], now: datetime, wait: float) -> list[Question]:
    """The open questions asked at least ``wait`` seconds before ``now``."""
    return [
        question
        for question in questions
        if question.state == "open" and now - question.asked_at >= timedelta(seconds=wait)
    ]


async def answering(
    state: Path, project: str, session: str, log: Path, done: asyncio.Event,
    *, wait: float = scenario.ASK_USER_AFTER, poll: float = ANSWER_POLL,
) -> int:  # fmt: skip
    """While a step runs, answer each ``ask_user`` question the session's runs leave open
    for ``wait`` seconds with the scenario's scripted answer (``scenario.ASK_USER_ANSWER``,
    the decision left to the agent), the same for every arm, and log each to ``log``, one
    JSON line. Without it a run that asks waits for ever: the scenario's user never
    answers (round 2, phase 1). How many it answered."""
    control = ControlStore(store_path(state))
    questions = Questions(control)
    answered = 0
    try:
        while True:
            for question in due(
                await asyncio.to_thread(questions.open, project, session), datetime.now(UTC), wait
            ):
                given = Answer(kind=scenario.ASK_USER_ANSWER)
                closed = await asyncio.to_thread(
                    questions.answer, project, session, question.question_id, given
                )
                answered += 1
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a") as out:
                    record = {
                        "at": datetime.now(UTC).isoformat(), "question": question.question_id,
                        "run": question.run, "asked_at": question.asked_at.isoformat(),
                        "text": question.text, "answer": scenario.ASK_USER_ANSWER,
                        "state": closed.state,
                    }  # fmt: skip
                    out.write(json.dumps(record) + "\n")
            if done.is_set():
                return answered
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), poll)
    finally:
        control.close()


async def run_answering(
    argv: list[str], log: Path, transcript: Path, state: Path, project: str, session: str,
    answers: Path,
) -> int:  # fmt: skip
    """``_run``, with ``answering`` beside it for as long as the step runs."""
    done = asyncio.Event()
    watcher = asyncio.create_task(answering(state, project, session, answers, done))
    try:
        return await asyncio.to_thread(_run, argv, log, transcript)
    finally:
        done.set()
        await watcher


def spent(transcripts: Sequence[Path]) -> tuple[float, int]:
    """What the kept runs cost, as their calls report it, and how many calls had no cost."""
    cost, unpriced = 0.0, 0
    for path in transcripts:
        for call in json.loads(path.read_text()).get("usage") or []:
            if isinstance(call.get("cost"), int | float):
                cost += float(call["cost"])
            else:
                unpriced += 1
    return cost, unpriced


def view_tokens(transcript: Path) -> int:
    """The tokens the run's last call was sent: its view, as the run ended."""
    calls = json.loads(transcript.read_text()).get("usage") or []
    sizes = [
        call.get("input_tokens") for call in calls if isinstance(call.get("input_tokens"), int)
    ]
    return sizes[-1] if sizes else 0


# The workspace.


async def _ready(
    client: WorkerClient, store: ProjectStore, project: str, session: str
) -> ExecutionContext:
    """The session's context, once the worker runs its environment."""
    context = store.session_snapshot(project, session).context
    record = store.get(project)
    for _ in range(120):
        info = await ensure_environment(client, record, context.environment_id)
        if info is None or info.state is EnvironmentState.READY:
            return context
        await asyncio.sleep(5)
    raise SystemExit(f"environment {context.environment_id} is not ready after ten minutes")


async def patiently[T](call: Callable[[], Awaitable[T]], tries: int = 12) -> T:
    """``call``, tried again while the worker says its executor is unavailable: a sandbox
    suspended while idle, or starting again (lab round 1, after S01)."""
    for attempt in range(tries):
        try:
            return await call()
        except WorkerAPIError as error:
            if error.kind is not ErrorKind.UNAVAILABLE or attempt == tries - 1:
                raise
            await asyncio.sleep(10)
    raise AssertionError("unreachable")


async def write_files(
    client: WorkerClient, context: ExecutionContext, files: dict[str, bytes]
) -> None:
    for path, data in files.items():
        request = WriteFileRequest(
            context=context.model_copy(update={"actor": "user"}),
            path=path,
            content_b64=base64.b64encode(data).decode(),
        )
        await patiently(lambda request=request: client.write_file(request))


async def read_workspace(client: WorkerClient, context: ExecutionContext) -> dict[str, bytes]:
    """The workspace's files, but hidden directories and caches, and files over 16 MB."""
    found: dict[str, bytes] = {}
    pending = ["."]
    while pending:
        here = pending.pop()
        asked = ListFilesRequest(context=context, path=here)
        listing = await patiently(lambda asked=asked: client.list_files(asked))
        for entry in listing.entries:
            path = entry.name if here == "." else f"{here}/{entry.name}"
            if entry.type == "directory" and not entry.name.startswith(SKIPPED_DIRS):
                pending.append(path)
            elif entry.type == "file" and entry.size <= MAX_FILE:
                found[path] = await _read(client, context, path)
    return found


async def _read(client: WorkerClient, context: ExecutionContext, path: str) -> bytes:
    data, offset = b"", 0
    while True:
        asked = ReadFileRequest(context=context, path=path, offset=offset, length=CHUNK)
        part = await patiently(lambda asked=asked: client.read_file(asked))
        data += base64.b64decode(part.content_b64)
        offset = len(data)
        if part.eof:
            return data


# Snapshots.


def backup(source: Path, target: Path) -> None:
    """A consistent copy of a SQLite store, which others may be writing."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as live, sqlite3.connect(target) as copy:
        live.backup(copy)


def keep_files(lab: Path, files: dict[str, bytes]) -> dict[str, str]:
    """``files`` kept under the lab's blobs by hash; their hashes by path."""
    kept: dict[str, str] = {}
    for path, data in files.items():
        digest = hashlib.sha256(data).hexdigest()
        blob = lab / "blobs" / digest
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(data)
        kept[path] = digest
    return kept


def pick_snapshot(
    manifest: Sequence[dict[str, Any]], ceiling: int, counted: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """The last snapshot whose view was at most ``SEED_SHARE`` of ``ceiling``: the
    largest seed that starts the arm under its trigger. ``counted``: each snapshot's view
    as the arm's model counts it (``count``), in place of the tokens the seed's model
    reported."""
    if counted is not None:
        missing = [s["step"] for s in manifest if s["step"] not in counted]
        if missing:
            raise SystemExit(f"no count of the snapshots {', '.join(missing)}: run count again")
        manifest = [{**s, "tokens": counted[s["step"]], "recorded": s["tokens"]} for s in manifest]
    fitting = [s for s in manifest if s["tokens"] <= scenario.SEED_SHARE * ceiling]
    if not fitting:
        raise SystemExit(f"no seed snapshot fits a ceiling of {ceiling:,} tokens")
    return fitting[-1]


def clone(snapshot: Path, target: Path, project: str, session: str, environment: str) -> None:
    """The snapshot's store as the arm's, its session in ``environment``: the arm's own
    sandbox, so arms never share a workspace. The session keeps the shape the seed gave
    it (a session keeps whether it has an environment of its own: lab round 1): when it
    ran in the project's, the arm's project gets ``environment`` as its own."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, target)
    with sqlite3.connect(target) as db:
        found = db.execute(
            "SELECT dedicated FROM sessions WHERE project_id = ? AND session_id = ?",
            (project, session),
        ).fetchone()
        if found is None:
            raise SystemExit(f"the snapshot has no session {project}/{session}")
        db.execute(
            "UPDATE sessions SET environment_id = ? WHERE project_id = ? AND session_id = ?",
            (environment, project, session),
        )
        if not found[0]:
            db.execute(
                "UPDATE projects SET default_environment_id = ? WHERE project_id = ?",
                (environment, project),
            )


def adopt_policy(target: Path, live: Path, project: str) -> None:
    """The arm's project on the policy revision this instance's worker grants: the live
    store's. A seed recorded by an earlier round's instance names that instance's revision,
    which this worker refuses ("no policy revision", lab round 2)."""
    with sqlite3.connect(live) as db:
        found = db.execute(
            "SELECT policy FROM projects WHERE project_id = ?", (project,)
        ).fetchone()
    if found is None:
        raise SystemExit(f"the instance's store has no project {project}: run up first")
    with sqlite3.connect(target) as db:
        db.execute("UPDATE projects SET policy = ? WHERE project_id = ?", (found[0], project))


def hand_over_epoch(target: Path, worker: Path, project: str, session: str) -> int:
    """The arm's session's research epoch raised to the worker's recovery fence, before
    the arm's first run: every arm's session is the seed's (``project``/``session``), and
    the worker keeps one fence per session, which only rises, so an arm whose store counts
    its runs from 1 would be refused after another arm's ("recovery epoch cannot
    decrease", lab round 2). Its first run then takes the next epoch. Arms therefore run
    one at a time on one worker: two at once would supersede each other's authority."""
    with sqlite3.connect(f"file:{worker}?mode=ro", uri=True) as db:
        found = db.execute(
            "SELECT epoch FROM recovery_fences WHERE project_id = ? AND session_id = ?",
            (project, session),
        ).fetchone()
    fence = int(found[0]) if found else 0
    with sqlite3.connect(target) as db:
        row = db.execute(
            "SELECT epoch FROM research_sessions WHERE project = ? AND session = ?",
            (project, session),
        ).fetchone()
        if fence and (row is None or int(row[0]) < fence):
            db.execute(
                "INSERT INTO research_sessions VALUES (?, ?, 'lab-handover', ?) "
                "ON CONFLICT(project, session) DO UPDATE SET epoch = excluded.epoch",
                (project, session, fence),
            )
            return fence
    return int(row[0]) if row else 0


FREED = 1.6
"""What a fold frees, in chunks: its span, and the old tool output it stubs. Lab round 1
measured 1.5 to 1.8 (74,000 to 90,000 tokens at C = 250,000, 210,000 at 600,000)."""
SUMMARY = 5_000
"""What a compaction leaves of the conversation, in tokens: its summary (CM-38b). The
probe of 2026-11-02 summarized 89,000 tokens of synthetic pages in 220; a session's work
is denser, so this is a generous guess."""
COMPACTION_OUTPUT = 4_000
"""A compaction's own output tokens, its summary written, for the cost model."""


def forecast(
    start: int,
    ceiling: int,
    growth: Sequence[int],
    prompt: int = 30_000,
    *,
    compaction: bool = False,
) -> list[int]:
    """The folds each live step makes, roughly: the view grows by each step's tokens, and
    a fold frees ``FREED`` chunks whenever it passes the trigger; with ``compaction``, the
    compactions, each leaving the system message, tools and a ``SUMMARY``."""
    shape = geometry(ceiling, prompt)
    tokens, folds = start, []
    for added in growth:
        tokens += added
        made = 0
        while tokens > shape.trigger:
            tokens = prompt + SUMMARY if compaction else tokens - int(FREED * shape.chunk)
            made += 1
        folds.append(made)
    return folds


def growth(ratio: float = 1.0) -> list[int]:
    """Each live step's tokens in the main view, estimated from the pages it reads, as the
    seed's model counts them, times ``ratio`` for another model's. L16's pages are its
    child's, not the main view's."""
    pages = {"L01": 40, "L03": 12, "L06": 40, "L11": 22}
    return [
        int(ratio * (TOKENS_PER_RUN + TOKENS_PER_PAGE * pages.get(step.id, 0)))
        for step in scenario.LIVE
    ]


def child_growth(ratio: float = 1.0) -> int:
    """The child's reading (L16): its pages and its run's own messages."""
    return int(ratio * (TOKENS_PER_RUN + TOKENS_PER_PAGE * scenario.CHILD_GROWTH_PAGES))


# The arm's model's counts.

COUNTS = "counts.json"
"""In the lab's directory, beside the seed (which may be an earlier round's, kept as it
was): each alias's counts of the snapshots (``count``)."""


@dataclass(frozen=True)
class Counted:
    """A model's view of the seed: each snapshot's tokens as the gateway's ``count_tokens``
    counts them for the alias, with the main run's tool schemas; the system message and
    tools alone (``prompt``); and its tokens per token the seed's model reported, over the
    bulk steps' growth (``ratio``)."""

    alias: str
    views: dict[str, int]
    tools: int
    prompt: int
    ratio: float


def load_counts(lab: Path, alias: str) -> Counted | None:
    path = lab / COUNTS
    found = json.loads(path.read_text()).get(alias) if path.exists() else None
    return Counted(**found) if found else None


def growth_ratio(manifest: Sequence[dict[str, Any]], views: Mapping[str, int]) -> float:
    """How many tokens the counted model takes for each one the seed's model reported, over
    the bulk steps' growth: their pages, as the arms' live steps add them."""
    bulk = {step.id for step in scenario.SEED if step.bulk}
    pairs = [
        (views[after["step"]] - views[before["step"]], after["tokens"] - before["tokens"])
        for before, after in itertools.pairwise(manifest)
        if after["step"] in bulk and before["step"] in views and after["step"] in views
    ]
    counted, recorded = sum(c for c, _ in pairs), sum(r for _, r in pairs)
    return counted / recorded if recorded > 0 else 1.0


def main_tools() -> tuple[ToolSpec, ...]:
    """The tools a main run is offered, as the model is sent them: every harness tool,
    with its ``purpose_note``, and no connector tools loaded (as
    ``test_capability_assembly.py`` assembles them). Their schemas are about 37,000 of
    Claude's tokens: what a count of the seed's transcripts, which do not keep them,
    leaves out."""
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.agent_messages import (
        CHILD_SPEC,
        PARENT_SPEC,
        STATUS_SPEC,
        WAIT_SPEC,
    )
    from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS
    from sandbox_science.orchestrator.context.history import SPECS as HISTORY_SPECS
    from sandbox_science.orchestrator.delegation.specs import SPECS as DELEGATION_SPECS
    from sandbox_science.orchestrator.delegation.tracks import SPECS as TRACK_SPECS
    from sandbox_science.orchestrator.documents.tool import SPECS as DOCUMENT_SPECS
    from sandbox_science.orchestrator.jobs import JOB_SPECS
    from sandbox_science.orchestrator.ledger.tool import SPECS as LEDGER_SPECS
    from sandbox_science.orchestrator.memory_tools import SPECS as MEMORY_SPECS
    from sandbox_science.orchestrator.plan_tools import SPECS as PLAN_SPECS
    from sandbox_science.orchestrator.question_tool import SPECS as QUESTION_SPECS
    from sandbox_science.orchestrator.resource_hooks import SPEC as RESOURCE_SPEC
    from sandbox_science.orchestrator.skills.tool import SPEC as SKILL_SPEC
    from sandbox_science.orchestrator.tools import Toolbox

    def domain(specs: Sequence[ToolSpec] = (), **kwargs: Any) -> Any:
        return SimpleNamespace(specs=tuple(specs), **kwargs)  # only the specs are read

    context = ExecutionContext(project_id="p", session_id="s", environment_id="e", actor="agent")
    toolbox = Toolbox(
        cast(WorkerClient, object()),
        context,
        languages=("python", "sage"),
        features=("jobs", "pdf", "admission", "provisioning"),
        check_kernels=True,
        documents=domain(DOCUMENT_SPECS),
        jobs=domain(JOB_SPECS, handlers={spec.name: None for spec in JOB_SPECS}),
        ledger=domain(LEDGER_SPECS),
        memory=domain(MEMORY_SPECS),
        tracks=domain(TRACK_SPECS),
        delegations=domain(DELEGATION_SPECS, name="delegations", live=lambda: ()),
        mailbox=domain((CHILD_SPEC, PARENT_SPEC, STATUS_SPEC, WAIT_SPEC), name="agent_messages"),
        resources=domain((RESOURCE_SPEC,), name="resources"),
        skills=domain((SKILL_SPEC,)),
        history=domain(HISTORY_SPECS),
        images=domain(),
        math_checks=domain(),
        recorder=domain(),
        artifacts=domain(),
        reviews=domain(AUTHOR_SPECS),
        plans=domain(PLAN_SPECS),
        questions=domain(QUESTION_SPECS, name="questions"),
    )
    return toolbox.specs


def messages_body(alias: str, messages: Sequence[Any], tools: Sequence[ToolSpec]) -> dict[str, Any]:
    """The Messages request the harness's Claude adapter would send for ``messages``,
    without thinking, which a count does not take."""
    from sandbox_science.orchestrator import messages as adapter

    # The adapter's own rendering, so the count is of what a run sends.
    view = adapter._render(messages, tools, adapter._Served(False, False), False)
    body = {key: value for key, value in view.body.items() if key != "thinking"}
    return {"model": alias, **body}


# Costs.

LONG = 272_000
"""A view past this many tokens is priced at the gateway's long-context rates (GPT)."""
CALLS_PER_ARM = 170
"""An arm's calls, as lab round 1's seed measured them: about 7.5 a step, 15 to 20 a step
that reads 20 pages (plans, extraction, ledger), and the folds' enrichment calls."""
CHILD_CALLS = 60
"""L16's child's calls: 150 pages read 5 to 10 at a time, its notes, the experiment, its
messages and result, and its folds' enrichment calls."""
SEED_CALLS = 250
"""The seed's: 195 in its first 16 steps, to 300,000 tokens."""
OUTPUT_PER_CALL = 400
"""Each call's output, its reasoning included: 325 on average in the seed."""
MARGIN = 1.5
"""An arm's cap over its estimate: round 1's sessions took up to 1.4 times the calls planned
for a step."""


@dataclass(frozen=True)
class Prices:
    """An alias's prices per token, as the gateway's model information gives them, with
    those past ``long_at`` tokens; a cache read the gateway does not price costs input.
    ``cache_write``: a 5-minute cache write's price, for a model that charges for writes
    (Claude: 1.25 times input), ``None`` for one whose writes cost input (GPT)."""

    input: float
    cached: float
    output: float
    input_long: float
    cached_long: float
    output_long: float
    cache_write: float | None = None
    long_at: int = LONG
    cache_write_1h: float | None = None
    """A 1-hour cache write's price, where the gateway lists one: a compaction writes its
    input there (CM-38b). Without it, twice the input price, Anthropic's rate."""

    @classmethod
    def of(cls, info: Mapping[str, Any]) -> Prices | None:
        def price(key: str, fallback: float) -> float:
            value = info.get(key)
            return float(value) if isinstance(value, int | float) and value > 0 else fallback

        given, output = price("input_cost_per_token", 0.0), price("output_cost_per_token", 0.0)
        if not given or not output:
            return None
        above, long_at = "above_272k_tokens", LONG
        if any(str(key).endswith("above_200k_tokens") for key in info):
            above, long_at = "above_200k_tokens", 200_000
        long_input = price(f"input_cost_per_token_{above}", given)
        cached = price("cache_read_input_token_cost", given)
        write = price("cache_creation_input_token_cost", 0.0)
        hour = price("cache_creation_input_token_cost_above_1hr", 0.0)
        return cls(
            given,
            cached,
            output,
            long_input,
            # Without long-context rates the base cache price holds past ``long_at`` too.
            price(
                f"cache_read_input_token_cost_{above}",
                long_input if long_input != given else cached,
            ),
            price(f"output_cost_per_token_{above}", output),
            write or None,
            long_at,
            hour or None,
        )


OPUS_4_8: Final = Prices(5e-6, 5e-7, 25e-6, 5e-6, 5e-7, 25e-6, 6.25e-6, LONG, 1e-5)
"""``arr-claude-opus-4-8``'s prices as the gateway's model information gave them on
2026-11-02 (USD 5 and 25 a million, cache reads 0.50, 5-minute writes 6.25, 1-hour writes
10; no long-context rates): ``plan`` uses them offline, ``preflight`` reads them again."""


def views(ceiling: int, calls: int = CALLS_PER_ARM) -> list[int]:
    """An arm's calls' views: spread between 0.45 C, after a fold, and 0.75 C, its trigger."""
    return [int(ceiling * (0.45 + 0.3 * n / (calls - 1))) for n in range(calls)]


def child_views(ceiling: int, start: int) -> list[int]:
    """The child's calls' views: growing from its prompt and brief to its trigger."""
    top = max(start, int(0.75 * ceiling))
    return [start + (top - start) * n // (CHILD_CALLS - 1) for n in range(CHILD_CALLS)]


def seed_views(target: int, prompt: int = 35_000) -> list[int]:
    """The seed's calls' views: growing from the system message to its target."""
    return [prompt + (target - prompt) * n // (SEED_CALLS - 1) for n in range(SEED_CALLS)]


def estimate(sizes: Sequence[int], prices: Prices, cached: float) -> float:
    """What calls sent ``sizes`` tokens cost, with ``cached`` of their input a cache read
    (GPT's model: the gateway prices no cache write)."""
    total = 0.0
    for size in sizes:
        long = size > prices.long_at
        read = prices.cached_long if long else prices.cached
        fresh = prices.input_long if long else prices.input
        total += size * (cached * read + (1 - cached) * fresh)
        total += OUTPUT_PER_CALL * (prices.output_long if long else prices.output)
    return total


def spread(calls: int, marks: int) -> set[int]:
    """``marks`` of ``calls`` calls' indices, evenly spaced, the first among them."""
    return {min(calls - 1, j * calls // marks) for j in range(marks)} if marks > 0 else set()


def estimate_written(
    sizes: Sequence[int], prices: Prices, *, added: int, rebuilds: int, prompt: int
) -> float:
    """What calls sent ``sizes`` tokens cost on a model that charges for cache writes
    (Claude): each call reads its prefix from the cache and writes what is new to it, its
    share of ``added`` tokens, and ``rebuilds`` calls (each run's first, and the first
    after each fold) write the view again from the state block on. The fact pass of
    2026-11-02 measured both: the system message and tools stay cached across a rebuild,
    and everything after them is written again."""
    write = prices.cache_write if prices.cache_write is not None else prices.input
    rebuilt = spread(len(sizes), rebuilds)
    each = added / max(1, len(sizes))
    total = 0.0
    for index, size in enumerate(sizes):
        written = max(0, size - prompt) if index in rebuilt else min(each, size)
        long = size > prices.long_at
        total += (size - written) * (prices.cached_long if long else prices.cached)
        total += written * write
        total += OUTPUT_PER_CALL * (prices.output_long if long else prices.output)
    return total


@dataclass(frozen=True)
class Planned:
    """An arm's plan: the snapshot it starts from and its view there, the folds forecast
    for the main run and the child, and its estimated cost and cap (USD)."""

    arm: str
    ceiling: int
    seed: str
    start: int
    folds: int
    runs_with_folds: int
    child_folds: int
    main_cost: float
    child_cost: float
    cap: float

    @property
    def cost(self) -> float:
        return self.main_cost + self.child_cost


def plan_arm(
    chosen: scenario.Arm,
    ceiling: int,
    manifest: Sequence[dict[str, Any]],
    prices: Prices,
    counted: Counted | None = None,
) -> Planned:
    """``chosen``'s plan at ``ceiling`` from the seed's snapshots, as the arm's model counts
    them when ``counted`` is given."""
    prompt = counted.prompt if counted is not None else 30_000
    ratio = counted.ratio if counted is not None else 1.0
    start = pick_snapshot(manifest, ceiling, counted.views if counted is not None else None)
    compacting = chosen.compaction
    made = forecast(start["tokens"], ceiling, growth(ratio), prompt, compaction=compacting)
    child_start = prompt + 4_000
    child_made = forecast(
        child_start, ceiling, [child_growth(ratio)], prompt, compaction=compacting
    )
    if prices.cache_write is not None:
        main = estimate_written(
            views(ceiling), prices, added=sum(growth(ratio)),
            rebuilds=len(scenario.LIVE) + sum(made), prompt=prompt,
        )  # fmt: skip
        child = estimate_written(
            child_views(ceiling, child_start), prices, added=child_growth(ratio),
            rebuilds=1 + sum(child_made), prompt=prompt,
        )  # fmt: skip
    else:
        main = estimate(views(ceiling), prices, 0.5)
        child = estimate(child_views(ceiling, child_start), prices, 0.5)
    if compacting:  # each compaction writes the view at its trigger, priced at 1 hour's rate
        hour = prices.cache_write_1h or 2 * prices.input
        each = int(0.75 * ceiling) * hour + COMPACTION_OUTPUT * prices.output
        main += sum(made) * each
        child += sum(child_made) * each
    cap = chosen.budget or 5 * math.ceil(MARGIN * (main + child) / 5)
    return Planned(
        chosen.name, ceiling, start["step"], start["tokens"], sum(made),
        sum(1 for count in made if count), sum(child_made), main, child, float(cap),
    )  # fmt: skip


def _range(sizes: Sequence[int], prices: Prices | None) -> str:
    if prices is None:
        return "unknown"
    return " / ".join(f"{estimate(sizes, prices, share):.0f}" for share in (0.9, 0.5, 0.0))


# The round.

ROUND = "round.json"
"""In the lab's directory: the round's total, what was spent outside the kit, and whether
the seed is an earlier round's (``preflight --lab``)."""


@dataclass(frozen=True)
class Round:
    """The round's budget: ``total`` USD in all, of which ``elsewhere`` was spent outside
    the kit's transcripts (a fact-finding pass, live checks); a seed an earlier round
    recorded (``seed_reused``) spends nothing in this one."""

    total: float = scenario.TOTAL_BUDGET
    elsewhere: float = 0.0
    seed_reused: bool = False

    @classmethod
    def load(cls, lab: Path) -> Round:
        path = lab / ROUND
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, lab: Path) -> None:
        lab.mkdir(parents=True, exist_ok=True)
        (lab / ROUND).write_text(json.dumps(asdict(self), indent=1))


# Commands.


class Lab:
    """The launcher's instance and the lab's directory."""

    def __init__(self, config: Path, lab: Path) -> None:
        self.config: LauncherConfig = load_config(config)
        self.layout = Layout(self.config.state_dir)
        self.lab = lab
        self.base = run_argv(parts.harness_argv(self.config, self.layout))
        self.project = self.config.projects[0].id

    def client(self, name: str = "harness-worker") -> WorkerClient:
        token = self.layout.token(name).read_text().strip()
        return WorkerClient(parts.worker_url(self.config), token)

    def user(self) -> WorkerClient:
        """The workspace's client, an operator's, for the user's own file writes (the
        sources, an arm's restored workspace, a step's staged files), as the GUI's uploads
        are made: once a run has installed the session's recovery fence, the worker
        refuses a write without the run's authority from any other client (lab round 2)."""
        return self.client("workspace-worker")

    def gateway(self) -> ModelGateway:
        """The harness's gateway client, which reads the key from its file; nothing here
        prints it."""
        model = self.config.model
        return ModelGateway(model.url, Path(model.api_key_file).expanduser().read_text().strip())

    async def described(self) -> dict[str, dict[str, Any]]:
        """Each alias's model information, as the gateway gives it (``/model/info``)."""
        gateway = self.gateway()
        try:
            found = await gateway._described()  # the listing, which only the harness reads
        finally:
            await gateway.close()
        return dict(found or [])

    async def count_tokens(self, body: Mapping[str, Any]) -> int:
        """The input tokens of a Messages request, by the gateway's ``count_tokens``
        (free: no model call). The key is read from its file, as ``gateway`` does."""
        model = self.config.model
        key = Path(model.api_key_file).expanduser().read_text().strip()
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{model.url.rstrip('/')}/messages/count_tokens",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response,
        ):
            text = await response.text()
            if response.status != 200:
                raise SystemExit(f"count_tokens answered {response.status}: {text[:300]}")
            return int(json.loads(text)["input_tokens"])

    async def limit(self, alias: str, ceiling: int) -> int:
        """The arm's C: its ceiling, capped by the model's limit as the harness caps it."""
        model = self.config.model
        gateway = self.gateway()
        try:
            found = (await gateway.input_limits() or {}).get(alias)
        finally:
            await gateway.close()
        if found is None:
            return ceiling
        named = " ".join(name or "" for name in (found.provider, found.model, alias))
        if model.api.get(alias) == "messages" or "anthropic" in named or "claude" in named.lower():
            return capped(ceiling, window=found.input, max_output=found.output or MAX_TOKENS)
        return capped(ceiling, input_limit=found.input)


async def seed(lab: Lab, alias: str, session: str, target: int, budget: float) -> None:
    home = lab.lab / "seed"
    manifest_path = home / "manifest.json"
    manifest: list[dict[str, Any]] = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    )
    store = ProjectStore(lab.layout.control)
    async with lab.client() as client:
        context = await _ready(client, store, lab.project, session)
        if not store.runs(lab.project, session):  # the sources, before the first run only
            await write_files(client, context, sources.files())
        for step in scenario.SEED:
            transcript = home / "runs" / f"{step.id}.json"
            if not recovered(store, lab.project, session, step.message, transcript):
                within(lab.lab, budget, sorted((home / "runs").glob("*.json")), "the seed")
                argv = step_argv(
                    lab.base, state_dir=lab.layout.control, project=lab.project,
                    session=session, alias=alias, ceiling=scenario.FOLDING_OFF,
                    transcript=transcript, task=step.message, folding_off=True,
                )  # fmt: skip
                code = _run(argv, home / "logs" / f"{step.id}.log", transcript)
                if not recovered(store, lab.project, session, step.message, transcript):
                    raise SystemExit(f"{step.id} kept no transcript (exit {code}); see its log")
            if all(kept["step"] != step.id for kept in manifest):
                snapshot = home / "snapshots" / step.id
                backup(lab.layout.control / FILE_NAME, snapshot / FILE_NAME)
                files = keep_files(lab.lab, await read_workspace(client, context))
                (snapshot / "files.json").write_text(json.dumps(files, indent=1))
                tokens = view_tokens(transcript)
                manifest.append({"step": step.id, "tokens": tokens, "snapshot": str(snapshot)})
                manifest_path.write_text(json.dumps(manifest, indent=1))
                print(f"{step.id}: the view is {tokens:,} tokens")
            if step.bulk and manifest and manifest[-1]["tokens"] >= target:
                break
    store.close()
    last = manifest[-1]["tokens"] if manifest else 0
    if last < target:
        print(f"warning: the seed ends at {last:,} tokens, under its target of {target:,}")


async def count(lab: Lab, alias: str) -> Counted:
    """Each seed snapshot's view as ``alias`` counts it: the last call of the snapshot's
    run, rendered as the harness's Claude adapter sends it, with the main run's tools."""
    home = lab.lab / "seed"
    manifest = json.loads((home / "manifest.json").read_text())
    tools = main_tools()
    seen: dict[str, int] = {}
    for kept in manifest:
        messages = json.loads((home / "runs" / f"{kept['step']}.json").read_text())["messages"]
        while messages and messages[-1].get("role") == "assistant":
            messages = messages[:-1]  # the view the last call was sent
        seen[kept["step"]] = await lab.count_tokens(messages_body(alias, messages, tools))
        print(f"{kept['step']}: {seen[kept['step']]:,} (recorded {kept['tokens']:,})")
    first = json.loads((home / "runs" / f"{manifest[0]['step']}.json").read_text())["messages"]
    bare = [first[0], {"role": "user", "content": "."}]
    prompt = await lab.count_tokens(messages_body(alias, bare, tools))
    without = await lab.count_tokens(messages_body(alias, bare, ()))
    counted = Counted(alias, seen, prompt - without, prompt, growth_ratio(manifest, seen))
    path = lab.lab / COUNTS
    known = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**known, alias: asdict(counted)}, indent=1))
    print(f"tools {counted.tools:,}; system and tools {prompt:,}; ratio {counted.ratio:.3f}")
    return counted


def spent_all(lab: Path, round_: Round | None = None) -> float:
    """What the round has spent so far: its arms, their children, the seed unless an
    earlier round recorded it, and what was spent outside the kit."""
    round_ = round_ or Round.load(lab)
    seed = [] if round_.seed_reused else sorted(lab.glob("seed/runs/*.json"))
    arms = sorted(lab.glob("arms/*/runs/*.json")) + sorted(lab.glob("arms/*/runs/delegated/*.json"))
    return spent(seed + arms)[0] + round_.elsewhere


def within(lab: Path, budget: float, transcripts: Sequence[Path], what: str) -> None:
    """Refuse the next step once ``what`` spent its budget, or the round its total."""
    if spent(transcripts)[0] > budget:
        raise SystemExit(f"{what} spent its budget of USD {budget:g}")
    round_ = Round.load(lab)
    if (total := spent_all(lab, round_)) > round_.total:
        raise SystemExit(f"the round spent USD {total:.2f}, past its USD {round_.total:g}")


def runnable(name: str, *, proposed: bool) -> scenario.Arm:
    """The arm ``name``, if this round may run it."""
    chosen = scenario.ARM_BY_NAME[name]
    if chosen.deferred:
        raise SystemExit(f"{name} is not in this round: {chosen.deferred}")
    if chosen.proposed and not proposed:
        raise SystemExit(f"{name} is beyond the base plan: run it with --proposed once confirmed")
    return chosen


async def arm(
    lab: Lab,
    name: str,
    alias: str,
    session: str,
    budget: float | None,
    extra: Sequence[str] = (),
    *,
    proposed: bool = False,
) -> None:
    chosen = runnable(name, proposed=proposed)
    home = lab.lab / "arms" / name
    state = home / "control"
    ceiling = await lab.limit(alias, chosen.ceiling)
    manifest = json.loads((lab.lab / "seed" / "manifest.json").read_text())
    counted = load_counts(lab.lab, alias)
    if chosen.family == "claude" and counted is None:
        raise SystemExit(f"no counts of the seed for {alias}: run count first")
    if budget is None:
        prices = Prices.of((await lab.described()).get(alias, {}))
        if prices is None:
            raise SystemExit(f"{alias} gives no prices: its cap cannot be planned")
        budget = plan_arm(chosen, ceiling, manifest, prices, counted).cap
    meta_path = home / "arm.json"
    if not meta_path.exists():
        snapshot = pick_snapshot(manifest, ceiling, counted.views if counted else None)
        environment = environment_id(lab.project, "lab", name, secrets.token_hex(4))
        clone(
            Path(snapshot["snapshot"]) / FILE_NAME,
            state / FILE_NAME,
            lab.project,
            session,
            environment,
        )
        kept = json.loads((Path(snapshot["snapshot"]) / "files.json").read_text())
        meta = {
            "arm": name, "family": chosen.family, "alias": alias, "ceiling": chosen.ceiling,
            "effective_ceiling": ceiling, "skeleton_only": chosen.skeleton_only,
            "compaction": chosen.compaction,
            "seed": snapshot["step"], "seed_tokens": snapshot["tokens"],
            "project": lab.project, "session": session, "restored": False,
            "extra": list(extra), "budget": budget, "delegation": False,
            "params_sha256": kept.get("data/params.json"),
        }  # fmt: skip
        meta_path.write_text(json.dumps(meta, indent=1))
    meta = json.loads(meta_path.read_text())
    if not meta.get("delegation"):
        argv = delegation_argv(lab.base, state, lab.project)
        if (code := _run(argv, home / "logs" / "delegation.log", home / "delegation")) != 0:
            raise SystemExit(f"{name}: the delegation settings failed (exit {code}); see its log")
        meta["delegation"] = True
        meta_path.write_text(json.dumps(meta, indent=1))
    print(f"{name}: seed {meta['seed']} ({meta['seed_tokens']:,}), cap USD {budget:g}")
    adopt_policy(state / FILE_NAME, lab.layout.control / FILE_NAME, lab.project)
    epoch = hand_over_epoch(
        state / FILE_NAME, lab.layout.worker_state / "worker.sqlite3", lab.project, session
    )
    print(f"{name}: research epoch {epoch}", flush=True)
    store = ProjectStore(state)
    async with lab.client() as client, lab.user() as user:
        context = await _ready(client, store, lab.project, session)
        if not meta["restored"]:
            snapshot = next(s for s in manifest if s["step"] == meta["seed"])["snapshot"]
            kept = json.loads((Path(snapshot) / "files.json").read_text())
            blobs = {
                path: (lab.lab / "blobs" / digest).read_bytes() for path, digest in kept.items()
            }
            await write_files(user, context, blobs)
            meta["restored"] = True
            meta_path.write_text(json.dumps(meta, indent=1))
        for step in scenario.LIVE:
            transcript = home / "runs" / f"{step.id}.json"
            if recovered(store, lab.project, session, step.message, transcript):
                continue
            within(lab.lab, budget, sorted((home / "runs").glob("**/*.json")), name)
            later = {path: sources.LATER[path]() for path in step.stage}
            await write_files(user, context, later)
            argv = step_argv(
                lab.base, state_dir=state, project=lab.project, session=session, alias=alias,
                ceiling=chosen.ceiling, transcript=transcript, task=step.message,
                skeleton_only=chosen.skeleton_only, compaction=chosen.compaction,
                extra=meta["extra"],
            )  # fmt: skip
            code = await run_answering(
                argv, home / "logs" / f"{step.id}.log", transcript, state, lab.project,
                session, home / "logs" / "answers.jsonl",
            )  # fmt: skip
            keep_delegated(store, lab.project, session, home)
            if not recovered(store, lab.project, session, step.message, transcript):
                raise SystemExit(f"{name} {step.id} kept no transcript (exit {code}); see its log")
            print(
                f"{name} {step.id}: exit {code}, view {view_tokens(transcript) if transcript.exists() else 0:,}",
                flush=True,
            )
    store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cm_lab.drive")
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("sources", help="write the synthetic sources, to read them")
    built.add_argument("out", type=Path)
    counting = commands.add_parser(
        "count", help="each seed snapshot's view as a Claude alias counts it (free)"
    )
    counting.add_argument("--config", type=Path, required=True)
    counting.add_argument("--lab", type=Path, required=True)
    counting.add_argument("--alias", required=True)
    pre = commands.add_parser(
        "preflight", help="the aliases, routes and prices, and the round's plan, from the gateway"
    )
    pre.add_argument("--config", type=Path, required=True)
    pre.add_argument("--claude", help="the alias of the Claude arms")
    pre.add_argument("--primary", help="the alias of round 1's GPT arms")
    pre.add_argument("--second", help="the alias of round 1's second model's arms")
    pre.add_argument("--recording", help="the alias that records the seed; none if reused")
    pre.add_argument("--lab", type=Path, help="the lab: its seed and counts; writes round.json")
    _round_options(pre)
    dry = commands.add_parser("plan", help="the round's plan offline, with no gateway call")
    dry.add_argument("--lab", type=Path, required=True)
    dry.add_argument("--alias", default="arr-claude-opus-4-8", help="the Claude arms' alias")
    _round_options(dry, given=False)
    for name in ("seed", "arm"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--lab", type=Path, required=True)
        command.add_argument("--alias", required=True)
        command.add_argument("--session", default="seed")
        command.add_argument(
            "--budget",
            type=float,
            default=scenario.SEED_BUDGET if name == "seed" else None,
            help="USD, as calls report it (default: the seed's cap, or the arm's planned cap)",
        )
        if name == "seed":
            command.add_argument("--target", type=int, default=scenario.SEED_TARGET)
        else:
            command.add_argument("--arm", required=True, choices=sorted(scenario.ARM_BY_NAME))
            command.add_argument(
                "--extra",
                action="append",
                default=[],
                metavar="=OPTION",
                help="a further option of each run, as --extra=--fold-chunk --extra=40000 "
                "(repeat); kept with the arm at its first start",
            )
            command.add_argument(
                "--proposed", action="store_true", help="run an arm beyond the base plan"
            )
    told = commands.add_parser("forecast", help="the folds each arm should make")
    told.add_argument("--lab", type=Path, required=True)
    told.add_argument("--alias", help="the arms' alias, to start from its counts")
    out = commands.add_parser("export", help="an arm's session as one JSON bundle")
    out.add_argument("--lab", type=Path, required=True)
    out.add_argument("--arm", required=True)
    args = parser.parse_args(argv)
    match args.command:
        case "sources":
            for path, digest in sources.build(args.out).items():
                print(digest, path)
        case "count":
            asyncio.run(count(Lab(args.config, args.lab), args.alias))
        case "preflight":
            return asyncio.run(_preflight(args))
        case "plan":
            kept = Round.load(args.lab)
            round_ = Round(
                kept.total if args.total is None else args.total,
                kept.elsewhere if args.elsewhere is None else args.elsewhere,
                kept.seed_reused or args.seed_reused,
            )
            lines, problems = plan_round(
                args.lab, {"claude": args.alias}, {args.alias: OPUS_4_8}, round_
            )
            print("\n".join(lines))
            for problem in problems:
                print("problem:", problem)
            return 1 if problems else 0
        case "seed":
            asyncio.run(
                seed(Lab(args.config, args.lab), args.alias, args.session, args.target, args.budget)
            )
        case "arm":
            asyncio.run(
                arm(
                    Lab(args.config, args.lab),
                    args.arm,
                    args.alias,
                    args.session,
                    args.budget,
                    args.extra,
                    proposed=args.proposed,
                )
            )
        case "forecast":
            manifest = json.loads((args.lab / "seed" / "manifest.json").read_text())
            counted = load_counts(args.lab, args.alias) if args.alias else None
            prompt = counted.prompt if counted else 30_000
            for chosen in (arm for arm in scenario.ARMS if not arm.deferred):
                start = pick_snapshot(manifest, chosen.ceiling, counted.views if counted else None)
                folds = forecast(
                    start["tokens"], chosen.ceiling, growth(counted.ratio if counted else 1.0),
                    prompt, compaction=chosen.compaction,
                )  # fmt: skip
                runs = sum(1 for made in folds if made)
                print(
                    f"{chosen.name}: seed {start['step']} ({start['tokens']:,}), about {sum(folds)} folds in {runs} runs"
                )
        case "export":
            from .export import export

            home = args.lab / "arms" / args.arm
            meta = json.loads((home / "arm.json").read_text())
            (home / "bundle.json").write_text(json.dumps(export(home / "control", meta), indent=1))
            print(home / "bundle.json")
    return 0


def _round_options(parser: argparse.ArgumentParser, *, given: bool = True) -> None:
    """The round's budget; ``given``: with their defaults, else ``round.json``'s."""
    parser.add_argument(
        "--total",
        type=float,
        default=scenario.TOTAL_BUDGET if given else None,
        help="USD for the whole round",
    )
    parser.add_argument(
        "--elsewhere",
        type=float,
        default=0.0 if given else None,
        help="USD spent outside the kit's transcripts",
    )
    parser.add_argument(
        "--seed-reused", action="store_true", help="the seed is an earlier round's: it costs none"
    )


def plan_round(
    lab: Path,
    aliases: Mapping[str, str | None],
    prices: Mapping[str, Prices | None],
    round_: Round,
    ceilings: Mapping[str, int] | None = None,
) -> tuple[list[str], list[str]]:
    """The round's plan, as lines to print, and its problems: each arm's seed, forecast
    folds, estimated cost and cap; the arms deferred and why; the caps against the round's
    budget. ``aliases``: each model's alias (``claude``, ``primary``, ``second``);
    ``prices``: each alias's; ``ceilings``: each arm's C as the gateway caps it."""
    manifest = json.loads((lab / "seed" / "manifest.json").read_text())
    lines = [
        f"round: USD {round_.total:g} in all, {round_.elsewhere:g} spent outside the kit, "
        f"the seed {'reused (costs none)' if round_.seed_reused else 'recorded this round'}"
    ]
    problems: list[str] = []
    planned = round_.elsewhere + (0.0 if round_.seed_reused else scenario.SEED_BUDGET)
    for chosen in scenario.ARMS:
        alias = aliases.get(chosen.model)
        if chosen.deferred and chosen.name not in scenario.RESERVED:
            given = prices.get(alias) if alias else None
            if chosen.family == "claude" and alias and given is not None:
                counted = load_counts(lab, alias)
                try:
                    found = plan_arm(chosen, chosen.ceiling, manifest, given, counted)
                    lines.append(
                        f"{chosen.name}: not run, {chosen.deferred} (estimate USD "
                        f"{found.cost:.0f}, cap {found.cap:g}; {found.folds} folds forecast)"
                    )
                    continue
                except SystemExit as error:
                    lines.append(f"{chosen.name}: not run, {chosen.deferred} ({error})")
                    continue
            if chosen.family == "claude":
                lines.append(f"{chosen.name}: not run, {chosen.deferred}")
            continue
        if alias is None:
            problems.append(f"{chosen.name}: no alias given for the {chosen.model} model")
            continue
        found_prices = prices.get(alias)
        if found_prices is None:
            problems.append(f"{chosen.name}: {alias} gives no prices: its cap cannot be planned")
            continue
        counted = load_counts(lab, alias)
        if chosen.family == "claude" and counted is None:
            problems.append(f"{chosen.name}: no counts of the seed for {alias}: run count")
            continue
        ceiling = (ceilings or {}).get(chosen.name, chosen.ceiling)
        found = plan_arm(chosen, ceiling, manifest, found_prices, counted)
        planned += found.cap
        state = "reserved, " + str(chosen.deferred) if chosen.deferred else alias
        lines.append(
            f"{chosen.name} ({state}): C = {ceiling:,}; seed {found.seed} at {found.start:,} "
            f"tokens; about {found.folds} folds in {found.runs_with_folds} runs, the child "
            f"{found.child_folds}; estimate USD {found.main_cost:.2f} + child "
            f"{found.child_cost:.2f} = {found.cost:.2f}; cap {found.cap:g}"
        )
        needed, across = (4, 3) if chosen.ceiling < 600_000 else (2, 1)
        if chosen.compaction:  # a compaction frees nearly the whole view, not a chunk
            needed, across = 2, 2
        if found.folds < needed or found.runs_with_folds < across:
            problems.append(
                f"{chosen.name}: about {found.folds} folds in {found.runs_with_folds} runs "
                f"forecast, under the validity bar of {needed} in {across}"
            )
        if found.child_folds < 1:
            problems.append(f"{chosen.name}: the child is not forecast to fold")
    lines.append(f"caps in all, with what was spent elsewhere: USD {planned:g} of {round_.total:g}")
    if planned > round_.total:
        problems.append("the caps add up to more than the round's budget")
    return lines, problems


async def _preflight(args: argparse.Namespace) -> int:
    lab = Lab(args.config, args.lab or Path("."))
    described = await lab.described()
    aliases = {"claude": args.claude, "primary": args.primary, "second": args.second}
    problems = []
    for alias in sorted({a for a in (*aliases.values(), args.recording) if a}):
        info = described.get(alias)
        if info is None:
            problems.append(f"the gateway describes no {alias}")
            continue
        window = info.get("max_input_tokens")
        print(f"{alias}: input window {window or 'not given'}; prices {Prices.of(info)}")
        if not isinstance(window, int):
            problems.append(f"{alias} gives no input window: no ceiling or seed can be checked")
        if Prices.of(info) is None:
            problems.append(f"{alias} gives no prices: its costs cannot be capped")
        # Claude's aliases go through Anthropic's Messages API (CM-41), GPT's through
        # OpenAI's Responses API, as round 1 ran them.
        route = "messages" if alias == args.claude else "responses"
        if lab.config.model.api.get(alias) != route:
            problems.append(f"[model] api does not send {alias} through {route}")
    if args.claude and lab.config.model.delegated not in (None, args.claude):
        problems.append(
            f"[model] delegated is {lab.config.model.delegated}: L16's child would not run on "
            f"{args.claude} (the driver sets it to the arm's alias only when it is named)"
        )
    if args.lab is not None:
        round_ = Round(args.total, args.elsewhere, args.seed_reused or not args.recording)
        round_.save(args.lab)
        prices = {alias: Prices.of(described.get(alias, {})) for alias in aliases.values() if alias}
        ceilings = {
            chosen.name: await lab.limit(alias, chosen.ceiling)
            for chosen in scenario.ARMS
            if not chosen.deferred and (alias := aliases.get(chosen.model))
        }
        for name, size in ceilings.items():
            if size < scenario.ARM_BY_NAME[name].ceiling:
                problems.append(f"{name}'s C is capped at {size:,} by its model's window")
        lines, planned = plan_round(args.lab, aliases, prices, round_, ceilings)
        print("\n".join(lines))
        problems += planned
    if args.recording:
        recording = described.get(args.recording, {})
        target = int(scenario.SEED_SHARE * 600_000)
        window = recording.get("max_input_tokens")
        if isinstance(window, int) and window < target + 60_000:
            problems.append(f"{args.recording} cannot hold the 600,000 arms' seed")
        costs = _range(seed_views(target), Prices.of(recording))
        print(f"seed ({args.recording}): {costs}; cap {scenario.SEED_BUDGET:g}")
    for problem in problems:
        print("problem:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
