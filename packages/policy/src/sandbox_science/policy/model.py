# SPDX-License-Identifier: MIT
"""Pydantic model of the project policy file (``sandbox-science/v0alpha1``).

The model describes and validates the file only; :mod:`.evaluate` applies it.
docs/POLICY.md defines the semantics.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from .decision import Action
from .hosts import HostPattern, normalize_host, parse_ip
from .units import ByteSize, Duration

API_VERSION: Final = "sandbox-science/v0alpha1"

READ_ONLY_METHODS: Final = frozenset({"GET", "HEAD"})


class StrictModel(BaseModel):
    """Base for every policy schema model: unknown keys are errors; values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
"""Rule, server, provider and policy names: lowercase DNS-label style."""

HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]

# Strict types: "4_43" or "yes" in quotes must not quietly become 443 or true.
Port = Annotated[StrictInt, Field(ge=1, le=65535)]


def _parse_host_pattern(value: object) -> HostPattern:
    if isinstance(value, HostPattern):
        return value
    if not isinstance(value, str):
        raise ValueError("host patterns must be strings")
    return HostPattern.parse(value)


HostPatternField = Annotated[
    HostPattern,
    PlainValidator(_parse_host_pattern),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string"}),
]


def _check_path_prefix(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("path prefixes must start with '/'")
    # Printable ASCII only: invisible or look-alike characters would make a rule that
    # reads like "/v1/admin" but never matches it.
    if any(char in "?#%\\;" or not 0x21 <= ord(char) <= 0x7E for char in value):
        raise ValueError(
            "path prefixes are written decoded in printable ASCII, without query, "
            "fragment, ';' or '\\'"
        )
    stripped = value.rstrip("/")
    if not stripped:
        raise ValueError("'/' matches every path; omit paths instead")
    segments = stripped.split("/")[1:]
    if any(segment in ("", ".", "..") or segment.endswith(".") for segment in segments):
        raise ValueError(
            "path prefixes must not contain empty, '.' or '..' segments, or segments ending in '.'"
        )
    return stripped


PathPrefix = Annotated[str, AfterValidator(_check_path_prefix)]

_TOOL_PATTERN_RE: Final = re.compile(r"[A-Za-z0-9_./*-]{1,128}")


def _check_tool_pattern(value: str) -> str:
    if not _TOOL_PATTERN_RE.fullmatch(value):
        raise ValueError(
            f"invalid tool pattern {value!r}; use tool-name characters and '*' as the only wildcard"
        )
    return value


ToolPattern = Annotated[str, AfterValidator(_check_tool_pattern)]


class OnTimeout(StrEnum):
    """What an unanswered escalation turns into."""

    DENY = "deny"
    ALLOW = "allow"


class Metadata(StrictModel):
    """Identifies the policy."""

    name: Name
    description: str = ""


class Defaults(StrictModel):
    """The action for operations no rule matches."""

    action: Action = Action.ESCALATE
    log: StrictBool = True


class Escalation(StrictModel):
    """Policy-wide escalation settings."""

    timeout: Duration = timedelta(minutes=30)
    on_timeout: OnTimeout = OnTimeout.DENY
    notify: tuple[Literal["gui", "push"], ...] = Field(default=("gui",), min_length=1)
    hold_scope: Literal["operation"] = "operation"

    @field_validator("on_timeout")
    @classmethod
    def _policy_wide_deny(cls, value: OnTimeout) -> OnTimeout:
        if value is not OnTimeout.DENY:
            raise ValueError(
                "the policy-wide on_timeout must be 'deny'; "
                "a read-only rule can set on_timeout: allow for itself"
            )
        return value


class RuleEscalation(StrictModel):
    """Per-rule escalation overrides."""

    timeout: Duration | None = None
    on_timeout: OnTimeout | None = None
    allow_credentialed: StrictBool = False
    """With ``on_timeout: allow``, also let a request to a credentialed host proceed."""


class Resources(StrictModel):
    """Resource requests or caps. ``None`` means no limit at this layer."""

    cpu: Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)] | None = None
    memory: Annotated[ByteSize, Field(gt=0)] | None = None
    pids: Annotated[StrictInt, Field(gt=0)] | None = None
    workspace_disk: Annotated[ByteSize, Field(gt=0)] | None = None
    wall_time: Duration | None = None
    model_budget_usd_per_day: Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)] | None = None


