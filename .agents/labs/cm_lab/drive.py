# SPDX-License-Identifier: MIT
"""The context-management lab's driver (runbook: ../cm-lab-round-1.md).

```
D=".venv/bin/python -I .agents/labs/cm_drive.py"
$D sources OUT
$D preflight --config CFG --claude ALIAS --gpt ALIAS --recording ALIAS
$D seed --config CFG --lab DIR --alias ALIAS [--budget USD]
$D forecast --lab DIR
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
- ``arm`` copies the snapshot whose view is just under the arm's trigger into a state
  directory of its own, gives the session a new environment and its workspace's files,
  then runs the live steps there. Each arm is a store of its own, since the ledger is a
  project's: arms in one store would share it.
- ``export`` writes the arm's session as one JSON bundle, which ``score`` reads.

Every step is resumable: a run whose transcript is kept is not run again.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandbox_science.contracts import (
    EnvironmentState,
    ExecutionContext,
    ListFilesRequest,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.launcher import parts
from sandbox_science.launcher.config import LauncherConfig, load_config
from sandbox_science.launcher.state import Layout
from sandbox_science.orchestrator.history.geometry import ceiling as capped
from sandbox_science.orchestrator.history.geometry import geometry
from sandbox_science.orchestrator.messages import MAX_TOKENS
from sandbox_science.orchestrator.model import ModelGateway
from sandbox_science.orchestrator.projects import ProjectStore, ensure_environment, environment_id
from sandbox_science.orchestrator.worker_client import WorkerClient

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
    extra: Sequence[str] = (),
) -> list[str]:
    """One step's run: on ``alias``, under ``ceiling``; ``folding_off`` for the seed;
    ``extra``, the arm's further options, such as the lab's small shares."""
    argv = list(base)
    argv[argv.index("--model") + 1] = alias
    argv += ["--state-dir", str(state_dir), "--project", project, "--session", session]
    argv += ["--transcript", str(transcript), "--context-ceiling", str(ceiling)]
    if folding_off:
        argv += ["--input-limit", str(scenario.FOLDING_OFF)]
    if skeleton_only:
        argv.append("--skeleton-only-folds")
    return [*argv, *extra, task]


def _run(argv: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        return subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, check=False).returncode  # noqa: S603


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


async def write_files(
    client: WorkerClient, context: ExecutionContext, files: dict[str, bytes]
) -> None:
    for path, data in files.items():
        request = WriteFileRequest(
            context=context.model_copy(update={"actor": "user"}),
            path=path,
            content_b64=base64.b64encode(data).decode(),
        )
        await client.write_file(request)


async def read_workspace(client: WorkerClient, context: ExecutionContext) -> dict[str, bytes]:
    """The workspace's files, but hidden directories and caches, and files over 16 MB."""
    found: dict[str, bytes] = {}
    pending = ["."]
    while pending:
        here = pending.pop()
        listing = await client.list_files(ListFilesRequest(context=context, path=here))
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
        part = await client.read_file(
            ReadFileRequest(context=context, path=path, offset=offset, length=CHUNK)
        )
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


def pick_snapshot(manifest: Sequence[dict[str, Any]], ceiling: int) -> dict[str, Any]:
    """The last snapshot whose view was at most ``SEED_SHARE`` of ``ceiling``: the
    largest seed that starts the arm under its trigger."""
    fitting = [s for s in manifest if s["tokens"] <= scenario.SEED_SHARE * ceiling]
    if not fitting:
        raise SystemExit(f"no seed snapshot fits a ceiling of {ceiling:,} tokens")
    return fitting[-1]


