# SPDX-License-Identifier: MIT
"""Registering the Access provider from the owner's file (access_provider.py).

Every file here is synthetic, with obviously fake values. The OpenShell CLI is the
stand-in (fake_openshell.py), which keeps each call's arguments, environment and
core-file limit, and says the values it is given on stdout and stderr, as a careless CLI
could. The helper refuses to run with the stand-in's ``FAKE_*`` variables set, so each
stand-in here is a wrapper that sets them itself.
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
from sandbox_science.launcher.access_provider import RegistrationError, parse, read
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import Layout

from .conftest import openshell_stand_in

REPOSITORY = Path(__file__).parents[3]
PROFILE = REPOSITORY / "examples/openshell/profiles/cloudflare-access.yaml"
ID = "test-secret-client-id-4f1c.access"
SECRET = "test-secret-client-secret-9a7e0b51"
BOTH = {"CF_ACCESS_CLIENT_ID": ID, "CF_ACCESS_CLIENT_SECRET": SECRET}
PLAIN = f"CF_ACCESS_CLIENT_ID={ID}\nCF_ACCESS_CLIENT_SECRET={SECRET}\n"

FORMATS = {
    "header style, with a colon": (
        f'CF-Access-Client-Id: = "{ID}"\nCF-Access-Client-Secret:="{SECRET}"\n'
    ),
    "header style, single quotes": (
        f"CF-Access-Client-Id = '{ID}'\nCF-Access-Client-Secret = '{SECRET}'\n"
    ),
    "env style, CRLF": PLAIN.replace("\n", "\r\n"),
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


@pytest.mark.parametrize(
    "character", [*"\x00\x0b\x0c\r\t\x85", chr(0x2028), chr(0x2029), chr(0x200B)]
)
def test_a_control_character_in_a_value_is_refused_not_taken_as_a_line_break(
    character: str,
) -> None:
    """Lines end at ``\\n`` alone, so no other break splits a value unseen."""
    text = f"CF_ACCESS_CLIENT_ID={ID}\nCF_ACCESS_CLIENT_SECRET={SECRET}{character}{SECRET}\n"
    with pytest.raises(RegistrationError) as refused:
        parse(text)
    assert (
        str(refused.value) == "line 2, CF_ACCESS_CLIENT_SECRET: its value has a control character"
    )


@pytest.fixture
def openshell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in list(os.environ):
        if name.startswith(access_provider.REFUSED_VARIABLES):
            monkeypatch.delenv(name)
    # In this process, which is pytest's: the subprocess test below runs it for real.
    monkeypatch.setattr(access_provider, "harden", lambda: None)

    class Stand:
        state = tmp_path / "openshell.json"
        calls = tmp_path / "calls.jsonl"
        journal = tmp_path / "journal"

        def cli(self, behaviour: str = "") -> Path:
            return openshell_stand_in(
                tmp_path / "bin" / f"openshell{behaviour}",
                FAKE_OPENSHELL=self.state,
                FAKE_JOURNAL=self.journal,
                FAKE_OPENSHELL_CALLS=self.calls,
                FAKE_PARTS=json.dumps({"openshell": behaviour}),
            )

        def has(self, providers: list[str], profiles: list[str]) -> None:
            self.state.write_text(
                json.dumps({"providers": providers, "profiles": profiles, "sandboxes": {}})
            )

        def called(self) -> list[dict[str, Any]]:
            if not self.calls.exists():
                return []
            return [json.loads(line) for line in self.calls.read_text().splitlines()]

        def steps(self) -> list[str]:
            return self.journal.read_text().splitlines() if self.journal.exists() else []

    return Stand()


def owners_file(tmp_path: Path, text: str | bytes = PLAIN, mode: int = 0o600) -> Path:
    folder = tmp_path / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "cloudflare.env"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    path.chmod(mode)
    return path


def register(path: Path, cli: Path | str) -> int:
    return access_provider.main([str(path), "--profile", str(PROFILE), "--openshell", str(cli)])


def only_the_registration_had_them(calls: list[dict[str, Any]], registration: str) -> None:
    """The values were in no argument, and in the environment of the one call that
    registered them."""
    assert calls
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
    assert register(path, openshell.cli()) == 0
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
    assert register(owners_file(tmp_path), openshell.cli()) == 0
    assert openshell.steps() == [
        "openshell provider update crypto-research-mcp CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET"
    ]
    only_the_registration_had_them(openshell.called(), "update")
    said = capsys.readouterr()
    assert said.out.startswith("updated the OpenShell provider crypto-research-mcp")
    assert not leaked(said.out + said.err)


def test_values_already_in_the_environment_reach_no_other_command(
    openshell: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "test-secret-exported-earlier")
    assert register(owners_file(tmp_path), openshell.cli()) == 0
    only_the_registration_had_them(openshell.called(), "create")


def test_a_failed_registration_shows_nothing_and_dumps_no_core(
    openshell: Any, tmp_path: Path
) -> None:
    """As ``up`` runs it: a process of its own, hardened, whose child fails."""
    openshell.has([], ["cloudflare-access"])
    done = subprocess.run(
        [sys.executable, "-I", "-m", "sandbox_science.launcher.access_provider",
         str(owners_file(tmp_path)), "--profile", str(PROFILE),
         "--openshell", str(openshell.cli("provider-fails"))],
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == 1
    assert done.stderr == (
        "error: openshell provider create failed, with status 1; what it said is not shown, "
        "since it could hold the values\n"
    )
    assert not leaked(done.stdout + done.stderr)
    calls = openshell.called()
    only_the_registration_had_them(calls, "create")
    assert {tuple(call["core"]) for call in calls} == {(0, 0)}


@pytest.mark.parametrize("variable", ["FAKE_OPENSHELL_CALLS", "FAKE_JOURNAL", "LD_PRELOAD"])
def test_it_will_not_run_where_calls_could_be_recorded_or_code_loaded(
    openshell: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    monkeypatch.setenv(variable, str(tmp_path / "recorded"))
    assert register(owners_file(tmp_path), openshell.cli()) == 1
    assert capsys.readouterr().err == f"error: refusing to run with {variable} set\n"
    assert openshell.called() == []


def test_an_openshell_cli_others_control_is_refused(
    openshell: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = owners_file(tmp_path)
    cli = openshell.cli()
    loose = openshell.cli("-loose")
    loose.chmod(0o775)
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    inside = openshell_stand_in(checkout / "bin" / "openshell")
    linked = tmp_path / "bin" / "linked"
    linked.symlink_to(inside)
    for given in ("openshell", "bin/openshell", loose, inside, linked):
        assert register(path, given) == 1
    in_repository = REPOSITORY / "packages/launcher/tests/fake_openshell.py"
    assert register(path, in_repository) == 1
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    assert register(path, cli) == 1
    assert capsys.readouterr().err.splitlines() == [
        "error: the OpenShell CLI openshell is not an absolute path",
        "error: the OpenShell CLI bin/openshell is not an absolute path",
        f"error: the OpenShell CLI {loose} may be changed by others",
        f"error: the OpenShell CLI {inside} is in the git checkout {checkout}",
        f"error: the OpenShell CLI {inside} is in the git checkout {checkout}",
        f"error: the OpenShell CLI {in_repository} is in the git checkout {REPOSITORY}",
        f"error: the OpenShell CLI {cli} is neither this user's nor root's",
    ]
    assert openshell.called() == []


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o644, 0o660])
def test_a_file_others_may_use_is_refused(
    openshell: Any, tmp_path: Path, mode: int, capsys: pytest.CaptureFixture[str]
) -> None:
    path = owners_file(tmp_path, mode=mode)
    assert register(path, openshell.cli()) == 1
    assert f"{path} has mode {mode:o}: its group or others may use it" in capsys.readouterr().err
    assert openshell.called() == []


def test_a_file_that_is_linked_or_in_an_open_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = owners_file(tmp_path)
    refusals: list[str] = []

    def refused(given: Path) -> None:
        with pytest.raises(RegistrationError) as error:
            read(given)
        refusals.append(str(error.value))

    link = tmp_path / "secrets" / "link.env"
    link.symlink_to(path)
    refused(link)
    folder_link = tmp_path / "linked-secrets"
    folder_link.symlink_to(path.parent)
    refused(folder_link / path.name)
    second = tmp_path / "secrets" / "second.env"
    second.hardlink_to(path)
    refused(path)
    second.unlink()
    path.parent.chmod(0o770)
    refused(path)
    path.parent.chmod(0o700)
    refused(tmp_path / "secrets")
    refused(tmp_path / "secrets" / "absent.env")
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    refused(path)
    assert refusals == [
        f"{link} is a symbolic link: name what it links to",
        f"{folder_link} is a symbolic link: name what it links to",
        f"{path} has 2 links: it must have one",
        f"{path.parent} has mode 770: its group or others may use it",
        f"{tmp_path / 'secrets'} is not a regular file",
        f"cannot open {tmp_path / 'secrets' / 'absent.env'}: No such file or directory",
        f"{path.parent} is not this user's: it must be",
    ]


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        ((PLAIN + "#" * access_provider.MAX_BYTES).encode(), "is over 65536 bytes"),
        (PLAIN.encode() + SECRET.encode() + b"\xff\xfe" + SECRET.encode(), "is not UTF-8 text"),
        (
            f"CF_ACCESS_CLIENT_ID={ID}\x01\nCF_ACCESS_CLIENT_SECRET={SECRET}\n".encode(),
            "line 1, CF_ACCESS_CLIENT_ID: its value has a control character",
        ),
    ],
    ids=["size", "utf-8", "control character"],
)
def test_a_file_it_refuses_is_never_shown(
    openshell: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    refusal: str,
) -> None:
    assert register(owners_file(tmp_path, content), openshell.cli()) == 1
    said = capsys.readouterr()
    assert refusal in said.err
    assert not leaked(said.out + said.err)
    assert openshell.called() == []


def test_an_unexpected_error_is_named_by_its_type_alone(
    openshell: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(text: str) -> dict[str, str]:
        raise ValueError(f"cannot take {text!r}")

    monkeypatch.setattr(access_provider, "parse", broken)
    assert register(owners_file(tmp_path), openshell.cli()) == 1
    said = capsys.readouterr()
    assert said.err == "error: ValueError while registering the provider\n"
    assert not leaked(said.out + said.err)


def test_a_missing_value_is_named_and_nothing_runs(
    openshell: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = f"CF-Access-Client-Secret: = {SECRET}\n"
    assert register(owners_file(tmp_path, text), openshell.cli()) == 1
    assert capsys.readouterr().err == "error: no CF_ACCESS_CLIENT_ID\n"
    assert openshell.called() == []


def test_a_refresh_needs_the_owners_file(make_config: Any) -> None:
    proxy = {"image": "sandbox-science/mcp-proxy:abc1234", "providers": ["p"], "servers": ["s"]}
    config = load_config(make_config({"mcp_proxy": proxy}))
    layout = Layout(config.state_dir)
    with pytest.raises(LauncherError, match="--refresh-provider needs"):
        asyncio.run(mcp.register_provider(config, layout, cast(Any, None), refresh=True))


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_DUMPABLE is Linux's")
def test_hardening_leaves_no_core_and_no_dumpable_process() -> None:
    probe = (
        "import ctypes, resource\n"
        "from sandbox_science.launcher.access_provider import harden\n"
        "harden()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE), ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout == "(0, 0) 0\n"
