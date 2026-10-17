# Research foundation integration

Local signed implementation on `codex/research-foundations`, based on main
`57e29536`. The primary checkout and personal services are unchanged. Nothing was
pushed, merged or deployed to the personal instance. Combined validation and
disposable-runtime teardown are recorded below.

## Implemented behavior

- One explicit Resume recovers the selected tree, including grandchildren, with
  at most four ready continuation branches. Completed and stopped work is skipped.
  Original policy, model/prompt pins, kernel generations and remaining grants are
  checked before another paid call. Unknown charges or effects remain blocked.
  The claim admits only its own unconsumed held input atomically, even when the
  operator disconnected immediately after saving the durable request.
- Parent completion depends on durable child outcomes. Schema 65 checkpoints close
  the final-save/handback/ledger crash windows. Repeated interruption before parent
  admission retains recovered child evidence. Terminal child kernels are cleaned
  under current root authority; paused kernels remain available for reconciliation.
- Historical messages, native data, tool records, folds, usage and receipt membership
  use bounded views and paged disk collections. Final records and downloads retain
  references to exact evidence. Completed child task objects are released; a
  240-child fixture verifies bounded retained objects, paged history and all receipts.
- Installed-tool manifests generate binding restrictions, including provisioning
  changes. Runtime inventory digests reject stale surviving kernels. Existing deny,
  namespace and approval rules remain in force.
- The normal launcher puts parent/child model loops in a separate OpenShell VM.
  Fixed scoped host capabilities own durable state and validated tool dispatch.
  Research code runs in worker sandboxes. Explicit orchestrator replacement retains
  the workspace, control service and worker, and grants no automatic research restart.
- Runtime disconnect closes and joins owned runs through the same cleanup path as
  local shutdown. Canonical input receipts and original annotation offers survive
  lost leases; full attempt identities prevent reuse of a short run ID from mixing
  distinct work. Annotation claims and offer membership commit atomically. Stop
  retains the original request's hold, while fresh user input remains eligible.
- Main responses atomically save usage and input receipts. Auxiliary observations
  count before legacy report materialization, with exact identity links preventing
  double counting. Operator-supplied observed tokens reduce the original grant.
- The workspace exposes Resume and blocked reasons even without held chat messages,
  including when a newer root completed while older work remains unfinished.
  Consolidation holds survive resumed context stops and service replacement;
  a ledger change clears the prerequisite but never restarts unfinished paid work.

Worker protocol 8, executor protocol 3, control schema 65 and private runtime
protocol 1 are pinned. The orchestrator image's embedded runtime revision must
match the host's configured `[orchestrator].revision`; upgrades are lockstep and
there is no host research-loop fallback.

## Deterministic evidence

- Recovery integration: 132 passed in 30.74 seconds. Log
  `/home/andy/.cache/pt/foundations-recovery-final.log`, SHA256
  `bcf50f3f453b6a43ef30ada6b5b563bdcf1fa084e822d1886ee0c02afb415c89`.
- Auxiliary accounting: 182 focused tests passed; canonical-before-report crash
  windows, exact links/rollback, distinct equal charges, daily dispatch windows,
  unpriced calls and actual model/embedding callback identity transport.
- Workspace: 2,180 tests passed. Log
  `/home/andy/.cache/pt/foundations-web-tests.log`, SHA256
  `893637999d920145a2ed94489da090c994aec9943b15bb4a3d2ab9979e1da071`.
  Typecheck, app build and math-checker build passed.
- Launcher regression: 114 passed; see the [fixture report](launcher-fixtures-2026-10-17.md).
- Finalization, model-usage and prompt fixtures: 85 passed in 6.08 seconds. Log
  `/home/andy/.cache/pt/foundations-final-fixtures.log`, SHA256
  `edc0e7b8391f76745f8e22667f37638ec8741d02d042e8bae98e7f03f50e22cc`.
- Consolidation and request admission: two focused gates of 55 tests passed; see
  [consolidation](consolidation-recovery-2026-10-17.md) and
  [atomic Resume input](resume-input-claim-2026-10-17.md).
