# SPDX-License-Identifier: MIT
"""Command line: run one task through the harness against a worker, and keep projects.

```
sandbox-science-harness run --worker http://127.0.0.1:8100 --token-file worker.token \\
    --project p1 --session s1 (--environment env-1 | --state-dir state [--dedicated]) \\
    (--script turns.json | --model-url http://127.0.0.1:4000 --model main --api-key-file key) \\
    [--min-isolation-tier 3 | --allow-unisolated] [--transcript out.json] "the task"
sandbox-science-harness project create --state-dir state --project p1 --profile minimal \\
    --policy sha256:...
sandbox-science-harness project show --state-dir state --project p1
sandbox-science-harness project revise --state-dir state --project p1 --policy sha256:... \\
    [--worker URL --token-file worker.token]
sandbox-science-harness session open --state-dir state --project p1 --session s1 [--dedicated]
sandbox-science-harness session close --state-dir state --project p1 --session s1 \\
    --worker URL --token-file worker.token
sandbox-science-harness document import --state-dir state --project p1 [--session s1] \\
    --name ff [--title "…"] [--reason "…"] [--base N] ff.md
sandbox-science-harness document export DOC [--revision N] [--output ff.md] \\
    --state-dir state --project p1
sandbox-science-harness document (list | show DOC [--revision N] | history DOC) \\
    --state-dir state --project p1
sandbox-science-harness document suggestions DOC (on | off) --state-dir state --project p1
sandbox-science-harness document decide DOC PATCH (accept [--base N] | reject) \\
    --state-dir state --project p1
sandbox-science-harness document delete DOC --reason "…" --state-dir state --project p1
```

``run`` builds a ``Harness`` (harness.py) from its arguments: the worker client, the
models and the stores. The harness runs the task; the CLI prints its progress and answer.
With ``--state-dir``, the session runs in its project's default environment, or in
an environment of its own with ``--dedicated`` (W3, projects.py). The harness has
the worker create or revise it first, and keeps the run's record, the same that
``--transcript`` writes, with the project's in the control store (W4). The run ID names that
record, so it must be an identifier. The agent also gets the document tools, over the
living documents in the state directory's control store (documents/store.py). The
harness refuses a worker whose isolation tier in effect is below the minimum, so a
misconfigured deployment cannot quietly run research code without a sandbox.

The ``project``, ``session`` and ``document`` commands are in cli_documents.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import signal
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier

from .chat import session_lease
from .cli_documents import add_parsers, document, ledger, project, read_secret, session
from .cli_skills import add_parser as add_skills_parser
from .cli_skills import embedding_alias, skills
from .connectors import ProxyClient, check_proxy
from .consistency.models import ReviewConfig
from .control import ControlError
from .documents import DocumentError, DocumentStore, store_path
from .harness import (
    Finished,
    Harness,
    IsolationRefusedError,
    McpAccess,
    Role,
    RunRequest,
    Settings,
    Stores,
    admit,
    closing,
    save,
)
from .jobstate import JobState
from .loop import MAX_INPUT_CHARS, Interruption
from .mathcheck import MathChecker
from .model import (
    API_KINDS,
    MAX_RESPONSE_BYTES,
    ChatModel,
    GatewayModel,
    ModelError,
    ModelGateway,
    ScriptedModel,
    gateway_model,
)
from .projects import ProjectError, ProjectStore, run_name
from .runstate import RunState
from .service import POLL_SECONDS, RunService
from .skills.embeddings import Embedder, SkillVectors
from .stock_prompts import TEMPLATE_IDS
from .tools import ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient

_SERVER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
"""A server's name in a project policy's ``mcp.servers``."""


class _Printer:
    """A run's progress on standard output, and its errors on standard error."""

    def note(self, text: str) -> None:
        print(text)

    def error(self, text: str) -> None:
        print(f"error: {text}", file=sys.stderr)

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        print(f"\n→ {name} {arguments}\n← {outcome.text}")


