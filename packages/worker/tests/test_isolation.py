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
from sandbox_science.worker import executor
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
        "facts": executor.probe_facts(),
        "sentinels": {path: executor.sentinel_state(path) for path in paths},
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
