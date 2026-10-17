# Long-run memory acceptance increment

The production loop stores historical messages, tool previews, usage, folds, rebuilds,
view measurements, review IDs and delivery membership in scoped disk collections.
Exact assistant/native payloads and tool outputs remain in the immutable archive.
Current folded context preserves the existing rendering and user-word rules while
streaming historical input through disposable SQLite work tables. Irreducible state
above 8 MiB/8192 rows pauses visibly with `memory_limit`; it does not report success.
The service admits at most four simultaneous roots. Final inline records are bounded
to 1 MiB and refer to paged history and exact evidence.

The Linux fresh-process acceptance fixture runs 40 and 400 real production loop turns,
including 64 KiB native responses and 256 KiB original tool output per turn, multiple
folds, real review dispatch, final save, reopened folded-view construction and exact
early/middle/late journal retrieval. Both cases passed the predeclared Python peak
32 MiB, RSS growth64 MiB and final-record1 MiB ceilings. The deliberate full-history
control exceeded64 MiB. The first measured run (before adding initialization and
reopened-view coverage) recorded Python peaks3.68/16.48 MB, RSS growth19.31/55.37 MB,
final records45,685/294,349 bytes, folds8/102 and reviews5/41; the stronger fixture
passed the same fixed gates in the combined regression run.

Validation:54 schema/export/API tests;142 combined consumer/scale tests;55 final
archive and incremental-review regressions. Scoped ruff and strict orchestrator/
workspace pyright passed; workspace frontend typecheck passed. Tests used no live
gateway/integration environment and at most four workers, one pytest invocation at
a time. No personal service was accessed. These are local implementation checks,
not a live OpenShell deployment or a research-quality evaluation. Independent
cross-review and coordinator-wide integration validation remain separate gates.
