# SPDX-License-Identifier: MIT
"""Passive lookup of a scoped operation, independent of executor lifecycle."""

from __future__ import annotations

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.contracts.recovery import OperationLookup, OperationLookupRequest

from .errors import WorkerError
from .store import Store


def lookup(store: Store, request: OperationLookupRequest) -> OperationLookup:
    """Use durable records only; another session/environment is indistinguishable from absence."""
    context = request.context

    def matches(found: ExecutionContext) -> bool:
        return (found.project_id, found.session_id, found.environment_id) == (
            context.project_id,
            context.session_id,
            context.environment_id,
        )

    execution = None
    provision = None
    found = store.find_operation(context.project_id, request.operation_id)
    if found is not None:
        record = store.execution(found[0])
        if record is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "operation execution record is unavailable")
        if matches(record.context):
            execution = record
    found = store.find_provision(context.project_id, request.operation_id)
    if found is not None:
        operation = store.provision(found[0])
        if operation is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "operation provisioning record is unavailable")
        if matches(operation.request.context):
            provision = operation
    if execution is not None and provision is not None:
        raise WorkerError(ErrorKind.CONFLICT, "operation ID identifies execution and provisioning")
    return OperationLookup(execution=execution, provision=provision)
