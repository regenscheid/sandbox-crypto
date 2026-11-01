# SPDX-License-Identifier: MIT
"""The repository's test configuration: strict marks, the tier check's reports, and
the shards a tier is divided into."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sandbox_science.testing.shards import assign, module_of, parse_shard

ROOT = Path(__file__).resolve().parents[3]


def _tiers() -> ModuleType:
    """``scripts/tiers.py``, which is not a package's module."""
    spec = importlib.util.spec_from_file_location("tiers", ROOT / "scripts" / "tiers.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tiers = _tiers()


def test_a_misspelt_mark_fails_collection(tmp_path: Path) -> None:
    # Without --strict-markers, a module marked `slwo` would land in the fast tier.
    module = tmp_path / "test_misspelt.py"
    module.write_text("import pytest\npytestmark = pytest.mark.slwo\n\ndef test_x():\n    pass\n")
    collect = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    config = ["-c", str(ROOT / "pyproject.toml"), "--rootdir", str(tmp_path)]
    done = subprocess.run(
        [*collect, *config, str(module)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "'slwo' not found in `markers`" in done.stdout


@pytest.mark.slow
def test_the_tier_check_shows_why_a_collection_failed(tmp_path: Path) -> None:
    # A module that raises ImportError on import breaks the PDF helper's test module
    # (importorskip skips only a missing module), without touching the repository.
    shadow = tmp_path / "pdf_oxide"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('raise ImportError("tier-check probe: broken import")\n')
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tiers.py"), "check"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode != 0
    report = done.stdout + done.stderr
    assert "collection failed" in report
    assert "tier-check probe: broken import" in report
    assert "test_pdfhelper.py" in report


# Shards (`tiers.py run --shard I/N`, `sandbox_science.testing.shards`).

MODULES = {
    "packages/a/tests/test_big.py": 40,
    "packages/a/tests/test_mid.py": 25,
    "packages/b/tests/test_mid.py": 25,
    "packages/b/tests/test_small.py": 3,
    "images/tests/test_one.py": 1,
    **{f"packages/c/tests/test_{n:02}.py": n for n in range(1, 30)},
}
NODEIDS = [f"{module}::test_{n}[{n}]" for module, size in MODULES.items() for n in range(size)]


def shard_of(nodeids: list[str], index: int, total: int) -> list[str]:
    shards = assign(nodeids, total)
    return [nodeid for nodeid in nodeids if shards[module_of(nodeid)] == index]


@pytest.mark.parametrize("total", [1, 2, 3, 5, 7, 80])
def test_every_module_is_in_exactly_one_shard_and_the_shards_are_the_selection(
    total: int,
) -> None:
    shards = assign(NODEIDS, total)
    assert set(shards) == set(MODULES)
    assert set(shards.values()) <= set(range(1, total + 1))
    parts = [shard_of(NODEIDS, index, total) for index in range(1, total + 1)]
    assert sorted(nodeid for part in parts for nodeid in part) == sorted(NODEIDS)
    homes = [{module_of(nodeid) for nodeid in part} for part in parts]
    assert sum(len(modules) for modules in homes) == len(MODULES)  # no module in two


def test_one_shard_is_the_whole_selection() -> None:
    assert shard_of(NODEIDS, 1, 1) == NODEIDS


def test_shards_do_not_depend_on_the_order_tests_were_collected_in() -> None:
    assert assign(NODEIDS, 3) == assign(list(reversed(NODEIDS)), 3)


def test_shards_are_balanced_by_test_count() -> None:
    for total in (2, 3, 4):
        sizes = [len(shard_of(NODEIDS, index, total)) for index in range(1, total + 1)]
        # Within the largest module's size of each other.
        assert max(sizes) - min(sizes) <= max(MODULES.values())
        assert max(sizes) <= len(NODEIDS) / total + max(MODULES.values())


@pytest.mark.parametrize("text", ["0/3", "4/3", "1/0", "3", "1/3/5", "a/b", "-1/3", ""])
def test_a_shard_that_does_not_exist_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="shard"):
        parse_shard(text)
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tiers.py"), "run", "fast", f"--shard={text}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 2
    assert "--shard" in done.stderr


def test_tiers_run_hands_the_shard_to_pytest_after_the_tier() -> None:
    command = tiers.pytest_command("slow", "2/3", ["-n", "4", "--count", "2"])
    assert command[1:] == [
        "-m", "pytest", "-m", "slow", "-p", tiers.SHARDS, "--shard=2/3", "-n", "4", "--count", "2"
    ]  # fmt: skip
    assert tiers.pytest_command("fast", None, ["-x"])[1:] == [
        "-m",
        "pytest",
        "-m",
        "not slow",
        "-x",
    ]


def write_suite(root: Path) -> dict[str, int]:
    """Twelve test modules of 1 to 12 tests, every fourth marked slow; the sizes of the
    others, by file name."""
    sizes: dict[str, int] = {}
    for n in range(1, 13):
        slow = n % 4 == 0
        mark = "pytestmark = pytest.mark.slow\n" if slow else ""
        module = root / f"test_m{n:02}.py"
        module.write_text(
            f"import pytest\n{mark}\n@pytest.mark.parametrize('k', range({n}))\n"
            "def test_it(k):\n    pass\n"
        )
        if not slow:
            sizes[module.name] = n
    return sizes


def run_pytest(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", tiers.SHARDS]
    config = ["-c", str(ROOT / "pyproject.toml"), "--rootdir", str(root)]
    return subprocess.run(
        [*command, *config, *arguments, str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def collected(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_a_tiers_shards_are_that_tier_divided_by_module(tmp_path: Path) -> None:
    sizes = write_suite(tmp_path)
    tier = collected(run_pytest(tmp_path, "-m", "not slow", "--collect-only", "-q"))
    assert len(tier) == sum(sizes.values())
    whole = run_pytest(tmp_path, "-m", "not slow", "--shard", "1/1", "--collect-only", "-q")
    assert collected(whole) == tier
    parts: list[list[str]] = []
    for index in (1, 2, 3):
        done = run_pytest(tmp_path, "-m", "not slow", f"--shard={index}/3", "--collect-only", "-q")
        assert done.returncode == 0, done.stdout + done.stderr
        parts.append(collected(done))
    # The marker selected first: the shards share out only the tier's 9 modules.
    assert sorted(nodeid for part in parts for nodeid in part) == sorted(tier)
    homes = [{module_of(nodeid) for nodeid in part} for part in parts]
    assert sum(len(modules) for modules in homes) == len(sizes)
    assert [len(part) for part in parts] == [len(shard_of(tier, index, 3)) for index in (1, 2, 3)]
    assert max(len(part) for part in parts) - min(len(part) for part in parts) <= max(
        sizes.values()
    )


def test_xdist_workers_agree_on_a_shard(tmp_path: Path) -> None:
    # xdist stops a run whose workers collected different tests.
    write_suite(tmp_path)
    expected = len(collected(run_pytest(tmp_path, "--shard=2/3", "--collect-only", "-q")))
    done = run_pytest(tmp_path, "--shard=2/3", "-n", "2", "--dist=load", "-q")
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{expected} passed" in done.stdout


def test_tiers_run_runs_one_shard_of_a_tier() -> None:
    module = "packages/testing/tests/test_suite_rules.py"
    here = [
        subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "tiers.py"), "run", "fast",
                f"--shard={index}/2", "--collect-only", "-q", "-p", "no:cacheprovider", module,
            ],
            cwd=ROOT, capture_output=True, text=True, check=False, timeout=120,
        )
        for index in (1, 2)
    ]  # fmt: skip
    # One module: all of its fast tests are in one shard, none in the other.
    assert sorted(done.returncode for done in here) == [0, 5]
    found = collected(next(done for done in here if done.returncode == 0))
    assert found
    assert all(nodeid.startswith(module + "::") for nodeid in found)
    assert not any("test_the_tier_check_shows_why" in nodeid for nodeid in found)
