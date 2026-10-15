# SPDX-License-Identifier: MIT
"""Registering the Access provider from the owner's file (access_provider.py).

Every file here is synthetic, with obviously fake values. The OpenShell CLI is the
stand-in (fake_openshell.py), which keeps each call's arguments and environment and says
the values it is given on stdout and stderr, as a careless CLI could.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.launcher import access_provider, mcp
from sandbox_science.launcher.access_provider import RegistrationError, parse
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import Layout

PROFILE = Path(__file__).parents[3] / "examples/openshell/profiles/cloudflare-access.yaml"
ID = "test-secret-client-id-4f1c.access"
SECRET = "test-secret-client-secret-9a7e0b51"
BOTH = {"CF_ACCESS_CLIENT_ID": ID, "CF_ACCESS_CLIENT_SECRET": SECRET}
PLAIN = f"CF_ACCESS_CLIENT_ID={ID}\nCF_ACCESS_CLIENT_SECRET={SECRET}\n"

FORMATS = {
    "header style, with a colon": f'CF-Access-Client-Id: = "{ID}"\nCF-Access-Client-Secret:="{SECRET}"\n',  # noqa: E501
    "header style, single quotes": f"CF-Access-Client-Id = '{ID}'\nCF-Access-Client-Secret = '{SECRET}'\n",  # noqa: E501
    "env style": PLAIN,
    "any case, comments, export and other keys": (
        "# the harness's service token\n\n"
        f"export cf_access-client_ID =   {ID}   # added 2026-10-14\n"
        f'  Cf-Access_Client-SECRET:   =   "  {SECRET}  "   # rotated\n'
        "CF_ACCOUNT_NAME=ignored\n"
    ),
}


def leaked(text: str) -> bool:
    return ID in text or SECRET in text


@pytest.mark.parametrize("text", FORMATS.values(), ids=FORMATS.keys())
def test_the_owners_formats(text: str) -> None:
    assert parse(text) == BOTH


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        (f"CF_ACCESS_CLIENT_ID={ID}\n", "no CF_ACCESS_CLIENT_SECRET"),
        (f"CF_ACCESS_CLIENT_ID={ID}\nCF_ACCESS_CLIENT_SECRET=\n", "no CF_ACCESS_CLIENT_SECRET"),
        (f"# nothing\nCF-Access-Client-Secret: {SECRET}\n", "line 2 is not KEY=VALUE"),
        (
            f'CF_ACCESS_CLIENT_ID="{ID}\nCF_ACCESS_CLIENT_SECRET=x\n',
            "line 1, CF_ACCESS_CLIENT_ID: its quotes",
        ),
        (f"CF_ACCESS_CLIENT_ID='{ID}' {SECRET}\n", "line 1, CF_ACCESS_CLIENT_ID: its quotes"),
        (f"{PLAIN}cf-access-client-id={ID}\n", "CF_ACCESS_CLIENT_ID is set twice"),
    ],
)
def test_a_file_it_cannot_read_both_values_from_is_refused_by_key_alone(
    text: str, refusal: str
) -> None:
    with pytest.raises(RegistrationError) as refused:
        parse(text)
    assert refusal in str(refused.value)
    assert not leaked(str(refused.value))


@pytest.fixture
def openshell(fake_openshell: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    class Stand:
        state = tmp_path / "openshell.json"
        calls = tmp_path / "calls.jsonl"
        journal = tmp_path / "journal"
        cli = fake_openshell

        def has(self, providers: list[str], profiles: list[str]) -> None:
            self.state.write_text(
                json.dumps({"providers": providers, "profiles": profiles, "sandboxes": {}})
            )

        def called(self) -> list[dict[str, Any]]:
            if not self.calls.exists():
                return []
            return [json.loads(line) for line in self.calls.read_text().splitlines()]

        def steps(self) -> list[str]:
            return self.journal.read_text().splitlines()

    stand = Stand()
    stand.journal.touch()
    monkeypatch.setenv("FAKE_OPENSHELL", str(stand.state))
    monkeypatch.setenv("FAKE_JOURNAL", str(stand.journal))
    monkeypatch.setenv("FAKE_OPENSHELL_CALLS", str(stand.calls))
    monkeypatch.delenv("FAKE_PARTS", raising=False)
    return stand


def owners_file(tmp_path: Path, text: str = PLAIN, mode: int = 0o600) -> Path:
    path = tmp_path / "secrets" / "cloudflare.env"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def register(path: Path, cli: Path) -> int:
    return access_provider.main([str(path), "--profile", str(PROFILE), "--openshell", str(cli)])


def only_the_registration_had_them(calls: list[dict[str, Any]], registration: str) -> None:
    """The values were in no argument, and in the environment of the one call that
    registered them."""
    for call in calls:
        assert not any(leaked(arg) for arg in call["argv"]), call["argv"]
        given = {key: call["env"].get(key) for key in BOTH}
        if call["argv"][:2] == ["provider", registration]:
            assert given == BOTH
        else:
            assert given == dict.fromkeys(BOTH)
            assert not any(leaked(value) for value in call["env"].values())


def test_a_new_gateway_gets_the_profile_and_the_provider(
    openshell: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = owners_file(tmp_path, FORMATS["header style, with a colon"])
    assert register(path, openshell.cli) == 0
    assert openshell.steps() == [
        "openshell provider profile lint cloudflare-access.yaml",
        "openshell provider profile import cloudflare-access.yaml",
        "openshell provider create cloudflare-access crypto-research-mcp "
        "CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET",
    ]
    only_the_registration_had_them(openshell.called(), "create")
    said = capsys.readouterr()
    assert said.out == f"created the OpenShell provider crypto-research-mcp from {path}\n"
    assert not leaked(said.out + said.err)
    assert not any(leaked(value) for value in os.environ.values())


def test_an_existing_provider_is_updated(
    openshell: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    openshell.has(["crypto-research-mcp"], ["cloudflare-access"])
    assert register(owners_file(tmp_path), openshell.cli) == 0
    assert openshell.steps() == [
        "openshell provider update crypto-research-mcp CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET"
    ]
    only_the_registration_had_them(openshell.called(), "update")
    said = capsys.readouterr()
    assert said.out.startswith("updated the OpenShell provider crypto-research-mcp")
    assert not leaked(said.out + said.err)


def test_a_failed_registration_shows_nothing_openshell_said(
    openshell: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openshell.has([], ["cloudflare-access"])
    monkeypatch.setenv("FAKE_PARTS", json.dumps({"openshell": "provider-fails"}))
    done = subprocess.run(
        [sys.executable, "-I", "-m", "sandbox_science.launcher.access_provider",
         str(owners_file(tmp_path)), "--profile", str(PROFILE), "--openshell", str(openshell.cli)],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == 1
    assert done.stderr == (
        "error: openshell provider create failed, with status 1; what it said is not shown, "
        "since it could hold the values\n"
    )
    assert not leaked(done.stdout + done.stderr)
    only_the_registration_had_them(openshell.called(), "create")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o644, 0o660])
def test_a_file_others_may_use_is_refused(
    openshell: Any, tmp_path: Path, mode: int, capsys: pytest.CaptureFixture[str]
) -> None:
    path = owners_file(tmp_path, mode=mode)
    assert register(path, openshell.cli) == 1
    assert f"{path} has mode {mode:o}: its group or others may use it" in capsys.readouterr().err
    assert openshell.called() == []


def test_a_file_that_is_not_the_users_is_refused(
    openshell: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = owners_file(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    assert register(path, openshell.cli) == 1
    assert f"{path} is not this user's" in capsys.readouterr().err
    assert openshell.called() == []


def test_what_is_not_a_private_file_is_refused(
    openshell: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir(mode=0o700)
    assert register(folder, openshell.cli) == 1
    assert register(tmp_path / "absent.env", openshell.cli) == 1
    said = capsys.readouterr().err.splitlines()
    assert said == [
        f"error: {folder} is not a regular file",
        f"error: cannot open {tmp_path / 'absent.env'}: No such file or directory",
    ]
    assert openshell.called() == []


def test_a_missing_value_is_named_and_nothing_runs(
    openshell: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        register(owners_file(tmp_path, f"CF-Access-Client-Secret: = {SECRET}\n"), openshell.cli)
        == 1
    )
    said = capsys.readouterr()
    assert said.err == "error: no CF_ACCESS_CLIENT_ID\n"
    assert openshell.called() == []


def test_a_refresh_needs_the_owners_file(make_config: Any) -> None:
    proxy = {"image": "sandbox-science/mcp-proxy:abc1234", "providers": ["p"], "servers": ["s"]}
    config = load_config(make_config({"mcp_proxy": proxy}))
    layout = Layout(config.state_dir)
    with pytest.raises(LauncherError, match="--refresh-provider needs"):
        asyncio.run(mcp.register_provider(config, layout, cast(Any, None), refresh=True))


def test_values_already_in_the_environment_reach_no_other_command(
    openshell: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "test-secret-exported-earlier")
    assert register(owners_file(tmp_path), openshell.cli) == 0
    only_the_registration_had_them(openshell.called(), "create")
