<!-- SPDX-License-Identifier: MIT -->
# Selected retained lockfile differences

The worker compares the packages of two retained environment revisions, read-only,
for the package comparison of [CP-26](specs/conda-provisioning.md). It sits beside the
other [provisioning observations](PROVISIONING-OBSERVATIONS.md). The workspace passes
it on through a [proxy](LOCKFILE-DIFF-WORKSPACE.md), and the GUI shows it in
[selected pages](LOCKFILE-DIFFERENCES-VIEW.md). It neither changes packages nor
answers permissions, restores an environment, extends retention or wakes a guest.

`GET /v1/environments/{environment_id}/provisioning/observations/lockfile-diff`
requires `project_id`, `before`, `before_pin`, `after`, `after_pin` and optionally
`offset=0&limit=8`. Each pin is the exact original revision-record SHA-256 shown
by the bounded revision observer. Both revisions are explicit; there is no
latest, nearest retained lockfile or current-operation fallback. Indices use
canonical decimal spelling; revisions are at most 2^62, offset at most 80,000 and
limit 1–8. Existing bearer/project/managed-environment creation checks apply even
for the pool operator. Unknown, deleted and foreign environments remain absent.

The response has exact scope, receipt time, both selected original record pins
and lockfile references, verified lockfile headers where available, and either:

- `status=complete`, exact added/removed/changed totals, header changes, bounded
  difference rows and an explicit continuation; or
- `status=held`, bounded source-specific reasons, unknown totals and no comparison
  rows. Unrecorded, missing, unsupported, skipped and over-limit inputs never
  become empty package inventories.

A true validated empty lockfile (`conda=[]`, `pip=[]`, `skipped=0`) can be compared
as an observed empty set. Neither that nor a skipped-zero source proves the
untrusted guest's inventory complete or its packages correct. Unknown optional
metadata remains unknown.

## Exact sources and limits

The observation pair selector captures both bounded revision
headers and their exact raw-record hashes in one SQLite read snapshot. It checks
original ref columns, timestamps and record types as the revision observer does;
no full retained revision reaches Python. Each selected revision is at most 2 MiB.
SQL is released before file work and rechecked afterward. A probe/ref/history
change can therefore invalidate the original pin even when the revision number
stays the same: the request refuses 409 instead of selecting newer evidence.

Only the two recorded content-addressed blob paths are opened under the configured
worker blob root. Directory components below that root and the root's final
component use no-follow directory descriptors; each leaf must be a regular file,
owned by the worker UID, with one link and the exact recorded size. Held file and
canonical path identity/size/mtime/ctime are checked around reads. Actual raw bytes
are hashed separately against each recorded SHA-256. A final scope and exact-pair
recheck precedes a response. This proves explicit observation boundaries, not a
continuous atomic filesystem/database snapshot or immunity to arbitrary host
writes after the final check.

The fixed-format streaming reader decodes one bounded header value or package
entry at a time. It does not call full `read_lockfile`, full `Lockfile` validation,
`Store.revision_entry/page`, a provisioner or an executor. Limits are:

| Input/work | Bound |
| --- | --- |
| Raw bytes | 8 MiB per blob; two blobs / 16 MiB total |
| Read/refill | 8 KiB; entry at most 16 KiB |
| Sections | 20,000 entries each for Conda and pip, per source |
| JSON | Seven exact top-level keys; duplicate decoded keys refused recursively; depth at most four; numeric tokens at most 20 bytes and integer-only |
| Header value | 2 KiB raw; platform at most 128 characters |
| Entry string | 2 KiB UTF-8 per string, in addition to existing field patterns |
| Index accounting | 16 MiB across both sources, charging actual UTF-8 key/alias bytes plus fixed per-entry metadata |
| Display | At most eight rows and 64 KiB for the entire response |

The index quota is a serialized accounting bound, **not an exact Python heap/RSS
limit**. Python object, sorting-reference and interpreter overhead remain. Large
but otherwise valid historical inputs may be held by these conservative
limits; they are not modified or deleted. A supported source has all seven current
format fields; omitted headers or an unsupported integer format version hold.
Malformed scalar types, JSON, duplicate keys/identities, ref counts, bytes or
hashes refuse fixed 503 without exposing raw error/source text. Stored source
URLs/channels must already match the existing credential-free canonical spelling;
this reader refuses unsafe forms rather than sanitizing them into different proof.
The unchanged old lockfile download route is not used as a stronger pin/byte proof.

## Identity and licenses

Conda and pip have separate namespaces. Conda identity is the retained name;
duplicates and case-colliding names refuse. Pip identity is normalized name plus
`prefix`/`user` location; punctuation/case collisions in the same location refuse,
and user/prefix entries remain separate. This is recorded membership comparison,
not proof of a particular filesystem mutation.

All retained fields are compared, including same-version Conda build/channel/
subdir/archive/SHA-256/MD5/license differences, pip installer/source/hash/license changes
and original name spelling. Rows retain both validated original entries and exact
changed-field names. Python/platform differences are separate bounded header
changes. Unknown-to-known metadata is a recorded difference, not a claimed install.

Conda license text is declared metadata. Its AGPL flag uses the existing bounded
`is_agpl` heuristic; it is no new policy decision or licensing certification.
Absent license text means unknown AGPL status. A pip entry's license is the
declaration its snapshot recorded ([pip license declarations](PIP-LOCKFILE-LICENSES.md));
an entry without one is unknown. No license is borrowed from another operation or
a similarly named Conda package.

## Stateless pages and owned work

Each explicit page revalidates/recomputes the same two source pins. There is no
durable diff table, cache, automatic polling or retention protection. Consumers
must follow actual continuation cursors rather than assume eight rows were consumed. If normal
retention removes a source, later pages hold/refuse rather than quietly switching.
The finite indexes support sorting/merge; only selected rows are materialized,
while all differences are counted for exact totals. No full diff list is retained.

Admission is immediate: global two comparisons and one per environment, before
SQL selection. File/hash/parse work runs in a private two-thread lane. The caller
has an asynchronous eight-second deadline, effective when the event loop regains
control; a timeout/disconnect does not terminate a
blocked regular-file read. Its task/descriptors/lane remain owned until actual
completion; repeated cancellation and app cleanup drain before the pool closes.
There is no hard I/O liveness or SIGKILL durability guarantee. Worker
shutdown must keep the Store/blob owners alive through app cleanup. The
workspace proxy waits 10 seconds for a comparison, against 6 for the other
observations.

Not built: operator-answer APIs, lockfile retention tied to result lineage, and rebuilds.
