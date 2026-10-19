# SPDX-License-Identifier: MIT
"""The command line: where it listens, what it refuses, and a real server end to end."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import socket
import sqlite3
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.workspace import cli

COMMAND = Path(sys.executable).parent / "sandbox-science-workspace"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("127.0.0.1:8200", ("127.0.0.1", 8200)),
        ("localhost:0", ("localhost", 0)),
        ("[::1]:8200", ("::1", 8200)),
        ("0.0.0.0:65535", ("0.0.0.0", 65535)),  # noqa: S104 - parsing only
    ],
)
def test_listen_addresses(text: str, expected: tuple[str, int]) -> None:
    assert cli.parse_listen(text) == expected


@pytest.mark.parametrize("text", ["8200", ":8200", "127.0.0.1:", "127.0.0.1:x", "h:65536", ""])
def test_malformed_listen_addresses_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not host:port"):
        cli.parse_listen(text)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),  # noqa: S104 - classifying only
        ("::", False),
        ("192.168.1.5", False),
        ("babbage", False),
        ("localhost.example", False),
    ],
)
def test_only_loopback_counts_as_loopback(host: str, loopback: bool) -> None:
    assert cli.is_loopback(host) is loopback


@pytest.mark.parametrize(
    ("host", "preview_host", "given", "name"),
    [
        # By default the viewer on 127.0.0.1 is given by localhost: another site (FG-17).
        ("127.0.0.1", "127.0.0.1", None, "localhost"),
        # A workspace named localhost keeps the address, which is then the other site.
        ("localhost", "127.0.0.1", None, "127.0.0.1"),
        ("localhost", "localhost", None, "localhost"),
        ("127.0.0.1", "127.0.0.2", None, "127.0.0.2"),
        ("::1", "::1", None, "::1"),
        # Configurable: the listen address itself, as before.
        ("127.0.0.1", "127.0.0.1", "127.0.0.1", "127.0.0.1"),
    ],
)
def test_the_preview_origin_is_given_by_localhost_by_default(
    host: str, preview_host: str, given: str | None, name: str
) -> None:
    assert cli.preview_name(host, preview_host, given) == name
    cli.check_preview_name(name, preview_host)


@pytest.mark.parametrize(
    ("name", "preview_host"),
    [
        ("127.0.0.2", "127.0.0.1"),  # not where the viewer listens
        ("viewer.example", "127.0.0.1"),  # the host check would refuse it
        ("[::1]", "::1"),
        ("bad name", "0.0.0.0"),  # noqa: S104 - checking only
        ("http://viewer.example", "0.0.0.0"),  # noqa: S104 - checking only
    ],
)
def test_a_preview_name_the_listener_would_not_answer_to_is_refused(
    name: str, preview_host: str
) -> None:
    with pytest.raises(ValueError, match="--preview-name"):
        cli.check_preview_name(name, preview_host)


@pytest.mark.parametrize(
    ("listen", "name", "warned"),
    [
        (["--listen", "127.0.0.1:8200"], "localhost", None),
        (["--listen", "localhost:8200"], "localhost", "listen on 127.0.0.1"),
        (["--preview-name", "127.0.0.1"], "127.0.0.1", "drop --preview-name, or give "
         "--preview-name localhost"),
        (["--preview-listen", "127.0.0.2:8201"], "127.0.0.2", None),
    ],
)  # fmt: skip
def test_serve_warns_only_when_the_preview_origin_shares_the_site(
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    listen: list[str],
    name: str,
    warned: str | None,
) -> None:
    given: list[object] = []

    async def serve(*args: object) -> None:
        preview = args[4]
        assert isinstance(preview, tuple)
        given.append(preview[-1])

    monkeypatch.setattr(cli, "serve", serve)
    args = ["serve", "--state-dir", str(state_dir), "--static", str(static_dir), *listen]
    assert cli.main(args) == 0
    assert given == [name]
    assert ("shares the workspace's host" in caplog.text) is (warned is not None)
    assert warned is None or warned in caplog.text


def test_messages_agent_reviewer_starts_without_an_effort_default(
    state_dir: Path, static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[bool] = []

    async def serve(*_: object) -> None:
        served.append(True)

    monkeypatch.setattr(cli, "serve", serve)
    args = [
        "serve",
        "--state-dir",
        str(state_dir),
        "--static",
        str(static_dir),
        "--agent-model",
        "main=sol",
        "--agent-model",
        "delegated=sol",
        "--agent-model",
        "reviewer=claude",
        "--agent-api",
        "claude=messages",
    ]
    assert cli.main(args) == 0
    assert served == [True]


def test_another_address_needs_allow_remote(tmp_path: Path, static_dir: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(
            ["serve", "--state-dir", str(tmp_path), "--static", str(static_dir),
             "--listen", "0.0.0.0:8200"]
        )  # fmt: skip
    assert refused.value.code == 2


def test_a_missing_build_or_state_directory_is_an_error(
    tmp_path: Path, static_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = ["serve", "--listen", "127.0.0.1:0"]
    no_state = cli.main([*common, "--state-dir", str(tmp_path / "missing"), "--static", "x"])
    assert no_state == 1
    assert "no state directory" in capsys.readouterr().err
    no_build = cli.main([*common, "--state-dir", str(tmp_path), "--static", str(tmp_path)])
    assert no_build == 1
    assert "npm run build" in capsys.readouterr().err


def test_the_server_brings_the_control_store_up_to_date_or_says_why_not(
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served: list[bool] = []

    async def serve(*_: object) -> None:
        served.append(True)

    monkeypatch.setattr(cli, "serve", serve)
    args = ["serve", "--state-dir", str(state_dir), "--static", str(static_dir)]
    store = DocumentStore(store_path(state_dir))
    store.initialize()
    store.close()

    def add_notes(schema: migrations.Schema) -> None:
        schema.execute("CREATE TABLE notes (text TEXT NOT NULL) STRICT")

    # A later version adds a step: the server's writer runs it before the views read.
    with monkeypatch.context() as later:
        later.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, add_notes))
        later.setattr(migrations, "VERSION", migrations.VERSION + 1)
        assert cli.main(args) == 0
        reader = DocumentStore(store_path(state_dir), readonly=True)
        assert reader.documents("p1") == []
        reader.close()
    assert served == [True]
    # This version cannot use a newer store: the server does not start, and says why.
    assert cli.main(args) == 1
    assert served == [True]
    assert "newer than this version's" in capsys.readouterr().err


@pytest.mark.parametrize("worker", [False, True])
def test_an_old_control_store_stops_the_server_with_the_reason(
    state_dir: Path,
    static_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: bool,
) -> None:
    async def serve(*_: object) -> None:
        raise AssertionError("the server must not start")

    monkeypatch.setattr(cli, "serve", serve)
    for found in state_dir.glob("control.sqlite3*"):  # in place of the new store
        found.unlink()
    database = sqlite3.connect(store_path(state_dir))
    database.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY)")
    database.execute("PRAGMA user_version = 1")
    database.commit()
    database.close()
    token = tmp_path / "worker.token"
    token.write_text("test-secret-worker-token")
    extra = ["--worker", "http://127.0.0.1:9", "--worker-token-file", str(token)]
    args = ["serve", "--state-dir", str(state_dir), "--static", str(static_dir)]
    assert cli.main([*args, *(extra if worker else [])]) == 1
    err = capsys.readouterr().err
    assert "error: control.sqlite3 is a development control store" in err
    assert "Traceback" not in err


def ipv6_loopback() -> bool:
    """Whether this computer has an IPv6 loopback to listen on."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.asyncio
