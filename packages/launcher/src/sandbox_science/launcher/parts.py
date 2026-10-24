# SPDX-License-Identifier: MIT
"""Each part's settings and command line, from the config.

The worker gets a settings file; the run service and the workspace take flags. Each
token a part needs is a file in the state directory (state.py), and the worker's
settings hold the digests of the two clients' tokens:

| Token | Read by | Checked by |
| --- | --- | --- |
| ``worker`` | nobody: the worker's own pool token, which acts for every project | the worker |
| ``harness-worker`` | the run service, for the projects in the config | the worker |
| ``workspace-worker`` | the workspace, for the same projects | the worker |
| ``launcher-worker`` | the launcher, for the same projects' sandboxes | the worker |

The run service and the workspace share the control store and need no token between
them; the browser logs in to the workspace with the address the workspace makes at
each start.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import mcp, openshell_version, toml, unit
from .config import LauncherConfig, LauncherError
from .state import Layout, ensure_token, write_private

WORKER = "sandbox-science-worker"
HARNESS = "sandbox-science-harness"
WORKSPACE = "sandbox-science-workspace"
CLIENTS = ("harness", "workspace", "launcher")
"""The worker's clients, each with a token limited to the config's projects: the launcher's
own lists and suspends the projects' research sandboxes (sandboxes.py)."""


def commands(config: LauncherConfig) -> Path:
    """Where the parts' commands are: ``bin_dir``, or beside this Python."""
    return config.bin_dir if config.bin_dir is not None else Path(sys.executable).parent


def check(config: LauncherConfig) -> None:
    """Refuse what would only fail later: a missing file, build or command."""
    environments = config.openshell.environments
    problems = [
        f"no {what} at {path}"
        for what, path in (
            ("model key file", config.model.api_key_file),
            ("project policy", config.worker.policy),
            ("targets file", environments.targets),
            ("pool ceiling", environments.ceiling),
        )
        if not path.is_file()
    ]
    if config.orchestrator is None:
        problems.append(
            "configure [orchestrator] image and revision; host agent execution is unavailable"
        )
    problems += [p for p in key_problems(config) if not p.startswith("no model key file")]
    if not environments.policy_store.is_dir():
        problems.append(f"no policy store at {environments.policy_store}")
    if not (config.workspace.static / "index.html").is_file():
        problems.append(
            f"no web app build at {config.workspace.static}: in web/workspace, run "
            "npm ci --ignore-scripts && npm run build"
        )
    cli_problem = openshell_version.problem(config.openshell.cli)
    if cli_problem is not None:
        problems.append(cli_problem)
    names = [WORKER, HARNESS, WORKSPACE, "sandbox-science-control"]
    if config.mcp_proxy is not None:
        names.append(mcp.COMMAND)
        problems.extend(
            f"no MCP proxy {what} at {path}"
            for what, path in mcp.sources(config).items()
            if not path.is_file()
        )
    problems.extend(
        f"no {name} in {commands(config)}"
        for name in names
        if shutil.which(name, path=str(commands(config))) is None
    )
    problems.extend(_math_problems(config))
    problems.extend(unit_problems(config))
    if problems:
        raise LauncherError("cannot start:\n" + "\n".join(f"  {problem}" for problem in problems))


def unit_problems(config: LauncherConfig) -> list[str]:
    """What is wrong with the unit config the worker runs in, if there is one."""
    if config.worker.unit is None:
        return []
    try:
        found = unit.load(config.worker.unit, read=False)
    except LauncherError as exc:
        return [str(exc)]
    worker = found.services["worker"].settings_file
    expected = Layout(config.state_dir).worker_settings
    if worker is None or Path(os.path.abspath(worker)) != Path(os.path.abspath(expected)):
        return [
            f"{found.path}: [worker] must be settings_file = {str(expected)!r}, the worker "
            "settings that sandbox-science prepare writes, so the clients' tokens reach it"
        ]
    return []


def key_problems(config: LauncherConfig) -> list[str]:
    """What is wrong with the model key file, looked at and never read."""
    key = config.model.api_key_file
    if not key.is_file():
        return [f"no model key file at {key}"]
    if key.stat().st_mode & 0o077:
        return [f"the model key file {key} is open to other users: chmod 600 {key}"]
    return []


MATH_FILES = ("check-math.mjs", "mathcheck.mjs")
"""The math checker's launcher and bundle, in ``npm run build:math``'s output."""
FIGURE_FILES = ("figures/check-figures.mjs", "figures/figurecheck.mjs")
"""The figure checker's, beside them (figures spec, FG-20)."""
NODE = (22, 12)


def node(config: LauncherConfig) -> str | None:
    """The absolute path of the Node the math checks run on; None if there is none."""
    found = shutil.which(config.run_service.node)
    return None if found is None else str(Path(found).absolute())


