<!-- SPDX-License-Identifier: MIT -->
# Explicit retained lockfile comparison view

The project EnvironmentPanel adds a read-only consumer of the
[worker comparison](LOCKFILE-DIFFERENCES.md). This is a separate CP-26 slice from
its [existing observation viewer](PROVISIONING-WORKSPACE-VIEW.md). It does not
install packages, answer permissions, cancel operations, rebuild environments,
change retention or start a guest. Root integration and actual browser/worker
acceptance are separate from this frontend milestone.

## Selecting exact sources

Open revision history for the selected canonical project environment and choose
**Use as before** and **Use as after** on explicit retained rows. Selections can
come from different history pages; selecting the same revision twice is valid.
The selector retains each original revision number, raw-record SHA-256 and full
recorded lockfile reference. It does not replace these pins when an overview
poll reports a newer current revision. The project/environment key fences the
selector as well as displayed comparison data across scope changes.

**Compare selected lockfiles** makes one explicit request to
`GET /api/v0/projects/{project}/environments/{environment}/provisioning/observations/lockfile-diff`
with `before`, `before_pin`, `after`, `after_pin`, `offset` and `limit=8`. It
never fetches a raw lockfile. Backend scope/authentication and the matching proxy
are independent source prerequisites; the browser selects no worker URL,
credential, permission authority or current/latest fallback.

A stale 409 clears both UI selections and stops the read. The user must refresh
history and explicitly select both sides again. A missing/evicted or incomplete
input is held or unavailable, with unknown differences rather than an empty
inventory. A genuine complete zero-difference result is labeled as equality of
the two validated retained package records, not completeness of guest inventory.

## Bounded validation and pages

The consumer checks exact scope, aware receipt timestamp, allowlisted keys,
JavaScript safe integers, both selected raw pins and lockfile refs, and every
available header's hash/size/count binding even for held responses. Complete
responses require both headers and skipped-zero inputs. Added/removed/changed
totals must sum and agree with aggregate entry-count arithmetic. Rows validate
all Conda and pip fields, exact changed-field names, side/change agreement,
normalized pip name plus prefix/user identity, declared license bindings and
strict tuple ordering. Missing optional fields on an absent side have the same
null semantics as the worker's comparison.

Pages contain at most eight rows within the existing 64 KiB streamed body cap
and 256-chunk cap. Next follows the actual returned continuation, which can
consume fewer than eight rows. Previous uses a retained stack of actual page
starts. Later complete pages must preserve the selected headers, header changes
and global totals; next-page keys must follow the previous page. No full diff,
raw 8 MiB lockfile, durable browser cache or background diff poll is introduced.

As in the original viewer, parsing uses the platform JSON parser followed by
strict DTO validation. It does not detect duplicate raw JSON keys before that
parser normalizes them. The separately reviewed worker/proxy refuse ambiguous
retained/upstream JSON and generate canonical response envelopes. This frontend
makes no independent raw-byte hashing claim: displayed source proof is the
backend's verified observation at its named boundaries.

## Ownership and presentation

Comparison reads have a 12-second whole transport/body and view deadline, above
the worker's eight-second comparison budget and the comparison proxy/client
budgets. Ordinary observation reads retain their existing eight-second deadline.
All summary/detail/history/comparison reads use the same actual global fetch/body
owner. Timeout, hide, scope change, unmount or logout abort the old generation;
ignored abort/cancel still retains physical ownership until settlement. One-shot
comparison owners stop after their actual attempt settles, including hidden or
expired attempts. An unavailable previous page stays explicitly dated and its
paging controls are disabled. A new comparison requires an explicit user action.
There is no hard network/I/O liveness guarantee when a browser implementation
ignores cancellation.

Rows show both original entries and all recorded metadata differences, including
same-version build/channel/hash changes. Conda licenses remain declared metadata
with the existing AGPL flag; pip licenses and their AGPL status remain unknown.
Source URLs and every other retained string render as escaped text, never active
links or HTML. Membership and metadata differences certify neither installed
files nor licensing, policy permission or scientific correctness.

Focused tests cover pin/header/held/totals/type/row coherence, actual short-page
continuations and cross-page facts, pip/Conda metadata and escaped presentation,
comparison-specific deadlines, shared ignored-abort/body-cancel ownership and
one-shot visibility settlement. Native revision selection, paging and real
backend/source proofs require root-owned acceptance after integration.

Local validation passed 81 focused cases, all 775 web cases, Svelte/TypeScript
with zero errors or warnings, both production builds, 24 post-build math cases,
locked npm/uv installation and all 94 locked/69 installed npm license checks.
The existing large GUI chunk advisory remains. Independent source probes also
accepted 17 pages generated by the actual worker comparator across six small
synthetic cases and refused damaged pins/coherence. These are local component
proofs; no actual browser, worker service, VM, provider or external model was run
by the frontend author or reviewer for this milestone.
