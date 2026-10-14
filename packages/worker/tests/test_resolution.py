# SPDX-License-Identifier: MIT
"""Resolving a provisioning request, and deciding each file (resolution.py): the parts
that need no sandbox. What a dry run prints is untrusted, so each odd shape has a case."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.contracts import (
    CondaPackage,
    Lockfile,
    PipDistribution,
    ProvisionRequest,
    ResolvedFile,
)
from sandbox_science.policy import Action, HardFloor, PoolCeiling, parse_policy
from sandbox_science.worker import provisioning, resolution
from sandbox_science.worker.catalog import Catalog, CatalogError, load_catalog
from sandbox_science.worker.resolution import ResolveError

CACHE = "http://host.openshell.internal:8090"
SHA = "c" * 64
PARI_URL = f"{CACHE}/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"


def link(**fields: Any) -> dict[str, Any]:
    entry = {
        "name": "pari",
        "version": "2.15.4",
        "build": "h0_0",
        "subdir": "linux-64",
        "fn": "pari-2.15.4-h0_0.conda",
        "url": PARI_URL,
        "channel": f"{CACHE}/conda/conda-forge/linux-64",
        "sha256": SHA,
        "license": "GPL-2.0-or-later",
    }
    entry.update(fields)
    return {key: value for key, value in entry.items() if value is not None}


def dry_run(*entries: dict[str, Any], prefix: str = "") -> str:
    return prefix + json.dumps({"actions": {"LINK": list(entries)}, "success": True})


def test_a_conda_dry_run_names_files_by_channel_and_their_cache_path() -> None:
    [resolved] = resolution.parse_conda_dry_run(dry_run(link()), CACHE)
    file = resolved.file
    assert (file.channel, file.subdir, file.file, file.sha256) == (
        "conda-forge",
        "linux-64",
        "pari-2.15.4-h0_0.conda",
        SHA,
    )
    assert resolved.path == "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"
    assert "8090" not in file.model_dump_json()


@pytest.mark.parametrize(
    ("entry", "channel", "path"),
    [
        # micromamba 2 names the channel by URL, with its subdir.
        (link(url=None), "conda-forge", "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"),
        (
            link(url=None, channel="conda-forge"),
            "conda-forge",
            "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda",
        ),
        (
            link(build=None, build_string="h0_0"),
            "conda-forge",
            "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda",
        ),
        # A file from anywhere but the cache would meet no decision: it gets no path.
        (
            link(url="https://conda.anaconda.org/conda-forge/linux-64/pari-2.15.4-h0_0.conda"),
            "conda-forge",
            "",
        ),
        (
            link(url="https://elsewhere.example/channel/linux-64/pari-2.15.4-h0_0.conda"),
            "https://elsewhere.example/channel",
            "",
        ),
    ],
)
def test_conda_dry_runs_in_other_shapes(entry: dict[str, Any], channel: str, path: str) -> None:
    [resolved] = resolution.parse_conda_dry_run(dry_run(entry), CACHE)
    assert (resolved.file.channel, resolved.path) == (channel, path)


def test_a_dry_run_after_a_line_of_text_still_reads() -> None:
    [resolved] = resolution.parse_conda_dry_run(dry_run(link(), prefix="warning: x\n"), CACHE)
    assert resolved.file.name == "pari"
    assert (
        resolution.parse_conda_dry_run('{"actions": {"PREFIX": "/p"}, "success": true}', CACHE)
        == []
    )


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"success": false}',
        dry_run("not a package"),  # pyright: ignore
        dry_run(link(name="Bad Name")),
        dry_run(link(version="1 2")),
        dry_run(link(url=None, channel=None)),
        dry_run(link(sha256="zz")),
    ],
)
def test_a_dry_run_the_worker_cannot_hold_is_refused(text: str) -> None:
    with pytest.raises(ResolveError):
        resolution.parse_conda_dry_run(text, CACHE)


@pytest.mark.parametrize(
    "entry",
    [
        # A name that is not its file's: the grant would hold another package's path.
        link(
            name="numpy",
            version="1.26.4",
            build="py_0",
            fn=None,
            url=f"{CACHE}/conda/conda-forge/linux-64/evilpkg-6.6.6-h0_0.conda",
        ),
        link(fn="evilpkg-6.6.6-h0_0.conda"),
        link(subdir="noarch"),
        link(version="2.15.5"),
        link(url=None, fn="evilpkg-6.6.6-h0_0.conda"),
        # Another ecosystem's file at the cache, or a path the cache does not lay out.
        link(url=f"{CACHE}/npm/left-pad/-/left-pad-1.3.0.tgz"),
        link(url=f"{CACHE}/conda/conda-forge/label/x/linux-64/pari-2.15.4-h0_0.conda"),
    ],
)
def test_a_dry_run_whose_names_and_paths_disagree_is_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(ResolveError):
        resolution.parse_conda_dry_run(dry_run(entry), CACHE)


def test_a_dry_run_longer_than_an_operation_may_install_is_refused() -> None:
    entries = [link()] * 2001
    with pytest.raises(ResolveError, match="more than the 2000"):
        resolution.parse_conda_dry_run(dry_run(*entries), CACHE)


def report(*entries: dict[str, Any]) -> str:
    return json.dumps({"version": "1", "install": list(entries)})


def distribution(**fields: Any) -> dict[str, Any]:
    url = f"{CACHE}/pypi/files/fakeflint/{SHA}/packages/ab/cd/fakeflint-0.7.1-py3-none-any.whl"
    metadata = {"name": "fakeflint", "version": "0.7.1", "license_expression": "MIT"}
    metadata.update(fields.pop("metadata", {}))
    entry: dict[str, Any] = {
        "download_info": {"url": url, "archive_info": {"hashes": {"sha256": SHA}}},
        "is_direct": False,
        "metadata": metadata,
    }
    entry.update(fields)
    return entry


def test_pips_report_names_each_distribution_upstream() -> None:
    [resolved] = resolution.parse_pip_report(report(distribution()), CACHE)
    file = resolved.file
    assert (
        file.url == "https://files.pythonhosted.org/packages/ab/cd/fakeflint-0.7.1-py3-none-any.whl"
    )
    assert (file.file, file.sha256, file.license, file.agpl) == (
        "fakeflint-0.7.1-py3-none-any.whl",
        SHA,
        "MIT",
        False,
    )
    assert resolved.path.startswith("/pypi/files/fakeflint/")


@pytest.mark.parametrize(
    "metadata",
    [
        {"license_expression": "AGPL-3.0-or-later"},
        {"license_expression": None, "license": "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3"},
        {
            "license_expression": None,
            "classifier": ["License :: OSI Approved :: GNU Affero General Public License v3"],
        },
    ],
)
def test_the_agpl_is_found_in_any_license_field(metadata: dict[str, Any]) -> None:
    [resolved] = resolution.parse_pip_report(report(distribution(metadata=metadata)), CACHE)
    assert resolved.file.agpl


def test_licenses_that_only_resemble_the_agpl_pass() -> None:
    for text in ("LGPL-3.0-only", "GPL-3.0-or-later", "MIT AND BSD-3-Clause", "Apache-2.0"):
        assert not resolution.is_agpl(text)
    assert resolution.is_agpl("GPL-3.0-only OR AGPL-3.0-only")


@pytest.mark.parametrize(
    "entry",
    [
        distribution(is_direct=True),
        distribution(download_info={"url": f"{CACHE}/pypi/files/x/-/packages/x-1.whl"}),
        {"metadata": {"name": "x", "version": "1"}},
    ],
)
def test_a_report_without_an_index_file_and_its_hash_is_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(ResolveError):
        resolution.parse_pip_report(report(entry), CACHE)


def wheel_at(url: str) -> dict[str, Any]:
    return {"url": url, "archive_info": {"hashes": {"sha256": SHA}}}


@pytest.mark.parametrize(
    "entry",
    [
        distribution(metadata={"name": "numpy"}),  # the path is fakeflint's
        distribution(
            download_info=wheel_at(
                f"{CACHE}/pypi/files/fakeflint/{'d' * 64}/packages/ab/cd/"
                "fakeflint-0.7.1-py3-none-any.whl"
            )
        ),
        distribution(download_info=wheel_at(f"{CACHE}/npm/left-pad/-/left-pad-1.3.0.tgz")),
        distribution(download_info=wheel_at(f"{CACHE}/pypi/simple/fakeflint/")),
    ],
)
def test_a_report_whose_names_and_paths_disagree_is_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(ResolveError):
        resolution.parse_pip_report(report(entry), CACHE)


def test_a_path_that_is_not_the_records_file_is_denied() -> None:
    loaded = parse_policy(POLICY)
    [good] = resolution.parse_conda_dry_run(dry_run(link()), CACHE)
    other = "/conda/conda-forge/linux-64/evilpkg-6.6.6-h0_0.conda"
    index = "/conda/conda-forge/linux-64/repodata.json"
    resolved = [good, resolution.Resolved(good.file, other), resolution.Resolved(good.file, index)]
    decided = resolution.decide(resolved, loaded, PoolCeiling.unrestricted(), HardFloor())
    assert [decision.action for decision in decided.decisions] == [
        Action.ALLOW,
        Action.DENY,
        Action.DENY,
    ]
    assert "not the package file of pari" in decided.decisions[1].reason


def test_the_conda_install_is_pinned_to_the_decided_files() -> None:
    [resolved] = resolution.parse_conda_dry_run(dry_run(link()), CACHE)
    argv = resolution.conda_install(["pari>=2.15"], [resolved.file])
    assert argv[3:] == ("sh", "conda-forge/linux-64::pari==2.15.4=h0_0", "pari>=2.15")
    assert "MAMBA_PINNED_PACKAGES" in argv[2]
    elsewhere = resolved.file.model_copy(update={"channel": "https://elsewhere.example/c"})
    with pytest.raises(ValueError, match="cannot pin"):
        resolution.conda_pins([elsewhere])


POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata: {name: test}
packages:
  conda: {channels: [conda-forge], action: allow}
  agpl_exceptions: {conda: [pymupdf], pypi: [some-agpl]}
"""


