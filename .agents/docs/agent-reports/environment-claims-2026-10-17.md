# Active research environment claims

Local implementation on `codex/research-foundations`; no personal service or live
OpenShell gateway was started. Worker protocol stays 7 (a new route), control schema 58.

A stored root attempt retains its managed workload sandbox for its live subtree,
including model-only research and waiting for children. Claims persist at the worker,
expire in 120 seconds, renew every 30 seconds, and pin the original sandbox identity.
Worker restart preserves claims; expiry and scoped release permit idle suspension.
An unavailable renewal interrupts the root. Cancellation joins renewal and releases
before cleanup ends. Explicit deletion remains authoritative. Unmanaged workers
explicitly report that managed idle suspension does not apply.

The API bounds duration and claim count and checks project and policy scope. Tests
cover wrong project, stale policy, a replacement sandbox, independent holders,
worker-store restart, lapse, failed renewal, repeated cancellation, canonical run IDs
and a real root/child harness wait. An adjacent cleanup regression ensures a retry
validates its replacement request before deleting the retained original allocation.

Validation: 249 affected tests passed in 25.74 seconds. The earlier combined run had
6,196 passes, 51 skips and 14 failures, all missing claim methods in test worker
fixtures. All failed modules were rerun successfully in the 249-test gate. Ruff and
pyright pass. Existing legacy-fixture warnings remain. These deterministic and local
HTTP fixtures do not establish real VM continuity, durable paused-parent ownership,
tree Resume or orchestrator confinement; those remain separate work.

Evidence outside Git in the local test evidence directory:

- `research-foundations-claims-final.log`: SHA-256
  `7d0cae6e7311e88d1784772bade429f769c2226e8f3d386cf45f8dd715987ff9`.
- `research-foundations-combined.log`: SHA-256
  `198807124066ba46d005c848ce6dfe74ddfab5226ece740f5f6d7577f18f7ad3`.

Independent review and integration remain pending.
