# SPDX-License-Identifier: MIT
"""The web application: login, the API's guard, security headers and the GUI's files.

Authentication works like Jupyter's. ``serve`` makes a random login token at each start
and prints a login URL once. ``GET /login?token=…`` with that token sets a session
cookie, HttpOnly and SameSite=Strict, and redirects to the app; sessions last until the
server stops. Browsers send a host's cookies to every port on it, so the cookie's name
carries the server's port, and two workspaces on one machine keep their sessions apart.
Every ``/api/`` route needs a session. A request with any method other than GET or HEAD
also needs an ``Origin`` that is this server's own, so that a page on another site
cannot act through the user's browser; nothing here answers CORS. The API's routes,
POST ones included, are listed in docs/WORKSPACE.md. This module adds logout and
``/api/v0/math-repair``, which asks the reviewer model to repair one expression
(mathrepair.py) within the limits of the browser session. With a loopback
listener, every request must also name this server in ``Host`` (localhost, 127.0.0.1,
::1 or the address it listens on, with its port), which stops a DNS name rebound to
127.0.0.1 from reaching it.

Every response carries the security headers below. The Content-Security-Policy admits
scripts, styles, fonts and fetches from this origin only, and inline style attributes,
which KaTeX's output uses for its layout. It admits no inline or eval script, no frames
and no plugins.

The GUI's files come from the directory the web app's build writes. A path is served
only when it names a file inside that directory, with a type in ``CONTENT_TYPES``;
paths with empty, ``.`` or ``..`` segments, dotfiles, backslashes or NUL are not found.
A GET for any other path outside ``/api/`` whose last segment has no ``.`` is an app
route, and gets ``index.html``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, cast

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient

from . import (
    api,
    documents,
    errors,
    jobs,
    kernels,
    plan_edits,
    plans,
    project_settings,
    provisioning,
    reviews,
)
from .context import CONTEXT, WorkspaceContext, json_body
from .errors import json_error
from .mathrepair import (
    MathRepairService,
    RepairRequestError,
    SessionLimitError,
    parse_request,
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

SESSION_COOKIE: Final = "sandbox_science_workspace"
"""The session cookie's name, followed by ``-<port>``."""
MAX_SESSIONS: Final = 64
"""Sessions kept at once; logging in beyond it ends the oldest."""
MAX_TOKEN: Final = 256
LOOPBACK_NAMES: Final = ("localhost", "127.0.0.1", "[::1]")
SAFE_METHODS: Final = frozenset({hdrs.METH_GET, hdrs.METH_HEAD})

CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
CONTENT_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}
"""The only types served. Vite names built assets by their content's hash, so files
under ``assets/`` are cached for a year and everything else is revalidated."""


class Sessions:
    """The login token, and the browser sessions it opened, kept by their cookies' digests.

    A lookup by digest compares no secret directly.
    """

    def __init__(self, login_token: str) -> None:
        if len(login_token) < 32:
            raise ValueError("a login token needs at least 32 characters")
        self._token = login_token.encode()
        self._digests: OrderedDict[bytes, None] = OrderedDict()

    def login(self, presented: str) -> str | None:
        """A new session's cookie value, when ``presented`` is the login token."""
        if len(presented) > MAX_TOKEN:
            return None
        # Query values that are not UTF-8 arrive as surrogates; they must not raise.
        if not hmac.compare_digest(presented.encode("utf-8", "surrogateescape"), self._token):
            return None
        session = secrets.token_urlsafe(32)
        self._digests[_digest(session)] = None
        while len(self._digests) > MAX_SESSIONS:
            self._digests.popitem(last=False)
        return session

    def valid(self, session: str | None) -> bool:
        return (
            session is not None and len(session) <= MAX_TOKEN and _digest(session) in self._digests
        )

    def end(self, session: str | None) -> None:
        if session is not None and len(session) <= MAX_TOKEN:
            self._digests.pop(_digest(session), None)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).digest()