def test_agpl_exceptions_are_per_ecosystem_and_normalized() -> None:
    loaded = parse_policy(POLICY)

    def agpl(ecosystem: str, name: str) -> ResolvedFile:
        return ResolvedFile.model_validate(
            {
                "ecosystem": ecosystem,
                "name": name,
                "version": "1",
                "file": "x-1-0.conda",
                "agpl": True,
                "action": "allow",
            }
        )

    files = [
        agpl("conda", "pymupdf"),
        agpl("pypi", "Some_AGPL"),
        agpl("pypi", "pymupdf"),
        agpl("conda", "other"),
    ]
    assert [f.name for f in resolution.agpl_refused(files, loaded)] == ["pymupdf", "other"]


def test_index_reads_of_an_unlisted_channel_are_refused() -> None:
    loaded = parse_policy(POLICY)
    refusal = resolution.index_refusal(
        ["bioconda::samtools"], [], loaded, PoolCeiling.unrestricted(), HardFloor()
    )
    assert refusal is not None
    assert (refusal.rule, "bioconda" in refusal.reason) == ("packages.conda", True)
    assert (
        resolution.index_refusal(
            ["pari>=2", "conda-forge::gmp"], [], loaded, PoolCeiling.unrestricted(), HardFloor()
        )
        is None
    )


