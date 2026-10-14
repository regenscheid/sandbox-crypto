<!-- SPDX-License-Identifier: MIT -->
# Provisioning observations in the workspace

The project's Environments panel is the read-only GUI portion of [CP-26](specs/conda-provisioning.md). It selects only environments in the loaded canonical project registry: the shared default and retained dedicated environments, including those whose sessions are closed. An environment's provisioning queue is project-wide; a session label identifies the requester rather than a private queue.

It reads the [provisioning workspace API](PROVISIONING-WORKSPACE.md). A selected revision's recorded packages and declared licenses load explicitly and page without polling ([API](PROVISIONING-WORKSPACE.md#recorded-packages-at-a-selected-revision)), and two retained lockfiles can be compared ([comparison view](LOCKFILE-DIFFERENCES-VIEW.md)).

## What the panel shows

The compact overview shows recorded package admission, its generation and revision, current package revision and the complete bounded pending-operation summaries. Package admission is independent of VM state; this panel explicitly says that VM state is not observed. A missing gate or revision is unknown rather than inferred. An empty pending queue says only that no unfinished operations were recorded.

Selecting Request or Resolved files pins the operation's exact sequence. Request detail retains purpose, stage, outcome, reason, revision context and recorded next step as escaped text. A permission request reference is not an approval: only the operation's waiting state is described as awaiting an answer, and later stages retain the reference without claiming its current decision.

Files show the exact total and selected range, package/build/channel/source metadata, recorded hash, declared license and AGPL flag. Pages contain at most eight files. A subset is explicitly labeled; an unknown declared license is not treated as permissive. Source URLs and next-step prose are text, not executable links or instructions to this application.

Revision history pins a retained window and uses at most eight rows per page. It distinguishes managed, unmanaged and unknown origin, and displays a lockfile's recorded identity and counts when present. Missing lockfiles can be unrecorded or evicted. Probe detail pins the revision record hash; an import/version check does not certify functional capability, and a probe's null revision remains unknown attribution.

A history row's recorded operation ID offers **Load current retained operation**. This explicit request omits the sequence, returns the current retained operation state and its actual sequence, and is labeled as such. It does not reconstruct the operation's state at the selected revision. Subsequent file pages use the newly received exact sequence. No stale request silently falls back or adopts a newer sequence.

There are no approve, deny, install, cancel, rebuild, restore or policy-edit controls. Notes, raw exceptions, complete execution records, installed-license certification and raw lockfile contents are outside this view.

## Transport and ownership

[The client](../web/workspace/src/lib/provisioning.ts) builds only same-origin GET observation paths below `/api/v0/projects/{project}/environments/{environment}/provisioning/observations`. The browser supplies no worker endpoint, credential, execution context or provisioning action. Explicit selectors cover the overview, operation detail, exact-sequence file pages, retained revision windows and exact-hash probe pages.

Success responses must match their project/environment, aware receipt timestamp, selected ID/sequence/hash/range and exact DTO shape. Counters must be safe JavaScript integers; values that would round are refused. Queue entries, declared counts, page continuations, request kind and revision windows are checked before display. Receipt time is not a promise of an atomic or fresh guest snapshot.

The transport uses `no-store`, refuses redirects and bounds the entire response to 64 KiB, 256 chunks and strict UTF-8. One eight-second deadline covers headers and body. Arbitrary error bodies are canceled without parsing or retaining their contents. A module-wide single actual-read owner covers overview and selected-detail transport, including scope changes. If abort or body cancellation is ignored, that owner remains held until actual settlement: the UI can report unknown promptly, but cannot promise hard liveness or start overlapping reads.

The overview polls sequentially every three seconds while mounted and visible, with failure backoff up to thirty seconds. Selected detail is an explicit one-shot read. Visibility changes, unmount, scope selection and logout invalidate late publication; logout initiation also prevents late or newly created transport owners. A 401 clears facts, 404 clears the missing selection, 409 requires explicit re-selection, and unsupported/unavailable states retain only dated previous facts where appropriate. A departing or advancing queue entry makes an old selected queue record visibly historical. Retained history-selected operations remain explicitly pinned current-record observations, with server conflicts still refusing later drift.

## Tests

The [focused cases](../web/workspace/src/lib/provisioning.test.ts) cover canonical scope refusals, strict DTO and pin coherence, streamed-response bounds, ignored header and body aborts, hide, unmount, logout and late replies, read settlement, pagination, explicit current retained records, escaped text and honest license, probe and permission labels. The panel was checked in a browser against a real worker in [Lab30](../.agents/docs/agent-reports/babbage-openshell-lab-30-2026-10-13.md), the comparison in [Lab31](../.agents/docs/agent-reports/babbage-openshell-lab-31-2026-10-13.md) and the package pages in [Lab32](../.agents/docs/agent-reports/babbage-openshell-lab-32-2026-10-13.md).
