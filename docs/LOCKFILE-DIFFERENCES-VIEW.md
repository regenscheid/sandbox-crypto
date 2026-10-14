<!-- SPDX-License-Identifier: MIT -->
# Explicit retained lockfile comparison view

The project EnvironmentPanel adds a read-only consumer of the
[worker comparison](LOCKFILE-DIFFERENCES.md), through the
[workspace proxy](LOCKFILE-DIFF-WORKSPACE.md), beside the
[observation viewer](PROVISIONING-WORKSPACE-VIEW.md). It does not
install packages, answer permissions, cancel operations, rebuild environments,
change retention or start a guest.

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
never fetches a raw lockfile. The browser selects no worker URL, credential,
permission authority or current/latest fallback.

A stale 409 clears both UI selections and stops the read. The user must refresh
history and explicitly select both sides again. A missing/evicted or incomplete
input is held or unavailable, with unknown differences rather than an empty
inventory. A genuine complete zero-difference result is labeled as equality of
the two validated retained package records, not completeness of guest inventory.

## Pages and reads

The browser checks only that a page names the selected project and environment;
the worker and the workspace server validate the comparison's contents. Pages
contain at most eight rows within the 64 KiB answer limit. Next follows the actual
returned continuation, which can consume fewer than eight rows. Previous uses a
retained stack of actual page starts. No full diff, raw 8 MiB lockfile, durable
browser cache or background diff poll is introduced.

Comparison reads are one-shot reads with a 12-second deadline, above the worker's
eight-second comparison budget; ordinary observation reads keep eight seconds.
Hide, scope change, unmount or sign-out drop the old read's reply. An unavailable
previous page stays explicitly dated and its paging controls are disabled. A new
comparison requires an explicit user action.

## Presentation

Rows show both original entries and all recorded metadata differences, including
same-version build/channel/hash changes. Conda licenses remain declared metadata
with the existing AGPL flag; pip licenses and their AGPL status remain unknown.
Source URLs and every other retained string render as escaped text, never active
links or HTML. Membership and metadata differences certify neither installed
files nor licensing, policy permission or scientific correctness.

The tests in `web/workspace/src/lib/provisioning.test.ts` cover held and
empty comparisons, pip and Conda metadata and escaped presentation. Native revision selection and paging against a real worker were checked in
[Lab31](../.agents/docs/agent-reports/babbage-openshell-lab-31-2026-10-13.md).
