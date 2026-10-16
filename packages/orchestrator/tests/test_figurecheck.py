# SPDX-License-Identifier: MIT
"""The figure checker (docs/specs/figures.md, FG-19 to FG-21, FG-31): its process
protocol with a stand-in Node, and the real bundle that ``npm run build:math`` writes,
which the web CI job builds and requires (``SS_REQUIRE_FIGURE_CHECK``)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from sandbox_science.orchestrator.figurecheck import (
    ENGINE,
    NO_PERMISSIONS,
    Chart,
    FigureChecker,
    permitted,
)

ROOT = Path(__file__).resolve().parents[3]
BUILD = ROOT / "web/workspace/dist-math/figures"
TIMING = "key_bits,median_ms\n1024,3\n2048,9\n3072,20\n4096,41\n"
SKILL_DATA = {"data/timing.csv": TIMING}
"""The data the figures skill's charts name, for checking them (FG-31)."""


def fake(
    tmp_path: Path, body: str, *, timeout: float = 2.0, version: str = "v24.20.0"
) -> FigureChecker:
    """A checker whose Node is a Python script: ``batch`` is the request it read, and
    ``ARGS`` the file its arguments are written to."""
    node = tmp_path / "fake-node"
    node.write_text(
        f"#!{sys.executable}\nimport json,sys,time\n"
        f"if sys.argv[1:] == ['--version']: print({version!r}); sys.exit()\n"
        f"open({str(tmp_path / 'args')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "batch=json.loads(sys.stdin.buffer.read())\n"
        f"ENGINE={ENGINE!r}\n" + body
    )
    node.chmod(0o700)
    (tmp_path / "launcher").write_text("launcher")
    (tmp_path / "bundle").write_text("bundle")
    return FigureChecker(node, tmp_path / "launcher", tmp_path / "bundle", timeout=timeout)


ANSWER = (
    "def answer(item):\n"
    "    bad = 'bad' in item['source']\n"
    "    return {'protocol': 1, 'id': item['id'], 'engine': ENGINE, 'runtime': '24.20.0',\n"
    "            'status': 'rejected' if bad else 'accepted',\n"
    "            'errors': [{'kind': 'schema', 'message': 'bad'}] if bad else [], 'notes': []}\n"
)


async def test_a_batch_runs_in_one_process_and_answers_in_order(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    checker = fake(
        tmp_path,
        ANSWER + f"open({str(runs)!r}, 'a').write('run\\n')\n"
        "print(json.dumps([answer(item) for item in batch]))\n",
    )
    charts = [Chart(id=f"c{n}", source="bad" if n == 3 else "{}") for n in range(10)]
    results = await checker.check_many(charts)
    assert [r.status for r in results] == ["accepted"] * 3 + ["rejected"] + ["accepted"] * 6
    assert results[3].errors[0].message == "bad"
    assert runs.read_text().count("run") == 2  # at most eight charts a process


async def test_a_chart_that_hangs_leaves_only_itself_unavailable(tmp_path: Path) -> None:
    checker = fake(
        tmp_path,
        ANSWER + "if any(item['source'] == 'hang' for item in batch): time.sleep(30)\n"
        "print(json.dumps([answer(item) for item in batch]))\n",
        timeout=1.0,
    )
    charts = [Chart(id=f"c{n}", source="hang" if n == 2 else "{}") for n in range(4)]
    results = await checker.check_many(charts)
    assert [r.status for r in results] == ["accepted", "accepted", "unavailable", "accepted"]
    assert results[2].reason == "figure checker deadline exceeded"


@pytest.mark.parametrize(
    "answer",
    [
        "[dict(answer(item), engine='vega-lite 5.0.0, vega 5.0.0') for item in batch]",
        "[dict(answer(item), status='rejected', errors=[]) for item in batch]",
        "[dict(answer(item), id='other') for item in batch]",
    ],
)
async def test_an_answer_of_another_version_shape_or_batch_is_unavailable(
    tmp_path: Path, answer: str
) -> None:
    checker = fake(tmp_path, ANSWER + f"print(json.dumps({answer}))\n")
    results = await checker.check_many([Chart(id="a", source="{}"), Chart(id="b", source="{}")])
    assert [r.status for r in results] == ["unavailable", "unavailable"]


async def test_the_process_runs_under_nodes_permission_model(tmp_path: Path) -> None:
    """Security review of #513, M3: it reads only its own temporary directory, evaluates
    no strings as code; and stderr past its old bound fails nothing (M2)."""
    checker = fake(
        tmp_path,
        ANSWER + "sys.stderr.write('WARN ' + 'x' * 20000)\n"
        "print(json.dumps([answer(item) for item in batch]))\n",
    )
    (result,) = await checker.check_many([Chart(id="a", source="bad")])
    assert result.status == "rejected"
    args = json.loads((tmp_path / "args").read_text())
    launcher, bundle = Path(args[-2]), Path(args[-1])
    assert launcher.parent == bundle.parent != tmp_path  # the batch's pinned copies
    assert args[2:5] == [
        "--permission",
        f"--allow-fs-read={bundle.parent}",
        "--disallow-code-generation-from-strings",
    ]


@pytest.mark.parametrize("version", ["v22.12.0", "v23.4.0", "v20.18.1", "nonsense"])
async def test_a_node_without_the_permission_model_checks_nothing(
    tmp_path: Path, version: str
) -> None:
    checker = fake(tmp_path, ANSWER + "print(json.dumps([answer(item) for item in batch]))\n",
                   version=version)  # fmt: skip
    (result,) = await checker.check_many([Chart(id="a", source="{}")])
    assert (result.status, result.reason) == ("unavailable", NO_PERMISSIONS)
    assert not (tmp_path / "args").exists()  # no chart reached it


def test_the_permission_model_is_read_from_the_version() -> None:
    assert [permitted(v) for v in ("v22.13.0", "v23.5.0", "v24.20.0\n", "v23.1.0")] == [
        True,
        True,
        True,
        False,
    ]


async def test_no_node_is_unavailable(tmp_path: Path) -> None:
    checker = fake(tmp_path, "")
    checker.node = tmp_path / "missing-node"
    (result,) = await checker.check_many([Chart(id="a", source="{}")])
    assert (result.status, result.reason) == ("unavailable", "figure checker unavailable")


def real() -> FigureChecker:
    node = shutil.which("node")
    if not (BUILD / "figurecheck.mjs").is_file() or node is None:
        if os.environ.get("SS_REQUIRE_FIGURE_CHECK"):
            pytest.fail("the figure checker's build is required here: npm run build:math")
        pytest.skip("build:math and Node needed; exercised in the web CI job")
    launcher = ROOT / "web/workspace/server/check-figures.mjs"
    return FigureChecker(Path(node), launcher, BUILD / "figurecheck.mjs", timeout=30)


def skill_charts() -> list[str]:
    """The charts in the figures skill's text: its `vega-lite` fences, and the `json`
    fences that hold a Vega-Lite specification."""
    text = (ROOT / "skills/builtin/figures/SKILL.md").read_text()
    found: list[str] = []
    for language, body in re.findall(r"^```([\w-]*)\n(.*?)^```", text, re.M | re.S):
        if language == "vega-lite" or (language == "json" and "/vega-lite/" in body):
            found.append(body)
    return found


async def test_the_skills_charts_pass_the_check() -> None:
    """FG-31: each chart the skill teaches passes the save-time check, at the pinned
    versions."""
    charts = skill_charts()
    assert charts
    named = [Chart(id=f"s{n}", source=body, files=SKILL_DATA) for n, body in enumerate(charts)]
    for chart, result in zip(named, await real().check_many(named), strict=True):
        assert (result.status, result.errors, result.notes) == ("accepted", [], []), chart.source


def spec(**changes: object) -> str:
    base: dict[str, object] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"url": "data/timing.csv"},
        "mark": "bar",
        "encoding": {
            "x": {"field": "key_bits", "type": "ordinal"},
            "y": {"field": "median_ms", "type": "quantitative"},
        },
    }
    return json.dumps({**base, **changes}, indent=2)


