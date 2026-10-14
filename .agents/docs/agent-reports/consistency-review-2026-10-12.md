# Consistency review browser acceptance — 2026-10-12

The independently implemented reviewer and the production workspace GUI were checked
on babbage against a disposable, marked control-store fixture. The source was the
reviewed consistency implementation (`122a844`), integrated with verified main
`61105bf` at `dceb46a`, then corrected confirmation scope (`3d2a551`, integrated as `fe0801e`). All models were scripted. No provider network requests,
worker calls, research code or OpenShell VMs were used in this check.

## Fixture and observations

The reusable script is [`consistency_review.py`](../../labs/consistency_review.py).
It creates sessions `a` and `b`, a document whose first revision says 16, and pinned
execution evidence recording 15. A scripted constrained reviewer reads the complete
manifest and raises a contradiction with document and execution anchors. A later
checkpoint has no reviewer alias and ends explicitly failed.

| Check | Observed result |
| --- | --- |
| Immutable evidence | After the head changed to revision 2 and count 15, the original review preview still showed revision 1 and count 16 with its original digest. |
| Unavailable reviewer | The GUI showed failed, unavailable alias/model, zero model calls and the explicit unreviewed-window message. |
| Delayed selection | Synthetic two-second audit and evidence responses from the first review never appeared under the subsequently selected checkpoint. |
| Session boundary | Navigating to session `b` while evidence was pending showed no reviews or findings there. Returning to `a` reset selection and restored only its records. |
| Manual request | The GUI queued a request for the next live safe point and explicitly said an idle session does not start a model turn. |
| Retained claim | Decision, explanation and immutable revision pins survived reload. Saving advanced the finding to revision 2, **claimed fixed**, with an empty new draft. |
| Confirmation | The scripted reviewer read the pinned original and corrected evidence and confirmed the exact claim. Finding revision 3 was **confirmed fixed**, attributed to its confirmation review. |
| Concurrent decision | A separate USER reopened the finding at revision 4. The GUI retained its revision-3 dismissal draft; a stale submission was refused as a conflict without losing the draft. |
| Explicit adoption | Choosing “Use current finding revision” then saving produced revision 5, withdrawn, attributed to USER. History retained all five distinct transitions. |
| Browser diagnostics | No warning or error console entries. |

The initial confirmation run was **inconclusive**. It both lacked an explicit checked-item
record and counted unrelated pending author feed against confirmation coverage. The
initial records (archived outside git, sha256 `b9beff1a6515`) and
initial coverage view (archived outside git, sha256 `9cc0b182410e`) preserve that
observation. Independent review identified the latter as a conservative scope bug.

The correction measures confirmation coverage against all exact target, original and
response evidence, independently of unconsumed author feed. The final fixture records
an explicit checked claim, and its confirmation run **passed** in the GUI, labeled
“Pinned finding and fix scope · no author feed consumed.” Its finding was confirmed
fixed. The unrelated checkpoint remained failed and its exact author-work window
`watermark=2, retry_end=3, failures=1` was unchanged. Regression fixtures still refuse a
pass when a checked record is absent, a target is omitted or granted evidence is incomplete.

The recorded evidence (archived outside git, sha256 `c0af16531831`) combines token-free
control-store records and observed browser snapshots. Screenshots show the
claimed fix (archived outside git, sha256 `65c4883217a2`),
confirmation view (archived outside git, sha256 `58c75ab56859`),
retained stale draft (archived outside git, sha256 `9e59e0036579`) and
complete attribution history (archived outside git, sha256 `5f5e5866cba9`).

## Validation and limits

Final integrated Python validation passed **9,162 tests**, with 43 opt-in skips, in
287.95 seconds. Web validation passed **429 tests**,
Svelte/TypeScript with zero errors or warnings, both production builds and locked and
installed license checks. Ruff, strict pyright, generated-protocol checks and all 75
Python dependency license checks passed.

Independent adversarial review covered evidence grants and full read coverage,
expired-owner fencing, token caps, author-work retry windows, bounded confirmation
fallback, monotonic severity, deferred reflags during response confirmation,
authenticated same-origin USER decisions and cross-session denials. This browser
check adds actual rendering, interaction, delayed-response and retained-draft evidence.

This does not validate real-model judgment, a live message stream, automatic idle
wake-up, shared monetary accounting, immutable shell artifact inventories, or D82
math finding integration. Those remain separate implementation work.

## Cleanup

Both browser tabs were closed, the transient loopback server stopped, all three
marked synthetic fixture directories removed, and port 35997 verified free. No
persistent configuration was created.