@pytest.mark.parametrize(
    ("text", "changed", "stage"),
    [
        ("error    libmamba post-link script failed for x", True, "post-link"),
        ("error    libmamba post-link script failed for x", False, "post-link"),
        ("anything at all", True, "link"),
        (
            "error    libmamba Download error (22) ... returned error: 403 approval_required",
            False,
            "download",
        ),
        ("Could not solve for environment specs", False, "resolve"),
        ("killed", False, "download"),
    ],
)
def test_where_a_failed_install_stopped(text: str, changed: bool, stage: str) -> None:
    assert resolution.failure_stage(text, changed) == stage


def test_lockfiles_tell_what_changed_and_what_was_not_decided() -> None:
    before = Lockfile(
        conda=(CondaPackage(name="gmp", version="6.2.1", build="h0_0"),),
        pip=(PipDistribution(name="Old_Pkg", version="1.0"),),
    )
    after = Lockfile(
        conda=(
            CondaPackage(name="gmp", version="6.3.0", build="h0_1"),
            CondaPackage(name="pari", version="2.15.4", build="h0_0"),
            CondaPackage(name="extra", version="1.0", build="0"),
        ),
        pip=(PipDistribution(name="old-pkg", version="1.0"),),
    )
    changes = resolution.package_changes(before, after)
    assert [(c.name, c.before, c.after) for c in changes] == [
        ("extra", None, "1.0"),
        ("gmp", "6.2.1", "6.3.0"),
        ("pari", None, "2.15.4"),
    ]
    planned = [
        ResolvedFile.model_validate(
            {
                "ecosystem": "conda",
                "name": n,
                "version": v,
                "file": f"{n}-{v}-0.conda",
                "action": "allow",
            }
        )
        for n, v in (("gmp", "6.3.0"), ("pari", "2.15.4"))
    ]
    assert resolution.unplanned(changes, planned) == ["conda:extra 1.0"]
    # A build or a channel the decision did not see is beyond it too: micromamba 2.9 holds a
    # pin to its name, version and build, but not to its channel.
    decided = [
        ResolvedFile.model_validate(
            {
                "ecosystem": "conda",
                "name": "gmp",
                "version": "6.3.0",
                "build": build,
                "channel": "conda-forge",
                "file": f"gmp-6.3.0-{build}.conda",
                "action": "allow",
            }
        )
        for build in ("h0_1", "h0_9")
    ]
    gmp = [change for change in changes if change.name == "gmp"]
    assert resolution.unplanned(gmp, decided[:1]) == []
    assert resolution.unplanned(gmp, decided[1:]) == ["conda:gmp 6.3.0"]
    elsewhere = Lockfile(
        conda=(CondaPackage(name="gmp", version="6.3.0", build="h0_1", channel="other"),)
    )
    assert resolution.unplanned(gmp, decided[:1], elsewhere) == ["conda:gmp 6.3.0"]


