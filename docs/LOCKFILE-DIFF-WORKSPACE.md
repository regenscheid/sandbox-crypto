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

## Authority and owned reads

This route uses the same [provisioning proxy](PROVISIONING-WORKSPACE.md): original
workspace-cookie authentication, bounded no-follow canonical project registry,
retained directory/file descriptors, exact recorded project environment membership
and the operator-configured borrowed WorkerClient transport. The project is
server-owned, not an accepted query field. Closed dedicated environments can
remain recorded history without reopening a session. No lifecycle or filesystem
lock is held across the HTTP request.

After the worker returns, the proxy validates the exact requested scope,
before/after revision and raw-record pin, offset and limit. The contract checks
held/complete coherence, header-to-reference hashes/counts, totals, difference
sides and continuation. Fresh bounded registry reads and canonical pathname/file
identity proof, original-cookie validity and the original client/configuration/
shared-session binding are rechecked before returning. A concurrent replacement,
close/reopen or login change can conservatively invalidate the observation.
These are receipt-time proofs, not a continuous atomic host/worker snapshot.

The comparison shares immediate admission with ordinary provisioning reads:
global eight, two per login and one per project/environment. It has no second lane
or queue. The existing private two-thread registry lane, held descriptors and
actual request owner remain counted until work really drains, including after
repeated caller cancellation, timeout or shutdown. The borrowed shared HTTP
session is never closed by the observer, and proxy cleanup precedes its owner's
normal client shutdown.

Comparison transport permits nine seconds for the worker's eight-second budget;
the workspace response budget is ten seconds including registry work. Ordinary
small observations retain their existing five-second transport and six-second
response budgets. Every comparison response is still capped at 64 KiB and 256
chunks before UTF-8/JSON parsing; redirects, compression, duplicate decoded keys
and nonfinite JSON numbers refuse. The all-or-refuse output cap remains 64 KiB.
No two raw eight-MiB lockfiles pass through this proxy.

A deadline or browser disconnect cannot forcibly interrupt blocked regular-file
I/O. Actual file/request tasks and descriptors remain owned until completion;
shutdown drains them before shared-client closure. No hard I/O liveness guarantee
or remote cancellation proof is added.

## Errors and boundaries

Responses and errors are `no-store`. Invalid selection is 400, missing/foreign
scope is 404, stale source pins are 409, unconfigured/unsupported worker is 422,
and unavailable/corrupt/oversize or incoherent peer evidence is 503. Errors carry
an observation time and fixed safe messages; worker error text/source bytes are
not forwarded. A 409 does not select a newer pin or retry automatically.

This adds one read-only route and a fixed-budget transport method. It changes no
worker/contracts, execution/provisioning admission, approvals, packages, wake,
cache, source retention, controller, GUI or dependency behavior. The first viewer's
Lab30 source/runtime acceptance is separate and remains frozen. Local synthetic
HTTP and temporary-file tests establish proxy shape, authority and ownership;
combined gates and real worker/native-browser acceptance remain root-owned.

The author checks passed 52 new comparison cases plus 79 existing provisioning
cases (131 scoped), and 548 affected workspace/kernel-client/provision-tool cases.
Workspace source and the new test passed typing; scoped lint/format, locked
dependencies and all 75 installed license checks passed. The initial two fixture
failures compared equivalent `+00:00` and `Z` timestamp spellings, then used the
contract's canonical serialization. A separate invalid affected-test filename
invocation ran no tests and remains preserved. These local outcomes do not
replace root's future integrated full gates or runtime acceptance.
