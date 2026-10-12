# MAIN explanation math

This D82/LD-53–55 slice checks the durable explanation field of session MAIN
`fix` and `rebut` responses to review-origin findings. It extends
[document](MATH-CHECKS.md) and [comment](COMMENT-MATH.md) checks using the same
isolated renderer, quotas, immutable attempts, closing rounds and saved-run ACK.
Renderer configuration remains operator-owned and defaults to unavailable.

Reviewer assessments, USER responses, platform prose and mechanical diagnostic
prose are excluded. A mechanical finding cannot become another prose source.
Ledger fields, model repair proposals, idle dispatch and automatic compaction
remain open. This checks renderability, not scientific truth.

## Source and ordinary correction

The immutable original scientific transition identifies the source by finding ID,
original response revision and literal `explanation` field. Its action, evidence,
original explanation and original actor remain unchanged. A separate append-only
body history records every correction, exact SHA-256, stable block/span IDs and
last-changing MAIN actor. Unchanged blocks retain their proved owner. Ordinary
Markdown uses empty macros; unrelated anchor documents never supply a parent FK,
macro configuration or authority.

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

A check pins the real finding/response source and exact body revision/hash, with
no fabricated document revision. Admission, load, current-result projection and
commit fence that canonical parent. Explanation check/evidence/queue views are
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

A confirmation snapshot also pins the current explanation body revision/hash.
Even if the scientific finding CAS is unchanged, a correction makes an older
reviewer's decision conflict. A fresh reviewer must read the corrected pinned
source and existing original/fix evidence before deciding. Scientific confirmation
and mechanical child resolution remain independent.

## Validation and acceptance boundary

Focused fixtures cover source authority, cross-run replay, parent independence,
body/reviewer races, immutable original history, empty macros, failed-edit budgets,
unavailable retries, stale closing ACK, receipt/queue rollback, bounded corrupt
reads, unknown legacy isolation and populated source-neutral job migration with
child FKs/rollback. Workspace API and read-only rendering fixtures cover historical
scope and original/current attribution. Full combined gates and root-owned native
browser acceptance are separate completion gates.

`.agents/labs/response_math.py` builds marked disposable synthetic state using
actual configured local KaTeX without worker or model calls. Root owns the browser
server, acceptance evidence and complete teardown; the helper provides main-fix
and confirmation actions for live refresh checks.
