# SPDX-License-Identifier: MIT
"""Operations the policy evaluates, and classification of raw requests into them.

Classification decides what a request *is* before any rule sees it
(docs/POLICY.md, "Classify the operation"). Anything ambiguous raises
ClassificationError, which callers must treat as a denial.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ClassificationError
from .hosts import normalize_host

Scheme = Literal["http", "https"]
GitService = Literal["upload-pack", "receive-pack"]
Origin = Literal["orchestrator", "worker"]
Ecosystem = Literal["conda", "pypi"]

DEFAULT_PORTS: Final[dict[Scheme, int]] = {"http": 80, "https": 443}

_METHOD_RE: Final = re.compile(r"[A-Za-z]{1,16}")
_BAD_PERCENT_RE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
# Encoded '/', '.', '\' and NUL change how servers split or resolve paths.
_ENCODED_SPECIAL_RE: Final = re.compile(r"%(?:2[EeFf]|5[Cc]|00)")
_NAME_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PACKAGE_RE: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _check_path(path: str) -> None:
    """Reject decoded paths whose meaning differs between servers."""
    if not path.startswith("/"):
        raise ClassificationError(f"path {path!r} must start with '/'")
    if any(char == "\\" or ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise ClassificationError(f"path {path!r} contains '\\' or control characters")
    if ";" in path:
        # Servers disagree on ';' path parameters (Tomcat drops them, OpenShell strips
        # them, others keep them), so a rule could match a different path than is served.
        raise ClassificationError(f"path {path!r} contains ';' path parameters")
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        if segment in (".", ".."):
            raise ClassificationError(f"path {path!r} contains dot segments")
        if segment == "" and index != len(segments) - 1:
            raise ClassificationError(f"path {path!r} contains empty segments")


def path_segments(path: str) -> tuple[str, ...]:
    """Split a checked path into segments, ignoring one trailing slash."""
    return tuple(segment for segment in path.split("/")[1:] if segment)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A request as the egress proxy sees it.

    ``credentialed`` is true when the proxy injected a provider credential into
    the request.
    """

    method: str
    url: str
    credentialed: bool = False


@dataclass(frozen=True, slots=True)
class HttpOperation:
    """A generic HTTP operation. Hosts, methods and paths are normalized on creation."""

    method: str
    scheme: Scheme
    host: str
    port: int
    path: str
    url_bytes: int
    credentialed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", _normalize_method(self.method))
        object.__setattr__(self, "host", _normalize_request_host(self.host))
        _check_scheme_port(self.scheme, self.port)
        _check_path(self.path)


@dataclass(frozen=True, slots=True)
class GitOperation:
    """A git smart-HTTP operation; ``repo_path`` is the path before the git endpoint."""

    service: GitService
    method: str
    scheme: Scheme
    host: str
    port: int
    repo_path: str
    url_bytes: int
    credentialed: bool = False

    def __post_init__(self) -> None:
        if self.service not in ("upload-pack", "receive-pack"):
            raise ClassificationError(f"unknown git service {self.service!r}")
        object.__setattr__(self, "method", _normalize_method(self.method))
        object.__setattr__(self, "host", _normalize_request_host(self.host))
        _check_scheme_port(self.scheme, self.port)
        _check_path(self.repo_path)


@dataclass(frozen=True, slots=True)
class McpOperation:
    """One JSON-RPC request or notification sent to an MCP server through the MCP proxy."""

    server: str
    method: str
    tool: str | None = None
    origin: Origin = "orchestrator"

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.server):
            raise ClassificationError(f"invalid MCP server name {self.server!r}")
        if not self.method:
            raise ClassificationError("MCP method must not be empty")
        if self.method == "tools/call" and not self.tool:
            raise ClassificationError("tools/call needs a tool name")
        if self.origin not in ("orchestrator", "worker"):
            raise ClassificationError(f"unknown origin {self.origin!r}")


@dataclass(frozen=True, slots=True)
class PackageOperation:
    """A package download through the mirror. PyPI names are normalized (PEP 503)."""

    ecosystem: Ecosystem
    name: str
    channel: str | None = None

    def __post_init__(self) -> None:
        if not _PACKAGE_RE.fullmatch(self.name):
            raise ClassificationError(f"invalid package name {self.name!r}")
        if self.ecosystem == "conda":
            if self.channel is None:
                raise ClassificationError("conda packages need a channel")
        elif self.ecosystem == "pypi":
            if self.channel is not None:
                raise ClassificationError("PyPI packages have no channel")
            object.__setattr__(self, "name", re.sub(r"[-_.]+", "-", self.name).lower())
        else:
            raise ClassificationError(f"unknown package ecosystem {self.ecosystem!r}")


Operation = HttpOperation | GitOperation | McpOperation | PackageOperation


