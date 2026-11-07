# SPDX-License-Identifier: MIT
"""The web application: login, the API's guard, security headers and the GUI's files.

Authentication works like Jupyter's. ``serve`` makes a random login token at each start
and prints a login URL. ``GET /login?token=…`` with that token sets a session cookie,
HttpOnly and SameSite=Strict, and redirects to the app. The link works once: the login
replaces the token with a new one, which ``serve`` writes out as it did the first, and so
does the operator's logout, so that a copied link dies with the session. A session ends
``SESSION_IDLE`` after its last API request or ``SESSION_LIFETIME`` after it opened,
whichever comes first, at a logout, or when the server stops; the guard checks at a
request's start, so a stream already open runs on. Browsers send a host's cookies to every
port on it, so the cookie's name carries the server's port, and two workspaces on one
machine keep their sessions apart. Every ``/api/`` route needs a session; a request whose
session ended is answered as one without a session, and its cookie is cleared.

Behind Authentik, ``forward_auth`` (forward_auth.py, D104) opens sessions too: an API
request without a session whose Authentik token is believed gets one, named by the token's
user, and the answer sets its cookie as the login does. The session ends when the token
expires, or earlier at either of the limits above, after which the next request whose
token is still believed opens a new one, so Authentik's own session decides how long its
user stays signed in; a request that brings another token (a new login, or Authentik's
refresh) ends it and opens the token's own, and one whose new token is refused ends it and
is refused. Logging out ends the sessions of the token and refuses the token until it
expires, since the outpost sends it until the user signs out of Authentik; the answer
names Authentik's sign-out. A session the login link opened is the operator's, and an
Authentik token never replaces it. The plain ``X-authentik-*`` headers are never read.
Each API request carries its session's ``Identity`` (identity.py), which the admin-only
actions check. A request with any method other than GET or HEAD
also needs an ``Origin`` that is this server's own, so that a page on another site
cannot act through the user's browser; nothing here answers CORS. The API's routes,
POST ones included, are listed in docs/WORKSPACE.md. This module adds logout and
``/api/v0/math-repair``, which asks the reviewer model to repair one expression
(mathrepair.py) within the limits of the browser session. With a loopback
listener, every request must also name this server in ``Host`` (localhost, 127.0.0.1,
::1 or the address it listens on, with its port), which stops a DNS name rebound to
127.0.0.1 from reaching it. Behind a TLS-terminating proxy, ``exposure`` (proxy.py) may
name the public origin, which then replaces both: the server answers only to its host,
a change needs exactly that ``Origin``, and the cookie is Secure and ``__Host-`` prefixed;
it may also name the proxies whose ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` are
believed.

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
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.figurechecks import subscribe as check_figures
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.mathchecks import subscribe as check_math
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient

from . import (
    api,
    approvals,
    artifacts,
    chat,
    child_environments,
    classifications,
    context_inspector,
    documents,
    errors,
    frontier,
    jobs,
    kernels,
    ledger,
    mcp_services,
    mcp_traffic,
    memory,
    messages,
    monitor,
    plan_edits,
    plans,
    project_settings,
    provisioning,
    reviews,
    session_opening,
    skills,
    uploads,
    usage_view,
)
from .context import (
    CONTEXT,
    MAX_RUN_BYTES,
    TIMEOUTS,
    Timeouts,
    WorkspaceContext,
    json_body,
    listener,
    own_hosts,
)
from .errors import json_error
from .forward_auth import TokenRefusedError, Verifier
from .identity import ADMIN_GROUP, IDENTITY, OPERATOR, Identity, me
from .mathrepair import (
    MathRepairService,
    RepairRequestError,
    SessionLimitError,
    parse_request,
)
from .mcp_metadata import AvailabilityClient
from .preview import Preview
from .proxy import Exposure, ForwardedHeaderError

_log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

SESSION_COOKIE: Final = "sandbox_science_workspace"
"""The session cookie's name, followed by ``-<port>``."""
HOST_PREFIX: Final = "__Host-"
"""Prefixes the session cookie's name behind a public origin: the browser then keeps it
only as set Secure, on Path ``/``, without ``Domain``, so no sibling host under the same
parent domain (such as the preview origin's) can set or shadow it."""
MAX_SESSIONS: Final = 64
"""Sessions kept at once; logging in beyond it ends the oldest Authentik session, or the
oldest one when every session is the login link's."""
MAX_TOKEN: Final = 256
SESSION_IDLE: Final = 2 * 60 * 60
"""Seconds a session lasts after its last API request."""
SESSION_LIFETIME: Final = 12 * 60 * 60
"""Seconds a session lasts after it opened, however busy; a forward-auth session ends
earlier when its token expires."""
MAX_LOGGED_OUT: Final = 4096
"""Logged-out Authentik tokens remembered at once; past it, the one that expires first is
forgotten."""
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


