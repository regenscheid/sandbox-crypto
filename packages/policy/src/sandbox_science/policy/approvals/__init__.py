# SPDX-License-Identifier: MIT
"""Permission requests and their answers (D76), for the policy middleware and the MCP proxy.

An escalated operation is a permission request. Its first occurrence is recorded in the
event store as an ``escalation`` whose payload carries a ``target``: the request's key.
Retries of the same request share the key's fingerprint and are not recorded again. An
operator answers with an ``approval`` or a ``denial`` record, which the component
honours on the next retry until the answer expires. Unanswered past the rule's
escalation timeout, a request follows ``on_timeout``.

Keys come in three kinds, each with the scopes an answer can take:
- ``http`` (the middleware): sandbox, policy revision, method, host, port, path and a
  digest of the body (M1). ``request`` covers the request itself, its body included;
  ``host`` every path and body on that host and port with that method.
- ``mcp`` (the MCP proxy): project, caller, policy revision, server, method, tool and a
  digest of the arguments. ``call`` covers the call itself; ``tool`` the tool with any
  arguments. No scope covers other tools: grants of a class on a server are the
  workspace's (D97), which answers each call they cover.
- ``packages`` (the worker's provisioning operations, Conda provisioning CP-8): sandbox,
  policy revision, operation and a digest of an exact set of package files, which the
  request's record lists by their paths at the package cache. ``files`` covers exactly
  those files: the middleware allows their downloads to that sandbox under that revision,
  and nothing else. When its operation ends, the worker withdraws the request (a denial
  marked ``withdrawn``), and nothing can grant its files again. The request also names
  the time after which it grants nothing (``valid_until``), whatever its answers say, so
  a withdrawal the worker failed to record still ends the grant.

The package's modules: ``keys`` (what a request asks for, and the scopes of answers),
``records`` (the typed payloads of the records), ``ledger`` (the fold of the records, and
``resolve``), ``grants`` (file-set grants), ``ceilings`` (how widely a user may grant a
request, D97), ``summary`` (what an approval card shows of it), ``render`` (what an
operator sees) and ``commands`` (what the components' command lines share). Records are
read through a small protocol, so the package needs no event store.
"""

from __future__ import annotations

from .ceilings import (
    PACKAGES_MAX_GRANT,
    READ_METHODS,
    GrantScope,
    allowed,
    network_max_grant,
    offered,
    widest,
)
from .commands import MAX_APPROVAL_HOURS, prepare_answer
from .keys import ALL_SCOPES, DEFAULT_SCOPES, RequestKey, body_digest, check_scope, files_digest
from .ledger import (
    APPROVAL,
    DENIAL,
    ESCALATION,
    KINDS,
    Approvals,
    Approved,
    Denied,
    RecordLike,
    Resolution,
    TimedOut,
    Unrecorded,
    Waiting,
)
from .records import (
    VALID_UNTIL,
    WITHDRAWN,
    Answer,
    PermissionRequest,
    answer_payload,
    withdrawal_payload,
)
from .render import describe, pending_lines, request_lines
from .summary import call_summary, http_summary

__all__ = [
    "ALL_SCOPES",
    "APPROVAL",
    "DEFAULT_SCOPES",
    "DENIAL",
    "ESCALATION",
    "KINDS",
    "MAX_APPROVAL_HOURS",
    "PACKAGES_MAX_GRANT",
    "READ_METHODS",
    "VALID_UNTIL",
    "WITHDRAWN",
    "Answer",
    "Approvals",
    "Approved",
    "Denied",
    "GrantScope",
    "PermissionRequest",
    "RecordLike",
    "RequestKey",
    "Resolution",
    "TimedOut",
    "Unrecorded",
    "Waiting",
    "allowed",
    "answer_payload",
    "body_digest",
    "call_summary",
    "check_scope",
    "describe",
    "files_digest",
    "http_summary",
    "network_max_grant",
    "offered",
    "pending_lines",
    "prepare_answer",
    "request_lines",
    "widest",
    "withdrawal_payload",
]