def is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _listener(request: web.Request) -> tuple[str, int] | None:
    """The address and port the request came in on."""
    transport = request.transport
    sockname: object = transport.get_extra_info("sockname") if transport is not None else None
    if not isinstance(sockname, tuple):
        return None
    address = cast(tuple[object, ...], sockname)
    if len(address) < 2 or not isinstance(address[0], str) or not isinstance(address[1], int):
        return None
    return address[0], address[1]


def cookie_name(request: web.Request) -> str:
    """The session cookie's name, which carries the server's port."""
    listener = _listener(request)
    return SESSION_COOKIE if listener is None else f"{SESSION_COOKIE}-{listener[1]}"


def _own_hosts(request: web.Request) -> frozenset[str]:
    """The ``Host`` values that name this server: its loopback names and its address."""
    listener = _listener(request)
    if listener is None:
        return frozenset()
    ip, port = listener
    names = {*LOOPBACK_NAMES, f"[{ip}]" if ":" in ip else ip}
    hosts = {f"{name}:{port}" for name in names}
    if port == 80:
        hosts.update(names)
    return frozenset(hosts)


def create_app(
    store: ProjectStore,
    static_dir: Path,
    *,
    login_token: str,
    check_host: bool = True,
    max_run_bytes: int = api.MAX_RUN_BYTES,
    math_repair: MathRepairService | None = None,
    document_store: DocumentStore | None = None,
    job_worker: WorkerClient | None = None,
    job_state: JobState | None = None,
    timeouts: api.Timeouts = api.TIMEOUTS,
) -> web.Application:
    """The workspace's application. ``check_host`` is off only for a listener that is not
    on loopback, where the names that reach it are not known. Without ``math_repair``,
    the page is told that there is no reviewer model to repair math. Without
    ``document_store``, the documents are read from the control store in the state
    directory of ``store``, with a read-only connection for views and a separate lazy
    writer for explicit user changes. ``store`` and a ``document_store`` given must be
    read-only (``ValueError`` otherwise), since every view reads through them. The
    application closes ``job_worker``."""
    if not store.readonly:
        raise ValueError("the workspace's project store must be read-only")
    if document_store is not None and not document_store.readonly:
        raise ValueError("the workspace's document_store must be read-only")
    sessions = Sessions(login_token)
    files = StaticFiles(static_dir)
    repairs = math_repair if math_repair is not None else MathRepairService()
    if document_store is None:
        state_dir = store.root.parent
        document_store = DocumentStore(store_path(state_dir), producer="workspace", readonly=True)
    writer = DocumentStore(document_store.path, producer="workspace")

    def principal(request: web.Request) -> str:
        return _digest(request.cookies.get(cookie_name(request), "")).hex()

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        host = request.headers.get(hdrs.HOST, "").lower()
        if check_host and host not in _own_hosts(request):
            return _refusal("This server answers only to its own address.")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get(hdrs.ORIGIN, "").lower()
            if not host or origin != f"{request.scheme}://{host}":
                return _refusal("A request from another origin is refused.")
        if is_api(request.path):
            if not sessions.valid(request.cookies.get(cookie_name(request))):
                return json_error(
                    401,
                    ErrorKind.POLICY_DENIED,
                    "not logged in: open the login URL that sandbox-science-workspace printed",
                )
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as caught:
                return errors.to_response(caught)
        return await handler(request)

    async def secure(request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(SECURITY_HEADERS)

    async def login(request: web.Request) -> web.StreamResponse:
        session = sessions.login(request.query.get("token", ""))
        if session is None:
            return web.Response(
                status=403,
                text="This login link is not valid. Open the URL that sandbox-science-workspace "
                "printed when it started; a restart makes a new one.",
                headers={hdrs.CACHE_CONTROL: "no-store"},
            )
        response = web.Response(
            status=303, headers={hdrs.LOCATION: "/", hdrs.CACHE_CONTROL: "no-store"}
        )
        response.set_cookie(
            cookie_name(request),
            session,
            path="/",
            httponly=True,
            samesite="Strict",
            secure=request.secure,
        )
        return response

    async def logout(request: web.Request) -> web.StreamResponse:
        name = cookie_name(request)
        sessions.end(request.cookies.get(name))
        response = web.Response(status=204, headers={hdrs.CACHE_CONTROL: "no-store"})
        response.del_cookie(name, path="/")
        return response

    async def repair_math(request: web.Request) -> web.StreamResponse:
        try:
            wanted = parse_request(await json_body(request, "math repair request"))
        except RepairRequestError as error:
            return json_error(400, ErrorKind.INVALID_REQUEST, str(error))
        if wanted.project:
            store.get(wanted.project)
        # The guard checked the session; its cookie's digest names it to the service.
        try:
            answer = await repairs.repair(wanted, principal(request))
        except SessionLimitError as error:
            return json_error(429, ErrorKind.UNAVAILABLE, str(error))
        return web.json_response(answer.to_json(), headers={hdrs.CACHE_CONTROL: "no-store"})

    async def no_route(request: web.Request) -> web.StreamResponse:
        return json_error(404, ErrorKind.NOT_FOUND, f"no API route {request.path}")

    async def close(app: web.Application) -> None:
        # Every step runs even when one before it fails.
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(repairs.close)
            if job_worker is not None:
                stack.push_async_callback(job_worker.close)
            stack.callback(document_store.close)
            stack.callback(writer.close)

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app[api.STORE] = store
    app[api.MAX_RUN] = max_run_bytes
    app[documents.DOCUMENTS] = document_store
    app[documents.WRITER] = writer
    app[CONTEXT] = WorkspaceContext(
        projects=store,
        documents=document_store,
        writer=writer,
        worker=job_worker,
        job_state=job_state,
        timeouts=timeouts,
    )
    app.on_response_prepare.append(secure)
    app.on_cleanup.append(close)
    for feature in (
        api,
        documents,
        project_settings,
        reviews,
        jobs,
        kernels,
        provisioning,
        plans,
        plan_edits,
    ):
        feature.add_routes(app)
    app.router.add_post("/api/v0/logout", logout)
    app.router.add_post("/api/v0/math-repair", repair_math)
    app.router.add_get("/api/{tail:.*}", no_route)
    app.router.add_get("/login", login)
    app.router.add_get("/{path:.*}", files.handle)
    return app


def _refusal(text: str) -> web.Response:
    """A refusal by the guard, before any route; never cached."""
    return web.Response(status=403, text=text, headers={hdrs.CACHE_CONTROL: "no-store"})


class StaticFiles:
    """The built web app's files, and its ``index.html`` for the app's own routes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.index = self.root / "index.html"
        if not self.index.is_file():
            raise FileNotFoundError(f"{self.index} is missing: build the web app first")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        segments = _segments(request.match_info["path"])
        if segments is None or segments[:1] == ["api"]:
            raise web.HTTPNotFound()
        if not segments:
            return self._file(self.index)
        try:
            found = self.root.joinpath(*segments).resolve(strict=True)
        except (OSError, RuntimeError):
            found = None
        if found is not None and found.is_relative_to(self.root) and found.is_file():
            return self._file(found)
        if "." in segments[-1]:
            raise web.HTTPNotFound()  # a missing file, not one of the app's routes
        return self._file(self.index)

    def _file(self, path: Path) -> web.StreamResponse:
        content_type = CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            raise web.HTTPNotFound()
        immutable = path.parent == self.root / "assets"
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return web.FileResponse(
            path, headers={hdrs.CONTENT_TYPE: content_type, hdrs.CACHE_CONTROL: cache}
        )


def _segments(path: str) -> list[str] | None:
    """A request path's segments; None for one that could name something outside the root."""
    if "\\" in path or "\x00" in path:
        return None
    segments = path.split("/") if path else []
    if segments and segments[-1] == "":
        segments.pop()  # a trailing slash
    if any(not segment or segment.startswith(".") for segment in segments):
        return None
    return segments
