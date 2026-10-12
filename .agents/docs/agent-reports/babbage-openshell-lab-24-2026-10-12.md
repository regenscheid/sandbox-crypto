# Worker-local terminal observations in OpenShell on babbage

Run on 2026-10-12 against exact independently reviewed, signed source
`35863776eb7d88a909a8fadafbc1660146ef8493`, with actual main
`d2b133f3061dcf99098aab6ce509453169fdf844` integrated. Bounded regular-stat
observations survived successful, failed, cancelled and timed-out work and a
graceful worker restart. The original attempt, managed VM and Python kernel
remained intact. The complete disposable lab was removed before documentation edits.

The [immutable evidence](babbage-openshell-lab-24-evidence-2026-10-12.json) contains
23 exact UTF-8 text records, totaling 180,606 underlying bytes. The 200,670-byte
archive has SHA-256
`6b8ce007f6c7f1e111244dc01f7c3a94debbcd93c7e6f157b5c1e91f1b1c3f64`.
It retains API/helper results, independent SQLite and guest inspection, initial
and corrected inspection scripts/logs, source/settings identities, full-gate logs
and teardown evidence. Every embedded length/hash matches its captured source.
The retained export check found none of the four actual disposable credential
values in the nineteen files exported at that point. Raw service journals are
not retained. The reusable check is [`output_observations.py`](../../labs/output_observations.py).

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04; worker Python 3.13.15 |
| Worker | Signed clean `35863776eb7d88a909a8fadafbc1660146ef8493` |
| Main services | Main `d2b133f3061dcf99098aab6ce509453169fdf844` |
| Gateway | OpenShell 0.1.3, MicroVM driver, loopback mTLS gateway |
| Sandbox | `sandbox-science/minimal:02f85b3`, Python 3.14.8, kernel 6.12.76 |
| Isolation | Effective and declared tier 3 |
| Scope | Synthetic project `lab24`, environment `lab24-observations`, session `observations` |
| Managed identity | `ss-f6ad4d6b8e8f1e97`, immutable ID `c8251eb4-1405-4f18-812f-99daa5dbcd38` |
| Guest boot | `8e0ddf4e-dc9f-496b-81ac-836cb518687e` |
| Services | Four disposable units ran: gateway, middleware, cache and worker; synthetic credentials |

The helper SHA-256 was
`bcddecd34cec221c6e4331ab7568c77281260a04420181ce4b82068b67f08ca9`;
the executor SHA-256 was
`2890bb32daffbeff8f4464485ff78c2dc5b8a415efbfb612ae4524d4fcc9cb93`.
No product, test, helper, assertion, security control or runtime image changed
during acceptance. Root restarted only the disposable worker, retaining its
SQLite state and the same live VM; there was no VM recycle.

## Observed outcomes and retained metadata

All nineteen executions were terminal. Independent inspection counted ten shell,
five cell and four job executions, with these outcomes:

| State | Count |
| --- | ---: |
| Succeeded | 11 |
| Failed | 4 |
| Cancelled | 3 |
| Timed out | 1 |

All nineteen fixed markers retained the scoped original attempt/context and
managed sandbox binding. Eighteen report payloads occupied 32,820 bytes; the
largest was 10,540 bytes. Scoped head was 19, eviction floor was zero and the
scope remained available. The one request refused for a missing working directory
retained `gap="missing"` and no source report. The other exercised outcomes required
a report; failed/cancelled/timed-out fixtures also required their known partial
file path and after metadata. The helper checked successful, failed and cancelled
shell, cell and job paths, plus a timed-out shell.

Raw names `0xfe`, `0xff` and the UTF-8 bytes for U+FFFD remained distinct.
Session `shared/collision` and the project-shared `collision` also remained
separate through their explicit root tags. Symlink/FIFO sources were excluded
from regular-file observations. A removed regular path was recorded only as
`not_seen_after`, without a deletion or tombstone claim. Creating 300 known
fixture files produced explicit source/report truncation. The helper retained
that evidence, then removed only those known files before the recovery proof so
it did not depend on directory iteration order or an over-budget snapshot.

A gate-held job wrote `before`, survived the graceful worker restart and wrote
`after` once released. Its file read returned exactly the eleven bytes
`beforeafter`; its recovered terminal report retained the `runs` path with
`after.size=11`. The original attempt and full dispatch binding matched the
pre-restart pin, including daemon incarnation
`inc-682781096e536dc85736344c`. A checkpoint pinned at scoped target 16 retained
the same stream identity and exact page hash after restart. The Python global
`lab24_value=1729` remained usable after cancellation and again after restart.
These checks establish same-attempt recovery for this run, without command replay.

The minimal profile did not advertise PDF, so PDF observation behavior was
**not tested** in this OpenShell run. Its unavailable boundary is retained.

## Inspection corrections

The first independent inspector omitted the report hash's domain prefix and
failed its hash assertion. Root corrected it to hash the exact stored payload
with the `output-report-v1` plus NUL prefix. All eighteen report hashes then
verified. Both original and corrected scripts/logs are retained; the product and
helper were unchanged.

A diagnostic initially passed command arguments to `sandbox connect`, which the
CLI refused before running guest code. The corrected `sandbox exec` invocation
returned guest identity. An export assertion initially expected an
`effective_isolation_tier` key; the actual API uses `isolation_tier` and
`declared_isolation_tier`, both 3, with the same boot ID as independent guest
inspection. These were inspection/bookkeeping corrections, not product failures
or relaxed runtime assertions.

## Local validation and remaining limits

The exact integrated source passed 83 focused contract/worker cases and all
required local gates: 9,714 Python tests, 43 opt-in skips and two existing
Pydantic serializer warnings in the forged-legacy-artifact negative fixture;
441 web tests; 24 post-build math cases; locked uv/npm installation, Python/npm
license checks, Ruff formatting/lint, strict Pyright, Svelte/TypeScript checks,
generated protocol consistency and both production builds. Independent adversarial
review cleared the exact source and helper. Acceptance documentation changes no
product, test, helper, configuration or lockfile bytes. Fresh PR CI remains the
merge gate.

Corrupt derived bindings/counters/large scalars, invalid sidecars, quota/eviction,
terminal/source-cursor atomicity faults and the regular-to-special namespace swap
were local hostile tests, not faults induced in this VM run. The added sidecar
is bounded; the legacy stat walk's pending directory lists and filesystem I/O
remain outside a new hard bound. The detail quotas do not bound all execution
history, SQLite/WAL allocation or RSS.

Every report remains mutation-incomplete. No observed stat change proves the
actual writer, producer, inputs, closed writable handles, all file mutations or
attempt-end content. `produced_by`, where exposed by existing worker publication,
remains an unproven recent-path hint. RA-5's every-file-minus-ignore-patterns
requirement is unchanged and remains open. This prerequisite adds no public feed,
WorkerClient method, scheduler, content capture, artifact revision or automatic
reviewer consumer. One-shot capture receipts, authenticated transport, complete
telemetry/snapshotting and control ingestion remain follow-ons.

## Cleanup

Teardown reported zero sandboxes and removed 1.1 GiB of disposable state.
Independent verification at `2026-10-12T14:59:02.325873Z` found all seven possible
lab unit names inactive, no occupied lab ports, no lab processes, no driver socket
directories, no lab state or OpenShell XDG paths, and clean unchanged worker/primary
source trees. Shared images and persistent configuration were preserved.
