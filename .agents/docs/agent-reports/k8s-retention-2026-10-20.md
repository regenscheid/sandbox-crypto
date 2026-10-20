# Worker capture replica retention, 2026-10-20

Continuation on `k8s` after the [persistence increment](k8s-persistence-2026-10-20.md).
The owner explicitly deferred image publication. No image was built or published,
cluster accessed, real credential read, model called or existing service restarted.

## Implemented scope

Worker protocol 11 adds a bounded exact retention acknowledgement: the immutable
worker snapshot, matching retained control revision and a retry-stable identity.
Project-limited clients need explicit `artifact_retention`; ordinary clients
default to denial. The pool token has it. The local launcher grants it only to
the trusted `harness` client, within its configured projects. This is a lockstep
application update; older published orchestrator images are not protocol 11 builds.

A verified control publication commits its successful receipt and pending ACK in
one transaction. Metadata-only, held or uncertain publications do not create ACKs;
legacy receipts are not backfilled. Outbox bytes are charged to metadata quotas.
Delivery binds the original endpoint digest, checks the exact echo and retains
immutable audit. Failure cannot downgrade a successful receipt or repeat a mutable
capture. Run-start, completed explicit/automatic capture replay and a bounded
operator command use the same delivery component. Its HTTP attempts share a
five-second budget and stop on failure; no model calls or tight retry loop are added.

Physical release is an explicit offline worker CLI, dry-run by default. It holds
the same exclusive state lease as the running worker. Every snapshot sharing a
hash must have an exact ACK, including other projects. Installer references and
unknown, invalid or oversized metadata/audits preserve bytes. The command pages
only referenced hash-named CAS content, leaving artifact metadata, source
workspaces, staging and orphan files intact. Durable intent precedes unlink and
directory fsync; settlement survives restart. Later recapture of identical bytes
requires a fresh eligibility check rather than trusting an old settled release.

Commands and failed-page semantics are in [artifacts](../../../docs/ARTIFACTS.md#acknowledging-durable-worker-copies)
and [worker retention](../../../docs/WORKER.md#offline-capture-replica-retention).

## Review and focused validation

Independent adversarial review is clear with no critical, high or medium finding
remaining. Corrections include bounded original/audit and SQL join-key reads,
classification of embedded-NUL hashes, and retrying the original ACK when a later
run reuses an automatic execution capture. Regression cases preserve shared and
installer bytes, cap allocation on corrupt rows, reject altered IDs/snapshots,
isolate projects/endpoints, keep receipt/outbox commit atomic, and retry lost
replies without additional publication or download.

The worker's final retention regressions passed 38 tests; its earlier broader
schema/protocol/service/catalog/auth checks passed 106. Root artifact tests passed
88 cases before the final page/corruption/replay additions; the combined root and
worker retention check then passed 54. Independent focused verification passed
five replay/corruption cases. The final launcher permission check passed 27 tests
with zero Pyright errors. Original development failures remain separate from final
verification; no assertion was removed to make cleanup succeed.

## Disposable real-process check

The new slow process test passed in 3.44 seconds using synthetic credentials and
temporary local-backend state. It:

1. Starts the real worker CLI and captures immutable bytes over HTTP into control
   storage. The worker commits an ACK, then the caller deliberately loses its reply.
2. Confirms the pending outbox remains, changes the source workspace file, and
   verifies offline cleanup refuses while the live worker owns its state lease.
3. Stops that worker, checks dry-run preserves the blob, then applies release to
   exactly the acknowledged CAS copy. Control-owned content remains readable.
4. Restarts the real worker, retries the same ACK, and returns the original receipt.
   The artifact count stays one, the released blob is not recaptured, and the
   changed source file remains present with its new bytes.

All test-owned worker/CLI/executor processes drain on exit. This is Linux local
process/HTTP/state-lock evidence, not OpenShell VM, Kubernetes, ARM64 or network
isolation qualification. No personal or preserved research environment is used.

## Combined validation and evidence

The integrated four-worker Python sweep ended with **14,739 passed, 51 skipped and
one failed** in 692.48 seconds. The v73 upload-migration fixture dropped the newest
migration while hard-coding version 73; after adding version 75 this inadvertently
ran v74 and tried to create its table again on reopen. Pinning the actual v73 prefix
fixes the fixture without changing a shipped migration or weakening its assertions.
The new worker-upgrade fixture likewise pins v6 instead of a moving latest-minus-one
prefix. The affected upload/process/control/retention/launcher check then passed
**144 tests** in 28.18 seconds. The full sweep was not rerun after that test-only fix.

Web validation passed **2,241 tests in 121 files**, Prettier, Svelte/TypeScript and
production/math builds. Happy DOM's disabled-iframe diagnostics are retained in the
log. The required post-build math-checker run passed **38 tests** in 5.19 seconds.
Ruff formatting/lint, full Pyright (zero errors), license checks (86 Python
distributions and npm dependencies), generated contracts and repository hygiene
pass. Tier verification classifies all 14,791 tests exactly once: 13,803 fast and
988 slow. No live-model or cluster test was enabled.

Evidence is preserved outside Git at
`/var/tmp/ss-k8s-retention-evidence-20261020`. Its manifest verifies 15 allowlisted
files and retains original hashes for logs whose home/workspace roots were replaced
with placeholders. `source-files.sha256` binds the tested product/test files through
signed source revision `5f0c769b`; the final documentation commit is separate.

| File | SHA-256 |
| --- | --- |
| `SHA256.json` | `07f0143dadf3a194e831c6b3c091207a0993df43db2bfb1745cc8d3fcd317c2e` |
| `python.log` | `22f054e617768762af046f88b0ae8aed4633598f07be95939fe5c9f0a958c423` |
| `focused.log` | `0b26cbf0072ec9f15f120fa34ca270ae7ecb0504930cdce8c5d425b8541778a6` |
| `web.log` | `e2f7bf1fb22eb1bd64aae78aade29eaa640dcbe883384039e744b4d8e9f4f4d7` |
| `mathcheck.log` | `78c584a1c883cc17ad14fa7fc63409489c961da1dd6e22a6bc7fb49df8c65f16` |
| `final-checks.log` | `403d224d47a800a5e86584a0c994bc1432912c227686a77658b4a3098f512a91` |
| `process.log` | `17b503cda9b730e097a8a42687173c7eb6c151e7464fe0c4ff842c2def27901b` |
| `source-files.sha256` | `ffbf4c9d066d4a432ad495a5d46b5ccca76ccf44c2f3935cacbc18903ccf26fb` |

## Remaining work

Complete whole-workspace archive/restore before releasing research PVCs. Add a
bounded retention policy/controller; online worker deletion needs writer fences
and pinned download descriptors first. Trusted cluster services, authenticated
callbacks, manifests/RBAC/network policy and mixed-architecture live acceptance
remain in the [Kubernetes plan](../../../docs/KUBERNETES.md). Remote object deletion
and automatic age-based expiry are not implemented. Image publication stays deferred.