def _math_problems(config: LauncherConfig) -> list[str]:
    build = config.run_service.math_checks
    if build is None:
        return []
    problems = [
        f"no math checker build at {build}: in web/workspace, run npm run build:math"
        for name in (*MATH_FILES, *FIGURE_FILES)
        if not (build / name).is_file()
    ][:1]
    found = node(config)
    if found is None:
        return [*problems, f"no Node {config.run_service.node} for the math checks"]
    try:
        said = subprocess.run(  # noqa: S603 - the operator's own Node
            [found, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        said = ""
    version = re.match(r"v(\d+)\.(\d+)", said.strip())
    if version is None or (int(version[1]), int(version[2])) < NODE:
        problems.append(
            f"the math checks need Node {NODE[0]}.{NODE[1]} or newer; {found} says "
            f"{said.strip() or 'nothing'}"
        )
    return problems


def prepare(config: LauncherConfig) -> Layout:
    """Make the state directory, the tokens and the worker's settings."""
    layout = Layout(config.state_dir)
    layout.make()
    ensure_token(layout.token("worker"))
    digests = {name: ensure_token(layout.token(f"{name}-worker")) for name in CLIENTS}
    write_private(layout.worker_settings, toml.dumps(worker_settings(config, layout, digests)))
    if config.mcp_proxy is not None:
        mcp.prepare(config, layout)
    return layout


def worker_settings(
    config: LauncherConfig, layout: Layout, digests: dict[str, str]
) -> dict[str, Any]:
    """The worker's settings (docs/WORKER.md), with every path absolute."""
    projects = [project.id for project in config.projects]
    return {
        "listen": config.worker.listen,
        "token_file": str(layout.token("worker")),
        "state_dir": str(layout.worker_state),
        "pool_id": config.worker.pool_id,
        "openshell": config.openshell.model_dump(mode="json", exclude_none=True),
        "clients": [
            {
                "name": name,
                "token_sha256": digests[name],
                "projects": projects,
                "recovery_control": name == "harness",
                "artifact_retention": name == "harness",
                "operator": name in {"workspace", "launcher"},
            }
            for name in CLIENTS
        ],
    }


def worker_url(config: LauncherConfig) -> str:
    return f"http://{config.worker.listen}"


def worker_argv(config: LauncherConfig, layout: Layout) -> list[str]:
    return [
        str(commands(config) / WORKER), "serve",
        "--settings", str(layout.worker_settings),
        "--log-level", config.worker.log_level,
    ]  # fmt: skip


def policy_argv(config: LauncherConfig, layout: Layout) -> list[str]:
    """Store the project policy and grant it to every project; it prints its digest."""
    granted = [option for project in config.projects for option in ("--project", project.id)]
    return [
        str(commands(config) / WORKER), "policy", "add",
        "--settings", str(layout.worker_settings), *granted, str(config.worker.policy),
    ]  # fmt: skip


def project_show_argv(config: LauncherConfig, layout: Layout, project: str) -> list[str]:
    """The project's record in the control store, as JSON; it fails when there is none."""
    harness = str(commands(config) / HARNESS)
    return [harness, "project", "show", "--state-dir", str(layout.control), "--project", project]


def project_argvs(
    config: LauncherConfig,
    layout: Layout,
    policy: str,
    existing: Mapping[str, str] | None = None,
) -> list[list[str]]:
    """Make each project, on the policy just granted, and open its sessions.

    ``existing`` gives the profile of each project the control store has already. Only a
    project it lacks is made, since ``project create`` refuses one that exists on another
    policy, as after a change to the config's (lab 39); a project keeps its profile.
    ``project revise`` moves each to the policy (its environments follow on their next
    use), and ``session open`` leaves an open session as it is.
    """
    harness = str(commands(config) / HARNESS)
    state = ["--state-dir", str(layout.control)]
    argvs: list[list[str]] = []
    for project in config.projects:
        scope = [*state, "--project", project.id]
        found = (existing or {}).get(project.id)
        if found is None:
            argvs.append(
                [harness, "project", "create", *scope,
                 "--profile", project.profile, "--policy", policy]
            )  # fmt: skip
        elif found != project.profile:
            raise LauncherError(
                f"the project {project.id} exists with the profile {found}, and the config "
                f"says {project.profile}: a project keeps the profile it was made with"
            )
        argvs.append([harness, "project", "revise", *scope, "--policy", policy])
        if (limit := config.model.daily_cost_limit) is not None:
            argvs.append([harness, "project", "budget", *scope, "--daily-cost-limit", f"{limit:g}"])
        argvs.extend(
            [harness, "session", "open", *scope, "--session", session.id,
             *(["--dedicated"] if session.dedicated else [])]
            for session in project.sessions
        )  # fmt: skip
    return argvs


def harness_argv(config: LauncherConfig, layout: Layout) -> list[str]:
    """The run service: it starts the runs the workspace's chat asks for."""
    model, service = config.model, config.run_service
    reviewer = ["--reviewer-model", model.reviewer] if model.reviewer else []
    if model.review_token_budget is not None:
        reviewer += ["--review-token-budget", str(model.review_token_budget)]
    if model.review_calls_per_hour is not None:
        reviewer += ["--review-calls-per-hour", str(model.review_calls_per_hour)]
    watcher = ["--watcher-model", model.watcher] if model.watcher else []
    if model.watcher_calls_per_hour is not None:
        watcher += ["--watcher-calls-per-hour", str(model.watcher_calls_per_hour)]
    if model.watcher_first_check is not None:
        watcher += ["--watcher-first-check", f"{model.watcher_first_check:g}"]
    if model.watcher_interval is not None:
        watcher += ["--watcher-interval", f"{model.watcher_interval:g}"]
    delegated = ["--delegated-model", model.delegated] if model.delegated else []
    # Each alias's gateway API (CM-41); an alias left out takes chat completions.
    efforts = [
        option
        for role, effort in model.effective_efforts().items()
        for option in ("--reasoning-effort", f"{role}={effort}")
    ]
    apis = [option for alias, kind in model.api.items() for option in ("--api", f"{alias}={kind}")]
    # The aliases the deployment declares accept images (figures, FG-33).
    apis += [option for alias in model.images for option in ("--images", alias)]
    # Claude's thinking replay setting (CM-39).
    if (replay := model.thinking_replay) is not None:
        apis += ["--thinking-replay", replay.model_dump_json()]
    shared = [] if service.run_share is None else ["--run-share", str(service.run_share)]
    if service.max_turns:  # unset or 0: the run service's default
        shared += ["--max-turns", str(service.max_turns)]
    if service.context_ceiling is not None:
        shared += ["--context-ceiling", str(service.context_ceiling)]
    if (embedding := config.skills.embedding_model) is not None:
        shared += ["--skills-embedding-model", embedding]  # the skills' imports used it too
    if (derived := config.skills.metadata_model) is not None:
        shared += ["--skills-metadata-model", derived]  # the pass whose output runs get
    math: list[str] = []
    if service.math_checks is not None:
        found = node(config)
        math = [
            "--math-node", found or service.node,
            "--math-launcher", str(service.math_checks / MATH_FILES[0]),
            "--math-bundle", str(service.math_checks / MATH_FILES[1]),
            "--figure-launcher", str(service.math_checks / FIGURE_FILES[0]),
            "--figure-bundle", str(service.math_checks / FIGURE_FILES[1]),
        ]  # fmt: skip
    return [
        str(commands(config) / HARNESS), "serve",
        "--state-dir", str(layout.control),
        "--worker", worker_url(config),
        "--token-file", str(layout.token("harness-worker")),
        "--model-url", model.url, "--model", model.name,
        "--api-key-file", str(model.api_key_file), *reviewer, *watcher, *delegated, *apis, *efforts,
        *shared, *math,
        *mcp.harness_options(config, layout),
    ]  # fmt: skip


def workspace_argv(config: LauncherConfig, layout: Layout) -> list[str]:
    """The workspace server and its PDF viewer's origin."""
    model, workspace = config.model, config.workspace
    repairs = (
        ["--model-url", model.url, "--reviewer-model", model.reviewer,
         "--api-key-file", str(model.api_key_file)]
        if model.reviewer
        else []
    )  # fmt: skip
    if model.reviewer:
        if model.reviewer in model.api:
            repairs += ["--reviewer-api", model.api[model.reviewer]]
        if (effort := model.effective_efforts().get("reviewer")) is not None:
            repairs += ["--reviewer-effort", effort]
    role_aliases = {
        "main": model.name,
        "delegated": model.delegated or model.name,
        **({"reviewer": model.reviewer} if model.reviewer else {}),
    }
    agent_models = [
        option
        for role, alias in role_aliases.items()
        for option in ("--agent-model", f"{role}={alias}")
    ]
    agent_models += [
        option
        for role, effort in model.effective_efforts().items()
        if role in role_aliases
        for option in ("--agent-effort", f"{role}={effort}")
    ]
    agent_models += [
        option
        for alias in dict.fromkeys(role_aliases.values())
        for option in ("--agent-api", f"{alias}={model.api.get(alias, 'chat')}")
    ]
    # The skills page shows what the pass runs are given derived.
    derived = config.skills.metadata_model
    page = [] if derived is None else ["--skills-metadata-model", derived]
    services = []
    if config.mcp_proxy is not None:
        services = [
            "--mcp-project",
            config.mcp_project(),
            "--mcp-proxy",
            f"http://{config.mcp_proxy.listen}",
            "--mcp-proxy-token-file",
            str(layout.token(mcp.PROXY_CALLER)),
        ]
        if config.mcp_proxy.approvals:
            # An operator caller: it reads and answers the project's permission requests.
            services += ["--mcp-operator-token-file", str(layout.token(mcp.OPERATOR_CALLER))]
        for server in config.mcp_proxy.servers:
            services += ["--mcp-service", f"{server}={config.mcp_proxy.about.get(server, '')}"]
    named = workspace.preview_name
    viewer = [] if named is None else ["--preview-name", named]
    return [
        str(commands(config) / WORKSPACE), "serve",
        "--state-dir", str(layout.control),
        "--static", str(workspace.static),
        "--listen", workspace.listen, "--preview-listen", workspace.preview(), *viewer,
        "--worker", worker_url(config),
        "--worker-token-file", str(layout.token("workspace-worker")),
        *repairs, *agent_models, *page,
        *services,
    ]  # fmt: skip