def _gateway(
    args: argparse.Namespace,
    alias: str,
    max_tokens: int | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> ChatModel:
    """The client for ``alias``, through the adapter ``--api`` names for it (CM-41)."""
    key = read_secret(args.api_key_file)
    return gateway_model(
        args.model_url,
        alias,
        key,
        api=_apis(args).get(alias, "chat"),
        max_tokens=max_tokens,
        max_response_bytes=max_response_bytes,
    )


def _apis(args: argparse.Namespace) -> dict[str, str]:
    """The adapter ``--api`` names for each alias; any other alias's is ``chat``."""
    return dict(cast(list[tuple[str, str]], getattr(args, "api", None) or []))


def _api(value: str) -> tuple[str, str]:
    """An ``--api`` setting, ``ALIAS=KIND``."""
    alias, equals, kind = value.partition("=")
    if not equals or not alias.strip() or kind not in API_KINDS:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not ALIAS=KIND, with KIND one of {', '.join(API_KINDS)}"
        )
    return alias, kind


def _role(script: Path | None, flag: str, alias: str, gateway: Callable[[], ChatModel]) -> Role:
    if script is None:
        return Role(gateway(), alias)
    try:
        return Role(ScriptedModel.from_json(json.loads(script.read_text())), alias)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ModelError(f"the {flag} file cannot be read: {exc}") from None


async def _close(model: ChatModel) -> None:
    if isinstance(model, GatewayModel):
        await model.close()


async def _run(args: argparse.Namespace) -> int:
    """``_run_with`` the state directory's project store, closed when the run is kept."""
    projects = ProjectStore(args.state_dir) if args.state_dir is not None else None
    try:
        return await _run_with(args, projects)
    finally:
        if projects is not None:
            projects.close()


async def _run_with(args: argparse.Namespace, projects: ProjectStore | None) -> int:
    """Run one task against the worker, then save its record and acknowledge its notices.

    The session is resolved and the worker's isolation checked before the stores open
    and the watcher's and reviewer's scripts are read; with stores, the session's run
    lease is taken then, and held until the record is saved (RS-5). The worker client,
    the models and the stores close after the run and before its record is saved, in
    reverse order; a failure there is recorded as the run's.
    """
    async with AsyncExitStack() as held:
        return await _held_run(args, projects, held)


async def _held_run(
    args: argparse.Namespace, projects: ProjectStore | None, held: AsyncExitStack
) -> int:
    sink = _Printer()
    alias = "scripted" if args.script is not None else args.model
    main = _role(args.script, "--script", alias, lambda: _gateway(args, args.model))
    request = RunRequest(
        project=args.project,
        session=args.session,
        task=args.task,
        environment=args.environment,
        dedicated=args.dedicated,
        run_id=args.run_id,
        research_template=args.research_template,
    )
    minimum: int | None = None if args.allow_unisolated else args.min_isolation_tier
    finished: Finished | None = None
    state_dir: Path | None = args.state_dir
    interruption = Interruption()
    try:
        async with AsyncExitStack() as stack:
            with interruption.protect():
                client = await stack.enter_async_context(
                    interruption.context(WorkerClient(args.worker, read_secret(args.token_file)))
                )
                closing(stack, interruption, lambda: _close(main.model))
                try:
                    admitted = await admit(
                        client, projects, request, sink, min_isolation_tier=minimum
                    )
                except IsolationRefusedError as refused:
                    sink.error(f"{refused} (--allow-unisolated for development)")
                    return 2
                stores = None
                if projects is not None and state_dir is not None:
                    documents = DocumentStore(store_path(state_dir), producer="harness")
                    closing(stack, interruption, lambda: _closed(documents))
                    runs = RunState(projects.control)
                    await held.enter_async_context(
                        session_lease(
                            runs,
                            admitted.context,
                            run_name(admitted.started_at, admitted.run_id),
                        )
                    )
                    stores = Stores(
                        projects, documents, JobState(state_dir, control=documents.control), runs
                    )
                harness = _harness(args, client, main, stores, stack, interruption)
                finished = await harness.start(admitted, sink)
    except BaseException as error:
        if interruption.error is not None:
            raise interruption.error from None
        if not isinstance(error, Exception) or finished is None:
            raise
        finished.transcript.fail("cleanup")
    transcript = finished.transcript
    if transcript.failure is not None:
        sink.error(f"run failed during {transcript.failure}")
    print(f"\n{transcript.final_text or '(no answer)'}")
    return await save(
        finished.fields(),
        finished.status,
        finished.hooks,
        projects=projects,
        error=sink.error,
        transcript=args.transcript,
    )


async def _closed(documents: DocumentStore) -> None:
    documents.close()