async def test_another_program_on_the_viewers_ipv6_port_is_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    if not ipv6_loopback():
        pytest.skip("no IPv6 loopback here")
    runner = web.AppRunner(web.Application())
    await runner.setup()
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as other:
        other.bind(("::1", 0))
        other.listen()
        port = other.getsockname()[1]
        with caplog.at_level(logging.WARNING, "sandbox_science.workspace"):
            await cli._also_on_ipv6(runner, port)
    await runner.cleanup()
    assert f"another program listens on [::1]:{port}" in caplog.text


@pytest.mark.asyncio
async def test_the_server_prints_a_login_url_that_opens_a_session(
    state_dir: Path, static_dir: Path
) -> None:
    process = await asyncio.create_subprocess_exec(
        str(COMMAND),
        *("serve", "--state-dir", str(state_dir), "--static", str(static_dir)),
        *("--listen", "127.0.0.1:0"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        printed = ""
        while "token=" not in printed:
            line = await asyncio.wait_for(process.stdout.readline(), 30)
            assert line, f"the server stopped; it printed {printed!r}"
            printed += line.decode()
        match = re.search(r"(http://127\.0\.0\.1:\d+)/login\?token=([A-Za-z0-9_-]{43})\n", printed)
        assert match is not None, printed
        base, token = match[1], match[2]
        # The viewer listens on 127.0.0.1 and is given by localhost, another site (FG-17).
        viewer = re.search(r"PDF viewer origin on (http://localhost:(\d+))/\n", printed)
        assert viewer is not None, printed
        origin, viewer_port = viewer[1], viewer[2]
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            async with session.get(
                f"http://127.0.0.1:{viewer_port}/viewer/",
                headers={"Host": f"localhost:{viewer_port}"},
            ) as named:
                assert named.status == 404  # answered: the stand-in build has no viewer
                assert f"frame-ancestors {base}" in named.headers["Content-Security-Policy"]
            async with session.get(f"{base}/") as page:
                assert origin in page.headers["Content-Security-Policy"]
            if ipv6_loopback():
                # Also on [::1], where a browser may try localhost first.
                async with session.get(
                    f"http://[::1]:{viewer_port}/viewer/",
                    headers={"Host": f"localhost:{viewer_port}"},
                ) as named_on_ipv6:
                    assert named_on_ipv6.status == 404
            async with session.get(f"{base}/api/v0/projects") as refused:
                assert refused.status == 401
            async with session.get(f"{base}/login?token={token}") as opened:
                assert opened.status == 200  # redirected to the app
                assert "<title>Workspace</title>" in await opened.text()
            async with session.get(f"{base}/api/v0/projects") as listed:
                assert listed.status == 200
                projects = (await listed.json())["projects"]
        assert [project["project_id"] for project in projects] == ["broken", "p1", "p2"]
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        status = await asyncio.wait_for(process.wait(), 30)
    assert status == 0
    assert process.stderr is not None
    # Two sites: no warning that the frames may share the page's thread.
    assert "shares the workspace's host" not in (await process.stderr.read()).decode()
