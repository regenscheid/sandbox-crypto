# SPDX-License-Identifier: MIT
"""Isolation-tier probes (D43): the rule, the probe, and the tier a backend must declare."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind, ProbeFacts
from sandbox_science.contracts.worker import SentinelState
from sandbox_science.worker import sandbox_setup
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.isolation import assess, failed, probe, sentinel_result
from sandbox_science.worker.settings import SettingsError, load_settings

HOST = ProbeFacts(boot_id="host-boot", seccomp="disabled", no_new_privs=False)
VM = ProbeFacts(
    boot_id="vm-boot", seccomp="filter", no_new_privs=True, capabilities="0000000000000000"
)
HARDENED = ProbeFacts(
    boot_id="host-boot", seccomp="filter", no_new_privs=True, capabilities="0000000000000000"
)
"""A hardened container on the worker host's kernel."""


# The rule.


@pytest.mark.parametrize("declared", [1, 2, 3])
def test_a_kernel_of_its_own_keeps_the_declared_tier(declared: int) -> None:
    assessment = assess(declared, HOST, VM, "hidden")
    assert assessment.tier == declared
    assert (assessment.evidence.kernel, assessment.evidence.findings) == ("separate", ())


def test_evidence_never_raises_the_declared_tier() -> None:
    assert assess(0, HOST, VM, "hidden").tier == 0
    assert assess(1, HOST, VM, "hidden").tier == 1
    assert assess(0, HOST, HARDENED, "hidden").tier == 0


def test_the_worker_hosts_kernel_allows_tier_1_at_most() -> None:
    assessment = assess(3, HOST, HARDENED, "hidden")
    assert assessment.tier == 1
    assert assessment.evidence.kernel == "shared"
    assert assessment.evidence.findings == (
        "the sandbox runs on the worker host's kernel (same boot ID)",
    )


@pytest.mark.parametrize(
    ("seccomp", "no_new_privs"),
    [("disabled", True), ("strict", True), (None, True), ("filter", False), ("filter", None)],
)
def test_a_shared_kernel_needs_seccomp_and_no_new_privs_for_tier_1(
    seccomp: str | None, no_new_privs: bool | None
) -> None:
    sandbox = HARDENED.model_copy(update={"seccomp": seccomp, "no_new_privs": no_new_privs})
    assessment = assess(3, HOST, sandbox, "hidden")
    assert assessment.tier == 0
    assert len(assessment.evidence.findings) == 2


@pytest.mark.parametrize(
    ("capabilities", "finding"),
    [
        ("00000000a80425fb", "the executor runs with capabilities 00000000a80425fb"),
        (None, "the executor's capabilities are unknown"),
        ("not-hex", "the executor runs with capabilities not-hex"),
    ],
)
def test_a_shared_kernel_needs_no_capabilities_for_tier_1(
    capabilities: str | None, finding: str
) -> None:
    # Docker's default set (00000000a80425fb) is not a hardened container (threat model, tier 1).
    sandbox = HARDENED.model_copy(update={"capabilities": capabilities})
    assessment = assess(3, HOST, sandbox, "hidden")
    assert assessment.tier == 0
    assert finding in assessment.evidence.findings


def test_capabilities_do_not_matter_with_a_kernel_of_its_own() -> None:
    sandbox = VM.model_copy(update={"capabilities": "000001ffffffffff"})
    assert assess(3, HOST, sandbox, "hidden").tier == 3


@pytest.mark.parametrize(("host_boot", "sandbox_boot"), [(None, "vm-boot"), ("host-boot", None)])
def test_an_unknown_boot_id_counts_as_a_shared_kernel(
    host_boot: str | None, sandbox_boot: str | None
) -> None:
    host = HOST.model_copy(update={"boot_id": host_boot})
    assert assess(3, host, VM.model_copy(update={"boot_id": sandbox_boot}), "hidden").tier == 1
    unhardened = HARDENED.model_copy(update={"boot_id": sandbox_boot, "seccomp": None})
    assessment = assess(3, host, unhardened, "hidden")
    assert assessment.tier == 0
    assert assessment.evidence.kernel == "unknown"


def test_a_linux_sandbox_on_a_host_of_another_kernel_has_one_of_its_own() -> None:
    # A Mac worker host has no boot ID; a sandbox that reports one runs on a Linux kernel,
    # so not on the host's (Mac test, 2026-10-21).
    mac = HOST.model_copy(update={"boot_id": None})
    assessment = assess(3, mac, VM, "hidden", host_linux=False)
    assert assessment.tier == 3
    assert assessment.evidence.kernel == "separate"
    # A sandbox that reports no boot ID still shows nothing.
    unknown = assess(3, mac, VM.model_copy(update={"boot_id": None}), "hidden", host_linux=False)
    assert unknown.tier == 1
    assert unknown.evidence.kernel == "unknown"
    # A visible sentinel still means tier 0.
    assert assess(3, mac, VM, "visible", host_linux=False).tier == 0


