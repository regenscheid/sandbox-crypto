# SPDX-License-Identifier: MIT
"""A file-set grant names files by their path at the package cache, and the middleware
matches that path exactly. So the path the worker builds from a dry run must be a route
the cache serves, and the policy must classify it as the same package (plan item D-06).
A mismatch would fail closed, the download denied; these checks keep it from happening."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.package_cache.adapters.conda import CondaAdapter
from sandbox_science.package_cache.adapters.pypi import PypiAdapter
from sandbox_science.policy import classify_package_request
from sandbox_science.policy.names import pep503
from sandbox_science.worker import resolution

CACHE = "http://host.openshell.internal:8090"
SHA = "c" * 64
CONDA = CondaAdapter(
    base="https://conda.anaconda.org",
    channels=["conda-forge"],
    subdirs=["linux-64", "linux-aarch64", "noarch"],
)
PYPI = PypiAdapter(
    index="https://pypi.org/simple", files="https://files.pythonhosted.org", public_url=CACHE
)


def conda_link(name: str, version: str, build: str, subdir: str, suffix: str) -> dict[str, Any]:
    file = f"{name}-{version}-{build}{suffix}"
    return {
        "name": name,
        "version": version,
        "build": build,
        "subdir": subdir,
        "fn": file,
        "url": f"{CACHE}/conda/conda-forge/{subdir}/{file}",
        "channel": f"{CACHE}/conda/conda-forge/{subdir}",
        "sha256": SHA,
    }


@pytest.mark.parametrize(
    "entry",
    [
        conda_link("pari", "2.15.4", "h0_0", "linux-64", ".conda"),
        conda_link("_openmp_mutex", "4.5", "2_gnu", "linux-64", ".tar.bz2"),
        conda_link("python_abi", "3.12", "5_cp312", "linux-aarch64", ".conda"),
        conda_link("typing-extensions", "4.12.2", "pyha770c72_0", "noarch", ".conda"),
        conda_link("ca-certificates", "2024.8.30", "hbcca054_0", "linux-64", ".conda"),
        conda_link("py-sirius-ms", "2.1+sirius6.0.3", "pyhd8ed1ab_0", "noarch", ".conda"),
        conda_link("dagster-airbyte", "1!0.26.10", "pyh29332c3_0", "noarch", ".conda"),
        # micromamba 2 names the channel and leaves out the URL.
        {**conda_link("pari", "2.15.4", "h0_0", "linux-64", ".conda"), "url": None},
    ],
)
def test_conda_grant_paths_are_cache_routes_of_the_same_package(entry: dict[str, Any]) -> None:
    entry = {key: value for key, value in entry.items() if value is not None}
    raw = json.dumps({"actions": {"LINK": [entry]}, "success": True})
    [resolved] = resolution.parse_conda_dry_run(raw, CACHE)
    assert resolved.path.startswith("/conda/")
    assert CONDA.route(resolved.path.removeprefix("/conda/"), {}) is not None
    operation = classify_package_request("GET", resolved.path)
    assert (operation.ecosystem, operation.name, operation.kind, operation.channel) == (
        "conda",
        resolved.file.name,
        "artifact",
        "conda-forge",
    )


@pytest.mark.parametrize(
    ("name", "file"),
    [
        ("fakeflint", "fakeflint-0.7.1-py3-none-any.whl"),
        ("typing_extensions", "typing_extensions-4.12.2-py3-none-any.whl"),
        ("zope.interface", "zope.interface-6.0.tar.gz"),
        ("Django", "django-5.1-py3-none-any.whl"),
        ("cysignals", "cysignals-1.11.4-cp312-cp312-manylinux_2_17_x86_64.whl"),
    ],
)
def test_pip_grant_paths_are_cache_routes_of_the_same_project(name: str, file: str) -> None:
    # files.pythonhosted.org's layout: two directories, then a 60-digit BLAKE2 digest.
    url = f"{CACHE}/pypi/files/{pep503(name)}/{SHA}/packages/ab/cd/{'e' * 60}/{file}"
    raw = json.dumps(
        {
            "version": "1",
            "install": [
                {
                    "download_info": {"url": url, "archive_info": {"hashes": {"sha256": SHA}}},
                    "is_direct": False,
                    "metadata": {"name": name, "version": "1", "license_expression": "MIT"},
                }
            ],
        }
    )
    [resolved] = resolution.parse_pip_report(raw, CACHE)
    assert resolved.path.startswith("/pypi/files/")
    assert PYPI.route(resolved.path.removeprefix("/pypi/"), {}) is not None
    operation = classify_package_request("GET", resolved.path)
    assert (operation.ecosystem, operation.name, operation.kind) == (
        "pypi",
        pep503(name),
        "artifact",
    )