- Remaining Blob readers: 137 passed; exact multi-chunk bytes, export checksums,
  connection closure, old-schema first reads and retained memory are covered
  ([reader lifetime report](blob-reader-lifetimes-2026-10-17.md)).
- Final regression repairs: worker 247 passed and 25 external tests skipped, plus
  the corrected cross-session fixture passed separately; orchestrator 78 passed;
  [workspace repositories and routes](workspace-repository-boundary-2026-10-17.md)
  32 passed.
- Locked dependency sync, full Pyright, Ruff, formatting, generated API/protocol,
  repository hygiene and Python license checks pass: 1,088 files formatted,
  Pyright 0 errors/warnings, all 81 installed Python licenses accepted. Log
  `/home/andy/.cache/pt/foundations-static-final6.log`, SHA256
  `53f454c35f2b0c721fc27e55476e4384aaacc41c53494260ce6d120c45a9f924`.
  Its hygiene step initially reported this new acceptance report as untracked;
  staging the report resolved both links. The successful hygiene/license rerun is
  `/home/andy/.cache/pt/foundations-hygiene-final.log`, SHA256
  `a86045888de759b40264d481c2dc47c9ce7227e0b20c51dab4d64ccdb7d8bcd5`.
- Full repository run on final product revision `be62e57b`: 14,028 passed,
  49 opt-in skips and one outdated test-stand-in failure in 619.31 seconds. Log
  `/home/andy/.cache/pt/foundations-full-final5.log`, SHA256
  `09caa65c095013a7ca29549d902b4bc37762c9fa1824c1d7f4a0de1cf36907af`.
  The stand-in omitted execution context, which the updated test client needs for
  cancellation. Its data now retains the original context and its handler asserts
  the exact cancel body before the unchanged HTTP 409 refusal. The full module
  passed 2/2 in 0.70 seconds, independently reviewed; no production code changed
  after the full run. Log `/home/andy/.cache/pt/foundations-test-client-final.log`,
  SHA256 `1383d6e879b0aa987c31fdfea236458757a2386b0470dfae2897f83e91477cc3`.
- Tier coverage passed for all 14,078 tests: 13,161 fast and 917 slow. Log
  `/home/andy/.cache/pt/foundations-tiers-final.log`, SHA256
  `e4b78e5ecb4dfff4956b05a3f80b0e91fc2e80991fe479a7273354bfdf871e69`.

One initial full run was deliberately stopped after 1,672 passed and 24 failed to
fix the systematic failures. Most launcher failures were caused by placing fake
executables beneath `/home/andy`, itself a Git checkout: the production security
check correctly refused them. The final suite uses the isolated owned directory
`/var/tmp/sandbox-science-test-cache/` as a documented exception to the usual
home-cache fixture path. Remaining fixtures were updated for separate VM/control
lifecycle and the worker fence API; security assertions were preserved.

A second broad run was stopped at 2,162 passed and eight failed. Stale automatic
retry and unbounded-record expectations were updated to assert explicit recovery
and exact journal evidence. The folded-memory positive control now deliberately
retains streamed payloads instead of calling the correctly bounded page API. These
checks also exposed the consolidation/finalization race repaired above. Subsequent
focused checks passed before restarting the full suite.

The next full run stopped at 3,778 passed and four failures. Three were narrow
fixture updates: the usage intent field, the worker fence seam, and waiting for
finalization after canonical input delivery. The memory gate found a real archive
read regression: closed SQLite Blob objects left retained connection bookkeeping
across repeated scans. The repair preserves the original memory ceilings and the
older supported SQLite baseline; its independent review found no medium-or-higher
issues. The [preflight repair report](archive-preflight-memory-2026-10-17.md)
records the unchanged 40/400-turn gate passing and 52 focused regression checks.

A subsequent complete run recorded 13,995 passed, 31 failed and 49 opt-in skips.
The worker failures mostly used the old cancellation contract or incomplete
provisioning/kernel fixtures; the corrected tests retain exact context, canonical
operation identity and trusted binding checks. Product repairs restore invalid
provisioning-action refusal before storage access and refresh the final review's
bounded ID summary after its last dispatch. Workspace repositories retain their
public store boundary, and route/table documentation inventories are complete.
All changed production paths received independent review before their focused
gates passed. The final combined result supersedes this failed run.