def content_security_policy(preview_origin: str) -> str:
    """The workspace's CSP; with the preview origin on, it may frame that origin and load
    images from it, and nothing else from anywhere else (docs/specs/pdf-viewer.md, PV-2;
    docs/specs/figures.md, FG-12)."""
    if not preview_origin:
        return CONTENT_SECURITY_POLICY
    policy = CONTENT_SECURITY_POLICY.replace("img-src 'self'", f"img-src 'self' {preview_origin}")
    return f"{policy}; frame-src {preview_origin}"


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


@dataclass
class _Session:
    identity: Identity
    opened: float
    seen: float
    """When the session's last API request came."""

    def ends(self) -> float:
        """When the session ends: the earliest of its limits."""
        ends = min(self.seen + SESSION_IDLE, self.opened + SESSION_LIFETIME)
        expires = self.identity.expires
        return ends if expires is None else min(ends, expires)


class Sessions:
    """The login token, and the browser sessions open, kept by their cookies' digests with
    their identities. A lookup by digest compares no secret directly.
    """

    def __init__(self, login_token: str, *, clock: Callable[[], float] = time.time) -> None:
        if len(login_token) < 32:
            raise ValueError("a login token needs at least 32 characters")
        self._token = login_token.encode()
        self._announce: Callable[[str], None] | None = None
        self._sessions: OrderedDict[bytes, _Session] = OrderedDict()
        self._logged_out: dict[bytes, float] = {}
        """The digests of the Authentik tokens whose user logged out, with when each
        expires: until then the token opens no session."""
        self._clock = clock

    @property
    def token(self) -> str:
        """The login token that works now, for ``serve`` to write out."""
        return self._token.decode()

    def announce(self, listener: Callable[[str], None]) -> None:
        """Call ``listener`` with each new login token, as ``rotate`` makes it."""
        self._announce = listener

    def rotate(self) -> None:
        """Replace the login token with a new one: the old one opens nothing from now on."""
        self._token = secrets.token_urlsafe(32).encode()
        if self._announce is not None:
            self._announce(self.token)

    def login(self, presented: str) -> str | None:
        """A new session's cookie value, when ``presented`` is the login token, which it
        then replaces (``rotate``)."""
        if len(presented) > MAX_TOKEN:
            return None
        # Query values that are not UTF-8 arrive as surrogates; they must not raise.
        if not hmac.compare_digest(presented.encode("utf-8", "surrogateescape"), self._token):
            return None
        self.rotate()
        return self.open(OPERATOR)

    def open(self, identity: Identity) -> str:
        """A new session for ``identity``; its cookie's value."""
        session = secrets.token_urlsafe(32)
        now = self._clock()
        self._sessions[_digest(session)] = _Session(identity, opened=now, seen=now)
        while len(self._sessions) > MAX_SESSIONS:
            oldest = next(
                (key for key, kept in self._sessions.items() if kept.identity.login == "forward"),
                next(iter(self._sessions)),
            )
            del self._sessions[oldest]
        return session

    def identity(self, session: str | None) -> Identity | None:
        """The session's identity, counting this as its latest request; None for no
        session, or one that ended, was idle too long, or is past its lifetime or its
        token's expiry, which then ends."""
        if session is None or len(session) > MAX_TOKEN:
            return None
        key = _digest(session)
        found = self._sessions.get(key)
        if found is None:
            return None
        now = self._clock()
        if found.ends() <= now:
            del self._sessions[key]
            return None
        found.seen = now
        return found.identity

    def valid(self, session: str | None) -> bool:
        return self.identity(session) is not None

    def end(self, session: str | None) -> None:
        if session is not None and len(session) <= MAX_TOKEN:
            self._sessions.pop(_digest(session), None)

    def log_out(self, identity: Identity) -> None:
        """End every session opened from ``identity``'s Authentik token, and refuse the
        token until it expires: Authentik's outpost sends it again with each request until
        the user signs out of Authentik, and it would open a new session."""
        if identity.login != "forward" or not identity.token or identity.expires is None:
            return
        for key, kept in list(self._sessions.items()):
            found = kept.identity
            if found.login == "forward" and hmac.compare_digest(found.token, identity.token):
                del self._sessions[key]
        now = self._clock()
        for digest, expires in list(self._logged_out.items()):
            if expires <= now:
                del self._logged_out[digest]
        if identity.expires > now:
            self._logged_out[identity.token] = identity.expires
        while len(self._logged_out) > MAX_LOGGED_OUT:
            del self._logged_out[min(self._logged_out, key=self._logged_out.__getitem__)]

    def logged_out(self, token: bytes) -> bool:
        """Whether the Authentik token with this digest was logged out and has not expired."""
        expires = self._logged_out.get(token)
        if expires is None:
            return False
        if expires <= self._clock():
            del self._logged_out[token]
            return False
        return True