def test_specs_reach_package_managers_as_arguments_only() -> None:
    argv = resolution.conda_dry_run(["pari>=2.15", "conda-forge::gmp"])
    assert argv[:2] == ("sh", "-c")
    assert argv[3:] == ("sh", "pari>=2.15", "conda-forge::gmp")
    assert "pari" not in argv[2]
    # Every spec the contract accepts names a package the dry run's name finds.
    request = ProvisionRequest.model_validate(
        {
            "operation_id": "op",
            "context": {"project_id": "p", "session_id": "s", "environment_id": "e"},
            "purpose": "x",
            "conda": ["conda-forge/linux-64::python-flint >=0.6,<1", "pari 2.15.4 h0_0"],
        }
    )
    assert [resolution.spec_name(spec) for spec in request.conda] == [
        ("conda-forge", "python-flint"),
        (None, "pari"),
    ]
    assert resolution.pip_name("Some_Thing[extra]>=1") == "some-thing"


def test_a_dry_run_takes_no_turn() -> None:
    assert not provisioning.changes_environment("micromamba", ["install", "--dry-run", "x"])
    assert not provisioning.changes_environment(
        "pip", ["install", "--dry-run", "--report", "-", "x"]
    )
    assert provisioning.changes_environment("pip", ["install", "x"])
    # Only right after install: elsewhere, --dry-run may be another option's value.
    assert provisioning.changes_environment("pip", ["install", "--no-binary", "--dry-run", "x"])
    assert provisioning.changes_environment("pip", ["--quiet", "install", "--dry-run", "x"])


def test_the_shipped_catalog_loads_and_a_broken_one_does_not() -> None:
    catalog = load_catalog()
    assert {"gp", "sage", "fpylll", "sympy"} <= set(catalog.capabilities)
    assert len(catalog.sha256) == 64
    gp = catalog.get("gp")
    assert gp is not None
    assert gp.probes[0].passes("[274177, 1; 67280421310721, 1]  \n\n")
    assert not gp.probes[0].passes("[274177, 1]")
    head = 'version = 1\n[capabilities.x]\ndescription = "x"\n'
    probe = '[[capabilities.x.probes]]\nid = "p"\nargv = ["true"]\n'
    for broken in (
        "version = 1\n",  # no capabilities
        head + 'conda = ["x"]\nprobes = []\n',  # no probe
        head + 'conda = ["-c x"]\n' + probe + 'stdout = ""\n',  # an option for a spec
        head + 'conda = ["x"]\n' + probe,  # nothing it expects
        head + 'conda = ["x"]\n' + probe + 'pattern = "("\n',  # a pattern that does not compile
    ):
        with pytest.raises(CatalogError):
            Catalog.parse(broken.encode())


def test_named_upgrade_commands_preserve_exact_file_pins_without_install_fallback() -> None:
    file = resolution.parse_conda_dry_run(dry_run(link()), CACHE)[0].file
    dry = resolution.conda_upgrade_dry_run(("pari>=2",))
    update = resolution.conda_upgrade(("pari>=2",), (file,))
    assert dry[-1] == "pari>=2"
    assert "micromamba update --dry-run" in dry[2]
    assert update[-1] == "pari>=2"
    assert "micromamba update --json" in update[2]
    assert update[-2] == "conda-forge/linux-64::pari==2.15.4=h0_0"
    assert "micromamba install" not in dry[2] + update[2]
    assert "--all" not in dry
    assert "--all" not in update
    # Legacy commands keep the original install action.
    assert "micromamba install --dry-run" in resolution.conda_dry_run(("pari",))[2]
    assert "micromamba install --json" in resolution.conda_install(("pari",), (file,))[2]


def test_upgrade_requires_targets_and_reuses_pin_validation() -> None:
    with pytest.raises(ValueError, match="named"):
        resolution.conda_upgrade_dry_run(())
    with pytest.raises(ValueError, match="named"):
        resolution.conda_upgrade((), ())
    file = resolution.parse_conda_dry_run(dry_run(link()), CACHE)[0].file
    with pytest.raises(ValueError, match="cannot pin"):
        resolution.conda_upgrade(("pari",), (file.model_copy(update={"build": None}),))