# Node kernels: on Kubernetes the worker and the sandbox usually run on different nodes,
# so a plain container shows a boot ID of its own; its kernel release gives it away.

NODES = ("*-talos",)
ON_A_NODE = VM.model_copy(update={"kernel_release": "6.18.42-talos"})
"""A hardened container on another node's kernel (plain runc)."""
IN_KATA = VM.model_copy(update={"kernel_release": "6.18.35"})
"""A sandbox in a Kata VM, whose guest kernel is Kata's own build."""


def test_without_node_patterns_the_release_is_not_evidence() -> None:
    assessment = assess(3, HOST, ON_A_NODE, "hidden")
    assert assessment.tier == 3
    assert assessment.evidence.findings == ()


def test_a_node_kernel_allows_tier_1_at_most() -> None:
    assessment = assess(3, HOST, ON_A_NODE, "hidden", node_kernels=("*-azure", *NODES))
    assert assessment.tier == 1
    # The boot ID still compares the sandbox with the worker host only.
    assert assessment.evidence.kernel == "separate"
    assert assessment.evidence.findings == (
        "the sandbox runs on a node's kernel (release 6.18.42-talos matches *-talos)",
    )
    assert assess(1, HOST, ON_A_NODE, "hidden", node_kernels=NODES).tier == 1
    assert assess(0, HOST, ON_A_NODE, "hidden", node_kernels=NODES).tier == 0


@pytest.mark.parametrize(
    ("update", "finding"),
    [
        ({"seccomp": "disabled"}, "the executor runs without a seccomp filter"),
        ({"no_new_privs": None}, "the executor runs without no-new-privs"),
        (
            {"capabilities": "00000000a80425fb"},
            "the executor runs with capabilities 00000000a80425fb",
        ),
    ],
)
def test_a_node_kernel_needs_the_hardened_container_for_tier_1(
    update: dict[str, object], finding: str
) -> None:
    sandbox = ON_A_NODE.model_copy(update=update)
    assessment = assess(3, HOST, sandbox, "hidden", node_kernels=NODES)
    assert assessment.tier == 0
    assert finding in assessment.evidence.findings


def test_a_kernel_that_matches_no_node_pattern_keeps_the_declared_tier() -> None:
    assessment = assess(3, HOST, IN_KATA, "hidden", node_kernels=NODES)
    assert assessment.tier == 3
    assert assessment.evidence.findings == ()
    # Patterns match the whole release, with case.
    for release in ("6.18.42-talos-kata", "6.18.42-TALOS"):
        sandbox = VM.model_copy(update={"kernel_release": release})
        assert assess(3, HOST, sandbox, "hidden", node_kernels=NODES).tier == 3


@pytest.mark.parametrize(
    "release",
    [None, "", "6.18.42-talos" + "x" * 60, "6.18.42 talos", "6.18.42-talos\n", "6.18/-talos"],
)
def test_no_valid_release_counts_as_a_node_kernel(release: str | None) -> None:
    sandbox = VM.model_copy(update={"kernel_release": release})
    assessment = assess(3, HOST, sandbox, "hidden", node_kernels=NODES)
    assert assessment.tier == 1
    assert assessment.evidence.findings == (
        "the sandbox reports no valid kernel release to compare with the nodes' kernels",
    )
    unhardened = sandbox.model_copy(update={"seccomp": None})
    assert assess(3, HOST, unhardened, "hidden", node_kernels=NODES).tier == 0
    # Without patterns, the release is not evidence at all.
    assert assess(3, HOST, sandbox, "hidden").tier == 3


def test_node_kernels_and_boot_ids_lower_the_tier_together() -> None:
    on_the_host = HARDENED.model_copy(update={"kernel_release": "6.18.42-talos"})
    assessment = assess(3, HOST, on_the_host, "hidden", node_kernels=NODES)
    assert assessment.tier == 1
    assert assessment.evidence.kernel == "shared"
    assert assessment.evidence.findings == (
        "the sandbox runs on the worker host's kernel (same boot ID)",
        "the sandbox runs on a node's kernel (release 6.18.42-talos matches *-talos)",
    )
    # The hardening rule applies once, whichever evidence brought the tier down.
    unhardened = on_the_host.model_copy(update={"no_new_privs": False})
    lowered = assess(3, HOST, unhardened, "hidden", node_kernels=NODES)
    assert lowered.tier == 0
    assert lowered.evidence.findings.count("the executor runs without no-new-privs") == 1
    # A Kata release does not undo what the boot ID shows.
    shared = HARDENED.model_copy(update={"kernel_release": "6.18.35"})
    assert assess(3, HOST, shared, "hidden", node_kernels=NODES).tier == 1
    # A Mac worker host cannot compare boot IDs; the release still counts.
    mac = HOST.model_copy(update={"boot_id": None})
    assert assess(3, mac, ON_A_NODE, "hidden", host_linux=False, node_kernels=NODES).tier == 1
    assert assess(3, mac, IN_KATA, "hidden", host_linux=False, node_kernels=NODES).tier == 3


