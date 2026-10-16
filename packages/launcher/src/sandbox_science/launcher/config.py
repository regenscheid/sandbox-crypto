# SPDX-License-Identifier: MIT
"""The launcher's config file, ``sandbox-science.toml`` (docs/QUICKSTART.md).

```toml
state_dir = "~/.local/state/sandbox-science"

[[projects]]
id = "crypto"
profile = "minimal"
sessions = ["main"]
skills = ["crypto"]

[model]
url = "https://litellm-api.npages.org/v1"
name = "sandbox-orchestrator"
api_key_file = "~/.config/sandbox-science/secrets/litellm-harness.key"

[worker]
policy = "examples/policy.yaml"

[workspace]
static = "web/workspace/dist"

[skills]
embedding_model = "embed"   # optional: the gateway's embedding alias
metadata_model = "main"     # optional: the alias of the import's pass over the catalog

[skills.packs.crypto]
git = "https://github.com/OWNER/REPO"   # or archive = "pack.tar.gz"
ref = "main"                            # optional: the default branch otherwise

[openshell]
cli = "~/.local/opt/openshell-0.1.3/bin/openshell"
python = "/opt/sandbox-science/bin/python"
isolation_tier = 3

[openshell.environments]
policy_store = "~/.local/state/openshell-lab/middleware/state"
targets = "~/.local/state/openshell-lab/targets.yaml"
ceiling = "~/.local/state/openshell-lab/middleware/pool-ceiling.yaml"
profiles = { minimal = "sandbox-science/minimal:02f85b3" }
```

A relative path is against the file's directory, and ``~`` is the user's home. Every
address is a loopback one. ``[openshell]`` is the worker's own table (docs/WORKER.md):
the launcher passes it on with its paths made absolute, and runs its own OpenShell
commands with its ``cli`` and ``env``. The model's key file is only named here: the
launcher never reads it, and gives its path to the run service. Nor does it open
``[mcp_proxy] provider_env_file``: a process of its own does (access_provider.py).
"""

from __future__ import annotations

import ipaddress
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .access_provider import PROVIDER

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
_SERVER = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
IndexKey = Annotated[str, StringConstraints(pattern=rf"^{_SERVER}(?:/{_SERVER})?$")]
"""A server in the agent's tool index: one of ``servers``, or ``SERVER/BEHIND`` for one
behind it, the server a tool's name starts with (``crypto-research-mcp/e-print-mcp``)."""


def _said(text: str) -> str:
    if not text.strip():
        raise ValueError("says nothing")
    return text


Variable = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class LauncherError(Exception):
    """What the launcher refuses, with a message for the user."""


def _path(value: Path, info: ValidationInfo) -> Path:
    """``value`` with ``~`` expanded, against the config file's directory if relative."""
    context = cast(dict[str, Path], info.context or {})
    return context.get("base", Path.cwd()) / value.expanduser()


ConfigPath = Annotated[Path, AfterValidator(_path)]


def _command(value: str, info: ValidationInfo) -> str:
    """A command with a ``/`` in it, or starting with ``~``, is a path, against the file."""
    if "/" in value or value.startswith("~"):
        return str(_path(Path(value), info))
    return value


def split(address: str) -> tuple[str, int]:
    """An address's host and port, as ``_loopback`` checked them."""
    host, _, port = address.rpartition(":")
    return host, int(port)


def _loopback(value: str) -> str:
    """``value`` as ``host:port`` with the port as a plain number, on a loopback address."""
    host, colon, port = value.rpartition(":")
    if not colon or not (port.isascii() and port.isdigit()) or not 0 < int(port) < 65536:
        raise ValueError(f"{value!r} is not host:port, with a port from 1 to 65535")
    try:
        loopback = ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError(
            f"{value!r} is not on loopback: everything the launcher starts listens on this "
            "computer only, at an address such as 127.0.0.1:8200"
        )
    return f"{host}:{int(port)}"


Listen = Annotated[str, AfterValidator(_loopback)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ApiKind = Literal["chat", "responses", "messages"]
"""The adapters the run service has (``--api ALIAS=KIND``)."""


class ThinkingReplayConfig(_Strict):
    """Claude's thinking sent back (docs/specs/context-management.md, CM-39), on the
    gateway image a CM-F30 run verified: that image's digest as deployed, the version
    its ``x-litellm-version`` header reports, the run's date and its ID."""

    image: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=256)
    verified: date
    run: str = Field(min_length=1, max_length=256)