def clone(snapshot: Path, target: Path, project: str, session: str, environment: str) -> None:
    """The snapshot's store as the arm's, its session in ``environment``: the arm's own
    sandbox, so arms never share a workspace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, target)
    with sqlite3.connect(target) as db:
        db.execute(
            "UPDATE sessions SET environment_id = ?, dedicated = 1 "
            "WHERE project_id = ? AND session_id = ?",
            (environment, project, session),
        )
        if db.total_changes != 1:
            raise SystemExit(f"the snapshot has no session {project}/{session}")


def forecast(start: int, ceiling: int, growth: Sequence[int], prompt: int = 30_000) -> list[int]:
    """The folds each live step makes, roughly: the view grows by each step's tokens, and
    a fold takes a chunk whenever it passes the trigger."""
    shape = geometry(ceiling, prompt)
    tokens, folds = start, []
    for added in growth:
        tokens += added
        made = 0
        while tokens > shape.trigger:
            tokens -= int(0.85 * shape.chunk)  # the chunk, less its record
            made += 1
        folds.append(made)
    return folds


def growth() -> list[int]:
    """Each live step's tokens, estimated from the pages it reads."""
    pages = {"L01": 40, "L03": 12, "L06": 40, "L11": 22}
    return [TOKENS_PER_RUN + TOKENS_PER_PAGE * pages.get(step.id, 0) for step in scenario.LIVE]


# Costs.

LONG = 272_000
"""A view past this many tokens is priced at the gateway's long-context rates."""
CALLS_PER_ARM = 80
"""An arm's calls: about five a live step, and the folds' enrichment calls."""
SEED_CALLS = 60
OUTPUT_PER_CALL = 2_500
"""Each call's output, its reasoning included."""


@dataclass(frozen=True)
class Prices:
    """An alias's prices per token, as the gateway's model information gives them, with
    those past ``LONG`` tokens; a cache read the gateway does not price costs input."""

    input: float
    cached: float
    output: float
    input_long: float
    cached_long: float
    output_long: float

    @classmethod
    def of(cls, info: dict[str, Any]) -> Prices | None:
        def price(key: str, fallback: float) -> float:
            value = info.get(key)
            return float(value) if isinstance(value, int | float) and value > 0 else fallback

        given, output = price("input_cost_per_token", 0.0), price("output_cost_per_token", 0.0)
        if not given or not output:
            return None
        long_input = price("input_cost_per_token_above_272k_tokens", given)
        return cls(
            given,
            price("cache_read_input_token_cost", given),
            output,
            long_input,
            price("cache_read_input_token_cost_above_272k_tokens", long_input),
            price("output_cost_per_token_above_272k_tokens", output),
        )


def views(ceiling: int) -> list[int]:
    """An arm's calls' views: spread between 0.45 C, after a fold, and 0.75 C, its trigger."""
    return [int(ceiling * (0.45 + 0.3 * n / (CALLS_PER_ARM - 1))) for n in range(CALLS_PER_ARM)]


