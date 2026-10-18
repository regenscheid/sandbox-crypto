# Delegated narratives and fold accounting

Date: 2026-10-18. Target: local `dev` in the development checkout, starting at
`d20e5fe3`. No paid calls or changes to the personal/preserved research services.

## Behavior

Child tasks/checks use the existing fold/overview engine on their own archived
work. New admissions pin their selected alias and independent fold/overview
reasoning settings. Resume preserves saved pins. Skeleton-only mode remains
available; insufficient estimated room for enrichment plus a report uses the
skeleton without a summary call. A temporary input/output token estimate is held
while each background attempt runs and released when it settles. This is token
bookkeeping, not an invented dollar charge.

Observed summary usage, legacy receipts, cancelled input estimates and explicit
reservations share the original child grant. Cumulative Resume and current-attempt
queries use the same SQL accounting; canonical receipts suppress only exactly
matched legacy copies, using the same helper as project costs. Summary retries
are individually admitted. A delegated parent refreshes auxiliary usage after
awaited planning immediately before assigning a grandchild allowance.

Fold events and summaries retain at most two structured attempt outcomes and
size measurements, even when a failed first attempt is repaired. Raw rejected
responses are excluded. No additional model calls were added to the retry policy.

A narrator now initializes the owning model's cache connection before making its
role copy. This fixes an unclosed HTTP session reproduced by the production child
narration fixture (the existing main narrator had the same ordering problem).

## Review and validation

The independent `merge_review` agent examined grant concurrency, receipt
precedence, Resume, profile pins and confinement. Its stale nested-grant finding
was fixed with a live auxiliary-usage refresh and a regression that changes fold
usage during awaited admission. The recheck found no remaining P1/P2 in that
slice. Follow-up review found two cancellation races: saved summaries were copied
before lease cleanup settled an interrupted fold, and a confined response observed
just before cancellation could gain a duplicate identityless estimate. Both are
fixed and re-reviewed: final summaries follow lease cleanup, preserving existing
skills-summary merge order; the model's explicit durable-intent capability leaves
confined usage with its canonical journal. Non-journaled local calls retain one
estimated cancellation receipt only while their call is in flight. Admission and
post-response validation cancellation do not invent a second call.

Focused runs pass for production child narration, scoped context isolation,
Resume/accounting, reservations, nested grants, role profiles and confinement.
The production fixture uses the synthetic gateway and real harness/tool-loop
assembly, reaches a narrative fold on the child alias and excludes the root's
private reasoning. Python type checking passes. Source-frozen `d6d37c70` passed
14,380 Python tests with 50 prerequisite/opt-in skips and two existing warnings from
the deliberately malformed artifact-size fixture. The final 164 focused lifecycle
tests, full Ruff checks and Pyright pass at `84bb0a4e`. The second source-frozen
broad suite also passed: 14,390 tests, 50
prerequisite/opt-in skips, with the same two malformed-fixture warnings. This
rerun covered the move of final summary capture across the shared loop lifecycle
boundary. Tier assignment, repository hygiene, installed licenses and generated
protocol checks pass. No web source or public API shapes changed.

A separate synthetic OpenShell lab first qualified the unmodified `d20e5fe3`
recovery baseline. The lab exposed a saved
model failure being overwritten as interrupted by service cleanup. The narrow fix
preserves paused/blocked diagnoses while explicit Stop still supersedes unfinished
work; an actual synthetic streamed EOF now triggers one automatic tree recovery
without a new user message. Both host and confined runtime at `84bb0a4e` repeated
that automatic recovery successfully.

The final runtime also accepted one synthetic child narrative fold, freeing
58,324 estimated context tokens. It used the child's selected alias and `high`
fold effort, excluded the parent-private marker and counted its 122,794 synthetic
fold tokens once despite the canonical receipt's legacy stored copy. The child's
total remained 989,567 tokens within its original 2,000,000-token grant. This
qualifies the confined path and single-attempt accounting; cumulative Resume,
grant exhaustion and overview dispatch remain covered by deterministic tests,
not this live probe. A synthetic peer cannot establish scientific summary quality
with a real LLM. Full runtime receipts and boundaries are recorded in the
[OpenShell recovery report](openshell-recovery-acceptance-2026-10-18.md).