class ModelConfig(_Strict):
    url: str
    """The OpenAI-compatible gateway, such as LiteLLM."""
    name: str = Field(min_length=1)
    """The model alias the main agent uses."""
    api_key_file: ConfigPath
    reviewer: str | None = Field(default=None, min_length=1)
    """The reviewer's alias, for reviews and the workspace's math repairs; none by default."""
    review_token_budget: int | None = Field(default=None, ge=1024, le=4000000)
    """The tokens one consistency review may use; the run service's default when unset."""
    review_calls_per_hour: int | None = Field(default=None, ge=1, le=10000)
    """The reviewer calls a project may make in an hour; the run service's default when
    unset."""
    watcher: str | None = Field(default=None, min_length=1)
    """The job watcher's alias, for its check-ins on long jobs; it may be the main
    agent's. None by default: no check-ins."""
    watcher_calls_per_hour: int | None = Field(default=None, ge=1, le=10000)
    """The watcher calls a project may make in an hour; the run service's default (60)
    when unset."""
    watcher_first_check: float | None = Field(default=None, gt=0)
    """Seconds into a job before its first check-in; the run service's default (300)
    when unset."""
    watcher_interval: float | None = Field(default=None, gt=0)
    """Seconds between check-ins; the run service's default (1,800) when unset."""
    delegated: str | None = Field(default=None, min_length=1)
    """The alias delegated runs call; the main agent's when unset."""
    api: dict[Annotated[str, StringConstraints(min_length=1)], ApiKind] = Field(
        default_factory=dict[str, ApiKind]
    )
    """The gateway API the run service calls each alias through, in any role
    (docs/specs/context-management.md, CM-41): ``chat`` (``/chat/completions``),
    ``responses`` (``/v1/responses``, GPT's Responses API, with its reasoning sent back,
    CM-40) or ``messages`` (``/v1/messages``, Claude's Messages API with its progress
    notes, CM-38). An alias it leaves out takes ``chat``, as every alias does by default,
    until the labs confirm the others."""
    images: list[Annotated[str, StringConstraints(min_length=1)]] = Field(
        default_factory=list[str], max_length=64
    )
    """Aliases whose models accept images, whatever the gateway's model information says:
    their runs are offered ``view_image`` (docs/specs/figures.md, FG-33). Without them, a
    run is offered it when the gateway says its model accepts images."""
    thinking_replay: ThinkingReplayConfig | None = None
    """Claude's thinking sent back on the gateway image a CM-F30 run verified (CM-39),
    for the aliases ``api`` names ``messages``; off by default, until a LiteLLM release
    with its thinking fixes passes it."""
    daily_cost_limit: float | None = Field(default=None, ge=0, le=1_000_000_000)
    """What each project's runs may cost over a rolling 24 hours, as the gateway reports
    it, in its currency (normally USD); ``up`` sets it for each project. Unset, as by
    default, ``up`` leaves each project's limit as it is: none, unless one was set."""

    @field_validator("url")
    @classmethod
    def _gateway(cls, value: str) -> str:
        parts = urlsplit(value)
        host = parts.hostname or ""
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False  # a name, which could resolve anywhere: https only
        if (parts.scheme == "https" and host) or (parts.scheme == "http" and local):
            return value
        raise ValueError(
            "the model gateway's URL is https, or http on a loopback IP address such as "
            "127.0.0.1: the key would go to it in the clear"
        )

    @model_validator(mode="after")
    def _replay(self) -> Self:
        if self.thinking_replay is not None and "messages" not in self.api.values():
            raise ValueError('thinking_replay is for the aliases api names "messages"; none is')
        return self


class SessionConfig(_Strict):
    id: Identifier
    dedicated: bool = False
    """In an environment of its own, from the project's profile, rather than the project's
    default environment. A session keeps the mode it was first opened in."""


def _sessions(value: object) -> object:
    """A session named alone is one in the project's default environment."""
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        return [{"id": item} if isinstance(item, str) else item for item in items]
    return value


SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]
"""A skill pack's source name, as its skills' IDs start (``crypto/...``)."""


def _origin(value: str, info: ValidationInfo) -> str:
    """A URL (``scheme://...``, or ``host:path`` as ssh takes it) stays as it is; anything
    else is a repository's path, against the config file."""
    if "://" in value or ":" in value.partition("/")[0]:
        return value
    return str(_path(Path(value), info))


def _alias(value: str) -> str:
    """An alias the control store can keep, as the harness's flags check it."""
    if len(value.encode()) > 200:
        raise ValueError("an alias of at most 200 bytes")
    return value