def seed_views(target: int, prompt: int = 35_000) -> list[int]:
    """The seed's calls' views: growing from the system message to its target."""
    return [prompt + (target - prompt) * n // (SEED_CALLS - 1) for n in range(SEED_CALLS)]


def estimate(sizes: Sequence[int], prices: Prices, cached: float) -> float:
    """What calls sent ``sizes`` tokens cost, with ``cached`` of their input a cache read."""
    total = 0.0
    for size in sizes:
        long = size > LONG
        read = prices.cached_long if long else prices.cached
        fresh = prices.input_long if long else prices.input
        total += size * (cached * read + (1 - cached) * fresh)
        total += OUTPUT_PER_CALL * (prices.output_long if long else prices.output)
    return total


def _range(sizes: Sequence[int], prices: Prices | None) -> str:
    if prices is None:
        return "unknown"
    return " / ".join(f"{estimate(sizes, prices, share):.0f}" for share in (0.9, 0.5, 0.0))


# Commands.


class Lab:
    """The launcher's instance and the lab's directory."""

    def __init__(self, config: Path, lab: Path) -> None:
        self.config: LauncherConfig = load_config(config)
        self.layout = Layout(self.config.state_dir)
        self.lab = lab
        self.base = run_argv(parts.harness_argv(self.config, self.layout))
        self.project = self.config.projects[0].id

    def client(self) -> WorkerClient:
        token = self.layout.token("harness-worker").read_text().strip()
        return WorkerClient(parts.worker_url(self.config), token)

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
        if not manifest:
            await write_files(client, context, sources.files())
        for step in scenario.SEED:
            transcript = home / "runs" / f"{step.id}.json"
            if not transcript.exists():
                within(lab.lab, budget, sorted((home / "runs").glob("*.json")), "the seed")
                argv = step_argv(
                    lab.base, state_dir=lab.layout.control, project=lab.project,
                    session=session, alias=alias, ceiling=scenario.FOLDING_OFF,
                    transcript=transcript, task=step.message, folding_off=True,
                )  # fmt: skip
                code = _run(argv, home / "logs" / f"{step.id}.log")
                if not transcript.exists():
                    raise SystemExit(f"{step.id} kept no transcript (exit {code}); see its log")
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


def spent_all(lab: Path) -> float:
    """What the round has spent so far, seed and arms together."""
    return spent(sorted(lab.glob("seed/runs/*.json")) + sorted(lab.glob("arms/*/runs/*.json")))[0]


def within(lab: Path, budget: float, transcripts: Sequence[Path], what: str) -> None:
    """Refuse the next step once ``what`` spent its budget, or the round its total."""
    if spent(transcripts)[0] > budget:
        raise SystemExit(f"{what} spent its budget of USD {budget:g}")
    if (total := spent_all(lab)) > scenario.TOTAL_BUDGET:
        raise SystemExit(f"the round spent USD {total:.2f}, past its USD {scenario.TOTAL_BUDGET:g}")


def runnable(name: str, *, proposed: bool) -> scenario.Arm:
    """The arm ``name``, if this round may run it."""
    chosen = scenario.ARM_BY_NAME[name]
    if chosen.deferred:
        raise SystemExit(f"{name} is deferred: this round runs no Claude model")
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
    budget = chosen.budget if budget is None else budget
    home = lab.lab / "arms" / name
    state = home / "control"
    ceiling = await lab.limit(alias, chosen.ceiling)
    manifest = json.loads((lab.lab / "seed" / "manifest.json").read_text())
    meta_path = home / "arm.json"
    if not meta_path.exists():
        snapshot = pick_snapshot(manifest, ceiling)
        environment = environment_id(lab.project, "lab", name, secrets.token_hex(4))
        clone(
            Path(snapshot["snapshot"]) / FILE_NAME,
            state / FILE_NAME,
            lab.project,
            session,
            environment,
        )
        meta = {
            "arm": name, "family": chosen.family, "alias": alias, "ceiling": chosen.ceiling,
            "effective_ceiling": ceiling, "skeleton_only": chosen.skeleton_only,
            "seed": snapshot["step"], "seed_tokens": snapshot["tokens"],
            "project": lab.project, "session": session, "restored": False,
            "extra": list(extra),
        }  # fmt: skip
        meta_path.write_text(json.dumps(meta, indent=1))
    meta = json.loads(meta_path.read_text())
    store = ProjectStore(state)
    async with lab.client() as client:
        context = await _ready(client, store, lab.project, session)
        if not meta["restored"]:
            snapshot = next(s for s in manifest if s["step"] == meta["seed"])["snapshot"]
            kept = json.loads((Path(snapshot) / "files.json").read_text())
            blobs = {
                path: (lab.lab / "blobs" / digest).read_bytes() for path, digest in kept.items()
            }
            await write_files(client, context, blobs)
            meta["restored"] = True
            meta_path.write_text(json.dumps(meta, indent=1))
        for step in scenario.LIVE:
            transcript = home / "runs" / f"{step.id}.json"
            if transcript.exists():
                continue
            within(lab.lab, budget, sorted((home / "runs").glob("*.json")), name)
            later = {path: sources.LATER[path]() for path in step.stage}
            await write_files(client, context, later)
            argv = step_argv(
                lab.base, state_dir=state, project=lab.project, session=session, alias=alias,
                ceiling=chosen.ceiling, transcript=transcript, task=step.message,
                skeleton_only=chosen.skeleton_only, extra=meta["extra"],
            )  # fmt: skip
            code = _run(argv, home / "logs" / f"{step.id}.log")
            print(
                f"{name} {step.id}: exit {code}, view {view_tokens(transcript) if transcript.exists() else 0:,}"
            )
    store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cm_lab.drive")
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("sources", help="write the synthetic sources, to read them")
    built.add_argument("out", type=Path)
    pre = commands.add_parser(
        "preflight", help="each arm's ceiling, and the round's cost, from the gateway"
    )
    pre.add_argument("--config", type=Path, required=True)
    pre.add_argument("--primary", required=True, help="the alias of the base plan's arms")
    pre.add_argument("--second", help="the alias of the proposed second model's arms")
    pre.add_argument("--recording", required=True, help="the alias that records the seed")
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
            help="USD, as calls report it (default: the seed's cap, or the arm's)",
        )
        if name == "seed":
            command.add_argument("--target", type=int, default=int(scenario.SEED_SHARE * 600_000))
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
    out = commands.add_parser("export", help="an arm's session as one JSON bundle")
    out.add_argument("--lab", type=Path, required=True)
    out.add_argument("--arm", required=True)
    args = parser.parse_args(argv)
    match args.command:
        case "sources":
            for path, digest in sources.build(args.out).items():
                print(digest, path)
        case "preflight":
            return asyncio.run(_preflight(args))
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
            for chosen in (arm for arm in scenario.ARMS if not arm.deferred):
                start = pick_snapshot(manifest, chosen.ceiling)
                folds = forecast(start["tokens"], chosen.ceiling, growth())
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


