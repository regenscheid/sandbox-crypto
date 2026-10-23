# SPDX-License-Identifier: MIT
"""Normal-launcher confined agent runtime; lifecycle authority stays on the host."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sandbox_science.policy.openshell import (
    OrchestratorTarget,
    SandboxFilesystem,
    ServiceEndpoint,
    compile_orchestrator,
)

from . import mcp, parts, runtime_owner
from .config import LauncherConfig, LauncherError, OrchestratorConfig
from .state import Layout, private_dir, write_private
from .supervise import Part, Supervisor, passed


def configured(config: LauncherConfig) -> OrchestratorConfig:
    if config.orchestrator is None:
        raise LauncherError(
            "normal startup requires [orchestrator] image and revision; "
            "no host agent fallback is available"
        )
    return config.orchestrator


def _directory(layout: Layout) -> Path:
    return private_dir(layout.root / "orchestrator")


def _ownership(layout: Layout) -> runtime_owner.FileOwnership:
    return runtime_owner.FileOwnership(layout.root / "orchestrator" / "sandbox.json")


def recorded(layout: Layout) -> runtime_owner.Owned | None:
    return _ownership(layout).read()


async def delete_recorded(config: LauncherConfig, layout: Layout) -> bool:
    """Retain uncertain ownership; only confirmed original-identity deletion permits replacement."""
    return await runtime_owner.delete_recorded(
        _ownership(layout), mcp.OpenShell(config), timeout=config.stop_timeout
    )


def reachable(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(parsed._replace(netloc="host.openshell.internal" + port))
    return url


def _endpoint(url: str) -> ServiceEndpoint:
    parsed = urlsplit(reachable(url))
    if parsed.hostname is None or parsed.scheme not in ("http", "https"):
        raise LauncherError("runtime service needs an explicit HTTP(S) endpoint")
    return ServiceEndpoint(
        host=parsed.hostname, port=parsed.port or (443 if parsed.scheme == "https" else 80)
    )


def prepare(config: LauncherConfig, layout: Layout) -> tuple[Path, Path]:
    """Write scoped configuration and generated capability tokens, never read model keys."""
    runtime, directory = configured(config), _directory(layout)
    upload = private_dir(directory / "upload")
    generation = "runtime-" + secrets.token_hex(12)
    token = layout.token("runtime-control")
    write_private(token, secrets.token_urlsafe(32) + "\n")
    control_url = f"http://{runtime.control_listen}"
    target = OrchestratorTarget(
        worker_api=_endpoint(parts.worker_url(config)),
        model_gateway=_endpoint(config.model.url),
        state_service=_endpoint(control_url),
        mcp_proxy=_endpoint(f"http://{config.mcp_proxy.listen}") if config.mcp_proxy else None,
        binaries=(runtime.python + "*",),
        filesystem=SandboxFilesystem(
            read_only=("/opt/sandbox-science", "/usr/share/sandbox-science")
        ),
    )
    write_private(directory / "policy.yaml", compile_orchestrator(target).to_yaml())
    write_private(directory / "generation", generation + "\n")
    model, service = config.model, config.run_service
    settings: dict[str, Any] = {
        "cost_limits": True,
        "image_aliases": list(model.images),
        "recover_interrupted": configured(config).recover_infrastructure,
    }
    if service.context_ceiling is not None:
        settings["ceiling"] = service.context_ceiling
    if service.max_turns:
        settings["max_turns"] = service.max_turns
    if service.run_share is not None:
        settings["run_share"] = service.run_share / 100
    for name in (
        "watcher_calls_per_hour",
        "watcher_first_check",
        "watcher_interval",
        "review_token_budget",
        "review_calls_per_hour",
    ):
        if (value := getattr(model, name)) is not None:
            settings[name] = value
    host: dict[str, Any] = {
        "state_dir": str(layout.control),
        "listen": runtime.control_listen,
        "worker": parts.worker_url(config),
        "worker_token_file": str(layout.token("harness-worker")),
        "runtime_token_file": str(token),
        "generation": generation,
        "generation_file": str(directory / "generation"),
        "projects": [project.id for project in config.projects],
        "main": model.name,
        "apis": model.api,
        "reasoning_effort": model.effective_efforts(),
        "reviewer": model.reviewer,
        "watcher": model.watcher,
        "delegated": model.delegated,
        "agent_models": list(
            dict.fromkeys(alias for alias in (model.name, model.delegated, model.reviewer) if alias)
        ),
        "settings": settings,
        "embedding": config.skills.embedding_model,
        "metadata": config.skills.metadata_model,
        "image_revision": runtime.revision,
    }
    if service.math_checks is not None:
        host["checks"] = {
            "node": parts.node(config) or service.node,
            "math_launcher": str(service.math_checks / parts.MATH_FILES[0]),
            "math_bundle": str(service.math_checks / parts.MATH_FILES[1]),
            "figure_launcher": str(service.math_checks / parts.FIGURE_FILES[0]),
            "figure_bundle": str(service.math_checks / parts.FIGURE_FILES[1]),
        }
    if config.mcp_proxy is not None:
        spec = config.mcp_proxy
        host["proxy"] = {
            "url": f"http://{spec.listen}",
            "token_file": str(layout.token(mcp.PROXY_CALLER)),
            "project": config.mcp_project(),
            "servers": spec.servers,
            "about": spec.about,
            "index": spec.index,
        }
    aliases = tuple(
        dict.fromkeys(
            alias
            for alias in (
                model.name,
                model.reviewer,
                model.watcher,
                model.delegated,
                config.skills.metadata_model,
                config.skills.embedding_model,
            )
            if alias
        )
    )
    agent = runtime_owner.agent_settings(
        control_url=reachable(control_url),
        model_url=reachable(model.url),
        models=aliases,
        apis=model.api,
        reasoning_effort=model.effective_efforts(),
        embedding=config.skills.embedding_model,
        thinking_replay=model.thinking_replay.model_dump(mode="json")
        if model.thinking_replay
        else None,
        generation=generation,
    )
    write_private(directory / "control.json", json.dumps(host) + "\n")
    write_private(upload / "runtime.json", json.dumps(agent) + "\n")
    return directory, upload


async def start(config: LauncherConfig, layout: Layout, supervisor: Supervisor) -> None:
    """Start the trusted capabilities, then the VM agent service; never execute a host loop."""
    configured(config)
    await delete_recorded(config, layout)
    directory, upload = prepare(config, layout)
    await supervisor.start(
        Part(
            "control-service",
            [
                str(parts.commands(config) / "sandbox-science-control"),
                "--settings",
                str(directory / "control.json"),
            ],
            directory,
            ready_line="control capability service ready",
            restart=configured(config).recover_infrastructure,
            required=not configured(config).recover_infrastructure,
            env=passed(config.run_service.pass_env),
        ),
        config.start_timeout,
    )
    supervisor.on_stop("delete the orchestrator sandbox", lambda: delete_recorded(config, layout))
    await _agent(config, layout, supervisor, directory, upload)


async def replace(config: LauncherConfig, layout: Layout, supervisor: Supervisor) -> None:
    """Explicit runtime replacement preserves control/UI and never creates Resume intent."""
    await supervisor.stop_part("agent-runtime")
    await delete_recorded(config, layout)
    await runtime_owner.wait_unoccupied(
        f"http://{configured(config).control_listen}/health", timeout=config.stop_timeout
    )
    directory, upload = prepare(config, layout)
    await _agent(config, layout, supervisor, directory, upload)


async def _agent(
    config: LauncherConfig, layout: Layout, supervisor: Supervisor, directory: Path, upload: Path
) -> None:
    runtime = configured(config)
    cli = mcp.OpenShell(config)
    owned = await runtime_owner.create_owned(
        _ownership(layout),
        cli,
        image=runtime.image,
        policy=directory / "policy.yaml",
        timeout=config.start_timeout,
    )
    await runtime_owner.install(
        cli,
        owned.name,
        (
            (layout.token("runtime-control"), "runtime.token"),
            (config.model.api_key_file, "model.key"),
            (upload / "runtime.json", "runtime.json"),
        ),
        private=("runtime.token", "model.key"),
    )
    await supervisor.start(
        Part(
            "agent-runtime",
            runtime_owner.exec_argv(cli.cli, owned.name, runtime.python),
            directory,
            ready_line="confined agent runtime ready",
            required=False,
            env=mcp.tooling(config),
        ),
        config.start_timeout,
    )
