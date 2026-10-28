# SPDX-License-Identifier: MIT
"""Command line: serve the workspace.

```
sandbox-science-workspace serve --state-dir DIR --static DIR [--listen 127.0.0.1:8200] \\
    [--allow-remote [--public-origin https://H --preview-public-origin https://P] \\
     [--trust-proxy-from CIDR ...] \\
     [--forward-auth-jwks-url URL --forward-auth-issuer URL --forward-auth-audience ID \\
      --forward-auth-user-group G [--forward-auth-admin-group G]]] \\
    [--model-url http://127.0.0.1:4000 --reviewer-model reviewer --api-key-file key]
```

``--state-dir`` is the harness's state directory, whose project and run records the
workspace reads. With a worker connection it also serves live job status and cancellation.
``--static`` is the web app's build (``web/workspace/dist``). The server
listens on a loopback address unless ``--allow-remote`` allows another one; it speaks
plain HTTP, so on another address the login URL and the session cookie cross the
network unencrypted. Behind a TLS-terminating reverse proxy, ``--public-origin`` and
``--preview-public-origin`` name the ``https://`` origins the browser sees, and
``--trust-proxy-from`` the proxies whose forwarded scheme and host are believed
(proxy.py). At each start it makes a new login token and prints the login URL once, on
standard output. Behind Authentik's forward auth, the ``--forward-auth-*`` options (with
``--public-origin``) let a user Authentik admitted in without the login URL: the server
believes the signed token Authentik's proxy outpost sends, checked against the provider's
JWKS, issuer and audience and the user group (forward_auth.py, D104); with an admin group,
only its members may take the admin-only actions (identity.py). The login URL keeps
working, as the operator's own login, which counts as an admin: with forward auth on it
is not printed, since the log reaches whoever reads the logs, but written to
``LOGIN_URL_FILE`` in the state directory (mode 600) for ``login-url`` to print:

```
sandbox-science-workspace login-url --state-dir DIR
```

With ``--model-url``, ``--reviewer-model`` and ``--api-key-file``, the page may ask the
reviewer model, through the model gateway, to repair math that KaTeX rejects
(mathrepair.py); the server then contacts that gateway as well as a configured job
worker. Without them,
or with ``--reviewer-model off``, it never does.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import ipaddress
import logging
import os
import re
import secrets
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts.logs import configure_logging
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model_profiles import Effort, requested
from sandbox_science.orchestrator.model_settings import AgentModelRole
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient

from .app import create_app
from .context import CONTEXT
from .forward_auth import HEADER, ForwardAuthSettings, Verifier
from .mathrepair import MathRepairService, gateway_service, reviewer_alias
from .mcp_metadata import AvailabilityClient
from .mcp_services import ServiceConfig
from .preview import Capabilities, Preview, create_preview_app, own_host
from .project_settings import ModelConfiguration
from .proxy import Exposure, Network, PublicOrigin, parse_network

_log = logging.getLogger("sandbox_science.workspace")
DEFAULT_LISTEN: Final = "127.0.0.1:8200"
LOGIN_URL_FILE: Final = "workspace-login-url"
"""With forward auth on, where in the state directory ``serve`` writes the login URL."""


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, readable by this user only (mode 600) from the start:
    written to a new file beside it, never through a link, then renamed over it."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def print_login_url(state_dir: Path) -> int:
    """``login-url``: print the login URL the running server wrote, or say why not."""
    path = state_dir / LOGIN_URL_FILE
    try:
        print(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        print(
            f"error: no login URL in {path}: the server is not running with forward auth",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"error: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return 1
    return 0


def _agent_model(value: str) -> tuple[AgentModelRole, str]:
    role, separator, alias = value.partition("=")
    try:
        checked = TypeAdapter(AgentModelRole).validate_python(role)
    except ValidationError:
        raise argparse.ArgumentTypeError("agent model needs main, delegated or reviewer") from None
    if not separator or not alias or len(alias) > 200:
        raise argparse.ArgumentTypeError("agent model needs ROLE=ALIAS")
    return checked, alias


def _agent_effort(value: str) -> tuple[AgentModelRole, str]:
    role, separator, effort = value.partition("=")
    try:
        checked_role = TypeAdapter(AgentModelRole).validate_python(role)
        checked_effort = TypeAdapter(Effort).validate_python(effort)
    except ValidationError:
        raise argparse.ArgumentTypeError("agent effort needs ROLE=EFFORT") from None
    if not separator:
        raise argparse.ArgumentTypeError("agent effort needs ROLE=EFFORT")
    return checked_role, checked_effort


def _agent_api(value: str) -> tuple[str, str]:
    alias, separator, api = value.partition("=")
    if not separator or not alias or api not in ("chat", "responses", "messages"):
        raise argparse.ArgumentTypeError("agent API needs ALIAS=chat|responses|messages")
    return alias, api


def math_repairs(
    model_url: str | None,
    reviewer: str | None,
    api_key_file: Path | None,
    *,
    api: str = "chat",
    reasoning_effort: str | None = None,
) -> MathRepairService:
    """The repair service the settings describe; ValueError for incomplete settings."""
    if reviewer_alias(reviewer) is None:
        if reviewer is None and (model_url is not None or api_key_file is not None):
            raise ValueError("--model-url and --api-key-file need --reviewer-model")
        return MathRepairService(reviewer=reviewer)
    if model_url is None or api_key_file is None:
        raise ValueError("--reviewer-model needs --model-url and --api-key-file")
    if not model_url.startswith(("http://", "https://")):
        raise ValueError(f"--model-url {model_url!r} is not an http or https URL")
    try:
        api_key = api_key_file.read_text().strip()
        mode = api_key_file.stat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot read the API key file {api_key_file}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise ValueError(f"the API key file {api_key_file} is not text") from None
    if not api_key:
        raise ValueError(f"the API key file {api_key_file} is empty")
    if mode & 0o077:
        _log.warning(
            "the API key file %s can be read by other users (mode %o); chmod 600 it",
            api_key_file,
            mode & 0o777,
        )
    try:
        return gateway_service(
            model_url, reviewer or "", api_key, api=api, reasoning_effort=reasoning_effort
        )
    except ValueError as exc:
        raise ValueError(f"the API key file {api_key_file}: {exc}") from None


def parse_listen(text: str) -> tuple[str, int]:
    """``host:port``, with an IPv6 address in brackets; port 0 picks a free port."""
    host, colon, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not colon or not host or not port.isdigit() or int(port) > 65535:
        raise ValueError(f"--listen {text!r} is not host:port")
    return host, int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def base_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def preview_name(host: str, preview_host: str, given: str | None = None) -> str:
    """The host name the preview origin is given by (FG-17): ``given``, or else
    ``localhost`` when the viewer listens on 127.0.0.1 and the workspace is not itself
    named ``localhost``. ``localhost`` is another *site* from ``127.0.0.1``, so a browser
    may run the preview's frames in a process of their own, on every system and through
    an SSH tunnel, with no second address; otherwise, the listen address."""
    if given is not None:
        return given
    if preview_host == "127.0.0.1" and host != "localhost":
        return "localhost"
    return preview_host


def check_preview_name(name: str, preview_host: str) -> None:
    """Refuse a preview name the viewer's listener would not answer to: on loopback, only
    ``localhost`` or its own address; elsewhere, a host name or an IP address."""
    if is_loopback(preview_host):
        if name not in ("localhost", preview_host):
            raise ValueError(
                f"--preview-name {name!r} must be localhost or {preview_host}, the address "
                "the viewer listens on"
            )
        return
    try:
        ipaddress.ip_address(name)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", name):
            raise ValueError(f"--preview-name {name!r} is not a host name") from None


def forward_auth_settings(
    jwks_url: str | None,
    issuer: str | None,
    audience: str | None,
    user_group: str | None,
    admin_group: str | None,
    header: str | None,
    *,
    public: bool,
) -> ForwardAuthSettings | None:
    """The forward-auth settings, or None when none are given; ``ValueError`` for an
    incomplete set, or one without a public origin behind the proxy."""
    required = {
        "--forward-auth-jwks-url": jwks_url,
        "--forward-auth-issuer": issuer,
        "--forward-auth-audience": audience,
        "--forward-auth-user-group": user_group,
    }
    given = [*required.values(), admin_group, header]
    if all(value is None for value in given):
        return None
    missing = [option for option, value in required.items() if not value]
    if missing:
        raise ValueError(f"forward auth needs {', '.join(missing)}")
    if not public:
        raise ValueError(
            "forward auth is for a server behind Authentik's proxy: it needs --public-origin"
        )
    return ForwardAuthSettings(
        jwks_url=str(jwks_url),
        issuer=str(issuer),
        audience=str(audience),
        user_group=str(user_group),
        admin_group=admin_group or None,
        header=header or HEADER,
    )


def proxy_settings(
    public_origin: str | None,
    preview_public_origin: str | None,
    trust_proxy_from: Sequence[str],
    *,
    allow_remote: bool,
    preview_name_given: bool,
) -> tuple[tuple[PublicOrigin, PublicOrigin] | None, tuple[Network, ...]]:
    """The public origins (the workspace's and the preview's) and the trusted proxy
    networks; ``ValueError`` for settings that are not both ``https://`` origins, not
    given together, not distinct, or given without ``--allow-remote``."""
    if (public_origin is None) != (preview_public_origin is None):
        raise ValueError("--public-origin and --preview-public-origin must be given together")
    if (public_origin is not None or trust_proxy_from) and not allow_remote:
        raise ValueError(
            "--public-origin and --trust-proxy-from are for a server behind a proxy, off "
            "loopback: they need --allow-remote"
        )
    networks: list[Network] = []
    for text in trust_proxy_from:
        try:
            network = parse_network(text)
        except ValueError as exc:
            raise ValueError(f"--trust-proxy-from {exc}") from None
        if network not in networks:
            networks.append(network)
    if public_origin is None or preview_public_origin is None:
        return None, tuple(networks)
    if preview_name_given:
        raise ValueError("--preview-public-origin names the viewer: drop --preview-name")
    try:
        workspace = PublicOrigin.parse(public_origin)
    except ValueError as exc:
        raise ValueError(f"--public-origin {exc}") from None
    try:
        viewer = PublicOrigin.parse(preview_public_origin)
    except ValueError as exc:
        raise ValueError(f"--preview-public-origin {exc}") from None
    if workspace == viewer:
        raise ValueError("--preview-public-origin must differ from --public-origin")
    return (workspace, viewer), tuple(networks)


async def serve(
    app: web.Application,
    host: str,
    port: int,
    login_token: str,
    preview: tuple[web.Application, Preview, str, int, str] | None = None,
    public: tuple[PublicOrigin, PublicOrigin] | None = None,
    login_file: Path | None = None,
) -> None:
    """Serve until SIGTERM or SIGINT, after printing the login URL once, or with
    ``login_file`` writing it there (mode 600) instead, removed when the server stops.
    With ``preview`` (its application, its state, host, port and the name it is given
    by), the PDF viewer's origin is served too. With ``public`` (the workspace's and the
    preview's public origins), those are the origins announced and printed, with the
    addresses served at beside them."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    viewer = web.AppRunner(preview[0], access_log=None) if preview else None
    try:
        await web.TCPSite(runner, host, port).start()
        bound = runner.addresses[0][1] if port == 0 else port
        url = base_url(host, bound)
        if preview is not None and viewer is not None:
            _, state, preview_host, preview_port, name = preview
            await viewer.setup()
            await web.TCPSite(viewer, preview_host, preview_port).start()
            shown = viewer.addresses[0][1] if preview_port == 0 else preview_port
            if name == "localhost" and preview_host == "127.0.0.1":
                await _also_on_ipv6(viewer, shown)
            if public is None:
                state.origin = base_url(name, shown)
                print(f"PDF viewer origin on {state.origin}/", flush=True)
                state.workspace_origin = url
            else:
                state.origin = public[1].origin
                served = base_url(preview_host, shown)
                print(f"PDF viewer origin on {state.origin}/ (served at {served}/)", flush=True)
                state.workspace_origin = public[0].origin
        if public is None:
            print(f"Sandbox Science workspace on {url}/", flush=True)
        else:
            print(
                f"Sandbox Science workspace on {public[0].origin}/ (served at {url}/)",
                flush=True,
            )
            url = public[0].origin
        login_url = f"{url}/login?token={login_token}"
        if login_file is None:
            print(
                f"Log in by opening this URL; it works until the server stops:\n    {login_url}",
                flush=True,
            )
        else:
            # The link logs in as the operator, an admin: it stays out of the log.
            write_private(login_file, login_url + "\n")
            print(
                f"Log in through Authentik; the operator's login URL is in {login_file} "
                f"(sandbox-science-workspace login-url --state-dir {login_file.parent})",
                flush=True,
            )
        await stopping.wait()
        _log.info("stopping")
    finally:
        if login_file is not None:
            login_file.unlink(missing_ok=True)
        if viewer is not None:
            await viewer.cleanup()
        await runner.cleanup()


