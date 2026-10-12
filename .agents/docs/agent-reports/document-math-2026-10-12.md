# Document math findings browser acceptance — 2026-10-12

The production workspace GUI was checked on babbage against marked, disposable
synthetic control-store state. The D82 document-only implementation was based on
verified main `1e2221a`; the production asset hashes are retained in the
[browser evidence](document-math-browser-evidence-2026-10-12.json). The fixture ran
the actual local KaTeX 0.18.9 checker under Node 24.20.0. No model gateway, worker,
OpenShell VM or research execution was contacted.

## Fixture and observations

The reusable [`document_math.py`](../../labs/document_math.py) fixture creates
seven documents and two sessions. Immutable checks retain exact revision/hash,
block/span, renderer configuration and historical block-author proof. The browser
test also delayed one evidence response by two seconds to exercise selection races.

| Check | Observed result |
| --- | --- |
| Agent rejection | Broken agent TeX produced an open `math` finding of severity `warn`, with the exact revision, span, renderer error and suggested remedy. The card explicitly said that no reviewer model was used. |
| USER exclusion | The same broken formula in a USER-owned document had a rejected mechanical observation with USER provenance and no finding. |
| Historical owner | After a USER changed another paragraph, the checked source was revision 2 and the broken block's proven agent owner remained revision 1. Its finding retained zero reflags. |
| Accepted formula | Ordinary source repair led to `confirmed fixed`, attributed to `platform:math`, with the reason that the configured renderer accepted the current formula. |
| Removed formula | Removing the formula led to `confirmed fixed`; history explicitly distinguished removal from a renderer-accepted repair. |
| Unavailable renderer | The latest attempt showed one unavailable observation and no rejection verdict. The existing historical finding remained open. |
| Bounded reflags | Two distinct failing patches produced revision 3, two reflags and `unaddressed`, displaying the latest formula's error. |
| Delayed selection | The delayed first check never replaced the subsequently selected unavailable check. |
| Session change | Navigating to session `b` during a pending evidence read showed no session `a` findings and no selected evidence. The project-wide check inventory intentionally remained visible. |
| Retained USER decision | A dismissal choice and explanation survived reload. A concurrent USER decision advanced the finding from revision 1 to 2; the stale draft was refused as a conflict and retained. Explicitly selecting the current revision then saving produced revision 3, `withdrawn`, attributed to USER. |
| Display/source separation | The document showed a marked automatic repair and exposed the original TeX. A canonical readonly source check confirmed that the stored formula remained unchanged. |
| Browser diagnostics | No warning or error console entries. |

Recorded screenshots show the [mechanical finding and pinned anchor](document-math-initial-2026-10-12.jpg),
the [retained stale USER draft](document-math-stale-draft-2026-10-12.jpg),
the [complete USER decision history](document-math-user-history-2026-10-12.jpg)
and the [marked repair with original TeX](document-math-display-2026-10-12.jpg).
The evidence JSON includes observed browser text, exact mechanical observations,
canonical final findings and retained source revisions, with no authentication tokens.

## Validation boundary

This checks document rendering, finding/evidence navigation, provenance, immutable
history, selection races and USER decisions in the real browser. Separate focused
and adversarial tests cover queue admission, stale results, configuration changes,
bounded retries, migration, cancellation ownership and closing delivery. Final
combined local and CI results are recorded in the accompanying status and PR.

This fixture does not run a live author turn or terminal reviewer, validate
real-model judgment, enable idle scheduling, or extend D82 to comments and ledger
fields. It does not enable worker document publication or filesystem replacement.

## Cleanup

The browser tab was closed, the transient loopback server stopped, the marked
synthetic state directory removed and port 35997 verified free. No persistent
service, provider configuration or worker state was created.
