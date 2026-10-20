# Handoff

Updated 2026-10-20. Active work is `k8s`, branched from `dev` at `af0fa0d5`, in the existing implementation worktree. The primary checkout has unrelated state and must be preserved. Read [STATUS.md](STATUS.md), [BACKLOG.md](BACKLOG.md), [CONVENTIONS.md](CONVENTIONS.md) and [Kubernetes work](../docs/KUBERNETES.md).

## Kubernetes continuation

The branch implements pinned placement/resource bounds, native scheduler shims and verified S3 retention/eviction. New upload originals use blobs/S3 with bounded HTTP streaming and exact-ID recovery; legacy inline rows remain. The 100 MiB HTTP test stays below 32 MiB traced allocations; 127 focused tests pass/one skip. Shared research images now retain real package prefixes under `/sandbox`; required Kubernetes continuity checks preserve known revisions, journals and holds. Exact restoration uses a durable ACK/epoch handshake with protected admission. AMD64 minimal container/Landlock and installed-package volume recreation pass. Native ARM64/crypto-base, image publication and actual OpenShell cluster qualification remain. No cluster was accessed; published images are the previous release set.

Next: durable worker-capture acknowledgement/offline replica release, complete workspace archive/restore before PVC deletion, then trusted services/manifests and authenticated callbacks. Acceptance must verify separately scheduled supervisors and actual runtime/network isolation; a different node boot ID alone is not VM proof. The Mac test remains unrun. Do not deploy implicitly. Validation and remaining gates are in the [foundations](docs/agent-reports/k8s-foundations-2026-10-20.md) and [persistence](docs/agent-reports/k8s-persistence-2026-10-20.md) reports.

## Accepted scope and current evidence

The owner authorized **$150 total for new campaign model tests**, superseding the earlier $75 proposal, and requested all six [completion items](docs/agent-reports/completion-campaign-2026-10-18.md). This is not permission to resume SNOVA or approve extra token grants. Root coordinates live labs, spend, final reports and acceptance; implementation agents use isolated worktrees and signed local commits.

All six implementation areas are integrated: owned infrastructure recovery, adaptive time/turn/storage allowances, actual dispatch capture and original-history indexing, fold/usage diagnostics, safe MCP metadata/current checks, and Overview/Memory/session refinements. Independent reviews covered storage, authority, recovery and read bounds. Reported fixes include huge schema integers, partial HTTP reads, draft durability warnings, capture omissions, incremental SQLite blob reads and approved resource receipts.

Actual authenticated synthetic browser checks passed MCP available → unavailable → restored with zero tool calls, Memory draft retention/navigation/save, captured-context chunk paging and original-history incomplete → complete indexing across same-work continuations. Web tests: **2,239 passed**. The frozen Python suite at `5a238f10` passed **14,524 tests with 50 skips**; Ruff, format and strict Pyright are clean. The exact isolated image digest and container checks are recorded in [STATUS.md](STATUS.md).

Three owned infrastructure probes passed worker restart, agent-VM replacement and host-process-loss root Resume while the same genuine Sage operation continued. The final real-model tree then proved concurrent root/child recovery, durable bidirectional steering, full child-history retrieval, final handback integration and no duplicate child/reference computation. Numeric GF(16)/GF(32) grades passed; two real Luna/medium reviewer findings led to fixes and were confirmed fixed. Observed campaign spend was **$24.56566378** with no unpriced usage. The 52-file evidence archive verifies complete owned teardown. No physical reboot, arbitrary kernel snapshot or broad scientific qualification is implied.

## Campaign closure

1. Preserve `/var/tmp/ss-recovery-evidence-a4addfda` while the campaign evidence is useful; its `SHA256.json` covers all 52 allowlisted files.
2. Do not reuse the five unsettled reviewer intents as permission to replay them. They remain visible while all observed usage is priced.
3. Treat role-profile edits as next-tree settings. Main/orchestrator, delegated and reviewer aliases and effort are independently configurable in Project settings; ordinary Resume retains the active tree's pins.
4. Remaining work is the open backlog, especially physical-host reboot qualification, broader lost-kernel provenance and residual transient-failure classes. It is not part of this completed disposable lab.

## Runtime and UI boundaries

- Infrastructure recovery is opt-in and bounded. It replaces only an immutable recorded original VM; pending/unknown replacement state cannot silently start another attempt. Exhausted optional host components retain other services/VMs for inspection.
- Parents retain responsibility for children. Root Resume drives the selected work tree, preserves explicit Stop and reconciles original effects. Approved paused root/child **turn/time/storage** allowances can qualify for automatic root Resume; this path does not grant tokens. Every next 10,000,000 tokens still needs explicit USER approval and exact revision checks. Opt-in root renewal requires newer persisted evidence and cannot relax hard capacity/security limits.
- Dispatch captures show provider-neutral visible input, not hidden reasoning/native replay/provider transformations or per-part token counts. Oversized captures have explicit omission receipts. Text is paged by UTF-8 bytes. Derived-index pending checks still scan scoped row metadata; index misses are incomplete until remaining originals are indexed. Children/siblings are never merged into same-work continuation scope.
- MCP schema/policy reads are authenticated, same-origin host → proxy metadata reads with no upstream call. Only the explicit availability control initializes/lists tools. Unknown/stale/unavailable states and last-check times remain separate; browser clients receive no credentials, raw configuration or private endpoints.
- `.agents/labs/context_browser.py --selftest` validates synthetic seed records offline. Normal invocation starts its own temporary root plus loopback workspace, MCP proxy, TLS peer and control listeners. Printed `mcp_controls` changes only the synthetic peer's availability; `mcp_stats` must retain zero attempted/executed tool calls. Stop it cooperatively and verify all owned listeners/root disappear after browser acceptance.

## Workflow and protections

Use signed commits, no attribution lines; do not push or mutate preserved services implicitly. Independent read-only review applies to security, concurrency, storage and new features. Current tool/developer instructions govern agent spawning.

Run one Python suite at a time, at most `-n 4`, with `SS_LIVE_GATEWAY` and `SS_INTEGRATION` unset and a unique `/var/tmp/sandbox-science-test-cache-*` basetemp. `/tmp` and `/home/andy` are Git checkouts. Use the locked `.venv`; strict checks use `pyright --pythonpath .venv/bin/python`. Regenerate API types after contract changes. Logs/screenshots/raw evidence stay outside Git; reports contain only sanitized summaries and hashes.

Never launch a real service from an actively edited checkout. Never read/list/copy/print real keys under `~/.config/sandbox-science/secrets/`; authorized programs receive an existing path only. No sudo, security-setting changes, Docker pruning or destructive actions on real data. SNOVA and its archive stay preserved; do not recreate its stopped monitor. Product prompts are independently written; do not import proprietary-derived OpenScience skills.
