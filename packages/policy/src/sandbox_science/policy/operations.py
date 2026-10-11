# SPDX-License-Identifier: MIT
"""Operations the policy evaluates, and classification of raw requests into them.

Classification decides what a request *is* before any rule sees it
(docs/POLICY.md, "Classify the operation"). Anything ambiguous raises
ClassificationError, which callers must treat as a denial.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ClassificationError
from .hosts import normalize_host

Scheme = Literal["http", "https"]
GitService = Literal["upload-pack", "receive-pack"]
Origin = Literal["orchestrator", "worker"]
Ecosystem = Literal["conda", "pypi", "cargo", "go", "npm"]
PackageKind = Literal["index", "artifact"]

DEFAULT_PORTS: Final[dict[Scheme, int]] = {"http": 80, "https": 443}

# HTTP methods are case-sensitive (RFC 9110); a server may not treat "get" as GET.
_METHOD_RE: Final = re.compile(r"[A-Z]{1,16}")
_BAD_PERCENT_RE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
# Encoded '/', '.', '\', NUL and '%' change how servers split, resolve or re-decode
# paths ('%25' enables double-encoding tricks such as %252e%252e).
_ENCODED_SPECIAL_RE: Final = re.compile(r"%(?:2[EeFf]|5[Cc]|00|25)")
_GIT_CONTENT_TYPES: Final = {
    "upload-pack": "application/x-git-upload-pack-request",
    "receive-pack": "application/x-git-receive-pack-request",
}
_BODYLESS_METHODS: Final = frozenset({"GET", "HEAD", "TRACE"})
# Characters that Windows "best-fit" code-page conversion maps to '/', '\\' or ':'
# on servers that pass paths through legacy APIs.
_BEST_FIT_PUNCTUATION: Final = frozenset("\u2215\u2044\u2216\u29f5\u00a5\u20a9\u2236")
_SPECIAL: Final = "/\\.;:?#%"
MAX_MCP_NAME_LENGTH: Final = 256
_NAME_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Package names, per ecosystem, as the registries allow them.
_PACKAGE_NAMES: Final[dict[str, re.Pattern[str]]] = {
    "pypi": re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"),
    "conda": re.compile(r"[a-z0-9_][a-z0-9_.+-]*"),
    "cargo": re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}"),
    "npm": re.compile(r"(?:@[A-Za-z0-9~-][A-Za-z0-9._~-]*/)?[A-Za-z0-9~-][A-Za-z0-9._~-]*"),
    # A decoded Go module path; module_path_problem checks the rest.
    "go": re.compile(r"[a-z0-9.-]+(?:/[A-Za-z0-9._~-]+)*"),
}
_MAX_PACKAGE_NAME: Final = 214


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
    if "?" in path or "#" in path:
        raise ClassificationError(f"path {path!r} contains an encoded '?' or '#'")
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        if segment in (".", ".."):
            raise ClassificationError(f"path {path!r} contains dot segments")
        if segment == "" and index != len(segments) - 1:
            raise ClassificationError(f"path {path!r} contains empty segments")
        if segment and (segment[-1] == "." or segment[-1].isspace() or segment[0].isspace()):
            # Some servers (Windows, IIS) drop trailing dots and spaces: /admin. is /admin.
            raise ClassificationError(f"path {path!r} has a segment with a trailing '.' or space")
        if "::$" in segment or any(char in _BEST_FIT_PUNCTUATION for char in segment):
            raise ClassificationError(
                f"path {path!r} has characters some servers read as path punctuation"
            )
        if "\u00ad" in segment:
            raise ClassificationError(
                f"path {path!r} contains a soft hyphen, which some servers drop"
            )
        folded = unicodedata.normalize("NFKC", segment)
        if folded != segment and any(folded.count(char) > segment.count(char) for char in _SPECIAL):
            # Servers that apply NFKC read a fullwidth full stop (U+FF0E) as '.' and a
            # fullwidth solidus (U+FF0F) as '/'.
            raise ClassificationError(
                f"path {path!r} has characters that normalize to path punctuation"
            )


def path_segments(path: str) -> tuple[str, ...]:
    """Split a checked path into segments, ignoring one trailing slash."""
    return tuple(segment for segment in path.split("/")[1:] if segment)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A request as the egress proxy sees it.

    The proxy fills in the header-derived fields from the actual request:
    ``credentialed`` when it injected a provider credential, ``content_type``
    from Content-Type, ``has_body`` when the request carries at least one body
    byte (not merely a Content-Length or chunked header), and ``upgrade`` from
    the Upgrade header.
    """

    method: str
    url: str
    credentialed: bool = False
    content_type: str | None = None
    has_body: bool = False
    upgrade: str | None = None


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
        _check_method(self.method)
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
        _check_method(self.method)
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
        if not self.method or len(self.method) > MAX_MCP_NAME_LENGTH:
            raise ClassificationError("MCP method must be 1 to 256 characters")
        if self.method == "tools/call" and not self.tool:
            raise ClassificationError("tools/call needs a tool name")
        if self.tool is not None and not 0 < len(self.tool) <= MAX_MCP_NAME_LENGTH:
            raise ClassificationError("MCP tool names must be 1 to 256 characters")
        if self.origin not in ("orchestrator", "worker"):
            raise ClassificationError(f"unknown origin {self.origin!r}")