def _harness(
    args: argparse.Namespace,
    client: WorkerClient,
    main: Role,
    stores: Stores | None,
    stack: AsyncExitStack,
    interruption: Interruption,
) -> Harness:
    """The harness the arguments describe; ``stack`` closes the models it opens."""
    watcher = reviewer = None
    if args.watcher_model is not None:
        watcher = _role(
            args.watcher_script,
            "--watcher-script",
            args.watcher_model,
            lambda: _gateway(args, args.watcher_model, max_tokens=300, max_response_bytes=16384),
        )
        closing(stack, interruption, lambda: _close(watcher.model))
    if args.reviewer_model is not None:
        reviewer = _role(
            args.reviewer_script,
            "--reviewer-script",
            args.reviewer_model,
            lambda: _gateway(args, args.reviewer_model, max_tokens=2048, max_response_bytes=65536),
        )
        closing(stack, interruption, lambda: _close(reviewer.model))
    delegated = None
    if (alias := getattr(args, "delegated_model", None)) is not None:
        delegated = _role(
            getattr(args, "delegated_script", None),
            "--delegated-script",
            alias,
            lambda: _gateway(args, alias),
        )
        closing(stack, interruption, lambda: _close(delegated.model))
    gateway = None
    if getattr(args, "model_url", None) is not None:
        # The aliases a delegation chooses, and the list it checks them in (PD-28, PD-30).
        gateway = ModelGateway(
            args.model_url,
            read_secret(args.api_key_file),
            apis=_apis(args),
        )
        closing(stack, interruption, gateway.close)
    vectors = None
    if (embedding := getattr(args, "skills_embedding_model", None)) is not None:
        embedder = Embedder(args.model_url, embedding, read_secret(args.api_key_file))
        vectors = SkillVectors(embedder)
        closing(stack, interruption, vectors.close)
    mcp = None
    if args.mcp_proxy is not None:
        token = read_secret(args.mcp_token_file)
        try:
            check_proxy(args.mcp_proxy, token)
        except ValueError as exc:
            raise ProjectError(ErrorKind.INVALID_REQUEST, str(exc)) from None
        if args.mcp_token_file.stat().st_mode & 0o077:
            print(
                f"warning: {args.mcp_token_file} is readable by others; keep the MCP proxy "
                "token to its owner (chmod 600)",
                file=sys.stderr,
            )
        proxy = ProxyClient(args.mcp_proxy, token)
        closing(stack, interruption, proxy.close)
        about = {name: text for name, _, text in (item.partition("=") for item in args.mcp_about)}
        shown = tuple(args.mcp_index) if args.mcp_index else None
        mcp = McpAccess(proxy, tuple(args.mcp_server), args.mcp_project, about, shown)
    return Harness(
        client,
        main,
        stores=stores,
        watcher=watcher,
        reviewer=reviewer,
        delegated=delegated,
        gateway=gateway,
        checker=(
            MathChecker(args.math_node, args.math_launcher, args.math_bundle)
            if args.math_node is not None
            else None
        ),
        settings=Settings(
            min_isolation_tier=None if args.allow_unisolated else args.min_isolation_tier,
            max_turns=args.max_turns,
            max_input_chars=args.max_input_chars,
            watcher_calls_per_hour=args.watcher_calls_per_hour,
            watcher_first_check=args.watcher_first_check,
            watcher_interval=args.watcher_interval,
            review_token_budget=args.review_token_budget,
            review_calls_per_hour=args.review_calls_per_hour,
            # serve keeps the budget during its runs; run on the command line does not.
            # serve keeps the projects' cost limits during its runs; run does not.
            cost_limits=getattr(args, "command", None) == "serve",
            run_share=None if (share := getattr(args, "run_share", None)) is None else share / 100,
        ),
        mcp=mcp,
        vectors=vectors,
        skills_metadata=getattr(args, "skills_metadata_model", None),
    )