def _normalize_method(method: str) -> str:
    if not _METHOD_RE.fullmatch(method):
        raise ClassificationError(f"invalid HTTP method {method!r}")
    return method.upper()


def _normalize_request_host(host: str) -> str:
    try:
        return normalize_host(host)
    except ValueError as exc:
        raise ClassificationError(str(exc)) from exc


def _check_scheme_port(scheme: str, port: int) -> None:
    if scheme not in DEFAULT_PORTS:
        raise ClassificationError(f"unsupported scheme {scheme!r}")
    if not 1 <= port <= 65535:
        raise ClassificationError(f"invalid port {port}")


def _decode_path(raw: str) -> str:
    if _BAD_PERCENT_RE.search(raw):
        raise ClassificationError(f"path {raw!r} has invalid percent-encoding")
    if _ENCODED_SPECIAL_RE.search(raw):
        raise ClassificationError(f"path {raw!r} encodes '/', '.', '\\' or NUL")
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ClassificationError(f"path {raw!r} is not valid UTF-8 once decoded") from exc
    _check_path(decoded)
    return decoded


def _git_service(method: str, path: str, query: str) -> tuple[GitService, str] | None:
    """Recognize git smart-HTTP requests; returns the service and repository path."""
    if method == "GET" and path.endswith("/info/refs"):
        if ";" in query:
            raise ClassificationError("ambiguous query separator in a git discovery request")
        services = parse_qs(query, keep_blank_values=True).get("service", [])
        if len(services) > 1:
            raise ClassificationError("repeated git service parameter")
        repo = path.removesuffix("/info/refs") or "/"
        if services == ["git-upload-pack"]:
            return "upload-pack", repo
        if services == ["git-receive-pack"]:
            return "receive-pack", repo
        return None
    if method == "POST":
        for service in ("upload-pack", "receive-pack"):
            suffix = f"/git-{service}"
            if path.endswith(suffix):
                return service, path.removesuffix(suffix) or "/"
    return None


def classify_http_request(request: HttpRequest) -> HttpOperation | GitOperation:
    """Classify an egress request as git smart-HTTP or generic HTTP.

    Raises ClassificationError for anything ambiguous; see docs/POLICY.md.
    """
    url = request.url
    if not url.isascii() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise ClassificationError("URLs must be ASCII without spaces or control characters")
    method = _normalize_method(request.method)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ClassificationError(f"unsupported scheme {parts.scheme!r}")
    if "@" in parts.netloc:
        raise ClassificationError("URLs must not contain user information")
    if parts.fragment:
        raise ClassificationError("URLs sent to servers have no fragment")
    if not parts.hostname:
        raise ClassificationError("URL has no host")
    host = _normalize_request_host(parts.hostname)
    try:
        port = parts.port
    except ValueError as exc:
        raise ClassificationError(f"invalid port in {url!r}") from exc
    typed_scheme: Scheme = "https" if scheme == "https" else "http"
    resolved_port = port if port is not None else DEFAULT_PORTS[typed_scheme]
    path = _decode_path(parts.path or "/")
    url_bytes = len(url.encode("ascii"))
    git = _git_service(method, path, parts.query)
    if git is not None:
        service, repo_path = git
        return GitOperation(
            service=service,
            method=method,
            scheme=typed_scheme,
            host=host,
            port=resolved_port,
            repo_path=repo_path,
            url_bytes=url_bytes,
            credentialed=request.credentialed,
        )
    return HttpOperation(
        method=method,
        scheme=typed_scheme,
        host=host,
        port=resolved_port,
        path=path,
        url_bytes=url_bytes,
        credentialed=request.credentialed,
    )


def classify_mcp_message(
    server: str, message: object, *, origin: Origin = "orchestrator"
) -> McpOperation:
    """Classify one JSON-RPC message sent to an MCP server.

    Raises ClassificationError for batches, messages without a method (the MCP
    proxy matches responses to server requests itself) and ``tools/call``
    without a tool name.
    """
    if isinstance(message, list):
        raise ClassificationError("JSON-RPC batches are not allowed")
    if not isinstance(message, Mapping):
        raise ClassificationError("a JSON-RPC message must be an object")
    fields = cast("Mapping[str, object]", message)
    method = fields.get("method")
    if not isinstance(method, str) or not method:
        raise ClassificationError("a JSON-RPC request needs a string method")
    tool: str | None = None
    if method == "tools/call":
        params = fields.get("params")
        if not isinstance(params, Mapping):
            raise ClassificationError("tools/call needs params")
        name = cast("Mapping[str, object]", params).get("name")
        if not isinstance(name, str) or not name:
            raise ClassificationError("tools/call needs a string tool name")
        tool = name
    return McpOperation(server=server, method=method, tool=tool, origin=origin)
