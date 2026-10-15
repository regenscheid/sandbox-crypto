# SPDX-License-Identifier: MIT
"""``sandbox-science-harness skills``: importing skill packs from git or an archive, and
pinning them in projects (docs/specs/skills.md, SK-1 to SK-6, SK-32; SK-F4, SK-F9,
SK-F13).

The pack is ``ss-test-skills``, written here under the MIT License, in a temporary git
repository: ``main`` at C1, tagged ``v1``, then C2, which changes ``alpha``'s body.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.skills import PackFile
from sandbox_science.orchestrator.skills.packs import snapshot_sha256

POLICY = "sha256:" + "a" * 64
ALPHA = "---\nname: alpha\ndescription: Says the marker.\n---\nSS-MARKER-ALPHA-1\n"
BETA = (
    "---\nname: beta\ndescription: Counts the lines of the notes.\nlicense: MIT\n---\n"
    "Read [the notes](reference/notes.md) and [alpha](../alpha/SKILL.md); "
    "run scripts/count.sh.\n"
)
COUNT = "#!/bin/sh\nwc -l reference/notes.md\n"
PACK = {
    "LICENSE": "MIT License\n",
    "README.md": "# ss-test-skills\n",
    "skills/alpha/SKILL.md": ALPHA,
    "skills/beta/SKILL.md": BETA,
    "skills/beta/reference/notes.md": "one\ntwo\n",
    "skills/beta/scripts/count.sh": COUNT,
    "skills/broken/SKILL.md": "---\nname: broken\n---\nNo description.\n",
    "skills/gamma/SKILL.md": "---\nname: delta\ndescription: Misnamed.\n---\nBody.\n",
    "skills/aliased/SKILL.md": "---\nname: &n aliased\ndescription: *n\n---\nBody.\n",
}


@pytest.fixture(autouse=True)
def plain_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git without the user's or the system's configuration, as on a fresh machine."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repository: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


@pytest.fixture
def pack(tmp_path: Path) -> tuple[Path, str, str]:
    """The pack's repository, C1 and C2."""
    root = _repository(tmp_path / "ss-test-skills", PACK)
    (root / "skills/beta/scripts/count.sh").chmod(0o755)
    (root / "skills/beta/reference/link").symlink_to("/etc/hostname")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "C1")
    _git(root, "tag", "-a", "v1", "-m", "v1")
    first = _git(root, "rev-parse", "HEAD")
    (root / "skills/alpha/SKILL.md").write_text(ALPHA + "Changed.\n")
    _git(root, "commit", "-qam", "C2")
    return root, first, _git(root, "rev-parse", "HEAD")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    projects = ProjectStore(root)
    projects.create("p1", "minimal", POLICY)
    projects.close()
    return root


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, object, str]:
    status = cli.main(list(argv))
    captured = capsys.readouterr()
    return status, json.loads(captured.out) if status == 0 else None, captured.err


def _expected(alpha: str) -> str:
    """The snapshot of the pack's valid and invalid skills with ``alpha``'s text: no
    README or LICENSE outside skills/, no symbolic link, and the script executable."""
    kept = {path.removeprefix("skills/"): text for path, text in PACK.items()}
    kept["alpha/SKILL.md"] = alpha
    files = tuple(
        PackFile(path, text.encode(), path.endswith(".sh"))
        for path, text in kept.items()
        if "/" in path
    )
    return snapshot_sha256(files)


