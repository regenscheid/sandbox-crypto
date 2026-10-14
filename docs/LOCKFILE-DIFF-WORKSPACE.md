<!-- SPDX-License-Identifier: MIT -->
# Workspace proxy for an explicit lockfile comparison

The authenticated workspace exposes the worker's bounded
[retained lockfile comparison](LOCKFILE-DIFFERENCES.md) at:

```text
GET /api/v0/projects/{project}/environments/{environment}/provisioning/observations/lockfile-diff
    ?before=N&before_pin=SHA256&after=N&after_pin=SHA256&offset=0&limit=8
```

All four revision/record-pin fields are required. Revisions have canonical decimal
spelling and range 0 through 2^62; pins are 64 lower-case hexadecimal characters.
Offset is 0 through 80,000 and limit is 1 through 8. Repeated or additional query
keys and request bodies refuse before a worker request. There is no latest,
current, nearest-source or automatic pin-adoption fallback. Before and after keep
their requested order, including explicit same-revision comparisons.

The success body is the strict `LockfileDiffPage` contract: exact project and
environment, observed receipt time, both original revision-record pins and
recorded lockfile references, verified headers where available, and either a
complete comparison or an explicit held observation. A complete result preserves
exact totals, header changes, selected rows and the actual continuation offset.
A held result preserves its source-specific reasons and unknown totals with no
difference rows. Missing/unrecorded/skipped/unsupported/over-limit inputs do not
become empty inventories. A validated empty set is distinct from an unavailable
source; neither proves the untrusted guest inventory complete.

Declared licenses and the worker's existing AGPL heuristic remain recorded
metadata. Missing license data, including pip's absent license field, stays
unknown. The proxy does not infer a license, grant, install, package correctness
or scientific verification from a comparison.

## Authority and reads

This route uses the same [provisioning routes](PROVISIONING-WORKSPACE.md): the
workspace login, the recorded project's environments and the operator-configured
borrowed WorkerClient transport. The project is server-owned, not an accepted
query field. Closed dedicated environments can remain recorded history without
reopening a session. No lock is held across the HTTP request.

The contract checks held/complete coherence, header-to-reference hashes/counts,
totals, difference sides and continuation.

Comparison transport permits nine seconds for the worker's eight-second budget;
the workspace response budget is ten seconds. Ordinary small observations retain
their five-second transport and six-second response budgets. Every comparison
response is capped at 64 KiB and 256 chunks before UTF-8/JSON parsing; redirects,
compression, duplicate decoded keys and nonfinite JSON numbers refuse. No two raw
eight-MiB lockfiles pass through the workspace.

## Errors and boundaries

Responses and errors are `no-store`. Invalid selection is 400, missing/foreign
scope is 404, stale source pins are 409, unconfigured/unsupported worker is 422,
and unavailable/corrupt/oversize peer evidence or a timeout is 503. Errors carry
an observation time and fixed safe messages; worker error text/source bytes are
not forwarded. A 409 does not select a newer pin or retry automatically.

The tests (`packages/workspace/tests/test_lockfile_diffs.py`) use synthetic HTTP
peers and temporary files; they cover the route's shape, selection and refusals.
The comparison was checked against a real worker in
[Lab31](../.agents/docs/agent-reports/babbage-openshell-lab-31-2026-10-13.md).
