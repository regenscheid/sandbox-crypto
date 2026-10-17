# Live parent ownership at a limit

Local implementation on `codex/research-foundations`. A parent's normal turn,
context or budget limit stops its model work while a finalization hook retains its
session and environment leases until every child saves its outcome. This also applies
to a delegated lead with a grandchild. Waiting makes no model calls and acknowledges
no unseen outcome. The parent's attempt remains classified as limited. Explicit Stop,
interruption and failure still use the existing bounded subtree cleanup.

Children receive their configured role turn allowance, independently of the parent's
remaining calls. Project spending, child token limits, grandchild token grants, role
restrictions and depth/concurrency caps remain enforced. A parent with no call left
still cannot start another delegation.

Validation: 192 tests passed in 37.08 seconds, covering delegation, two-level trees,
budgets/settings, hook lifecycle, the run service and the tree's environment claim.
The changed cases show a child making further calls after its parent reaches a limit,
its result saved first and consumed by a later root, a grandchild's unconsumed outcome
handed to the root, and Stop cancelling a limited parent's live child. Ruff and pyright
pass. Evidence outside Git: `research-foundations-parent-limit-final.log`, SHA-256
`8a0db9e9a2986365dd4a51c0aa97ce3b98a3dfcaac8744cb9f29988f3d1c4324`.

No live OpenShell service was used. This retains live attempt ownership; it does not
implement durable work identities, continuation attempts or recursive tree Resume.
Independent review and integration remain pending.
