# Review snapshot planning

The review repository currently chooses and budgets evidence while also owning review
transactions, watermarks, leases, call accounting and completion. This change gives
snapshot selection a separate policy owner without changing the public `ReviewStore`
facade or the reviewer's input.

## Boundary

A snapshot planner accepts a review kind, immutable window, confirmation targets,
coverage flag and explicit byte/item limits. The repository supplies the current plan
item, materialized finding groups and selected feed evidence. It performs the same
reads in the same order: initial window/findings/plan/actions, each finding's first
materialization, bounded feed selection, then a second materialization of findings
which did not fit the reserved share. In particular, excluded findings are not loaded
through a new eager unbounded read, and response pins are not reused across the two
passes.

The planner owns admission and grant deduplication, reservation of half an author
snapshot for findings (all of a confirmation), retry-dependent feed allowances,
ordering, confirmation keys and coverage calculation. Its final result is an immutable
value containing ordered evidence, grants, targets and response pins. JSON grant and
pin values are serialized inside that value so nested caller dictionaries cannot
mutate a prepared snapshot. The planner has no repository, document store, SQL
connection, transaction, clock, identifier generator or callback to repository methods.

The repository continues to materialize and verify document/artifact/ledger evidence.
A scoped read module owns the existing bounded feed/window/action SQL, independently
of the `ReviewStore` lifecycle. Shared JSON and evidence values live below the planner
and repository so action-item construction need not import the lifecycle owner.

## Preserved behavior

- Confirmation targets precede rebuttals and other open findings. Unadmitted groups
  may use room left after feed selection, without changing the finding output order.
- Plan items count against the findings' reservation. Duplicate grants consume only
  one slot, using their original first-admitted order.
- Retry shrinking, prior-context carry, raw-source skipping, delegated outcome carry,
  whole-prefix feed advancement and incomplete delegated windows keep their current
  limits and semantics.
- A confirmation with no admitted target still falls back to terminal work after
  rereading configuration and targets. Its coverage remains incomplete.
- Snapshot transaction ownership and commit-time window, response-body/macro-context,
  running-review, cooldown, confirmation-failure and call-deferral checks stay in the
  repository. Pins, watermarks, leases, completion and call accounting do not move.
- Public signatures and the golden dispatched snapshots, including content, ordering,
  grants and confirmation keys, remain unchanged.

## Acceptance

The unchanged dispatch golden tests must pass for author work, confirmation and
confirmation fallback. Public repository tests continue to cover feed overflow,
findings reservation, retries, plan/action evidence, immutable response pins and
commit-time races. Focused planner tests cover explicit budgets, admission ordering,
duplicate grants, confirmation coverage and immutable results using concrete evidence
values; they do not inspect private planner state. No model calls or services are
needed.
