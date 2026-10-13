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
fields refuse before a worker request. The server derives scope from the original
authenticated login, bounded canonical project record and configured worker.

The response forwards the complete strict `RevisionPackagePage`, including
project/environment, worker receipt time, original revision/raw-record pin/full
`LockfileRef`, proved header when available, complete/held status, fixed hold
reason, total, requested offset/limit, actual continuation and at most eight rows.
The proxy compares returned scope, revision, raw pin and page selection with the
original request; the DTO checks ref/header/count/row/continuation coherence.
The worker is the trusted source that binds the raw record to its retained ref.
There is no second raw revision or blob fetch to reconstruct that proof locally.

A complete known empty input remains total zero. Held input keeps unknown total
and no rows/continuation; missing, skipped, unsupported, oversized or unreadable
sources never become empty. Consumers follow the actual continuation, which can
advance by fewer rows than the requested limit. A stale original pin is 409;
there is no automatic adoption of a newer record or current-head substitution.
Missing/foreign scope is 404, unsupported observation is 422, and bounded-source,
transport or binding uncertainty is 503. All successes and errors are `no-store`;
fixed errors include a trusted workspace receipt time without forwarding peer
error bodies or private configuration.

The page includes unchanged packages even when a comparison reports no changes.
Conda declared text and the existing worker AGPL heuristic remain recorded facts,
not a licensing decision or permission. Missing Conda license and the pip schema's
absent license stay unknown. The response is not a complete current guest
inventory, an independent install verification or a functional/scientific probe.
An explicitly selected historical revision remains historical even if the live
environment head advances.

## Shared transport, scope and actual ownership

`ProvisionObserver.get_packages` reuses the existing capped observer transport:
64 KiB before parsing, at most 256 body chunks, duplicate-key rejection at every
JSON depth, strict JSON DTO validation, no redirect/decompression and a nine-second
whole-call timeout. It borrows the existing public WorkerClient observation
snapshot; endpoint/header/session metadata stays private in memory. It never
closes the shared client session. Ordinary observation calls retain their existing
five-second budget; comparison and package reads share the extended worker budget.
No orchestrator WorkerClient, worker source, dependency or legacy method changes
are needed by this proxy.

The route uses the same `ProvisionProxy` owner and global-eight/login-two/
project-environment-one immediate admissions as every provisioning observation.
Its ten-second response budget counts the same bounded file/HTTP owners;
there is no additional package lane. Original cookie authorization, canonical
project root/file identity and exact worker/client/configuration/session binding
are rechecked after await. Registry replacement, including an identical-byte
root replacement, is conservatively unavailable. No lifecycle or registry lock
is held across network work.

Deadline/caller cancellation is not proof that admitted filesystem or request
work stopped. Existing owned cancellation/drain keeps tasks, slots and descriptors
until actual completion; repeated shutdown cancellation drains before the borrowed
WorkerClient is closed. Blocked regular-file I/O can outlast the response budget;
no hard physical liveness or crash/SIGKILL guarantee is claimed. This read touches
no activity time, executor admission, kernel state, provisioning mutation or guest.

## Validation boundary

Focused tests use an isolated synthetic HTTP peer and temporary project records,
never a worker, provisioner, live lockfile store or guest. They cover exact
populated/empty/held forwarding, unchanged AGPL and unknown licenses, explicit
canonical selection, nested duplicate/strict-type/header/ref/page refusal,
redirect/oversize/error handling, actual short continuation, late logout/root/
configuration changes, unchanged ordinary timeout and shared task/FD ownership
through deadline and repeated-cancel shutdown. Component static/affected results
are retained in the author handoff. Full root integration, frontend/browser,
actual stopped-worker no-wake acceptance and delivery CI remain separate.

Author validation on the frozen source passes 50 new focused cases, 181 selected
package/comparison/base-proxy cases and 598 affected workspace/client/provision-tool
cases. Scoped workspace/new-test Pyright, Ruff/format, locked dependency sync,
75 installed Python licenses and protocol freshness pass. Initial test lint had
one compound assertion, corrected without weakening its task/scope checks; all
initial and final outputs are retained separately. These component checks do not
replace root's full combined gates, independent final review or runtime evidence.
