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
slice. Cancellation receipt precision is a separate follow-up under review.

Focused runs pass for production child narration, scoped context isolation,
Resume/accounting, reservations, nested grants, role profiles and confinement.
The production fixture uses the synthetic gateway and real harness/tool-loop
assembly, reaches a narrative fold on the child alias and excludes the root's
private reasoning. Python type checking passes. Full source-frozen validation is
pending the cancellation follow-up; earlier full-suite results do not cover this
change.

A separate synthetic OpenShell lab is qualifying the unmodified `d20e5fe3`
recovery baseline and genuine sustained Sage computation. That lab does not
qualify the new narrative code or scientific summary quality with a real LLM.