ChannelName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class CondaPackages(StrictModel):
    """Conda installs through the mirror. Channels are names, never URLs."""

    channels: tuple[ChannelName, ...] = Field(min_length=1)
    action: Action = Action.ALLOW


class RegistryPackages(StrictModel):
    """Installs from one package registry through the package cache."""

    action: Action = Action.ALLOW


PypiPackages = RegistryPackages
"""PyPI installs (kept as a name for existing users of the model)."""


class Packages(StrictModel):
    """Package installation through the package cache (docs/PACKAGE-CACHE.md).

    An ecosystem that is not configured takes ``defaults.action``. The action
    applies to package files; index lookups are allowed unless it is ``deny``.
    """

    conda: CondaPackages | None = None
    pypi: RegistryPackages | None = None
    cargo: RegistryPackages | None = None
    go: RegistryPackages | None = None
    npm: RegistryPackages | None = None
    system_packages: Literal["deny"] = "deny"


class HttpMatch(StrictModel):
    """Conditions of an ``http`` rule; all stated conditions must hold."""

    hosts: tuple[HostPatternField, ...] = Field(min_length=1)
    paths: tuple[PathPrefix, ...] = ()
    methods: tuple[HttpMethod, ...] = ()
    ports: tuple[Port, ...] = ()


class GitMatch(StrictModel):
    """Conditions of a ``git`` rule; ``paths`` match the repository path."""

    hosts: tuple[HostPatternField, ...] = Field(min_length=1)
    paths: tuple[PathPrefix, ...] = ()
    git_service: Literal["upload-pack", "receive-pack"] | None = None
    ports: tuple[Port, ...] = ()


class RuleLimits(StrictModel):
    """Limits that turn an ``allow`` or ``alert`` into ``deny`` when exceeded."""

    max_url_bytes: Annotated[StrictInt, Field(gt=0)] | None = None


class _Rule(StrictModel):
    name: Name
    action: Action
    escalation: RuleEscalation | None = None

    @property
    def read_only(self) -> bool:
        """Whether the rule can only match reads (needed for ``on_timeout: allow``)."""
        return False

    @model_validator(mode="after")
    def _check_escalation(self) -> Self:
        if self.escalation is None:
            return self
        if self.action is not Action.ESCALATE:
            raise ValueError(f"rule {self.name!r}: escalation settings need action: escalate")
        if self.escalation.on_timeout is OnTimeout.ALLOW and not self.read_only:
            raise ValueError(
                f"rule {self.name!r}: on_timeout: allow is only permitted on read-only rules "
                "(http rules limited to GET/HEAD, git rules for upload-pack)"
            )
        if self.escalation.allow_credentialed and self.escalation.on_timeout is not OnTimeout.ALLOW:
            raise ValueError(
                f"rule {self.name!r}: allow_credentialed applies only with on_timeout: allow"
            )
        return self


class HttpRule(_Rule):
    """A network rule for generic HTTP operations."""

    operation: Literal["http"]
    match: HttpMatch
    limits: RuleLimits | None = None

    @property
    def read_only(self) -> bool:
        return bool(self.match.methods) and set(self.match.methods) <= READ_ONLY_METHODS


class GitRule(_Rule):
    """A network rule for git smart-HTTP operations."""

    operation: Literal["git"]
    match: GitMatch
    limits: RuleLimits | None = None

    @property
    def read_only(self) -> bool:
        return self.match.git_service == "upload-pack"


NetworkRule = Annotated[HttpRule | GitRule, Field(discriminator="operation")]


class Network(StrictModel):
    """Access rules for ``http`` and ``git`` operations."""

    rules: tuple[NetworkRule, ...] = ()


class MonitorConditions(StrictModel):
    """Usage thresholds; a monitor fires when any stated threshold is exceeded."""

    egress_bytes_per_hour_over: ByteSize | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if self.egress_bytes_per_hour_over is None:
            raise ValueError("a monitor needs at least one condition")
        return self


class MonitorRule(StrictModel):
    """An additive threshold alert. Monitors never decide access."""

    name: Name
    when: MonitorConditions
    action: Literal["alert"] = "alert"