async def _serve(args: argparse.Namespace) -> int:
    """Run the sessions' main agent for the GUI until stopped (RS-1)."""
    alias = "scripted" if args.script is not None else args.model
    main = _role(args.script, "--script", alias, lambda: _gateway(args, args.model))
    interruption = Interruption()
    async with AsyncExitStack() as stack:
        projects = ProjectStore(args.state_dir)
        stack.callback(projects.close)
        documents = DocumentStore(store_path(args.state_dir), producer="harness")
        stack.callback(documents.close)
        client = await stack.enter_async_context(
            WorkerClient(args.worker, read_secret(args.token_file))
        )
        closing(stack, interruption, lambda: _close(main.model))
        stores = Stores(
            projects,
            documents,
            JobState(args.state_dir, control=documents.control),
            RunState(projects.control),
        )
        harness = _harness(args, client, main, stores, stack, interruption)
        print(f"serving the sessions of {args.state_dir}", file=sys.stderr)
        serving = asyncio.ensure_future(RunService(harness, poll=args.poll).serve())
        # SIGTERM stops it as Ctrl-C does: runs in progress end interrupted.
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, serving.cancel)
        try:
            await serving
        except asyncio.CancelledError:
            if (current := asyncio.current_task()) is not None and current.cancelling():
                raise  # Ctrl-C
            print("stopped: runs in progress end interrupted", file=sys.stderr)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    return 0


def _agent_arguments(command: argparse.ArgumentParser) -> None:
    """What ``run`` and ``serve`` both take: the worker, the models and the limits."""
    command.add_argument("--worker", required=True, help="worker API base URL")
    command.add_argument("--token-file", type=Path, required=True)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="replay model turns from a JSON file")
    source.add_argument(
        "--model-url",
        help="OpenAI-compatible base URL (the LiteLLM proxy), as the gateway answers it: "
        "redirects are not followed",
    )
    command.add_argument("--model", default="main", help="model alias")
    command.add_argument(
        "--api",
        action="append",
        type=_api,
        default=[],
        metavar="ALIAS=KIND",
        help="the gateway API a model alias is called through, in any role: chat "
        "(/chat/completions, every alias's by default) or responses (/v1/responses, GPT's "
        "Responses API, its reasoning sent back); repeat for more aliases",
    )
    command.add_argument(
        "--skills-embedding-model",
        type=embedding_alias,
        help="the gateway's embedding model alias: skills are compared by meaning too",
    )
    command.add_argument(
        "--skills-metadata-model",
        type=embedding_alias,
        help="the alias of the imports' model pass whose topics, entry skills and short "
        "descriptions runs are given; none without it",
    )
    command.add_argument("--api-key-file", type=Path)
    command.add_argument("--min-isolation-tier", type=int, default=3)
    command.add_argument("--allow-unisolated", action="store_true")
    command.add_argument("--max-turns", type=int, default=30)
    command.add_argument(
        "--max-input-chars",
        type=int,
        default=MAX_INPUT_CHARS,
        help="stop the run cleanly (context_limit) before MAIN is sent a longer conversation "
        f"(default {MAX_INPUT_CHARS:,})",
    )
    command.add_argument(
        "--watcher-model",
        help="the job watcher's model alias, which may be MAIN's; omitted: no check-ins",
    )
    command.add_argument(
        "--watcher-script", type=Path, help="scripted watcher replies for validation"
    )
    command.add_argument(
        "--watcher-calls-per-hour",
        type=int,
        default=60,
        help="durable per-project watcher call budget (default 60)",
    )
    command.add_argument("--watcher-first-check", type=float, default=300)
    command.add_argument("--watcher-interval", type=float, default=1800)
    command.add_argument(
        "--math-node",
        type=Path,
        help="operator-configured absolute Node path for document math checks",
    )
    command.add_argument(
        "--math-launcher", type=Path, help="operator-configured bounded math launcher"
    )
    command.add_argument(
        "--math-bundle", type=Path, help="operator-configured built math checker bundle"
    )
    command.add_argument(
        "--mcp-proxy", help="the MCP proxy's address; agents get its servers' tools (D-08)"
    )
    command.add_argument(
        "--mcp-token-file", type=Path, help="the harness's caller token for the MCP proxy"
    )
    command.add_argument(
        "--mcp-project",
        help="the project the MCP proxy token is for: only its runs get the servers' tools",
    )
    command.add_argument(
        "--mcp-server",
        action="append",
        default=[],
        help="an MCP server behind the proxy whose tools runs get; repeat for more",
    )
    command.add_argument(
        "--mcp-about",
        action="append",
        default=[],
        metavar="SERVER[/BEHIND]=TEXT",
        help="what an MCP server is for, which the tool index says: a --mcp-server, or "
        "SERVER/BEHIND for a server behind a portal, the one a tool's name starts with "
        "(crypto-research-mcp/e-print-mcp); repeat for more",
    )
    command.add_argument(
        "--mcp-index",
        action="append",
        default=[],
        metavar="SERVER[/BEHIND]",
        help="a server whose tools the index lists one by one, named as for --mcp-about; the "
        "others are found with a query. By default it lists every server's; repeat for more",
    )
    command.add_argument(
        "--reviewer-model", help="the consistency reviewer's model alias, which may be MAIN's"
    )
    command.add_argument(
        "--delegated-model", help="the model alias delegated runs call (default: MAIN's)"
    )
    command.add_argument(
        "--delegated-script", type=Path, help="scripted delegated replies for validation"
    )
    command.add_argument(
        "--reviewer-script", type=Path, help="scripted reviewer replies for validation"
    )
    review = ReviewConfig()
    command.add_argument(
        "--review-token-budget",
        type=int,
        default=review.token_budget,
        help="the tokens one consistency review may use over all its calls "
        f"(default {review.token_budget:,})",
    )
    command.add_argument(
        "--review-calls-per-hour",
        type=int,
        default=review.calls_per_hour,
        help=f"the reviewer calls a project may make in an hour (default {review.calls_per_hour})",
    )


