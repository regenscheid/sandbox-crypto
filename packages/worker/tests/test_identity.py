# SPDX-License-Identifier: MIT
"""Operation identity: golden digests per request type, and pre-protocol-2 digests replaying."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from sandbox_science.contracts import (
    Execution,
    ExecutionRequest,
    ExecutionState,
    JobRequest,
    ProvisionRequest,
)
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.identity import (
    execution_digest,
    provision_digest,
    same_execution,
    same_provision,
)

from .harness import Harness

_REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
CONTEXT = {"project_id": "p", "session_id": "s", "environment_id": "e"}
REQUESTS: dict[str, dict[str, Any]] = {
    "shell": {"kind": "shell", "operation_id": "op-shell", "context": CONTEXT, "script": "echo hi"},
    "shell_argv": {
        "kind": "shell",
        "operation_id": "op-argv",
        "context": CONTEXT,
        "argv": ["python", "-V"],
        "cwd": "shared",
        "timeout_seconds": 30,
    },
    "job": {
        "kind": "job",
        "operation_id": "op-job",
        "context": CONTEXT,
        "argv": ["sleep", "60"],
        "purpose": "wait",
        "expected_progress": "nothing",
    },
    "job_explicit": {
        "kind": "job",
        "operation_id": "op-job",
        "context": CONTEXT,
        "argv": ["sleep", "60"],
        "purpose": "wait",
        "expected_progress": "nothing",
        "timeout_seconds": 3600,
    },
    "cell": {
        "kind": "cell",
        "operation_id": "op-cell",
        "context": CONTEXT,
        "language": "sage",
        "code": "2+2",
    },
    "pdf": {
        "kind": "pdf",
        "operation_id": "op-pdf",
        "context": CONTEXT,
        "operation": "search",
        "path": "paper.pdf",
        "query": "lattice",
    },
}
GOLDEN = {
    "shell": "ca5306309509d34d909d50dd0850a69f83e6bd30957d06c7a58c0be3076463e8",
    "shell_argv": "d467069aa52fa0110ba9bd4d699bfccbc5bbd1d17d575ea8bcc34daf6bd08366",
    "job": "1382ab3c5ca15bc4c768bd963119dc47b5e0209683fce407588f2e4033f11ed3",
    "job_explicit": "1d3923126dde99f527bafaadda365508ea173de2e3464240850e97e2ac17aeb8",
    "cell": "a2bf51718cbca62088b5ce92586a8ea30aaeead00079d70bab45070e1878384e",
    "pdf": "de0fa609d125fa56cbe7c7d1e707ef5f7d664cfb1751a27a8045ec96e7d5c7c7",
}
"""The digests the worker stores now. A change to one changes stored identities."""

_CONTEXT_JSON = (
    '"context":{"project_id":"p","session_id":"s","environment_id":"e",'
    '"policy_revision":null,"actor":"agent"}'
)
LEGACY = {
    "shell": '{"kind":"shell","operation_id":"op-shell",' + _CONTEXT_JSON + ',"script":"echo hi",'
    '"argv":null,"cwd":".","timeout_seconds":120.0,"output_limit_bytes":1048576}',
    "shell_argv": '{"kind":"shell","operation_id":"op-argv",' + _CONTEXT_JSON + ',"script":null,'
    '"argv":["python","-V"],"cwd":"shared","timeout_seconds":30.0,"output_limit_bytes":1048576}',
    "job": '{"kind":"job","operation_id":"op-job",' + _CONTEXT_JSON + ',"script":null,'
    '"argv":["sleep","60"],"purpose":"wait","expected_progress":"nothing","wake_session":false,'
    '"cwd":".","timeout_seconds":86400.0,"output_limit_bytes":1048576}:explicit_timeout=False',
    "job_explicit": '{"kind":"job","operation_id":"op-job",' + _CONTEXT_JSON + ',"script":null,'
    '"argv":["sleep","60"],"purpose":"wait","expected_progress":"nothing","wake_session":false,'
    '"cwd":".","timeout_seconds":3600.0,"output_limit_bytes":1048576}:explicit_timeout=True',
    "cell": '{"kind":"cell","operation_id":"op-cell",' + _CONTEXT_JSON + ',"language":"sage",'
    '"code":"2+2","timeout_seconds":120.0,"output_limit_bytes":1048576}',
    "pdf": '{"kind":"pdf","operation_id":"op-pdf",' + _CONTEXT_JSON + ',"operation":"search",'
    '"path":"paper.pdf","pages":null,"query":"lattice","page":null,"region":null,"dpi":150,'
    '"include_artifacts":false,"out":null,"timeout_seconds":120.0,"output_limit_bytes":1048576}',
}
"""What the worker hashed before protocol 2, captured from that encoder (not built here)."""
LEGACY_PROVISION = (
    '{"operation_id":"upgrade-test","context":{"project_id":"p","session_id":"s",'
    '"environment_id":"e","policy_revision":null,"actor":"agent"},"purpose":"synthetic",'
    '"capability":null,"conda":["pari"],"pip":[],"pip_reason":null,"system":[]}'
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_each_request_type_has_its_golden_digest(name: str) -> None:
    request = _REQUEST.validate_python(REQUESTS[name])
    assert execution_digest(request) == GOLDEN[name]
    # A copy through JSON, or with its defaults spelled out, is the same request.
    assert execution_digest(_REQUEST.validate_json(request.model_dump_json())) == GOLDEN[name]
    assert same_execution(GOLDEN[name], request)


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_a_digest_stored_before_protocol_2_still_identifies_its_request(name: str) -> None:
    request = _REQUEST.validate_python(REQUESTS[name])
    assert same_execution(sha256(LEGACY[name]), request)
    others = [_REQUEST.validate_python(raw) for key, raw in REQUESTS.items() if key != name]
    assert not any(same_execution(sha256(LEGACY[name]), other) for other in others)
    assert not any(same_execution(GOLDEN[name], other) for other in others)
    assert not same_execution(None, request)


def test_an_explicit_policy_default_is_another_request() -> None:
    unset = _REQUEST.validate_python(REQUESTS["job"])
    assert isinstance(unset, JobRequest)
    explicit = unset.model_copy(update={"timeout_seconds": 86400.0})
    assert execution_digest(explicit) != execution_digest(unset)
    assert not same_execution(sha256(LEGACY["job"]), explicit)


def test_provisioning_digests_new_and_old() -> None:
    install = ProvisionRequest.model_validate_json(LEGACY_PROVISION)
    upgrade = install.model_copy(update={"action": "upgrade"})
    assert provision_digest(install) == (
        "2c1477ea228c40c360a358ad25eee4866a0db9a76110b779e8663ac7888c814b"
    )
    assert provision_digest(upgrade) == (
        "2cfec2de08e3ebf764e2d30e852bc436d58a5e610d25f46eb108ce7908c2ea0c"
    )
    assert same_provision(sha256(LEGACY_PROVISION), install)
    assert not same_provision(sha256(LEGACY_PROVISION), upgrade)
    assert same_provision(provision_digest(upgrade), upgrade)


async def test_an_operation_stored_before_protocol_2_replays_and_still_conflicts(
    tmp_path: Path,
) -> None:
    async with await Harness.start(tmp_path) as worker:
        raw = {**REQUESTS["job"], "context": {**CONTEXT, "project_id": "p1"}}
        request = _REQUEST.validate_python(raw)
        legacy = sha256(LEGACY["job"].replace('"project_id":"p"', '"project_id":"p1"'))
        stored = Execution(
            execution_id="exe-legacy",
            attempt_id="att-legacy",
            operation_id=request.operation_id,
            kind=request.kind,
            context=request.context,
            state=ExecutionState.SUCCEEDED,
            submitted_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        worker.manager.store.insert_execution(stored, legacy)
        again, created = await worker.manager.submit(request)
        assert (again.execution_id, created) == ("exe-legacy", False)
        changed = request.model_copy(update={"argv": ("sleep", "61")})
        with pytest.raises(WorkerError, match="different request"):
            await worker.manager.submit(changed)
