# MAIN explanation math

D82 math checks (LD-53 to LD-55) cover the durable explanation field of session MAIN
`fix` and `rebut` responses to review-origin findings. It extends
[document](MATH-CHECKS.md) and [comment](COMMENT-MATH.md) checks using the same
isolated renderer, quotas, immutable attempts, closing rounds and saved-run ACK.
Renderer configuration remains operator-owned and defaults to unavailable.

Reviewer assessments, USER responses, platform prose and mechanical diagnostic
prose are excluded. A mechanical finding cannot become another prose source.
Ledger fields other than statements ([ledger](LEDGER.md)), model repair proposals,
idle dispatch and automatic compaction are not built. This checks renderability, not scientific truth.

## Source and ordinary correction

The immutable original scientific transition identifies the source by finding ID,
original response revision and literal `explanation` field. Its action, evidence,
original explanation and original actor remain unchanged. A separate append-only
body history records every correction, exact SHA-256, stable block/span IDs and
last-changing MAIN actor. Unchanged blocks retain their proved owner. Ordinary
Markdown uses the bounded [project macros](PROJECT-MACROS.md), with definitions
from the original finding's immutable document anchors overriding them when all
anchors agree on one effective context. The source never borrows a document
parent FK or read authority. Missing, conflicting or unproved anchor contexts
hold mechanical checks as unavailable. Project-only diagnostic display carries
an explicit ambiguity warning and is not authoritative check evidence.

`review_response_read` returns bounded current or historical text with its body
revision/hash. `review_response_edit` requires that original source identity,
current scientific finding revision, exact current body revision/hash and an
operation ID. Only the same session MAIN may correct this agent-authored source;
delegated, foreign-session, USER and reviewer callers receive no new editing
capability. An exact replay remains idempotent across a later MAIN run. Changed
payload under the same operation ID conflicts. Body, queue target and receipt
commit atomically; parsing stays outside the write lock.

Correcting an explanation never creates a new fix/rebut, changes the scientific
finding revision/status, or confirms its claimed fix. Existing explicit USER
finding decisions keep their own exact CAS. The workspace shows original versus
current explanation, both actors, body revisions and historical bodies through
a scoped read-only route. It adds no USER body-write route.

## Checks, confirmation and delivery

A check records the real finding/response source and exact body revision/hash and
the project setting revision it used. Its original document anchors (the finding's
anchors at the explanation's decision revision) and resolved effective context are
derived when read, with no fabricated document revision. Original anchors survive body
corrections. Each new body revision also records the project setting revision it was
saved under; legacy history without one stays unknown. Admission, load, current-result
projection and commit fence that canonical parent. Explanation check/evidence/queue views are
session-private; ordinary document/comment check visibility stays project-wide.

Every saved body revision is durable backlog. The shared worker admits at most
two active attempts, processes the existing bounded span batches, and retains
explicit pending/held/unavailable results. Canonical saves remain usable when
check history quotas fill. Legacy immutable MAIN responses are discovered two
at a time; unknown/malformed author proof gets a visible durable held discovery
record and cannot starve healthy document/comment work. Identity/parser bounds
hold derived checks rather than invent ownership or formula removal.

Accepted current input or complete parsed removal confirms only the child math
finding. Stale/unavailable/unknown evidence cannot confirm, reflag or consume a
closing opportunity. Dependency changes refresh evidence; only a new proved
failing block edit spends the two-reflag budget. Mechanical notices share the
existing maximum two closing response rounds with reviewer findings and finalize
only after successful model delivery, durable run save and exact-current ACK.
No additional polling model or idle scheduler is introduced.

A confirmation snapshot also pins the current explanation body revision/hash,
effective context identity and project setting revision/hash. Even if the
scientific finding CAS is unchanged, a correction or setting change makes an
older reviewer's decision stale. A fresh reviewer must read the corrected pinned
source and existing original/fix evidence before deciding. Held or unknown
explanation context permits an inconclusive outcome but no reviewer confirmation
or rejection. This guard is independent of whole-window coverage; explicit USER
decisions retain their existing authority. Scientific confirmation and mechanical
child resolution remain independent.

## Validation and acceptance boundary

Focused fixtures cover source authority, cross-run replay, parent independence,
body/reviewer races, immutable original history, the initial empty-macro context,
failed-edit budgets, unavailable retries, stale closing ACK, receipt/queue rollback,
write-time checks of the response tables and unknown legacy isolation. Workspace API and read-only rendering fixtures
cover historical scope and original/current attribution. Checked in the browser in
[finding prose math acceptance](../.agents/docs/agent-reports/finding-prose-math-2026-10-12.md).

`.agents/labs/response_math.py` builds marked disposable synthetic state using
actual configured local KaTeX without worker or model calls. It provides `main-fix`,
`confirm` and `audit` actions for live refresh checks.

[Project macros](PROJECT-MACROS.md) add exact project/anchored contexts,
ambiguous-context holds and stale setting confirmation guards. Their lazy
current-head refresh keeps source ownership, failed-block reflag budgets and the
shared closing/save-ACK boundary; it adds no idle scheduler, renderer
auto-configuration or other eligible prose.