async def _preflight(args: argparse.Namespace) -> int:
    lab = Lab(args.config, Path("."))
    described = await lab.described()
    aliases = {"primary": args.primary, "second": args.second}
    problems = []
    for alias in sorted({a for a in (args.primary, args.second, args.recording) if a}):
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
        if lab.config.model.api.get(alias) != "responses":
            problems.append(f"[model] api does not send {alias} through responses")
    print("USD at 90% / 50% / 0% of input read from the cache:")
    planned = 0.0
    for chosen in (arm for arm in scenario.ARMS if not arm.deferred):
        alias = aliases.get(chosen.model)
        if alias is None:
            print(f"{chosen.name}: no alias given for the {chosen.model} model")
            continue
        size = await lab.limit(alias, chosen.ceiling)
        costs = _range(views(size), Prices.of(described.get(alias, {})))
        mark = ", proposed" if chosen.proposed else ""
        print(f"{chosen.name} ({alias}{mark}): C = {size:,}; {costs}; cap {chosen.budget:g}")
        planned += chosen.budget
        if size < chosen.ceiling:
            problems.append(f"{chosen.name}'s C is capped at {size:,} by {alias}'s window")
    recording = described.get(args.recording, {})
    target = int(scenario.SEED_SHARE * 600_000)
    window = recording.get("max_input_tokens")
    if isinstance(window, int) and window < target + 60_000:
        problems.append(f"{args.recording} cannot hold the 600,000 arms' seed")
    costs = _range(seed_views(target), Prices.of(recording))
    print(f"seed ({args.recording}): {costs}; cap {scenario.SEED_BUDGET:g}")
    planned += scenario.SEED_BUDGET
    print(f"caps in all: USD {planned:g} of {scenario.TOTAL_BUDGET:g}")
    if planned > scenario.TOTAL_BUDGET:
        problems.append("the caps add up to more than the round's budget")
    for problem in problems:
        print("problem:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