async def test_the_real_check_rejects_by_the_frames_rules_and_the_libraries() -> None:
    """FG-F6, FG-F7 and FG-19's steps, each with the reason it gives."""
    missing = spec(encoding={"x": {"field": "key_bits", "type": "ordinal"},
                             "y": {"field": "msec", "type": "quantitative"}})  # fmt: skip
    charts = {
        "inline": spec(data={"values": [{"key_bits": 64, "median_ms": 3}]}),
        "field": missing,
        "schema": spec(encodng={}),
        "json": '{\n  "mark": "bar",\n  "x": }',
        "expression": spec(transform=[{"filter": "datum.x >"}]),
        "address": spec(data={"url": "https://example.org/d.csv"}),
        "version": spec(**{"$schema": "https://vega.github.io/schema/vega-lite/v5.json"}),
    }
    results = await real().check_many(
        [Chart(id=name, source=text, files=SKILL_DATA) for name, text in charts.items()]
    )
    found = {r.id: [(e.kind, e.message, e.line) for e in r.errors] for r in results}
    assert all(r.status == "rejected" for r in results)
    assert found["inline"][0][0] == "inline_data"
    assert found["field"] == [
        ("field", 'the field "msec" is in none of the chart\'s data', 13),
        ("empty", "the chart draws no marks", None),
    ]
    assert found["schema"] == [("schema", '/encodng: "encodng" is not a property here', 17)]
    assert found["json"][0][0] == "json"
    assert found["expression"][0][:2] == (
        "invalid",
        "the chart is not a Vega-Lite specification that compiles: Unexpected end of input",
    )
    assert found["address"][0][0] == "address"
    assert found["version"][0][0] == "version"


async def test_the_real_check_answers_charts_whose_libraries_would_warn() -> None:
    """Security review of #513, M2: a warning echoing a long title, or a refusal's stack,
    no longer fills stderr and makes a rejected chart unavailable."""
    long = "t" * 5000
    noisy = spec(title=long, encoding={
        "x": {"field": "key_bits", "type": "ordinal"},
        "y": {"field": "median_ms", "type": "quantitative"},
        "shape": {"field": "key_bits", "type": "nominal"},
    })  # fmt: skip
    many = spec(transform=[{"calculate": "sequence(0, 20000)", "as": "a"}], title=long)
    results = await real().check_many(
        [Chart(id="warns", source=noisy, files=SKILL_DATA),
         Chart(id="refused", source=many, files=SKILL_DATA)]
    )  # fmt: skip
    assert [(r.status, [e.kind for e in r.errors]) for r in results] == [
        ("accepted", []),
        ("rejected", ["size"]),
    ]


async def test_the_real_check_notes_values_no_pinned_file_holds() -> None:
    """Review of #500, L2: literals in an expression, a parameter's value and a
    `flatten` of a computed array are noted, not refused."""
    text = spec(
        transform=[{"calculate": "[1, 2, 3, 4]", "as": "made"}, {"flatten": ["made"]}],
        params=[{"name": "cut", "value": 5}],
    )
    (result,) = await real().check_many([Chart(id="a", source=text, files=SKILL_DATA)])
    assert result.status == "accepted"
    assert sorted(note.kind for note in result.notes) == ["flatten", "literals", "params"]
