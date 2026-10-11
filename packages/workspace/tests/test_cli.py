# SPDX-License-Identifier: MIT
"""The command line: where it listens, what it refuses, and a real server end to end."""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import sys
from pathlib import Path

import aiohttp
import pytest

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


@pytest.mark.asyncio
async def test_the_server_prints_a_login_url_that_opens_a_session(
    state_dir: Path, static_dir: Path
) -> None:
    process = await asyncio.create_subprocess_exec(
        str(COMMAND),
        *("serve", "--state-dir", str(state_dir), "--static", str(static_dir)),
        *("--listen", "127.0.0.1:0"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
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
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
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