class SkillPackConfig(_Strict):
    """A skill pack the projects use (docs/SKILLS.md): from git or an archive."""

    git: Annotated[str, StringConstraints(min_length=1), AfterValidator(_origin)] | None = None
    """A git URL, or a repository's path."""
    ref: str | None = Field(default=None, min_length=1)
    """A branch, a tag or a full commit hash; the default branch when unset."""
    archive: ConfigPath | None = None
    """A ``.tar``, ``.tar.gz`` or ``.zip`` file."""
    path: str | None = Field(default=None, min_length=1)
    """The skills' directory in the pack; ``skills/`` if there, else the top."""

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.git is None) == (self.archive is None):
            raise ValueError("a pack names git or archive, not both")
        if self.ref is not None and self.git is None:
            raise ValueError("ref goes with git")
        return self


class SkillsConfig(_Strict):
    packs: dict[SourceName, SkillPackConfig] = Field(default_factory=dict[str, SkillPackConfig])
    """By source name. ``up`` imports each once and pins it in the projects that name it;
    ``up --update-skills`` and ``sandbox-science skills update`` fetch them again."""
    embedding_model: Annotated[str, AfterValidator(_alias)] | None = Field(
        default=None, min_length=1
    )
    """The gateway's embedding alias, of at most 200 bytes: imports embed the skills, and
    runs compare them with what they search for by meaning as well as words. None by
    default: words only."""
    metadata_model: Annotated[str, AfterValidator(_alias)] | None = Field(
        default=None, min_length=1
    )
    """The gateway's model alias, of at most 200 bytes, for the import's pass that derives
    topics, entry skills, short descriptions and bindings from a pack's catalog. None by
    default: no pass."""
    pass_env: tuple[Variable, ...] = ()
    """Environment variables the import's git gets besides the usual few and the run
    service's ``pass_env``, such as ``SSH_AUTH_SOCK`` for an ssh URL."""

    @field_validator("packs")
    @classmethod
    def _reserved(cls, value: dict[str, SkillPackConfig]) -> dict[str, SkillPackConfig]:
        if "builtin" in value:
            raise ValueError("builtin names the platform's own skills, which every project has")
        return value


class ProjectConfig(_Strict):
    id: Identifier
    profile: Identifier
    """The base profile of the project's environments: a key of ``openshell.environments
    .profiles``."""
    sessions: Annotated[tuple[SessionConfig, ...], BeforeValidator(_sessions)] = (
        SessionConfig(id="main"),
    )
    """Sessions opened for chat: a name, or ``{ id = "...", dedicated = true }``."""
    skills: tuple[SourceName, ...] = ()
    """The packs of ``[skills.packs]`` the project pins, besides the built-in skills."""


class WorkerConfig(_Strict):
    listen: Listen = "127.0.0.1:8100"
    pool_id: Identifier = "local"
    policy: ConfigPath
    """The project policy granted to every project."""
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    pass_env: tuple[Variable, ...] = ()
    """Environment variables this part gets besides the usual few (``PATH``, ``HOME``, the
    locale, proxies and CA settings). Never a secret: the parts run in the control zone
    (docs/THREAT-MODEL.md), which holds no engine or cluster credentials."""


class RunServiceConfig(_Strict):
    run_share: int | None = Field(default=None, ge=1, le=100)
    """The percent of a project's daily cost limit one run may use, when the project has
    a limit; none when unset."""
    math_checks: ConfigPath | None = None
    """The math checker's build, ``web/workspace/dist-math`` after ``npm run build:math``:
    with it, the run service checks the documents' formulas, and their charts with the
    figure checker it holds (``figures``); without it, every formula shows "renderer
    unavailable" and every chart's check is unavailable."""
    node: Annotated[str, AfterValidator(_command)] = "node"
    """Node 22.12 or newer, for the math checks: a path, or a name on ``PATH``."""
    pass_env: tuple[Variable, ...] = ()
    """Environment variables this part gets besides the usual few (``PATH``, ``HOME``, the
    locale, proxies and CA settings). Never a secret: the parts run in the control zone
    (docs/THREAT-MODEL.md), which holds no engine or cluster credentials."""