async def _also_on_ipv6(viewer: web.AppRunner, port: int) -> None:
    """The viewer given by ``localhost`` also listens on ``[::1]``, where a browser may try
    ``localhost`` first, so no other program on that port gets the preview's requests;
    quietly not where this computer has no IPv6 loopback."""
    try:
        await web.TCPSite(viewer, "::1", port).start()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            _log.warning(
                "another program listens on [::1]:%s: a browser that tries localhost on ::1 "
                "first reaches it rather than the PDF viewer. Free that port, or give "
                "--preview-listen another port",
                port,
            )
        else:
            _log.debug("no IPv6 loopback for the viewer: %s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-workspace``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-workspace", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    reading = commands.add_parser(
        "login-url",
        help="print the operator's login URL, which serve writes with forward auth on",
        allow_abbrev=False,
    )
    reading.add_argument("--state-dir", type=Path, required=True, help="serve's --state-dir")
    command = commands.add_parser(
        "serve", help="serve the workspace GUI and its API", allow_abbrev=False
    )
    command.add_argument(
        "--state-dir", type=Path, required=True, help="the harness's state directory"
    )
    command.add_argument(
        "--static", type=Path, required=True, help="the web app's build (web/workspace/dist)"
    )
    command.add_argument(
        "--listen", default=DEFAULT_LISTEN, help=f"host:port (default {DEFAULT_LISTEN})"
    )
    command.add_argument(
        "--preview-listen",
        help="host:port of the PDF viewer's own origin (default: the same host, the next port)",
    )
    command.add_argument(
        "--preview-name",
        help="the host name the viewer's origin is given by: localhost or its listen "
        "address on loopback (default: localhost when it listens on 127.0.0.1, another "
        "site from the workspace's 127.0.0.1; otherwise its listen address)",
    )
    command.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a listen address that is not loopback (plain HTTP: see the docs)",
    )
    command.add_argument(
        "--public-origin",
        help="behind a TLS-terminating proxy: the https:// origin the browser reaches the "
        "workspace at; the server then answers only to its host and takes changes only from "
        "it (needs --allow-remote and --preview-public-origin)",
    )
    command.add_argument(
        "--preview-public-origin",
        help="the https:// origin the browser reaches the PDF viewer at, through the proxy; "
        "an origin of its own (needs --public-origin; replaces --preview-name)",
    )
    command.add_argument(
        "--trust-proxy-from",
        action="append",
        default=[],
        metavar="CIDR",
        help="a network whose connections' X-Forwarded-Proto and X-Forwarded-Host are "
        "believed; repeat for each (needs --allow-remote; never X-Forwarded-For or Forwarded)",
    )
    forward = command.add_argument_group(
        "forward auth",
        "behind Authentik's forward auth (needs --public-origin): a user Authentik admits "
        "gets a session from its proxy outpost's signed token, without the login URL",
    )
    forward.add_argument(
        "--forward-auth-jwks-url", metavar="URL", help="the provider's JWKS (public keys)"
    )
    forward.add_argument(
        "--forward-auth-issuer", metavar="URL", help="the tokens' exact iss: the provider's"
    )
    forward.add_argument(
        "--forward-auth-audience", metavar="ID", help="the tokens' aud: the provider's client ID"
    )
    forward.add_argument(
        "--forward-auth-user-group", metavar="GROUP", help="the group a user must be in"
    )
    forward.add_argument(
        "--forward-auth-admin-group",
        metavar="GROUP",
        help="the group the admin-only actions need (default: none; every user may)",
    )
    forward.add_argument(
        "--forward-auth-header", metavar="NAME", help=f"the token's header (default {HEADER})"
    )
    command.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    command.add_argument("--worker", help="worker URL for live jobs and cancellation")
    command.add_argument("--worker-token-file", type=Path, help="worker key (server only)")
    command.add_argument("--worker-ca-file", type=Path, help="private worker CA; requires HTTPS")
    command.add_argument(
        "--skills-metadata-model",
        help="the alias of the import's pass whose topics, entry skills and short "
        "descriptions the skills page shows, as runs are given them (default: none)",
    )
    command.add_argument(
        "--mcp-project", help="the one project whose configured MCP service names are shown"
    )
    command.add_argument(
        "--mcp-service",
        action="append",
        default=[],
        metavar="NAME=PURPOSE",
        help="a configured MCP service and its public purpose; repeat for each service",
    )
    command.add_argument(
        "--mcp-check",
        metavar="URL",
        help="the control service, which lists a service through the agent runtime's own "
        "client for the user's availability check (D98)",
    )
    command.add_argument(
        "--mcp-check-token-file", type=Path, help="the workspace's token for --mcp-check"
    )
    command.add_argument(
        "--mcp-check-ca-file",
        type=Path,
        help="the private CA of an https --mcp-check (the Kubernetes bundle's control service)",
    )
    agents = command.add_argument_group(
        "agent models", "trusted project choices for main, delegated and reviewer roles"
    )
    agents.add_argument("--agent-model", action="append", default=[], type=_agent_model)
    agents.add_argument("--agent-effort", action="append", default=[], type=_agent_effort)
    agents.add_argument("--agent-api", action="append", default=[], type=_agent_api)
    repairs = command.add_argument_group(
        "math repairs", "repairs of LaTeX that KaTeX rejects, by the reviewer model"
    )
    repairs.add_argument(
        "--model-url",
        help="the model gateway's OpenAI-compatible base URL (the LiteLLM proxy), as the "
        "gateway answers it: redirects are not followed",
    )
    repairs.add_argument(
        "--reviewer-model",
        help="the alias of the reviewer model, the only model asked for repairs (off: none)",
    )
    repairs.add_argument(
        "--api-key-file", type=Path, help="a file holding the workspace's key for the gateway"
    )
    repairs.add_argument(
        "--reviewer-api", choices=("chat", "responses", "messages"), default="chat"
    )
    repairs.add_argument("--reviewer-effort")
    args = parser.parse_args(argv)
    if args.command == "login-url":
        return print_login_url(args.state_dir)
    configure_logging("workspace", level=args.log_level)
    try:
        host, port = parse_listen(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        repair = math_repairs(
            args.model_url,
            args.reviewer_model,
            args.api_key_file,
            api=args.reviewer_api,
            reasoning_effort=args.reviewer_effort,
        )
    except ValueError as exc:
        parser.error(str(exc))
    role_items = dict(args.agent_model)
    effort_items = dict(args.agent_effort)
    api_items = dict(args.agent_api)
    if len(role_items) != len(args.agent_model) or len(effort_items) != len(args.agent_effort):
        parser.error("agent model and effort roles may be named only once")
    if len(api_items) != len(args.agent_api):
        parser.error("an agent alias may name only one API")
    if role_items and not {"main", "delegated"}.issubset(role_items):
        parser.error("agent models need main and delegated roles")
    if not set(effort_items).issubset(role_items):
        parser.error("agent effort needs a configured agent model role")
    unknown_apis = set(api_items).difference(role_items.values())
    if unknown_apis:
        parser.error("agent API aliases must be in the agent model catalog")
    if "reviewer" in role_items and api_items.get(role_items["reviewer"], "chat") != "messages":
        effort_items.setdefault("reviewer", "medium")
    for role, alias in role_items.items():
        if api_items.get(alias, "chat") == "messages" and role in effort_items:
            parser.error(f"{role} uses Messages, which does not accept agent effort")
    model_configuration = (
        ModelConfiguration(
            defaults={
                role: requested(
                    alias,
                    api_items.get(alias, "chat"),
                    effort_items.get(role),
                )
                for role, alias in role_items.items()
            },
            available={alias: api_items.get(alias, "chat") for alias in set(role_items.values())},
        )
        if role_items
        else None
    )
    try:
        preview_host, preview_port = (
            parse_listen(args.preview_listen)
            if args.preview_listen
            else (host, port + 1 if port else 0)
        )
    except ValueError as exc:
        parser.error(str(exc))
    if preview_port > 65535:
        parser.error(
            f"--listen port {port} leaves no next port for the viewer: give --preview-listen"
        )
    loopback = is_loopback(host)
    if not is_loopback(preview_host) and not args.allow_remote:
        parser.error(f"{preview_host} is not a loopback address; see --allow-remote")
    if (preview_host, preview_port) == (host, port) and port:
        parser.error("--preview-listen must differ from --listen: the viewer needs its own origin")
    name = preview_name(host, preview_host, args.preview_name)
    try:
        check_preview_name(name, preview_host)
    except ValueError as exc:
        parser.error(str(exc))
    if not loopback and not args.allow_remote:
        parser.error(
            f"{host} is not a loopback address; --allow-remote accepts connections from "
            "other machines, over plain HTTP"
        )
    try:
        public, trusted = proxy_settings(
            args.public_origin,
            args.preview_public_origin,
            args.trust_proxy_from,
            allow_remote=args.allow_remote,
            preview_name_given=args.preview_name is not None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        forward_settings = forward_auth_settings(
            args.forward_auth_jwks_url,
            args.forward_auth_issuer,
            args.forward_auth_audience,
            args.forward_auth_user_group,
            args.forward_auth_admin_group,
            args.forward_auth_header,
            public=public is not None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not args.state_dir.is_dir():
        print(f"error: there is no state directory {args.state_dir}", file=sys.stderr)
        return 1
    job_worker = None
    job_state = None
    if any(
        value is not None for value in (args.worker, args.worker_token_file, args.worker_ca_file)
    ):
        if args.worker is None or args.worker_token_file is None:
            parser.error("--worker and --worker-token-file must be given together")
        if not args.worker.startswith(("http://", "https://")):
            parser.error("--worker must be an http or https URL")
        try:
            token = args.worker_token_file.read_text().strip()
            if not token:
                raise ValueError("worker token file is empty")
            job_worker = WorkerClient(args.worker, token, ca_file=args.worker_ca_file)
            job_state = JobState(args.state_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(f"cannot configure job worker: {exc}")
        except DocumentError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
    exposure = (
        Exposure(public[0] if public is not None else None, trusted)
        if public is not None or trusted
        else None
    )
    login_token = secrets.token_urlsafe(32)
    preview = Preview(Capabilities())
    mcp_inventory: list[ServiceConfig] = []
    if args.mcp_service and args.mcp_project is None:
        parser.error("--mcp-service needs --mcp-project")
    for item in args.mcp_service:
        # Not `name`: that is the viewer's host name, used below.
        service, separator, purpose = item.partition("=")
        if not separator or any(found.name == service for found in mcp_inventory):
            parser.error("--mcp-service needs a unique NAME=PURPOSE")
        try:
            mcp_inventory.append(ServiceConfig(name=service, purpose=purpose))
        except ValueError:
            parser.error("--mcp-service needs a valid service name and purpose")
    checks = None
    if args.mcp_check_ca_file is not None and args.mcp_check is None:
        parser.error("--mcp-check-ca-file needs --mcp-check")
    if args.mcp_check is not None or args.mcp_check_token_file is not None:
        if args.mcp_check is None or args.mcp_check_token_file is None or args.mcp_project is None:
            parser.error("--mcp-check needs --mcp-check-token-file and --mcp-project")
        try:
            checks = AvailabilityClient(
                args.mcp_check,
                args.mcp_check_token_file.read_text().strip(),
                ca_file=args.mcp_check_ca_file,
            )
        except (OSError, UnicodeError, ValueError):  # ssl.SSLError is an OSError
            parser.error("cannot configure the MCP availability check")
    try:
        app = create_app(
            ProjectStore(args.state_dir, readonly=True),
            args.static,
            login_token=login_token,
            # A public origin replaces the loopback names with its own host.
            check_host=loopback and public is None,
            math_repair=repair,
            job_worker=job_worker,
            job_state=job_state,
            preview=preview,
            skills_metadata=args.skills_metadata_model,
            mcp_project=args.mcp_project,
            mcp_inventory=tuple(mcp_inventory),
            mcp_checks=checks,
            model_configuration=model_configuration,
            exposure=exposure,
            forward_auth=Verifier(forward_settings) if forward_settings is not None else None,
        )
    except OSError as exc:
        print(f"error: {exc} (build the web app: npm run build)", file=sys.stderr)
        return 1
    try:
        # The writer brings the control store to the current schema now, so that the
        # read-only views can read it, or the server does not start, saying why.
        app[CONTEXT].writer.initialize()
    except DocumentError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    if repair.enabled:
        _log.info(
            "math repairs ask the reviewer model %r through the model gateway", repair.reviewer
        )
    else:
        _log.info("math repairs by a model are off")
    if public is not None:
        _log.info(
            "behind a proxy at the public origin %s, with the preview at %s; trusted to "
            "forward the scheme and host: %s",
            public[0].origin,
            public[1].origin,
            ", ".join(str(network) for network in trusted) or "none",
        )
        if public[0].host == public[1].host:
            _log.warning(
                "the preview's public origin %s shares the workspace's host: figures and the "
                "PDF viewer may run on the workspace page's thread. Give it a host name of "
                "its own",
                public[1].origin,
            )
    if forward_settings is not None:
        _log.info(
            "login through Authentik: tokens from %s, signed by the keys at %s, for users in "
            "%s; admin-only actions: %s",
            forward_settings.issuer,
            forward_settings.jwks_url,
            forward_settings.user_group,
            forward_settings.admin_group or "every user",
        )
    if public is None and not loopback:
        _log.warning(
            "listening on %s, which is not loopback: the login URL and session cookie travel "
            "unencrypted, and any Host name is accepted",
            host,
        )
    if name == host and public is None:
        # Same site: a browser may run the preview origin's frames on the workspace page's
        # own thread, where a heavy figure stalls the page (FG-17; review of #492, M1).
        remedy = (
            "drop --preview-name, or give --preview-name localhost"
            if args.preview_name not in (None, "localhost")
            else "listen on 127.0.0.1, where the viewer is given by localhost"
        )
        _log.warning(
            "the preview origin %s shares the workspace's host: figures and the PDF viewer "
            "may run on the workspace page's thread. For a process of their own, %s",
            name,
            remedy,
        )
    viewer_host: Callable[[web.Request], bool]
    if public is not None:
        viewer_host = Exposure(public[1], trusted).host_check  # its public host only
    elif is_loopback(preview_host):
        viewer_host = own_host
    else:
        viewer_host = lambda request: True  # noqa: E731 - the names are not known
    viewer_app = create_preview_app(
        app[CONTEXT].documents,
        preview.capabilities,
        workspace_origin=lambda: preview.workspace_origin,
        viewer_dir=args.static / "viewer",
        figures_dir=args.static / "figures",
        check_host=viewer_host,
    )
    try:
        asyncio.run(
            serve(
                app,
                host,
                port,
                login_token,
                (viewer_app, preview, preview_host, preview_port, name),
                public,
                args.state_dir / LOGIN_URL_FILE if forward_settings is not None else None,
            )
        )
    except KeyboardInterrupt:  # before the signal handlers are in place
        return 0
    except OSError as exc:
        print(f"error: cannot listen on {args.listen}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
