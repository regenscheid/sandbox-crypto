<!-- SPDX-License-Identifier: MIT -->
# Workspace selected revision packages

The authenticated workspace can read one explicitly selected retained revision's
recorded packages and declared licenses through the worker's bounded
[package projection](REVISION-PACKAGES.md). This is a project/environment read,
including a shared or dedicated environment already in that project's canonical
registry. It adds no install, cancel, approval, restore, policy or license grant,
no active status or guest connection, and no full lockfile fallback.

## Request and exact response

`GET /api/v0/projects/{project}/environments/{environment}/provisioning/observations/revisions/{revision}/packages`
requires `pin`, the original revision-record SHA-256; `offset=0&limit=8` are optional.
Revision spelling is canonical decimal in 0..2^62, offset in 0..40,000 and limit
in 1..8. Duplicate/unknown query keys, bodies, caller `project_id` or other scope
fields refuse before a worker request. The server derives scope from the project
record and the configured worker.

The response forwards the complete strict `RevisionPackagePage`, including
project/environment, worker receipt time, original revision/raw-record pin/full
`LockfileRef`, proved header when available, complete/held status, fixed hold
reason, total, requested offset/limit, actual continuation and at most eight rows.
The DTO checks ref/header/count/row/continuation coherence.
The worker is the trusted source that binds the raw record to its retained ref.
There is no second raw revision or blob fetch to reconstruct that proof locally.

A complete known empty input remains total zero. Held input keeps unknown total
and no rows/continuation; missing, skipped, unsupported, oversized or unreadable
sources never become empty. Consumers follow the actual continuation, which can
advance by fewer rows than the requested limit. A stale original pin is 409;
there is no automatic adoption of a newer record or current-head substitution.
Missing/foreign scope is 404, unsupported observation is 422, and bounded-source
or transport uncertainty, a timeout or a damaged project record is 503. All successes and errors are `no-store`;
fixed errors include a trusted workspace receipt time without forwarding peer
error bodies or private configuration.

The page includes unchanged packages even when a comparison reports no changes.
Conda declared text and the existing worker AGPL heuristic remain recorded facts,
not a licensing decision or permission. Missing Conda license and the pip schema's
absent license stay unknown. The response is not a complete current guest
inventory, an independent install verification or a functional/scientific probe.
An explicitly selected historical revision remains historical even if the live
environment head advances.

## Shared transport

`ProvisionObserver.get_packages` reuses the existing capped observer transport:
64 KiB before parsing, strict JSON DTO validation, no redirect/decompression and a
nine-second whole-call timeout. It borrows the existing public WorkerClient observation
snapshot; endpoint/header/session metadata stays private in memory. It never
closes the shared client session. Ordinary observation calls retain their existing
five-second budget; comparison and package reads share the extended worker budget.

The route is one of the [provisioning routes](PROVISIONING-WORKSPACE.md), with
their ten-second comparison timeout. No lock is held across network work. This
read touches no activity time, executor admission, kernel state, provisioning
mutation or guest.

## Tests

The tests (`packages/workspace/tests/test_revision_packages.py`) use an isolated
synthetic HTTP peer and temporary project records, never a worker or guest. They
cover populated, empty and held forwarding, unchanged AGPL and unknown licenses,
explicit canonical selection, strict types, incoherent headers and references, redirect, oversize and error handling, and
short continuations. The pages
were checked against a real worker and browser in
[Lab32](../.agents/docs/agent-reports/babbage-openshell-lab-32-2026-10-13.md).