def test_an_import_pins_the_default_branch_head_and_reports_what_it_left_out(
    state: Path, pack: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, second = pack
    status, found, _ = _run(
        capsys, "skills", "import", "--state-dir", str(state), "--name", "ss", "--git", str(root)
    )
    assert status == 0
    assert isinstance(found, dict)
    assert (found["kind"], found["ref"], found["commit"], found["root"]) == (
        "git",
        None,
        second,
        "skills",
    )
    assert found["origin"] == str(root.resolve())
    assert found["snapshot"] == _expected(ALPHA + "Changed.\n")
    report = found["report"]
    assert report["skills"] == ["alpha", "beta"]
    assert report["default_branch"] == "main"
    reasons = {item["path"]: item["reason"] for item in report["excluded"]}
    assert set(reasons) == {"aliased/SKILL.md", "broken/SKILL.md", "gamma/SKILL.md"}
    assert "description is missing" in reasons["broken/SKILL.md"]
    assert "not its directory's name" in reasons["gamma/SKILL.md"]
    assert "aliases" in reasons["aliased/SKILL.md"]
    assert report["skipped"] == [
        {"path": "beta/reference/link", "reason": "a symbolic link, not imported"}
    ]
    assert "changes" not in report
    assert found["unchanged"] is False


def test_a_tag_or_a_full_hash_pins_its_commit_and_an_update_says_what_changed(
    state: Path, pack: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, first, _ = pack
    where = ("--state-dir", str(state), "--name", "ss", "--git", str(root))
    _, latest, _ = _run(capsys, "skills", "import", *where)
    status, tagged, _ = _run(capsys, "skills", "import", *where, "--ref", "v1")
    assert status == 0
    assert isinstance(tagged, dict)
    assert isinstance(latest, dict)
    assert (tagged["ref"], tagged["commit"]) == ("v1", first)
    assert tagged["snapshot"] == _expected(ALPHA)
    assert tagged["report"]["changes"] == {
        "since": latest["import_id"],
        "added": [],
        "removed": [],
        "changed": ["alpha"],
    }
    # The same place at the same revision adds nothing.
    status, again, _ = _run(capsys, "skills", "import", *where, "--ref", "v1")
    assert isinstance(again, dict)
    assert (again["import_id"], again["unchanged"]) == (tagged["import_id"], True)
    status, hashed, _ = _run(capsys, "skills", "import", *where, "--ref", first)
    assert isinstance(hashed, dict)
    assert (hashed["commit"], hashed["snapshot"]) == (first, tagged["snapshot"])
    assert hashed["report"]["changes"]["changed"] == []
    status, _, error = _run(capsys, "skills", "import", *where, "--ref", first[:12])
    assert status == 1
    assert "full hash" in error
    status, listed, _ = _run(capsys, "skills", "list", "--state-dir", str(state), "--source", "ss")
    assert isinstance(listed, list)
    assert [item["commit"] for item in listed] == [first, first, latest["commit"]]
    assert listed[0]["report"]["skills"] == ["alpha", "beta"]


def test_a_pin_is_refused_when_another_pinned_source_has_a_skill_of_its_name(
    state: Path,
    pack: tuple[Path, str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, first, second = pack
    where = ("--state-dir", str(state))
    _run(capsys, "skills", "import", *where, "--name", "ss", "--git", str(root))
    _run(capsys, "skills", "import", *where, "--name", "ss", "--git", str(root), "--ref", "v1")
    other = _repository(tmp_path / "other", {"alpha/SKILL.md": ALPHA, "LICENSE": "MIT\n"})
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "one")
    status, imported, _ = _run(
        capsys, "skills", "import", *where, "--name", "other", "--git", str(other)
    )
    assert isinstance(imported, dict)
    assert (imported["root"], imported["report"]["skills"]) == ("", ["alpha"])
    status, pinned, _ = _run(capsys, "skills", "pin", *where, "--project", "p1", "--source", "ss")
    assert status == 0
    assert isinstance(pinned, dict)
    assert (pinned["revision"], pinned["pinned"]["commit"], pinned["skills"]) == (
        1,
        first,
        ["alpha", "beta"],
    )
    status, _, error = _run(capsys, "skills", "pin", *where, "--project", "p1", "--source", "other")
    assert status == 1
    assert "conflict: alpha already defined by ss" in error
    status, moved, _ = _run(
        capsys,
        "skills", "pin", *where, "--project", "p1", "--source", "ss", "--revision", second[:12],
    )  # fmt: skip
    assert isinstance(moved, dict)
    assert (moved["revision"], moved["pinned"]["commit"]) == (2, second)
    status, pins, _ = _run(capsys, "skills", "list", *where, "--project", "p1")
    assert isinstance(pins, list)
    assert [(pin["source"], pin["revision"]) for pin in pins] == [("ss", 2)]
    status, _, error = _run(capsys, "skills", "pin", *where, "--project", "p2", "--source", "ss")
    assert (status, "not_found" in error) == (1, True)


def _tar(entries: list[tuple[str, bytes | None, str | None]]) -> bytes:
    """A gzipped tar: each entry a file, or a symbolic link to its target."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, content, target in entries:
            info = tarfile.TarInfo(name)
            if target is not None:
                info.type, info.linkname = tarfile.SYMTYPE, target
                tar.addfile(info)
            else:
                assert content is not None
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


def test_an_archive_loses_its_top_directory_and_keeps_only_plain_files(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = b"x" * (1024 * 1024 + 1)
    archive = tmp_path / "pack.tar.gz"
    archive.write_bytes(
        _tar(
            [
                ("pack-1/skills/alpha/SKILL.md", ALPHA.encode(), None),
                ("pack-1/skills/alpha/link", None, "/etc/passwd"),
                ("pack-1/skills/alpha/../../escape.md", b"out", None),
                ("pack-1/skills/alpha/big.bin", big, None),
                ("pack-1/README.md", b"top", None),
            ]
        )
    )
    status, found, _ = _run(
        capsys,
        "skills", "import", "--state-dir", str(state), "--name", "arc", "--archive", str(archive),
    )  # fmt: skip
    assert status == 0
    assert isinstance(found, dict)
    assert (found["kind"], found["commit"], found["root"]) == ("archive", None, "skills")
    assert len(found["archive_sha256"]) == 64
    assert found["snapshot"] == snapshot_sha256((PackFile("alpha/SKILL.md", ALPHA.encode()),))
    assert {item["path"]: item["reason"] for item in found["report"]["skipped"]} == {
        "alpha/../../escape.md": "not a plain relative path",
        "alpha/big.bin": "larger than 1 MiB",
        "alpha/link": "a symbolic link, not imported",
    }


def test_a_zip_keeps_executables_and_leaves_out_symbolic_links(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("alpha/SKILL.md", ALPHA)
        script = zipfile.ZipInfo("alpha/run.sh")
        script.external_attr = 0o100755 << 16
        zipped.writestr(script, COUNT)
        link = zipfile.ZipInfo("alpha/link")
        link.external_attr = 0o120777 << 16
        zipped.writestr(link, "/etc/passwd")
    status, found, _ = _run(
        capsys,
        "skills", "import", "--state-dir", str(state), "--name", "zip", "--archive", str(archive),
    )  # fmt: skip
    assert status == 0
    assert isinstance(found, dict)
    assert found["root"] == ""
    assert found["snapshot"] == snapshot_sha256(
        (
            PackFile("alpha/SKILL.md", ALPHA.encode()),
            PackFile("alpha/run.sh", COUNT.encode(), executable=True),
        )
    )
    assert found["report"]["skipped"] == [
        {"path": "alpha/link", "reason": "a symbolic link, not imported"}
    ]


def test_a_pack_without_skills_and_a_bad_source_name_are_refused(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = _repository(tmp_path / "empty", {"README.md": "nothing\n"})
    _git(empty, "add", "-A")
    _git(empty, "commit", "-qm", "one")
    where = ("--state-dir", str(state))
    status, found, _ = _run(capsys, "skills", "import", *where, "--name", "e", "--git", str(empty))
    assert isinstance(found, dict)
    assert found["report"]["skills"] == []
    status, _, error = _run(
        capsys, "skills", "import", *where, "--name", "Bad_Name", "--git", str(empty)
    )
    assert (status, "lowercase letters" in error) == (1, True)
    status, _, error = _run(
        capsys, "skills", "import", *where, "--name", "x", "--git", str(tmp_path / "missing")
    )
    assert (status, "git fetch" in error) == (1, True)
