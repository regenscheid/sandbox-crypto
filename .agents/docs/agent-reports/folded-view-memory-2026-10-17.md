# Folded view memory

Local implementation on `codex/research-foundations`. Rebuilds load folds and spills
separately from the raw tail. Failure-marker retirement scans old calls one payload
at a time, retaining argument digests and match positions. SQL filters exclude child
contexts and prior state blocks before reading their payloads. Rendering, exact
archive evidence and the independent check's context boundaries are preserved.

A fresh-process fixture compares 4 and 40 folded 512 KiB tool results, with equally
large native replies and irrelevant child/state payloads. Both use the same view
geometry. Before running, the gate fixed maximum added Python allocations at 8 MiB,
peak RSS growth at 16 MiB and rendered tail at 10 KiB. The positive control loads the
old complete history and must exceed 16 MiB on both memory measures.

| Read | Python peak bytes | Peak RSS growth bytes | Raw tail bytes |
| --- | ---: | ---: | ---: |
| Streamed, 4 results | 1,072,576 | 0 | 142 |
| Streamed, 40 results | 1,091,470 | 0 | 142 |
| Whole-history control, 40 results | 84,028,622 | 101,527,552 | 62,916,806 |

Zero RSS growth means the process stayed below its earlier high-water mark, not that
reading allocated nothing. The fixture verifies the retired failure and exact hashes
of early, middle and late results after reopening. It passed in 3.84 seconds;
144 affected archive/view/fold/delegation/review tests passed in 34.19 seconds. Ruff
and pyright pass. No live service or gateway was used.

Evidence outside Git, in the local test evidence directory:

- `research-foundations-view-memory.log`, SHA-256
  `9f2e2ac4cc0cf3a3fe4f77e03261c735704f93b060ace01b5ba5637996cf1b74`.
- `research-foundations-view-final.log`, SHA-256
  `4859ebaa0b7dc6bcdbf34454e69e291d81766ba0ee3bf89b15e3620ac156992e`.

This is a rebuild-payload bound, not RC-F9 acceptance. The live transcript, saved
per-call summaries, fold metadata, preserved user words and review ingestion still
need separate bounds. Independent review and integration remain pending.