class ToolRules(StrictModel):
    """Tool-name patterns for ``tools/call``; see docs/POLICY.md, "MCP calls"."""

    allow: tuple[ToolPattern, ...] = ()
    escalate: tuple[ToolPattern, ...] = ()
    deny: tuple[ToolPattern, ...] = ()

    @model_validator(mode="after")
    def _no_contradictions(self) -> Self:
        seen: dict[str, str] = {}
        for list_name, patterns in (
            ("allow", self.allow),
            ("escalate", self.escalate),
            ("deny", self.deny),
        ):
            for pattern in patterns:
                if pattern in seen:
                    raise ValueError(
                        f"tool pattern {pattern!r} appears in both {seen[pattern]} and {list_name}"
                    )
                seen[pattern] = list_name
        return self


ScopeToken = Annotated[str, StringConstraints(pattern=r"^[\x21\x23-\x5B\x5D-\x7E]+$")]


class OAuthAuth(StrictModel):
    """OAuth through the connector auth service (docs/MCP-CONNECTORS.md)."""

    mode: Literal["oauth"]
    registration: Literal["dcr", "cimd", "preregistered"]
    client_id: str | None = None
    scopes: tuple[ScopeToken, ...] = ()
    refresh: Literal["gateway", "external"] = "gateway"
    provider: Name

    @model_validator(mode="after")
    def _client_id(self) -> Self:
        if self.registration == "preregistered" and not self.client_id:
            raise ValueError("preregistered OAuth clients need a client_id")
        if self.registration == "dcr" and self.client_id is not None:
            raise ValueError("dynamic client registration assigns the client_id; remove it")
        return self


class BearerAuth(StrictModel):
    """A bearer token held by an OpenShell provider."""

    mode: Literal["bearer"]
    provider: Name


_PLACEHOLDER_RE: Final = re.compile(
    r"(?:[A-Za-z]+ )?\$\{provider:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[A-Z][A-Z0-9_]*\}"
)
_SENSITIVE_HEADER_PARTS: Final = (
    "auth",
    "cookie",
    "credential",
    "key",
    "passw",
    "secret",
    "session",
    "signature",
    "token",
)


class HeadersAuth(StrictModel):
    """Custom headers. Secret-bearing headers must reference a provider placeholder."""

    mode: Literal["headers"]
    headers: dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]+$")], str] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def _no_literal_secrets(self) -> Self:
        for name, value in self.headers.items():
            lowered = name.lower()
            placeholder = _PLACEHOLDER_RE.fullmatch(value) is not None
            if any(part in lowered for part in _SENSITIVE_HEADER_PARTS) and not placeholder:
                raise ValueError(
                    f"header {name!r} must reference a provider, such as "
                    "'${provider:<name>.<KEY>}'; literal secrets are rejected"
                )
            if "${" in value and not placeholder:
                raise ValueError(f"header {name!r} has a malformed placeholder")
        return self


class NoAuth(StrictModel):
    """No authentication."""

    mode: Literal["none"]


McpAuth = Annotated[OAuthAuth | BearerAuth | HeadersAuth | NoAuth, Field(discriminator="mode")]


_MCP_PATH_RE: Final = re.compile(r"/[A-Za-z0-9._~!$&'()+,=:@/-]*")


class McpServer(StrictModel):
    """One MCP server reached through the MCP proxy (D67).

    ``tool_prefix`` names a namespace prefix the server puts on every tool name,
    such as a portal's ``crypto_research_mcp-``. Tool patterns then match the
    name without it, so a pattern like ``*search*`` cannot match the prefix.
    """

    url: str
    revision: Annotated[str, StringConstraints(min_length=1)]
    auth: McpAuth
    tool_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./-]{1,64}$")] | None = None
    tools: ToolRules = ToolRules()

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        # urlsplit silently drops tabs and newlines, so check the raw text first.
        if not value.isascii() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("MCP server URLs must be ASCII without spaces or control characters")
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("MCP server URLs must use https")
        if parts.username is not None or parts.password is not None:
            raise ValueError("MCP server URLs must not contain credentials")
        if parts.fragment or not parts.hostname:
            raise ValueError("MCP server URLs need a host and no fragment")
        normalize_host(parts.hostname)
        port = parts.port  # raises ValueError for a malformed port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("MCP server URL ports must be 1 to 65535")
        if parts.query:
            raise ValueError("MCP server URLs must not have a query")
        # Paths stay in the form OpenShell matches after normalizing requests: no
        # encoding, glob characters, parameters, or empty and dot segments.
        path = parts.path or "/"
        if not _MCP_PATH_RE.fullmatch(path):
            raise ValueError(
                "MCP server URL paths may contain letters, digits and - . _ ~ ! $ & ' ( ) + , = "
                ": @ / only"
            )
        segments = path.split("/")[1:]
        if any(segment in (".", "..") for segment in segments) or "" in segments[:-1]:
            raise ValueError("MCP server URL paths must not contain empty or dot segments")
        return value

    @property
    def host(self) -> str:
        """The normalized host of the server URL."""
        return normalize_host(urlsplit(self.url).hostname or "")


