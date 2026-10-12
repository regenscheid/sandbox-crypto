# Finding explanation math acceptance — 2026-10-12

The production workspace GUI and actual local KaTeX passed against marked,
disposable MAIN fix/rebut explanation sources. Source corrections preserve the
original scientific response, evidence and immutable history. They resolve only
the associated mechanical finding; a fresh, exact-body reviewer decision is
required to confirm the scientific parent.

## Exact source and validation

- Signed source: `f7bb0a3caf1ac7abfbb45766d26314af8c3aad94`, containing actual
  merged main `744d1b51c89c1b9ddccf1f6274f8749bc078564c` (PR #88).
- [Disposable helper](../../labs/response_math.py) SHA-256:
  `7bc82ebf57e01b7905327dabdd2cc558896b94374ea97db06c4f8ec08a92d94b`.
- Separate adversarial review cleared the exact source and helper, including
  old populated queue migration, malformed legacy source isolation, ordinary
  edit authority, session-private historical/current reads and queues, exact
  operation replay, stale/fresh reviewer pins and unchanged scientific CAS.
- A corrupt 20 MiB saved edit receipt is refused before JSON parsing; the
  independent probe measured 69,180 bytes of peak Python allocation. Legitimate
  replay of immutable body revision 2 still works after revision 3 and a new
  MAIN run. This correction is restricted to the new response edit receipt.
- Final Python gates passed: **9,730 tests**, **43 opt-in skips**, two existing
  Pydantic serializer warnings from forged legacy artifact negative fixtures;
  326.28 seconds. Locked sync, Ruff formatting/lint, strict Pyright, Python
  licenses and generated protocol checks passed in the same completed run.
- **443 web tests**, **24 explicit post-build math checks**, locked npm install
  with scripts disabled, lock/installed licenses, Svelte/TypeScript checks and
  both production builds passed. These source-identical web results were reused
  from combined source `c509bc1`; the final correction changed only the response
  receipt implementation and its regression. All 71 built assets matched after
  browser acceptance.

The superseded `c509bc1` Python run was deliberately interrupted after 8,731
passes and 33 skips. It is not counted as a completed gate. The final run above
completed successfully on the corrected source. Fresh PR CI remains a merge gate.

## Browser and durable-state evidence

The root-owned fixture created exactly six original explanation sources. It
required pending/open, accepted/confirmed-fixed, removed/confirmed-fixed,
unavailable/open, unaddressed and empty-macros/open mechanical outcomes.
Only the two proved failing edits spent reflags; the unaddressed child retained
exactly two and every other initial child retained zero. A document macro
definition did not supply macros to explanation Markdown. Reviewer statement
math did not become a new explanation source.

The GUI displayed the original response and its independent current explanation
body, original author `original-main` and correcting author `correcting-main`.
“Read previous explanation body” returned the exact original revision and
“Show current explanation body” restored the correction. Refresh after an
ordinary MAIN edit invalidated the old displayed source generation and loaded
the new body while preserving original decision history.

For the initially pending rebuttal, ordinary correction to `$y^2$` and actual
KaTeX acceptance confirmed the math child while the scientific parent remained
open at revision 2. For the claimed fix, the pre-edit reviewer snapshot was
refused with “explanation changed”; a separately dispatched reviewer with the
current body pin confirmed the scientific parent at revision 3. That review
remained explicitly **inconclusive overall**, because other fixture findings
were unresolved and the fixture made no complete review claim.

Session `b` showed the shared project document check but no explanation checks,
review runs or findings from session `a`. Independent read-only SQLite inspection
verified all 12 immutable explanation body hashes, 13 finished math jobs, empty
foreign-key diagnostics, original response action/reason/body history and the
independent parent revisions. Browser console and server warning/error records
were empty.

- [Original explanation history](finding-prose-original-2026-10-12.jpg)
- [Corrected explanation and attribution](finding-prose-current-2026-10-12.jpg)
- [Fresh scientific confirmation](finding-prose-confirmed-2026-10-12.jpg)
- [Other-session visibility](finding-prose-other-session-2026-10-12.jpg)

## Retained archive and teardown

[Evidence archive](finding-prose-math-evidence-2026-10-12.json): 536,469 bytes,
SHA-256 `1923c2959330d39f2328d7f2117fcb949eafcf4e508357fc8c5be11e37ab45d6`.
It retains 19 exact UTF-8 records, each with byte length and SHA-256, plus hashes
for the four screenshots. Records include original/current source audits, GUI
snapshots, independent database inspection, source/helper/asset freeze, complete
final gate logs and cleanup. All 23 exported files and the final archive were
checked against the actual disposable workspace login value; none contained it.
Raw service journals, login URLs and SQLite databases were not exported.

Complete teardown was independently verified at **15:37:06.734290 UTC**: browser
tab closed, transient `sandbox-response-math-browser.service` inactive, port
35999 free, marked fixture and private login file removed. Product, tests,
helper and all built assets remained unchanged after acceptance.

## Scope limits

This accepts MAIN-authored fix/rebut explanation source checks and ordinary
corrections on review-origin findings. Ledger fields, reviewer/USER/platform
prose, model repair proposals, idle dispatch and automatic history compaction
remain open. The fixture used a local renderer and scripted reviewer decisions;
it contacted no model, worker or VM and does not establish real-model judgment.