The [memory report](memory-bounds-2026-10-17.md),
[delegation memory report](delegation-memory-2026-10-17.md),
[tree recovery report](tree-recovery-2026-10-17.md) and
[skills report](skills-inventory-2026-10-17.md) record the focused measurements.

## Runtime qualification

The [OpenShell qualification](openshell-qualification-2026-10-17.md) passed on
product revision `49f5fde0296b63b41f53a9f73e7e1b4c03b70982`. The normal launcher ran
separate orchestration and research VMs. During an observed running shell command,
the orchestrator VM was deleted and explicitly replaced. UI, control and worker
processes survived; replacement made zero model calls before explicit Resume.
Resume continued the child before its parent and reconciled the single original
operation with its exact successful stdout. Network, host-path and worker-auth
denials passed. The report pins the image digest and sanitized evidence hashes.

Teardown verified all 10 recorded VM identities absent, an empty isolated gateway,
five inactive transient services, eight closed ports and no remaining lab process.
The temporary lab state, TLS and tokens were removed. Inert image/tool caches and
sanitized evidence remain outside the repository. Earlier failed probes are
retained and distinguished from final qualification in the runtime report.

An intermediate archive memory repair is product revision
`372a48e07bbb20af853326d929905056102e15dd`. Its orchestrator image
`sandbox-science/orchestrator:372a48e0` built successfully and was independently
inspected, digest
`sha256:c11a124cd4a2a1a152a51f104fb8dfdd72235e851445706a903c873ca5e0486e`.
That build did not rerun the live scenario. Subsequent fixes bound the remaining
Blob-reader connections, preserve workspace repository boundaries, validate unknown
provisioning actions before store access, and refresh final-review summaries.
The final product revision covered by combined validation is
`be62e57b`. The live scenario remains qualified at `49f5fde0`; these later paths have
deterministic regression and independent review evidence.

The final product image was built from checkout `34902773`. The unchanged build
recipe embeds the last runtime-input revision,
`be62e57b8006620c7aca74ce6856a39f2ce358c0`; `[orchestrator].revision` must match
that embedded value. The image tag is `sandbox-science/orchestrator:34902773`,
with the default recipe tag `sandbox-science/orchestrator:be62e57b` as an alias;
its independently inspected digest is
`sha256:f0e4db6fe2ea11e88adf30358a6e22a14d4dba7663871b4611cc2c253b094cc6`.
The checkout, runtime revision, tag, digest, inspection result and build-log hash
are retained in `/home/andy/.cache/pt/foundations-final-image.json` (SHA-256
`dd8f38868880e8a0217aa391e85ad05acf068817ba11f00af4f7bc65b1fb17d4`).
Later report-only changes do not change build inputs. This is build verification,
not another live scenario or deployment to the personal instance.

## Review and limits

Independent GPT-6.1-Sol reviews covered storage, authority, concurrency, recovery,
transport and confinement. Medium findings were repaired and re-reviewed, including
parent readiness, stale writes, child settlement, budget accounting, old-tree UI
visibility and atomic child admission history. The final disconnect review cleared
exact attempt ownership, stopped-input suppression and the annotation claim/history
transaction. Its 71 focused tests passed in 11.97 seconds. Further review cleared
consolidation paging, repeated context stops, restart baselines and atomic selected
tree input admission. The combined suite includes those regressions.

These checks establish implementation behavior with deterministic/synthetic model
traffic, not the quality of extended cryptanalysis. Real-model scientific continuity,
Claude folding and provider cache cost/latency measurements still need separately
budgeted acceptance. The offline cache comparison did not justify changing production
context order. Advanced unknown-outcome reconciliation currently uses authenticated
API routes; a full graphical reconciliation editor and the broader UI redesign remain
separate work. BACKLOG checkboxes remain open until integration and acceptance.