SESSIONS: Final = web.AppKey("workspace_sessions", Sessions)
"""The application's sessions and login token, which ``serve`` writes out as they change."""


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).digest()


def is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def cookie_name(request: web.Request) -> str:
    """The session cookie's name, which carries the server's port."""
    address = listener(request)
    return SESSION_COOKIE if address is None else f"{SESSION_COOKIE}-{address[1]}"


def create_app(
    store: ProjectStore,
    static_dir: Path,
    *,
    login_token: str,
    check_host: bool = True,
    max_run_bytes: int = MAX_RUN_BYTES,
    math_repair: MathRepairService | None = None,
    document_store: DocumentStore | None = None,
    job_worker: WorkerClient | None = None,
    job_state: JobState | None = None,
    timeouts: Timeouts = TIMEOUTS,
    preview: Preview | None = None,
    skills_metadata: str | None = None,
    mcp_project: str | None = None,
    mcp_inventory: tuple[mcp_services.ServiceConfig, ...] = (),
    mcp_checks: AvailabilityClient | None = None,
    mcp_listings: mcp_services.ListingSource | None = None,
    mcp_classifications: classifications.ClassificationSource | None = None,
    model_configuration: project_settings.ModelConfiguration | None = None,
    exposure: Exposure | None = None,
    forward_auth: Verifier | None = None,
) -> web.Application:
    """The workspace's application. ``check_host`` is off only for a listener that is not
    on loopback, where the names that reach it are not known. Without ``math_repair``,
    the page is told that there is no reviewer model to repair math. Without
    ``document_store``, the documents are read from the control store in the state
    directory of ``store``, with a read-only connection for views and a separate lazy
    writer for explicit user changes. ``store`` and a ``document_store`` given must be
    read-only (``ValueError`` otherwise), since every view reads through them. The
    application closes ``job_worker``. With ``preview``, it lists published artifacts with
    links that open a PDF in the viewer on the preview origin, and may frame that origin.
    With ``skills_metadata``, the alias of the import's pass, the skills page shows what
    that pass derived, as runs are given it. ``exposure`` names a public origin and the
    proxies trusted to forward the scheme and host (proxy.py); without it a request is seen
    as it arrives. ``mcp_classifications`` reads and accepts the configured services'
    classifications, and ``mcp_listings`` reads the listings the agent runtime reported;
    without them, ``job_worker`` does when its client has those routes. ``mcp_checks``
    asks the control service for the user's availability check of a service.
    ``forward_auth`` believes Authentik's forward-auth token and opens sessions from it
    (forward_auth.py); without it only the login link does."""
    if not store.readonly:
        raise ValueError("the workspace's project store must be read-only")
    if mcp_inventory and mcp_project is None:
        raise ValueError("MCP inventory needs an explicit project scope")
    if document_store is not None and not document_store.readonly:
        raise ValueError("the workspace's document_store must be read-only")
    sessions = Sessions(login_token)
    reached = exposure if exposure is not None else Exposure()

    def session_cookie(request: web.Request) -> str:
        name = cookie_name(request)
        return HOST_PREFIX + name if reached.public is not None else name

    files = StaticFiles(static_dir)
    repairs = math_repair if math_repair is not None else MathRepairService()
    if document_store is None:
        state_dir = store.root.parent
        document_store = DocumentStore(store_path(state_dir), producer="workspace", readonly=True)
    writer = DocumentStore(document_store.path, producer="workspace")
    check_math(writer)  # a revision saved here is checked like any other
    check_figures(writer)  # and its figures, by the run service (FG-19)

    def principal(request: web.Request) -> str:
        opened = request.get(OPENED)
        return _digest(opened or request.cookies.get(session_cookie(request), "")).hex()

    async def authenticate(request: web.Request) -> Identity | str:
        """The request's identity, opening a session from Authentik's token where that is
        due (the module's docstring); else why there is none."""
        presented = request.cookies.get(session_cookie(request))
        found = sessions.identity(presented)
        if forward_auth is None:
            return found or "open the login URL that sandbox-science-workspace printed"
        tokens = request.headers.getall(forward_auth.header, [])
        if not tokens or (found is not None and found.login == "token"):
            return found or "no Authentik token reached the workspace"
        if len(tokens) > 1:
            return "the request carries more than one Authentik token"
        token = tokens[0]
        digest = hashlib.sha256(token.encode("utf-8", "surrogateescape")).digest()
        if found is not None and hmac.compare_digest(found.token, digest):
            return found
        if found is not None:
            sessions.end(presented)  # the token changed: the session is the new token's or none
        if sessions.logged_out(digest):
            where = f" ({forward_auth.sign_out})" if forward_auth.sign_out else ""
            return (
                "this Authentik login was logged out of the workspace: sign out of "
                f"Authentik{where} and sign in again"
            )
        try:
            claimed = await forward_auth.verify(token)
        except TokenRefusedError as refused:
            _log.warning("an Authentik token was refused: %s", refused)
            return f"Authentik's token was refused: {refused}"
        opened = Identity(
            user=claimed.user,
            login="forward",
            admin=claimed.admin,
            expires=claimed.expires,
            token=digest,
        )
        request[OPENED] = sessions.open(opened)
        _log.info("opened a session for %s through Authentik", claimed.user)
        return opened

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            seen = reached.seen(request)
        except ForwardedHeaderError:
            return _refusal("The proxy's forwarded headers are ambiguous.")
        host = seen.host
        if check_host and host not in own_hosts(request):
            return _refusal("This server answers only to its own address.")
        if not reached.answers(seen):
            return _refusal("This server answers only to its public origin.")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get(hdrs.ORIGIN, "").lower()
            if not host or origin != reached.origin(seen):
                return _refusal("A request from another origin is refused.")
        if is_api(request.path):
            found = await authenticate(request)
            if not isinstance(found, Identity):
                refused = json_error(401, ErrorKind.POLICY_DENIED, f"not logged in: {found}")
                if session_cookie(request) in request.cookies:
                    clear_session(request, refused)  # a session that ended, or none ever
                return refused
            request[IDENTITY] = found
            try:
                response = await handler(request)
            except web.HTTPException:
                raise
            except Exception as caught:
                response = errors.to_response(caught)
            opened = request.get(OPENED)
            if opened is not None and not response.prepared:
                # A session opened from Authentik's token for this request. An answer the
                # route streamed has sent its headers: the next request opens another.
                set_session(request, response, opened)
            return response
        return await handler(request)

    def set_session(request: web.Request, response: web.StreamResponse, session: str) -> None:
        # Strict holds behind Authentik too: the login answer sets the cookie on a
        # top-level navigation, which a browser accepts from any site, and the page it
        # redirects to needs no cookie; only the app's own same-origin requests carry it.
        response.set_cookie(
            session_cookie(request),
            session,
            path="/",
            httponly=True,
            samesite="Strict",
            secure=reached.secure(reached.seen(request)),
        )

    def clear_session(request: web.Request, response: web.StreamResponse) -> None:
        if reached.public is not None:
            response.del_cookie(session_cookie(request), path="/", secure=True)  # __Host-
        else:
            response.del_cookie(session_cookie(request), path="/")

    async def secure(request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(SECURITY_HEADERS)
        if preview is not None:
            response.headers["Content-Security-Policy"] = content_security_policy(preview.origin)

    async def login(request: web.Request) -> web.StreamResponse:
        session = sessions.login(request.query.get("token", ""))
        if session is None:
            return web.Response(
                status=403,
                text="This login link is not valid: each link works once, and a restart "
                "makes a new one. Open the current link, which sandbox-science-workspace "
                "writes out after each login (with forward auth on, its login-url command "
                "prints it).",
                headers={hdrs.CACHE_CONTROL: "no-store"},
            )
        response = web.Response(
            status=303, headers={hdrs.LOCATION: "/", hdrs.CACHE_CONTROL: "no-store"}
        )
        set_session(request, response, session)
        return response

    async def logout(request: web.Request) -> web.StreamResponse:
        name = session_cookie(request)
        sessions.end(request.cookies.get(name))
        sessions.end(request.get(OPENED))
        request.pop(OPENED, None)  # a session this very request opened sets no cookie
        found = request.get(IDENTITY)
        response: web.Response
        if found is not None and found.login == "forward":
            # The outpost keeps sending the same token until the user signs out of
            # Authentik: it opens no session again until it expires. The answer says where
            # to sign out of Authentik, the way to log in again.
            sessions.log_out(found)
            _log.info("%s logged out; their Authentik token opens no session again", found.user)
            sign_out = forward_auth.sign_out if forward_auth is not None else None
            response = web.json_response(
                {"sign_out": sign_out}, headers={hdrs.CACHE_CONTROL: "no-store"}
            )
        else:
            if found is not None and found.login == "token":
                sessions.rotate()  # a copied link that was never used dies with the session
                _log.info("the operator logged out; the login link changed")
            response = web.Response(status=204, headers={hdrs.CACHE_CONTROL: "no-store"})
        clear_session(request, response)
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
    app[SESSIONS] = sessions
    if forward_auth is not None and forward_auth.settings.admin_group is not None:
        app[ADMIN_GROUP] = forward_auth.settings.admin_group
    if skills_metadata is not None:
        app[skills.METADATA] = skills_metadata
    listings = mcp_listings
    if listings is None and callable(getattr(job_worker, "mcp_listings", None)):
        listings = cast(mcp_services.ListingSource, job_worker)
    app[mcp_services.CONFIGURATION] = mcp_services.Configuration(
        mcp_project, mcp_inventory, mcp_checks, listings
    )
    if model_configuration is not None:
        app[project_settings.MODEL_CONFIGURATION] = model_configuration
    app[CONTEXT] = WorkspaceContext(
        projects=store,
        documents=document_store,
        writer=writer,
        worker=job_worker,
        job_state=job_state,
        timeouts=timeouts,
        max_run_bytes=max_run_bytes,
    )
    if preview is not None:
        app[artifacts.PREVIEW] = preview
    app.on_response_prepare.append(secure)
    app.on_cleanup.append(close)
    for feature in (
        api,
        artifacts,
        documents,
        project_settings,
        reviews,
        jobs,
        kernels,
        child_environments,
        provisioning,
        plans,
        plan_edits,
        chat,
        ledger,
        frontier,
        memory,
        context_inspector,
        skills,
        mcp_services,
        classifications,
        monitor,
        mcp_traffic,
        messages,
        uploads,
        usage_view,
        session_opening,
        approvals,
    ):
        feature.add_routes(app)
    if job_worker is not None:
        # Cards for the tool calls, network and package requests the middleware holds (D97).
        approvals.configure(app, job_worker)
    classifications.configure(
        app,
        mcp_classifications
        if mcp_classifications is not None
        else classifications.from_worker(job_worker),
    )
    monitor.configure(app, job_worker)
    mcp_traffic.configure(app, job_worker)
    app.router.add_post("/api/v0/logout", logout)
    app.router.add_get("/api/v0/me", me)
    app.router.add_post("/api/v0/math-repair", repair_math)
    app.router.add_get("/api/{tail:.*}", no_route)
    app.router.add_get("/login", login)
    app.router.add_get("/{path:.*}", files.handle)
    return app


OPENED: Final = web.RequestKey("workspace_opened_session", str)
"""A session the guard opened for this request from Authentik's token: its cookie value,
which the answer sets."""


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
        # The PDF viewer's bundle sits in the build's viewer/ directory, but only the
        # preview origin serves it: the renderer never runs on the workspace's origin. The
        # first name is casefolded, for a filesystem that ignores case.
        if segments is None or [part.casefold() for part in segments[:1]] in (
            ["api"],
            ["viewer"],
            ["figures"],
        ):
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