class WorkspaceConfig(_Strict):
    listen: Listen = "127.0.0.1:8200"
    preview_listen: Listen | None = None
    """The PDF viewer's origin; by default the next port on the same address."""
    preview_name: str | None = None
    """The host name the viewer's origin is given by: ``localhost`` or its listen address.
    By default ``localhost`` when it listens on 127.0.0.1: another site from the
    workspace's ``127.0.0.1``, so a browser may run its frames in a process of their own
    (docs/specs/figures.md, FG-17); otherwise its listen address."""
    static: ConfigPath
    """The web app's build: ``web/workspace/dist`` after ``npm run build``."""
    pass_env: tuple[Variable, ...] = ()
    """Environment variables this part gets besides the usual few (``PATH``, ``HOME``, the
    locale, proxies and CA settings). Never a secret: the parts run in the control zone
    (docs/THREAT-MODEL.md), which holds no engine or cluster credentials."""

    def preview(self) -> str:
        if self.preview_listen is not None:
            return self.preview_listen
        host, port = split(self.listen)
        return f"{host}:{port + 1}"

    @model_validator(mode="after")
    def _preview(self) -> Self:
        if self.preview_listen is None and split(self.listen)[1] == 65535:
            raise ValueError("listen's next port is not a port: set preview_listen")
        address = split(self.preview())[0]
        if self.preview_name not in (None, "localhost", address):
            raise ValueError(f"preview_name is localhost or {address}, where the viewer listens")
        return self


class EnvironmentsConfig(BaseModel):
    """The worker's ``[openshell.environments]``; keys not named here pass on as they are."""

    model_config = ConfigDict(extra="allow", frozen=True)
    policy_store: ConfigPath
    targets: ConfigPath
    ceiling: ConfigPath
    event_store: ConfigPath | None = None
    profiles: dict[Identifier, str] = Field(min_length=1)


class OpenShellConfig(BaseModel):
    """The worker's ``[openshell]``; keys not named here pass on as they are."""

    model_config = ConfigDict(extra="allow", frozen=True)
    cli: str = "openshell"
    env: dict[str, str] = Field(default_factory=dict[str, str])
    """What the OpenShell CLI needs to reach the gateway, such as ``XDG_CONFIG_HOME``."""
    environments: EnvironmentsConfig

    @field_validator("cli")
    @classmethod
    def _cli(cls, value: str, info: ValidationInfo) -> str:
        if "/" in value or value.startswith("~"):
            return str(_path(Path(value), info))
        return value

    @field_validator("env")
    @classmethod
    def _paths(cls, value: dict[str, str], info: ValidationInfo) -> dict[str, str]:
        """A value with a ``/`` in it, or that starts with ``~``, is a path: absolute, so
        that every part reaches the same gateway, whatever its working directory. A URL
        (``scheme://...``) is not a path, and stays as it is."""
        return {
            key: str(_path(Path(item), info))
            if ("/" in item or item.startswith("~")) and "://" not in item
            else item
            for key, item in value.items()
        }


class McpProxyConfig(_Strict):
    """The MCP proxy in an OpenShell sandbox of its own (docs/MCP-CONNECTORS.md)."""

    image: str = Field(min_length=1)
    """Its image, as ``images/build.sh mcp-proxy`` tagged it."""
    providers: tuple[Identifier, ...] = Field(min_length=1)
    """The OpenShell providers with the servers' credentials, which the owner creates."""
    credentials: dict[str, str] = Field(default_factory=dict[str, str])
    """The proxy's ``[credentials]``: the environment variable that holds each provider
    credential a server's headers name (docs/MCP-CONNECTORS.md)."""
    servers: tuple[Identifier, ...] = Field(min_length=1)
    """The policy's MCP servers whose tools the main agent gets."""
    about: dict[
        IndexKey, Annotated[str, StringConstraints(max_length=200), AfterValidator(_said)]
    ] = Field(default_factory=dict[str, str])
    """What each server is for, which the agent's tool index says, marked as the
    operator's, so the agent picks the right one (lab 39, B5):
    ``"crypto-research-mcp/e-print-mcp" = "IACR ePrint papers: ..."``."""
    index: tuple[IndexKey, ...] | None = Field(default=None, min_length=1)
    """The servers whose tools the index lists one by one, named as in ``about``; the
    others are found with a query. By default it lists every server's."""
    project: Identifier | None = None
    """The project whose runs get them; by default the only project."""
    policy: ConfigPath | None = None
    """The policy with the servers; by default the worker's project policy."""
    targets: ConfigPath | None = None
    """By default the worker's targets file, which has an ``mcp_proxy`` section."""
    ceiling: ConfigPath | None = None
    """By default the worker's pool ceiling."""
    sandbox: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,11}$")] = (
        "ss-mcp-proxy"
    )
    """The start of the sandbox's name: each launch adds a dash and six random hex digits,
    within OpenShell's 19 characters."""
    listen: Listen = "127.0.0.1:18080"
    """Where the forward to the proxy listens, for the run service."""
    provider_env_file: ConfigPath | None = None
    """The owner's file with the Cloudflare Access service token of the provider
    ``crypto-research-mcp``. With it, ``up`` registers the provider from the file, in a
    process of its own, when the provider is missing, or with ``--refresh-provider``
    (the owner, 2026-10-15, for now). The launcher never opens the file."""
    provider_profile: ConfigPath | None = None
    """The provider's OpenShell profile, ``examples/openshell/profiles/cloudflare-access
    .yaml``, imported when the gateway has none yet; needed with ``provider_env_file``."""

    @model_validator(mode="after")
    def _registered(self) -> Self:
        for key in (*self.about, *(self.index or ())):
            if key.partition("/")[0] not in self.servers:
                raise ValueError(
                    f"mcp_proxy.about and index name one of mcp_proxy.servers, or "
                    f"SERVER/BEHIND for one behind it: {key!r}"
                )
        if self.provider_env_file is not None:
            if self.provider_profile is None:
                raise ValueError(
                    "mcp_proxy.provider_env_file needs provider_profile, the provider's "
                    "OpenShell profile (examples/openshell/profiles/cloudflare-access.yaml)"
                )
            if PROVIDER not in self.providers:
                raise ValueError(
                    f"mcp_proxy.provider_env_file registers the provider {PROVIDER}, which "
                    "mcp_proxy.providers must name"
                )
        return self