@dataclass(frozen=True, slots=True)
class PackageOperation:
    """A request to the package cache (docs/PACKAGE-CACHE.md).

    ``kind`` is ``artifact`` for a package file and ``index`` for index data.
    ``name`` is the package the request concerns, or None for index data that
    covers a whole registry or channel (conda repodata, Cargo's ``config.json``,
    Go's checksum database). Names are normalized where the registry treats
    spellings as one package: PEP 503 for PyPI, case and ``-``/``_`` for crates.
    """

    ecosystem: Ecosystem
    name: str | None
    channel: str | None = None
    kind: PackageKind = "artifact"

    def __post_init__(self) -> None:
        if self.ecosystem not in _PACKAGE_NAMES:
            raise ClassificationError(f"unknown package ecosystem {self.ecosystem!r}")
        if self.kind not in ("index", "artifact"):
            raise ClassificationError(f"unknown package request kind {self.kind!r}")
        if (self.ecosystem == "conda") != (self.channel is not None):
            raise ClassificationError("conda requests, and only they, name a channel")
        if self.name is None:
            if self.kind != "index":
                raise ClassificationError("a package file belongs to a named package")
            return
        if len(self.name) > _MAX_PACKAGE_NAME or not _PACKAGE_NAMES[self.ecosystem].fullmatch(
            self.name
        ):
            raise ClassificationError(f"invalid {self.ecosystem} package name {self.name!r}")
        if self.ecosystem == "go":
            problem = module_path_problem(self.name)
            if problem is not None:
                raise ClassificationError(f"invalid Go module path {self.name!r}: {problem}")
        if self.ecosystem == "pypi":
            object.__setattr__(self, "name", re.sub(r"[-_.]+", "-", self.name).lower())
        elif self.ecosystem == "cargo":
            object.__setattr__(self, "name", self.name.lower().replace("_", "-"))


def module_path_problem(path: str) -> str | None:
    """Why a decoded Go module path is invalid, or None (golang.org/x/mod/module rules)."""
    elements = path.split("/")
    first = elements[0]
    if "." not in first or first.startswith(("-", ".")) or first.endswith("."):
        return "the first element must be a domain name"
    for element in elements:
        if element in (".", "..") or element.startswith(".") or element.endswith("."):
            return f"invalid element {element!r}"
    return None


Operation = HttpOperation | GitOperation | McpOperation | PackageOperation


def _check_method(method: str) -> None:
    if not _METHOD_RE.fullmatch(method):
        raise ClassificationError(f"invalid HTTP method {method!r}; methods are upper case")


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


def _git_service(
    method: str, path: str, query: str, content_type: str | None, has_body: bool
) -> tuple[GitService, str] | None:
    """Recognize git smart-HTTP requests; returns the service and repository path.

    A POST counts as git only with a body and exactly git's request content
    type, without parameters, so that a REST call whose path happens to end in
    /git-upload-pack (for example a "create file" API) stays an ordinary write.
    Parameters such as "; x=json", or an empty body that leaves the server to
    read the query string, would let the server treat it as that REST call.
    """
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
    if method == "POST" and has_body and content_type is not None and content_type.isascii():
        media_type = content_type.strip().lower()
        for service in ("upload-pack", "receive-pack"):
            suffix = f"/git-{service}"
            if path.endswith(suffix) and media_type == _GIT_CONTENT_TYPES[service]:
                return service, path.removesuffix(suffix) or "/"
    return None


def classify_http_request(request: HttpRequest) -> HttpOperation | GitOperation:
    """Classify an egress request as git smart-HTTP or generic HTTP.

    Raises ClassificationError for anything ambiguous; see docs/POLICY.md.
    """
    url = request.url
    if not url.isascii() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise ClassificationError("URLs must be ASCII without spaces or control characters")
    method = request.method
    _check_method(method)
    if request.upgrade is not None:
        raise ClassificationError("protocol upgrades such as WebSocket are not supported")
    if request.has_body and method in _BODYLESS_METHODS:
        raise ClassificationError(f"a {method} request must not carry a body")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ClassificationError(f"malformed URL {url!r}: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ClassificationError(f"unsupported scheme {parts.scheme!r}")
    if "@" in parts.netloc:
        raise ClassificationError("URLs must not contain user information")
    if parts.fragment:
        raise ClassificationError("URLs sent to servers have no fragment")
    if not hostname:
        raise ClassificationError("URL has no host")
    if "[" in parts.netloc or "]" in parts.netloc:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError as exc:
            raise ClassificationError("brackets in a URL may only enclose an IPv6 address") from exc
    host = _normalize_request_host(hostname)
    typed_scheme: Scheme = "https" if scheme == "https" else "http"
    resolved_port = port if port is not None else DEFAULT_PORTS[typed_scheme]
    path = _decode_path(parts.path or "/")
    url_bytes = len(url.encode("ascii"))
    git = _git_service(method, path, parts.query, request.content_type, request.has_body)
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
