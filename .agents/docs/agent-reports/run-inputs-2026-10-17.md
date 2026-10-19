# Immutable inputs before model dispatch

Control schema 59 stores each run's resolved request, execution context, selected
alias, harness settings and assembled prompt/tool/memory/skill pins before its first
model call. The mandatory hook applies to roots, children and grandchildren. It
excludes historical messages, process cancellation state and client credentials.
Each snapshot is limited to 1 MiB, scoped and immutable; failed persistence or a
duplicate identity prevents model/tool dispatch. These inputs also prove a parent
existed when its best-effort started event failed, allowing child partial records
to be retained before the parent's final save.

Evidence exports now use v2 and include the inputs even without a final run record.
The offline verifier still accepts v1 and checks each version's exact table set.
Migration does not invent inputs for older runs.

Validation: the initial focused gate passed 105 tests. The broad orchestrator/workspace
gate passed 4,586 with two [memory-fixture failures](memory-controls-2026-10-17.md) and
17 opt-in/dependency skips. After those repairs and the parent-proof case, all 106
affected input, project, delegation, export, migration and download tests passed in
17.11 seconds. Ruff and pyright pass. Evidence outside Git:

- `research-foundations-inputs-final.log`, SHA-256
  `5adc5f5d38b7cea5e0b6dd08b38117f74ed9d31d9aca857288fe662766785f59`
- `research-foundations-recovery-broad.log`, SHA-256
  `9a12bb9b8be8b65fe8724898dbbe5bfba64dbea73a8343eccced71f01827af0a`

These are observations for reconciliation, not replay authority or kernel checkpoints.
Work/attempt ownership, recursive Resume, pending-operation reconciliation, delivery
handling, whole-run RAM limits and separate orchestrator confinement remain unfinished.
No live services were used. Independent review and integration remain pending.