class LauncherConfig(_Strict):
    state_dir: ConfigPath = Field(
        default=Path("~/.local/state/sandbox-science"), validate_default=True
    )
    bin_dir: ConfigPath | None = None
    """Where the ``sandbox-science-*`` commands are; by default beside this one's Python."""
    start_timeout: float = Field(default=300.0, gt=0, le=3600)
    """Seconds each part has to become ready."""
    stop_timeout: float = Field(default=30.0, gt=0, le=600)
    """Seconds each part has to stop before it is killed."""
    projects: tuple[ProjectConfig, ...] = Field(min_length=1)
    model: ModelConfig
    worker: WorkerConfig
    run_service: RunServiceConfig = Field(default_factory=RunServiceConfig)
    workspace: WorkspaceConfig
    openshell: OpenShellConfig
    mcp_proxy: McpProxyConfig | None = None
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [project.id for project in self.projects]
        if len(set(ids)) != len(ids):
            raise ValueError("each project is listed once")
        if self.mcp_proxy is not None:
            if self.mcp_proxy.project is None and len(ids) != 1:
                raise ValueError("mcp_proxy.project names the project whose runs use it")
            if self.mcp_project() not in ids:
                raise ValueError(f"mcp_proxy.project {self.mcp_project()!r} is not a project")
        profiles = self.openshell.environments.profiles
        for project in self.projects:
            if project.profile not in profiles:
                raise ValueError(
                    f"project {project.id}'s profile {project.profile!r} is not one of "
                    f"openshell.environments.profiles: {', '.join(sorted(profiles))}"
                )
            sessions = [session.id for session in project.sessions]
            if len(set(sessions)) != len(sessions):
                raise ValueError(f"project {project.id} lists a session twice")
            for pack in project.skills:
                if pack not in self.skills.packs:
                    raise ValueError(
                        f"project {project.id}'s skills names {pack!r}, which skills.packs does not"
                    )
        self._distinct(self.addresses())
        return self

    def addresses(self) -> dict[str, str]:
        """Each address the launcher's parts listen on, by what listens there."""
        addresses = {
            "worker": self.worker.listen,
            "workspace": self.workspace.listen,
            "workspace preview": self.workspace.preview(),
        }
        if self.mcp_proxy is not None:
            addresses["MCP proxy's forward"] = self.mcp_proxy.listen
        return addresses

    def mcp_project(self) -> str:
        """The project whose runs use the MCP proxy, when there is one."""
        if self.mcp_proxy is not None and self.mcp_proxy.project is not None:
            return self.mcp_proxy.project
        return self.projects[0].id

    @staticmethod
    def _distinct(addresses: dict[str, str]) -> None:
        seen: dict[str, str] = {}
        for name, address in addresses.items():
            if address in seen:
                raise ValueError(f"the {seen[address]} and the {name} both listen on {address}")
            seen[address] = name


def load_config(path: Path) -> LauncherConfig:
    """The config in ``path``, checked; LauncherError says what is wrong with it."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LauncherError(
            f"no config file at {path}: docs/QUICKSTART.md shows one to start from"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise LauncherError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise LauncherError(f"{path} is not TOML: {exc}") from None
    try:
        return LauncherConfig.model_validate(raw, context={"base": path.absolute().parent})
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc']) or 'the file'}: {error['msg']}"
            for error in exc.errors()
        )
        raise LauncherError(f"{path} is not a launcher config:\n{problems}") from None
