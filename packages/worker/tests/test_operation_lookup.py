# SPDX-License-Identifier: MIT
"""Recovery lookups use durable identity without waking or replaying research work."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts import Execution, ExecutionContext, ExecutionState
from sandbox_science.contracts.provisioning import (
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
)
from sandbox_science.contracts.recovery import OperationLookupRequest
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.operation_lookup import lookup
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def execution(operation: str = "original") -> Execution:
    return Execution(
        execution_id="exe-original",
        attempt_id="att-original",
        operation_id=operation,
        kind="shell",
        context=CONTEXT,
        state=ExecutionState.RUNNING,
        submitted_at=NOW,
    )


def provision(operation: str = "original") -> ProvisionOperation:
    return ProvisionOperation(
        provision_id="prv-original",
        request=ProvisionRequest(
            operation_id=operation, context=CONTEXT, purpose="test installation", conda=("x",)
        ),
        state=ProvisionState.QUEUED,
        submitted_at=NOW,
    )


@pytest.mark.parametrize("kind", ["execution", "provision"])
def test_lookup_retains_original_identity_across_worker_store_restart(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "worker.sqlite3"
    store = Store(path)
    if kind == "execution":
        store.insert_execution(execution(), "ab" * 32)
    else:
        store.insert_provision(provision(), "cd" * 32)
    store.close()
    store = Store(path)
    try:
        found = lookup(store, OperationLookupRequest(context=CONTEXT, operation_id="original"))
        assert found.execution == (execution() if kind == "execution" else None)
        assert found.provision == (provision() if kind == "provision" else None)
        assert (
            lookup(
                store, OperationLookupRequest(context=CONTEXT, operation_id="absent")
            ).model_dump(exclude_none=True)
            == {}
        )
    finally:
        store.close()


@pytest.mark.parametrize("field", ["project_id", "session_id", "environment_id"])
def test_lookup_does_not_expose_other_scope(tmp_path: Path, field: str) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        store.insert_execution(execution(), "ab" * 32)
        store.insert_provision(provision("packages"), "cd" * 32)
        context = CONTEXT.model_copy(update={field: "other"})
        for operation in ("original", "packages"):
            found = lookup(store, OperationLookupRequest(context=context, operation_id=operation))
            assert found.execution is None
            assert found.provision is None
    finally:
        store.close()


def test_lookup_refuses_ambiguous_operation_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        store.insert_execution(execution(), "ab" * 32)
        store.insert_provision(provision(), "cd" * 32)
        with pytest.raises(WorkerError, match="identifies execution and provisioning"):
            lookup(store, OperationLookupRequest(context=CONTEXT, operation_id="original"))
    finally:
        store.close()
