<!-- SPDX-License-Identifier: MIT -->
# Selected revision packages and declared licenses

The worker provides a bounded, read-only view of one revision's packages and
their declared licenses, for the license part of
[CP-26](specs/conda-provisioning.md). Unlike
[lockfile differences](LOCKFILE-DIFFERENCES.md), it includes unchanged
packages and the recorded packages of base or unmanaged revisions with no producing
operation. The workspace passes it on ([workspace](REVISION-PACKAGES-WORKSPACE.md)),
and its environment panel shows it. It does not install, approve, restore, wake
an environment, change retention or certify its installed files or licenses.

## Explicit source and response

`GET /v1/environments/{environment_id}/provisioning/observations/revisions/{revision}/packages`
requires `project_id` and `pin`, the exact original revision-record SHA-256 from
the bounded revision observer. Optional `offset=0&limit=8` selects a stateless
page. Revisions use canonical decimal spelling in 0..2^62, offsets 0..40,000 and
limits 1..8; pins are 64 lowercase hexadecimal characters. Duplicate or unknown
query keys refuse. Existing bearer/project/managed-environment creation checks
apply even for the pool operator. Deleted/foreign environments and absent
revision records remain absent. No session-specific queue or new authority is
implied by a project-wide environment read.

`contracts.revision_packages.RevisionPackagePage` contains authenticated scope,
worker receipt time, exact `selection` (revision/raw pin/full LockfileRef), and:

- `status=complete`: a proved header, total equal to its validated Conda plus pip
  counts, at most eight `packages`, and the actual `next_offset`; or
- `status=held`: one fixed reason, unknown total, no package rows or continuation,
  and a proved header only when available. Reasons are `lockfile_unrecorded`,
  `blob_missing`, `unsupported_format`, `skipped_entries` and `input_limit`.

An exactly validated empty lockfile is complete with total zero. A missing,
evicted, unsupported, skipped or oversized input never becomes an empty
inventory. Malformed/ambiguous bytes, types, duplicate keys/identities, unsafe
URLs/channels, hash/ref mismatch or path replacement refuse fixed 503. A stale
raw-record pin refuses 409; the reader never selects the latest/nearest record.
Advancing the current environment head does not itself change an explicitly
selected historical source. All responses use `Cache-Control: no-store`.

Each `RevisionPackage` has `ecosystem`, canonical identity `name`, nullable
`location`, the validated original `package` (CondaPackage or PipDistribution),
and `license` (declared text and nullable AGPL flag). Conda identity is its exact
retained name; pip uses normalized name plus prefix/user location, retaining the
original name in `package`. Ordering is ecosystem/location/name, as in the
comparison reader. Case/punctuation identity collisions refuse rather than
silently merge. All existing package metadata is retained on the selected row;
there is no invented producing-operation or revision-era file-set reconstruction.

Conda license text is declared retained metadata; the flag uses the existing
bounded AGPL heuristic. Missing license means unknown AGPL status. A pip
entry's license is the declaration its snapshot recorded
([pip license declarations](PIP-LOCKFILE-LICENSES.md)). An older entry, or one
with a missing or ambiguous declaration, has none, and its license and AGPL flag
stay unknown; no license is borrowed from a similarly named Conda package or
another operation.
These facts do not make a licensing decision, grant permission, establish a
complete guest inventory, or supply a functional/scientific witness.

## Shared bounded proof and actual work ownership

`ProvisionObservations.selected_revision` selects one bounded canonical header
and raw hash in one SQLite read snapshot, using the same helper as pair
selection. The snapshot closes before file work. The final exact scope/source
recheck precedes response construction. It does not fetch a full revision or
call a provisioner/executor/backend.

The page shares the comparison's source scanner, nofollow regular
blob descriptors, streamed raw-byte SHA/ref/header/count proof, canonical path
and held-file pre/post checks, and bounded entry materialization. One blob is
scanned once; there is no fabricated empty comparison source, full Lockfile
decode, full inventory response, persistent table or cache. Limits remain 8 MiB
raw bytes, 8 KiB refill, 16 KiB per entry, 20,000 entries per ecosystem, 16 MiB
serialized index accounting and 64 KiB for the entire response. The index quota
is not an exact Python heap/RSS bound. Stored strings and fixed-format JSON use
the same conservative strict/depth/duplicate/type/UTF-8 limits as comparison.

`LockfileDiffs.read_packages` and `read` share the **same instance**, private
two-thread pool, task set and immediate global-two/per-environment-one admission.
One inventory and one comparison cannot acquire separate owners for the same
environment. Capacity refusal precedes SQL selection. Both kinds use the same
eight-second asynchronous deadline and final recheck. Cancellation/disconnect
does not stop blocked regular-file I/O: actual tasks, slots and descriptors stay
owned through completion, and repeated shutdown cancellation drains them before
pool close. No hard blocked-I/O liveness or SIGKILL guarantee is claimed. Existing
Store/blob owners must remain alive through cleanup. Heartbeats, guest state and
ordinary activity are untouched; a stopped environment's retained sources remain
readable without a guest connection.

Consumers must follow actual continuations, preserve original revision/ref pins,
and distinguish held/unavailable from known empty. A later retention change may
hold/refuse a page. Shared worker source proof is an observed boundary, not a
continuous atomic database/filesystem snapshot. The legacy raw lockfile route
is unchanged and is not a stronger source proof.