def test_a_node_kernel_does_not_hide_the_sentinel() -> None:
    assert assess(3, HOST, ON_A_NODE, "visible", node_kernels=NODES).tier == 0


@pytest.mark.parametrize("sentinel", ["visible", "unknown"])
def test_the_worker_files_must_be_out_of_sight(sentinel: SentinelState) -> None:
    assessment = assess(3, HOST, VM, sentinel)
    assert assessment.tier == 0
    assert assessment.evidence.sentinel == sentinel
    assert len(assessment.evidence.findings) == 1


def test_a_failed_probe_is_tier_0() -> None:
    assessment = failed("the executor went away " * 100)
    assert assessment.tier == 0
    assert assessment.evidence.sandbox is None
    assert assessment.evidence.error is not None
    assert len(assessment.evidence.error) <= 300


def test_sentinel_results() -> None:
    paths = ["/a", "/b"]
    assert sentinel_result({"/a": "absent", "/b": "absent"}, paths) == "hidden"
    assert sentinel_result({"/a": "absent", "/b": "present"}, paths) == "visible"
    assert sentinel_result({"/a": "absent", "/b": "unknown"}, paths) == "unknown"
    assert sentinel_result({"/a": "absent"}, paths) == "unknown"
    assert sentinel_result({"/a": "gone", "/b": "absent"}, paths) == "unknown"
    assert sentinel_result(["/a", "/b"], paths) == "unknown"
    assert sentinel_result({}, []) == "unknown"


# The probe, with an executor that answers in-process.


class FakeChannel(ExecutorChannel):
    """Answers ``probe`` requests with ``answer(sentinel paths)``."""

    def __init__(self, answer: Callable[[list[str]], Message]) -> None:
        self.answer = answer
        self.seen: list[list[str]] = []

    async def request(
        self, op: str, *, reply_timeout: float | None = 120.0, **fields: Any
    ) -> Message:
        assert op == "probe"
        self.seen.append(fields["sentinels"])
        return self.answer(fields["sentinels"])


def _beside_the_worker(paths: list[str]) -> Message:
    """What an executor on the worker's own host would say."""
    return {
        "facts": sandbox_setup.probe_facts(),
        "sentinels": {path: sandbox_setup.sentinel_state(path) for path in paths},
    }


@pytest.mark.asyncio
async def test_an_executor_beside_the_worker_probes_as_tier_0(tmp_path: Path) -> None:
    channel = FakeChannel(_beside_the_worker)
    assessment = await probe(channel, 3, tmp_path)
    assert assessment.tier == 0
    assert (assessment.evidence.kernel, assessment.evidence.sentinel) == ("shared", "visible")
    (paths,) = channel.seen
    assert all("/probe/sentinel-" in path for path in paths)
    assert not any(os.path.lexists(path) for path in paths)


@pytest.mark.asyncio
async def test_a_sandbox_with_its_own_kernel_and_files_keeps_its_tier(tmp_path: Path) -> None:
    def elsewhere(paths: list[str]) -> Message:
        facts = VM.model_dump()
        return {"facts": facts, "sentinels": dict.fromkeys(paths, "absent")}

    assessment = await probe(FakeChannel(elsewhere), 3, tmp_path)
    assert assessment.tier == 3
    assert assessment.evidence.sandbox == VM


@pytest.mark.asyncio
async def test_the_probe_compares_the_release_with_the_node_patterns(tmp_path: Path) -> None:
    def answer(sandbox: ProbeFacts) -> Callable[[list[str]], Message]:
        return lambda paths: {
            "facts": sandbox.model_dump(),
            "sentinels": dict.fromkeys(paths, "absent"),
        }

    on_a_node = await probe(FakeChannel(answer(ON_A_NODE)), 3, tmp_path, NODES)
    assert on_a_node.tier == 1
    assert on_a_node.evidence.sandbox == ON_A_NODE
    assert (await probe(FakeChannel(answer(IN_KATA)), 3, tmp_path, NODES)).tier == 3
    assert (await probe(FakeChannel(answer(ON_A_NODE)), 3, tmp_path)).tier == 3


