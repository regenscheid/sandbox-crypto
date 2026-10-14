<!-- SPDX-License-Identifier: MIT -->
# Watcher prompt reservation provenance, 2026-10-12

Independently reviewed signed author `ba35ca42a3fc59ea1be0c5382133647737cc8a2c`
adds exact historical watcher system and zero-tool input pins. Signed combined
source `96e1585414b4a993bae2a597195011a48a831dbf` integrates frozen MAIN delivery
head `6df40ba3c3b1a2ca8a2494cf154af46b70bb12fe`. The six author paths remain exact;
every other product/test/configuration/dependency path remains exact MAIN delivery.
MAIN PR #103 subsequently merged at `8859407` on signed `6df40ba` after all four
fresh checks passed (run `38334071913`). Signed `48e17b6` integrates that actual
main ancestry without changing the combined gate tree.

## Behavior and limits

The [implementation boundary](../../../docs/WATCHER-PROMPT-PROVENANCE.md) describes
one nullable envelope committed with the existing budget reservation, bounded
exact-scope reads, preserved historical text and legacy NULL unknowns. Prompt
wording, actual zero tools, due schedule, budget, failure/cancel/wake, attribution,
usage and MAIN save-before-notice-ACK behavior remain unchanged.

A pending reservation describes intended input, not dispatch/provider receipt.
The dynamic user payload is excluded; its old stored encoding and actual model
input encoding remain different. Cancellation during existing threaded reservation
work may still commit an intended-input record after task cancellation. The existing
journal has no global retention quota; new overhead is bounded per admitted call.
No model adherence, scientific correctness, provider-wire hash, public route,
automatic retry, scheduler or added watcher authority is claimed.

## Validation and evidence

Independent source and union reviews clear exact signed source. Six independent
disposable probes cover atomic pending pins, original scope/finish/reopen,
20-MiB/invalid-UTF-8 corruption refusal, concurrent budget admission, insert rollback
and concurrent legacy migration. The large retained metadata probe peaks at 2,375
Python bytes; this is Python allocation evidence, not SQLite/global RSS accounting.
The original probe script and result note are retained; no standalone original
probe stdout file was written.

Author final focused validation: 66 passed in 1.10 s; whole-orchestrator and
changed-test Pyright, Ruff/format and all 75 Python licenses pass. The first run had
five fixture setup failures and 61 passes: a zero interval and an uninitialized
legacy control-store fixture. Product behavior did not change to fix them. That
initial full log was overwritten before retention was requested; the archive's
clearly labeled reconstructed note is not presented as the original log.

Root combined gates at signed `96e1585` pass 10,385 Python cases/43 opt-in skips/
two existing serializer warnings in 379.82 s, full Pyright/Ruff/format, protocol
freshness and 75 Python licenses. Fresh web license/install/check, 656 web cases,
both production builds and 24 post-build math cases also pass.
No external model, service, worker or VM was used for this metadata slice.

The exact archive (archived outside git, sha256 `2ef4c217d987`) retains source,
review and focused/combined gate evidence. Fresh exact-head CI and final delivery
review remain required before merge. Planning browser evidence and MAIN provenance
validation keep their original pins. Reviewer provenance is separate in-process
work; display repair, dynamic payload hashes, general catalog loading, delegation,
USER plan editing/approval, automatic effect/liveness and compaction remain open.