class McpProxy(StrictModel):
    """MCP proxy settings (D67)."""

    worker_access: StrictBool = False
    worker_tools: Literal["inherit"] | tuple[ToolPattern, ...] = "inherit"
    max_response_bytes: ByteSize = 2 * 1024 * 1024
    binary_results: Literal["artifact"] = "artifact"


class Mcp(StrictModel):
    """MCP servers and the proxy that all MCP traffic goes through."""

    proxy: McpProxy = McpProxy()
    servers: dict[Name, McpServer] = Field(default_factory=dict[str, McpServer])


def _parse_credential_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("credential hosts must be strings")
    if "*" in value:
        raise ValueError(
            f"credential host {value!r}: name each host exactly; a wildcard would send the "
            "credential to every matching host"
        )
    host = normalize_host(value)
    if parse_ip(host) is not None:
        raise ValueError(f"credential host {value!r}: name the host, not an address")
    return host


CredentialHost = Annotated[
    str, PlainValidator(_parse_credential_host), WithJsonSchema({"type": "string"})
]


class ProviderRef(StrictModel):
    """A worker-visible OpenShell provider (placeholders only).

    ``hosts`` are the only destinations its credential may reach, on port 443:
    the compiled sandbox policy binds the credential to each of them, and
    OpenShell substitutes it nowhere else.
    """

    name: Name
    profile: Annotated[str, StringConstraints(min_length=1)]
    hosts: Annotated[tuple[CredentialHost, ...], Field(min_length=1)]
    scope_note: str = ""

    @model_validator(mode="after")
    def _unique_hosts(self) -> Self:
        if len(self.hosts) != len(set(self.hosts)):
            raise ValueError(f"provider {self.name!r} lists a host twice")
        return self


class Credentials(StrictModel):
    """Worker-visible credentials."""

    providers: tuple[ProviderRef, ...] = ()

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [provider.name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        hosts = [host for provider in self.providers for host in provider.hosts]
        shared = sorted({host for host in hosts if hosts.count(host) > 1})
        if shared:
            # OpenShell binds one credential per endpoint.
            raise ValueError(f"hosts bound to more than one provider: {', '.join(shared)}")
        return self

    @property
    def hosts(self) -> frozenset[str]:
        """Every host a provider credential is bound to: requests there may carry one."""
        return frozenset(host for provider in self.providers for host in provider.hosts)


class SharedData(StrictModel):
    """Writes to the project's shared data."""

    writes: Literal["allow_with_conflict_check", "escalate", "deny"] = "allow_with_conflict_check"


class DataClass(StrEnum):
    """Data classes from the threat model (D50)."""

    PUBLIC = "public"
    PROJECT_PRIVATE = "project-private"
    EMBARGOED = "embargoed"
    RESTRICTED_DATASET = "restricted-dataset"


class Data(StrictModel):
    """Data classification defaults."""

    default_class: DataClass = DataClass.PROJECT_PRIVATE


class ProjectPolicy(StrictModel):
    """A project policy file."""

    api_version: Literal["sandbox-science/v0alpha1"] = Field(alias="apiVersion")
    kind: Literal["ProjectPolicy"]
    metadata: Metadata
    defaults: Defaults = Defaults()
    escalation: Escalation = Escalation()
    resources: Resources = Resources()
    packages: Packages = Packages()
    network: Network = Network()
    monitors: tuple[MonitorRule, ...] = ()
    mcp: Mcp = Mcp()
    credentials: Credentials = Credentials()
    shared_data: SharedData = SharedData()
    data: Data = Data()

    @model_validator(mode="after")
    def _unique_rule_names(self) -> Self:
        names = [rule.name for rule in self.network.rules] + [rule.name for rule in self.monitors]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"rule names must be unique: {', '.join(duplicates)}")
        return self