def _check_agent(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse arguments of ``run`` or ``serve`` that do not go together."""
    if args.watcher_calls_per_hour < 1:
        parser.error("--watcher-calls-per-hour must be positive")
    try:
        ReviewConfig(
            token_budget=args.review_token_budget, calls_per_hour=args.review_calls_per_hour
        )
    except ValueError:
        parser.error(
            "--review-token-budget is 1,024 to 4,000,000, and --review-calls-per-hour 1 to 10,000"
        )
    if any(
        not math.isfinite(value) or value <= 0
        for value in (args.watcher_first_check, args.watcher_interval)
    ):
        parser.error("watcher intervals must be positive and finite")
    if args.watcher_model is not None:
        if args.state_dir is None:
            parser.error("--watcher-model needs --state-dir for durable accounting")
        if args.watcher_script is None and args.model_url is None:
            parser.error("--watcher-model needs --model-url or --watcher-script")
    elif args.watcher_script is not None:
        parser.error("--watcher-script needs --watcher-model")
    configured_math = (args.math_node, args.math_launcher, args.math_bundle)
    if any(v is not None for v in configured_math) and (
        any(v is None or not v.is_absolute() for v in configured_math) or args.state_dir is None
    ):
        parser.error("math checks need --state-dir and all three absolute --math-* paths")
    if args.reviewer_model is not None:
        if args.state_dir is None:
            parser.error("--reviewer-model needs --state-dir for durable snapshots")
        if args.reviewer_script is None and args.model_url is None:
            parser.error("--reviewer-model needs --model-url or --reviewer-script")
    elif args.reviewer_script is not None:
        parser.error("--reviewer-script needs --reviewer-model")
    if args.delegated_model is not None:
        if args.delegated_script is None and args.model_url is None:
            parser.error("--delegated-model needs --model-url or --delegated-script")
    elif args.delegated_script is not None:
        parser.error("--delegated-script needs --delegated-model")
    if args.model_url is not None and args.api_key_file is None:
        parser.error("--model-url needs --api-key-file")
    if args.api and args.model_url is None:
        parser.error("--api needs --model-url")
    if len({alias for alias, _ in args.api}) < len(set(args.api)):
        parser.error("--api names two adapters for one alias")
    if getattr(args, "skills_embedding_model", None) is not None and args.model_url is None:
        parser.error("--skills-embedding-model needs --model-url")
    if args.max_input_chars < 1:
        parser.error("--max-input-chars must be at least 1")
    mcp = (args.mcp_proxy, args.mcp_token_file, args.mcp_project)
    if (any(value is not None for value in mcp) and any(value is None for value in mcp)) or (
        args.mcp_proxy is None and args.mcp_server
    ):
        parser.error("--mcp-proxy, --mcp-token-file, --mcp-project and --mcp-server go together")
    if args.mcp_proxy is not None and not args.mcp_server:
        parser.error("--mcp-proxy needs at least one --mcp-server")
    for name in args.mcp_server:
        if not _SERVER.fullmatch(name):
            parser.error(f"--mcp-server {name!r} is not a server name of the project policy")
    if (args.mcp_about or args.mcp_index) and args.mcp_proxy is None:
        parser.error("--mcp-about and --mcp-index need --mcp-proxy")

    # A key names a configured server, or SERVER/BEHIND for a server behind it: what an
    # operator says about one server never reaches another's tools.
    def configured(key: str) -> bool:
        server, slash, behind = key.partition("/")
        return server in args.mcp_server and (not slash or bool(_SERVER.fullmatch(behind)))

    for item in args.mcp_about:
        key, equals, text = item.partition("=")
        if not equals or not configured(key) or not text.strip() or len(text) > 200:
            parser.error(
                "--mcp-about takes SERVER=TEXT or SERVER/BEHIND=TEXT, for a --mcp-server, "
                f"with at most 200 characters: {item!r}"
            )
    for key in args.mcp_index:
        if not configured(key):
            parser.error(f"--mcp-index {key!r} is not a --mcp-server, or SERVER/BEHIND for one")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one task to an answer")
    _agent_arguments(run)
    run.add_argument("--project", required=True)
    run.add_argument("--session", required=True)
    where = run.add_mutually_exclusive_group(required=True)
    where.add_argument("--environment", help="run in this environment as it is")
    where.add_argument("--state-dir", type=Path, help="the projects' records (W3)")
    run.add_argument(
        "--dedicated",
        action="store_true",
        help="with --state-dir: a new session gets an environment of its own",
    )
    run.add_argument(
        "--research-template",
        choices=TEMPLATE_IDS,
        help="optional original research framing; preserves offered tools and permissions",
    )
    run.add_argument(
        "--run-id",
        help="name the run (default: a new random ID). Under a reused ID, a call identical "
        "to one already made in the same place returns its earlier result, marked replayed",
    )
    run.add_argument("--transcript", type=Path)
    run.add_argument("task")
    serve = commands.add_parser(
        "serve", help="run the sessions' main agent for the GUI's messages, until stopped"
    )
    _agent_arguments(serve)
    serve.add_argument("--state-dir", type=Path, required=True, help="the projects' records")
    serve.add_argument(
        "--poll", type=float, default=POLL_SECONDS, help="seconds between looks at the store"
    )
    serve.add_argument(
        "--run-share",
        type=int,
        help="the percent of a project's daily cost limit one run may use, when the project "
        "has a limit (none by default); each run stops once the project reaches its limit",
    )
    add_parsers(commands)
    add_skills_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "skills":
        if args.action == "import" and args.archive is not None and args.ref is not None:
            parser.error("--ref goes with --git")
        if (
            args.action == "import"
            and (args.embedding_model is not None or args.metadata_model is not None)
            and (args.model_url is None or args.api_key_file is None)
        ):
            parser.error(
                "--embedding-model and --metadata-model need --model-url and --api-key-file"
            )
        try:
            return skills(args)
        except (ProjectError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "ledger":
        try:
            return ledger(args)
        except (ProjectError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "document":
        try:
            return document(args)
        except (ProjectError, OSError, UnicodeDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    match args.command:
        case "run":
            _check_agent(parser, args)
            if args.dedicated and args.state_dir is None:
                parser.error("--dedicated needs --state-dir")
            if args.state_dir is not None and args.run_id is not None:
                try:
                    TypeAdapter(Identifier).validate_python(args.run_id)
                except ValidationError:
                    parser.error(
                        "with --state-dir, --run-id names the run's record: use letters, "
                        "digits, '.', '_' and '-', starting with a letter or digit"
                    )
            action = _run(args)
        case "serve":
            _check_agent(parser, args)
            if not math.isfinite(args.poll) or args.poll <= 0:
                parser.error("--poll must be positive and finite")
            if args.run_share is not None and not 1 <= args.run_share <= 100:
                parser.error("--run-share is a percent, 1 to 100")
            action = _serve(args)
        case "project":
            if args.action == "create" and (args.profile is None or args.policy is None):
                parser.error("project create needs --profile and --policy")
            if args.action == "revise" and args.policy is None:
                parser.error("project revise needs --policy")
            if args.worker is not None and args.token_file is None:
                parser.error("--worker needs --token-file")
            action = project(args)
        case _:
            if args.action == "close" and (args.worker is None or args.token_file is None):
                parser.error("session close needs --worker and --token-file")
            if args.action == "open" and args.worker is not None:
                parser.error("session open needs no worker: the first run creates its environment")
            action = session(args)
    try:
        return asyncio.run(action)
    except (WorkerAPIError, ModelError, ProjectError, DocumentError, ControlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command != "serve":
            raise
        print("stopped: runs in progress end interrupted", file=sys.stderr)
        return 0