@pytest.mark.asyncio
async def test_an_oversized_release_is_a_failed_probe(tmp_path: Path) -> None:
    def answer(paths: list[str]) -> Message:
        facts = {**ON_A_NODE.model_dump(), "kernel_release": "6.18.42-talos" + "x" * 300}
        return {"facts": facts, "sentinels": dict.fromkeys(paths, "absent")}

    assessment = await probe(FakeChannel(answer), 3, tmp_path, NODES)
    assert assessment.tier == 0
    assert assessment.evidence.error is not None


def test_the_executor_reports_the_running_kernels_release() -> None:
    assert sandbox_setup.probe_facts()["kernel_release"] == os.uname().release


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"facts": {"uid": -1}, "sentinels": {}},
        {"facts": "not facts", "sentinels": {}},
        {"sentinels": {}},
    ],
)
async def test_malformed_answers_are_a_failed_probe(tmp_path: Path, answer: Message) -> None:
    assessment = await probe(FakeChannel(lambda paths: answer), 3, tmp_path)
    assert assessment.tier == 0
    assert assessment.evidence.error is not None


@pytest.mark.asyncio
async def test_an_executor_error_is_a_failed_probe(tmp_path: Path) -> None:
    def refuse(paths: list[str]) -> Message:
        raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")

    assessment = await probe(FakeChannel(refuse), 3, tmp_path)
    assert assessment.tier == 0
    assert assessment.evidence.error == "the executor went away"
    assert assessment.evidence.findings == ("the probe failed: the executor went away",)


@pytest.mark.asyncio
async def test_a_sentinel_the_worker_cannot_write_is_a_failed_probe(tmp_path: Path) -> None:
    (tmp_path / "probe").write_text("a file where the probe's directory should be")
    assessment = await probe(FakeChannel(_beside_the_worker), 3, tmp_path)
    assert assessment.tier == 0
    assert assessment.evidence.error is not None
    assert "sentinel" in assessment.evidence.error


# The declared tier.


def _settings(tmp_path: Path, backend: str) -> Path:
    path = tmp_path / "worker.toml"
    path.write_text(f'token_file = "token"\nstate_dir = "state"\n\n{backend}\n')
    return path


def test_the_openshell_backend_must_declare_its_tier(tmp_path: Path) -> None:
    # No default: a deployment that leaves the tier out must not pass as tier 3
    # (W1 review, finding M6).
    with pytest.raises(SettingsError, match="isolation_tier"):
        load_settings(_settings(tmp_path, '[openshell]\nsandboxes = {env-1 = "research-1"}'))
    with pytest.raises(SettingsError, match="isolation_tier"):
        load_settings(_settings(tmp_path, "[openshell]\nisolation_tier = 0"))
    settings = load_settings(_settings(tmp_path, "[openshell]\nisolation_tier = 2"))
    assert settings.openshell is not None
    assert settings.openshell.isolation_tier == 2


def test_the_openshell_interpreter_defaults_to_the_images_prefix_and_can_be_set(
    tmp_path: Path,
) -> None:
    # The system python3 has no ipykernel, so the executor would offer no kernels there.
    settings = load_settings(_settings(tmp_path, "[openshell]\nisolation_tier = 3"))
    assert settings.openshell is not None
    assert settings.openshell.python == "/opt/sandbox-science/bin/python"
    explicit = '[openshell]\nisolation_tier = 3\npython = "/usr/bin/python3"'
    settings = load_settings(_settings(tmp_path, explicit))
    assert settings.openshell is not None
    assert settings.openshell.python == "/usr/bin/python3"


def test_node_kernel_patterns_are_optional_and_checked(tmp_path: Path) -> None:
    def openshell(patterns: str) -> Path:
        return _settings(
            tmp_path, f"[openshell]\nisolation_tier = 3\nnode_kernel_releases = {patterns}"
        )

    settings = load_settings(_settings(tmp_path, "[openshell]\nisolation_tier = 3"))
    assert settings.openshell is not None
    assert settings.openshell.node_kernel_releases == ()
    settings = load_settings(openshell('["*-talos", "6.1[0-9].*"]'))
    assert settings.openshell is not None
    assert settings.openshell.node_kernel_releases == ("*-talos", "6.1[0-9].*")
    too_many = ", ".join(f'"*-node{index}"' for index in range(17))
    for patterns in ('[""]', '["* talos"]', '["*-talos\\n"]', f'["{"x" * 65}"]', f"[{too_many}]"):
        with pytest.raises(SettingsError, match="node_kernel_releases"):
            load_settings(openshell(patterns))
