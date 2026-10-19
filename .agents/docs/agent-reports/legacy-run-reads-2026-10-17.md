# Bounded legacy run reads

Local implementation on `codex/research-foundations`. Saved run reads default to an
8 MiB encoded-byte limit, checked through SQLite incremental I/O before allocating
the JSON payload. Oversized records report `unsupported` and direct the reader to
streamed evidence export. An explicit offline full read remains available. Context
rebuilds and carried model/fold constraints propagate the refusal rather than
silently omitting saved instructions or limits.

Validation: 799 project, retention, export and workspace tests passed in 51.30 seconds;
113 project, view, switch, fold and harness tests passed after the propagation change
in 24.65 seconds. The new cases check exact UTF-8 byte boundaries, refusal below
1 MiB additional Python allocations, preserved constraints and explicit offline reads.
Ruff and pyright pass. Logs outside Git:

- `research-foundations-legacy-reads.log`, SHA-256
  `ddac4efc22920650f3f84741d8d4331a1471c8e258d7a672d339e644d3a1c5e8`
- `research-foundations-legacy-followup.log`, SHA-256
  `262b22a13a2cc99cfc6143dd5d8160746a4d1427fdff2bed1d598cc6ff56ad24`

This does not bound final serialization or metadata queries over old JSON. No live
service was used. Independent review and integration remain pending.
