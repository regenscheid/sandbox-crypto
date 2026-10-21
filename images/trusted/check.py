# SPDX-License-Identifier: MIT
"""Local container qualification of installed CLIs and immutable runtime assets."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import json
import subprocess
from pathlib import Path


def command(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed image-owned CLIs, never agent input
        args, capture_output=True, text=True, check=True, timeout=30
    ).stdout


async def application() -> None:
    from sandbox_science.orchestrator.figurecheck import Chart, FigureChecker
    from sandbox_science.orchestrator.mathcheck import Formula, MathChecker

    static = Path("/usr/share/sandbox-science/workspace")
    math = Path("/usr/share/sandbox-science/math")
    for file in (
        static / "index.html",
        static / "viewer/index.html",
        static / "figures/index.html",
    ):
        if not file.is_file():
            raise ValueError(f"missing immutable web asset: {file.name}")
    builtin = importlib.resources.files("sandbox_science.orchestrator.skills") / "builtin_pack"
    if not (builtin / "scientific-writing/SKILL.md").is_file():
        raise ValueError("installed application lacks built-in skills")
    importlib.metadata.distribution("boto3")
    node = Path("/usr/local/bin/node")
    checker = MathChecker(node, math / "check-math.mjs", math / "mathcheck.mjs")
    formulas = await checker.check_many(
        [Formula(id="good", tex="x^2 + 1"), Formula(id="bad", tex=r"\notARealCommand")]
    )
    if [item.status for item in formulas] != ["accepted", "rejected"]:
        raise ValueError("installed math bundle did not check both cases")
    figures = FigureChecker(
        node, math / "figures/check-figures.mjs", math / "figures/figurecheck.mjs"
    )
    spec = {
        "data": {"url": "data.csv"},
        "mark": "bar",
        "encoding": {
            "x": {"field": "x", "type": "ordinal"},
            "y": {"field": "y", "type": "quantitative"},
        },
    }
    charts = await figures.check_many(
        [
            Chart(id="good", source=json.dumps(spec), files={"data.csv": "x,y\na,1\n"}),
            Chart(id="bad", source="{"),
        ]
    )
    if [item.status for item in charts] != ["accepted", "rejected"]:
        raise ValueError("installed figure bundle did not check both cases")
    for cli in (
        "sandbox-science-control",
        "sandbox-science-workspace",
        "sandbox-science-artifacts",
    ):
        command(cli, "--help")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("trusted-app", "trusted-worker"))
    profile = parser.parse_args().profile
    record = json.loads(Path("/usr/share/sandbox-science/image.json").read_text())
    if record["profile"] != profile or record["revision"] == "unknown":
        raise ValueError("trusted service image has the wrong build identity")
    for helper in ("copy-credentials.py", "initialize-state.py"):
        if not (Path("/usr/local/libexec/sandbox-science") / helper).is_file():
            raise ValueError(f"trusted service image lacks its init helper: {helper}")
    installed = {
        dist.metadata["Name"].lower().replace("_", "-")
        for dist in importlib.metadata.distributions()
    }
    if installed & {"ipykernel", "jupyter-client", "sagelib", "sage"}:
        raise ValueError("trusted service image unexpectedly contains research kernels")
    if profile == "trusted-app":
        asyncio.run(application())
    else:
        if installed & {"sandbox-science-orchestrator", "sandbox-science-workspace"}:
            raise ValueError("worker image unexpectedly includes the agent application")
        for cli in (
            "sandbox-science-worker",
            "sandbox-science-policy-middleware",
            "sandbox-science-package-cache",
        ):
            command(cli, "--help")
        if command("openshell", "--version").strip() != "openshell 0.1.3":
            raise ValueError("worker CLI does not match the pinned OpenShell release")
        command("ssh", "-V")
        command("git", "--version")
    print(json.dumps({"profile": profile, "revision": record["revision"], "qualified": True}))


if __name__ == "__main__":
    main()
