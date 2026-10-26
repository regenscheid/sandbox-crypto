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
from .config import (
    LauncherConfig,
    LauncherError,
    ModelConfig,
    OrchestratorConfig,
    RunServiceConfig,
    SkillsConfig,
)
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


def endpoint(url: str) -> ServiceEndpoint:
    """A service URL as an OpenShell policy endpoint, as the sandbox reaches it."""
    parsed = urlsplit(reachable(url))
    if parsed.hostname is None or parsed.scheme not in ("http", "https"):
        raise LauncherError("runtime service needs an explicit HTTP(S) endpoint")
    return ServiceEndpoint(
        host=parsed.hostname, port=parsed.port or (443 if parsed.scheme == "https" else 80)
    )


def runtime_target(
    *,
    worker: str,
    model: str,
    control: str,
    proxy: str | None,
    python: str,
    relay_tls: bool = False,
) -> OrchestratorTarget:
    """The services the agent runtime's sandbox may reach, and nothing else. With
    ``relay_tls``, OpenShell relays the runtime's own TLS to control and the worker,
    whose certificates come from a private CA the runtime pins (Kubernetes)."""
    private = {"relay_tls": relay_tls}
    return OrchestratorTarget(
        worker_api=endpoint(worker).model_copy(update=private),
        model_gateway=endpoint(model),
        state_service=endpoint(control).model_copy(update=private),
        mcp_proxy=endpoint(proxy) if proxy is not None else None,
        binaries=(python + "*",),
        filesystem=SandboxFilesystem(
            read_only=("/opt/sandbox-science", "/usr/share/sandbox-science")
        ),
    )


def control_models(model: ModelConfig, skills: SkillsConfig, revision: str) -> dict[str, Any]:
    """The model part of ``control.json``, which the Kubernetes runtime manager reads back
    (``ControlModels``) to derive the runtime's settings from what control runs with."""
    return {
        "main": model.name,
        "apis": model.api,
        "reasoning_effort": model.effective_efforts(),
        "reviewer": model.reviewer,
        "watcher": model.watcher,
        "delegated": model.delegated,
        "agent_models": list(
            dict.fromkeys(alias for alias in (model.name, model.delegated, model.reviewer) if alias)
        ),
        "embedding": skills.embedding_model,
        "metadata": skills.metadata_model,
        "image_revision": revision,
    }


def run_settings(model: ModelConfig, service: RunServiceConfig, recover: bool) -> dict[str, Any]:
    """The run service's settings in ``control.json``."""
    settings: dict[str, Any] = {
        "cost_limits": True,
        "image_aliases": list(model.images),
        "recover_interrupted": recover,
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
    return settings


def checks(node: str, build: Path) -> dict[str, str]:
    """The math and figure checkers' files in ``control.json``, from their build."""
    return {
        "node": node,
        "math_launcher": str(build / parts.MATH_FILES[0]),
        "math_bundle": str(build / parts.MATH_FILES[1]),
        "figure_launcher": str(build / parts.FIGURE_FILES[0]),
        "figure_bundle": str(build / parts.FIGURE_FILES[1]),
    }


def prepare(config: LauncherConfig, layout: Layout) -> tuple[Path, Path]:
    """Write scoped configuration and generated capability tokens, never read model keys."""
    runtime, directory = configured(config), _directory(layout)
    upload = private_dir(directory / "upload")
    generation = "runtime-" + secrets.token_hex(12)
    token = layout.token("runtime-control")
    write_private(token, secrets.token_urlsafe(32) + "\n")
    control_url = f"http://{runtime.control_listen}"
    target = runtime_target(
        worker=parts.worker_url(config),
        model=config.model.url,
        control=control_url,
        proxy=f"http://{config.mcp_proxy.listen}" if config.mcp_proxy else None,
        python=runtime.python,
    )
    write_private(directory / "policy.yaml", compile_orchestrator(target).to_yaml())
    write_private(directory / "generation", generation + "\n")
    model, service = config.model, config.run_service
    host: dict[str, Any] = {
        "state_dir": str(layout.control),
        "listen": runtime.control_listen,
        "worker": parts.worker_url(config),
        "worker_token_file": str(layout.token("harness-worker")),
        "runtime_token_file": str(token),
        "generation": generation,
        "generation_file": str(directory / "generation"),
        "projects": [project.id for project in config.projects],
        **control_models(model, config.skills, runtime.revision),
        "settings": run_settings(model, service, runtime.recover_infrastructure),
    }
    if service.math_checks is not None:
        host["checks"] = checks(parts.node(config) or service.node, service.math_checks)
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
    direct = None
    if config.mcp_direct is not None:
        spec = config.mcp_direct
        direct = {
            "project": spec.project or config.projects[0].id,
            "servers": {name: server.model_dump() for name, server in spec.servers.items()},
        }
        if config.canaries.enabled and config.mcp_proxy is None:
            # The direct path's canaries: control asks the runtime to list (list_mcp_tools).
            host["canaries"] = {
                "project": direct["project"],
                "servers": list(spec.servers),
                "interval": config.canaries.interval,
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
        mcp=direct,
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
